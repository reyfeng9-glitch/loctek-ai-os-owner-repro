"""V2 Multi-Label 默认编排器。

默认路径：
  SystemFilter → Cleaner → ThreadContextBuilderV2
  → MultiLabelExtractor → RuleValidator → PrimaryTopicDeriver → ContextUpdaterV2

Legacy 模块（FactExtractor / HardOverride / CandidateGenerator / Adjudicator /
LabelResolver / FunnelEngine / SignalDetection）完全不参与。

只读 multi_label_recent_events，不读 legacy email_topic/sub_action/primary_event。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.agt_email_labeling.src.models import EmailLabelingResult, EmailLabelingThread
from src.agents.agt_email_labeling.src.pipeline.cleaner import clean_email
from src.agents.agt_email_labeling.src.pipeline.authoritative_context import build_authoritative_context
from src.agents.agt_email_labeling.src.pipeline.confidence_scorer import score_confidence_v2
from src.agents.agt_email_labeling.src.pipeline.context_updater_v2 import derive_multi_label_event
from src.agents.agt_email_labeling.src.pipeline.multi_label_extractor import (
    _derive_primary_topic, multi_label_extract,
)
from src.agents.agt_email_labeling.src.pipeline.rule_validator import rule_validate
from src.agents.agt_email_labeling.src.pipeline.runtime_config import get_config
from src.agents.agt_email_labeling.src.pipeline.stage_risk import derive_risk_signals
from src.agents.agt_email_labeling.src.pipeline.system_filter import system_filter
from src.agents.agt_email_labeling.src.pipeline.thread_context_v2 import (
    build_thread_context_v2, n122_history_context_refs,
)
from src.agents.agt_email_labeling.src.pipeline.customer_context_v2 import (
    build_customer_context, build_n122_complete_history,
)
from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TOPIC_DISPLAY_MAP
from src.agents.agt_email_labeling.src.schemas import (
    LabelEmailRequest, LabelResultResponse, MultiLabelRecentEvent,
)
from src.agents.agt_email_labeling.src.services.result_store import (
    _derive_customer_email, _get_existing_result, _next_version,
    _persist_filtered, _to_response, _upsert_email, delete_existing_results,
)
from src.services.n122_history_service import (
    HistoryCompleteness, HistoryDecision, HistoryEmail,
    apply_history_decision_to_business_labels, qualify_current_i01_reply,
)

logger = logging.getLogger(__name__)

MAX_RECENT_EVENTS = 20


async def run_pipeline(req, db, model_name: str = "", force: bool = False, **_) -> "LabelResultResponse":
    """邮件打标统一入口（legacy V3.3 已删除，固定走 V2 多标签流水线）。

    force=True：忽略幂等跳过，用当前最新规则强制重跑（覆盖旧结果）。
    """
    return await run_labeling_pipeline_v2(req, db, model_name=model_name, force=force)


async def run_labeling_pipeline_v2(
    req: LabelEmailRequest,
    db: AsyncSession,
    model_name: str = "",
    force: bool = False,
) -> LabelResultResponse:
    """V2 Multi-Label 默认编排器。

    Stage 0  SystemFilter
    Stage 1  Cleaner
    Stage 2a ThreadContextBuilderV2（只读 multi_label_recent_events）
    Stage 2b MultiLabelExtractor
    Stage 3  RuleValidator
    Stage 4  PrimaryTopicDeriver
    Stage 5  ContextUpdaterV2（写入 multi_label_recent_events）
    """
    # 幂等性检查：仅当 V2 multi_label 结果已存在时跳过
    # 如果只有 legacy 结果（无 multi_label），仍重新跑 V2
    # force=True：跳过幂等、并先删旧行（覆盖语义：重写后 version 归 1，读侧不双计）
    if force:
        deleted = await delete_existing_results(req.email_id, db)
        if deleted:
            logger.info("force re-run email_id=%s: 删除旧结果 %d 行后重打", req.email_id, deleted)
    else:
        existing = await _get_existing_result(req.email_id, db)
        if existing:
            facts = existing.facts or {}
            if facts.get("multi_label"):
                logger.info("V2 result exists for email_id=%s, skipping", req.email_id)
                return _to_response(existing)
            logger.info(
                "Legacy result exists for email_id=%s (v%d), re-running V2",
                req.email_id, existing.version,
            )

    # Stage 1: Cleaner
    cleaning = clean_email(req.raw_body)

    # Stage 0: SystemFilter — auto-reply/bounce/empty/too-short → 直接落库为 system_noise
    filter_result = system_filter(cleaning.cleaned_body, req.subject)
    if filter_result:
        return await _persist_filtered(req, cleaning, filter_result, db)

    customer_email = _derive_customer_email(req)

    # Stage 2a: 会话层上下文（ThreadContextV2，只读本会话 multi_label_recent_events）
    thread_ctx = await build_thread_context_v2(
        thread_id=req.thread_id,
        current_email_id=req.email_id,
        db=db,
        customer_email=customer_email,
    )
    # Stage 2a': 客户级上下文（跨该客户全部会话聚里程碑，按 sent_at 截断；见 README 两层模型）
    customer_ctx = await build_customer_context(
        customer_id=customer_email,
        as_of=req.sent_at,
        current_thread_id=req.thread_id,
        db=db,
    )

    # 权威业务事实（ERP 下单/发货，截至本邮件时点）——客户级事实，进"客户全局上下文"段。
    auth_events = getattr(req, "authoritative_events", None)

    history_decision: Optional[HistoryDecision] = None
    if req.customer_id == "N122":
        history_decision = await build_n122_complete_history(
            current=HistoryEmail(
                email_id=req.email_id,
                customer_id=req.customer_id,
                thread_id=req.thread_id,
                sender_type=req.sender_type,
                sent_at=req.sent_at,
            ),
            authoritative_events=auth_events or [],
            completeness=HistoryCompleteness(
                email_history_complete=req.n122_email_history_complete,
                order_history_complete=auth_events is not None,
            ),
            db=db,
        )

    # 运行时配置（可在管理页改：温度/封顶/复核阈值/硬兜底开关）
    cfg = await get_config(db)

    # Stage 2b: MultiLabelExtractor（两层上下文入参：会话层 + 客户级层）
    # 这是唯一的 LLM 语义抽取边界：如果要“重跑中间层”，必须从这里重新调用 LLM。
    # 注意：抽取结果会写入 multi_label_recent_events 并影响同线程/同客户后续邮件上下文，
    # 所以管理端重抽不能只改单封；应规划“当前邮件 + 下游受影响邮件”的范围和成本。
    thread_context_text = _format_context_v2(thread_ctx)
    customer_context_text = _format_customer_context(
        customer_ctx,
        build_authoritative_context(
            auth_events,
            as_of=req.sent_at,
            subject=req.subject or "",
            body=cleaning.cleaned_body,
        ),
    )
    if history_decision is not None:
        history_refs = n122_history_context_refs(history_decision)
        if history_refs:
            customer_context_text = _merge_context(
                customer_context_text,
                "N122完整历史审计: " + "; ".join(history_refs),
            )
    multi = await multi_label_extract(
        cleaned_body=cleaning.cleaned_body,
        sender_type=req.sender_type,
        thread_context_text=thread_context_text,
        customer_context_text=customer_context_text,
        db=db,
        temperature=cfg["temperature"],
    )
    multi.sender_type = req.sender_type

    # ★ 冻结抽取：在任何后处理(规则/封顶/派生)改动 multi 之前，结构化快照 LLM 原始抽取。
    # 落库后，所有后处理实验都能离线重放(规则/封顶/deriver)，**不再调 LLM**——省 token、可证伪。
    # facts.raw_extraction = 冻结 LLM 中间层；facts.multi_label = 后处理后的最终标签结果。
    raw_extraction = {
        "topics": [{"topic": t.topic, "role": t.role, "evidence": t.evidence} for t in multi.topics],
        "actions": [{"action": a.action, "evidence": a.evidence} for a in multi.actions],
        "current_facts": multi.current_facts.model_dump(),
    }

    # Stage 3: RuleValidator
    multi = rule_validate(multi, thread_ctx, cleaning.cleaned_body,
                          enable_outreach_backstop=cfg["outreach_quote_backstop"])

    # Stage 4: PrimaryTopicDeriver
    multi.primary_topic = _derive_primary_topic(multi.topics, multi.actions)

    # Stage 4·精度：主题封顶（≤cfg.topic_cap），剪噪声主题、保真实主题（召回安全）。
    _cap_topics(multi, max_n=cfg["topic_cap"])

    # Stage 4·一致性：把 topics[] 的 primary 角色对齐到权威 primary_topic（恰一个 primary）。
    # 修「双 primary / 无 primary」结构工件（全量约 34 封）。不改主题集合 → 不影响多标签覆盖,
    # 只让置信度的 primary_item 与系统真实主标一致。
    _normalize_topic_roles(multi)

    # Stage 4a: 派生风险信号（动作即真相）。业务阶段(漏斗)层已移除——以动作为主、动作反推意图。
    # 权威事件已在抽取前作为分层事实上下文喂入；这里继续留存到 facts，供回放、审计和动作校验。
    multi.risk_signals = derive_risk_signals(multi)
    _append_system_decision_trace(multi, auth_events)

    # Stage 4c: 业务标签派生（动作 + 邮件摘要 + ERP权威 + 时序上下文；多标签、规则为主）。
    # 失败不致命：拿不到信号就给空业务标签,不影响主打标。
    business_labels = await _derive_business_labels(req, multi, auth_events, db)
    if history_decision is not None:
        history_decision = qualify_current_i01_reply(
            history_decision,
            sender_type=req.sender_type,
            action_codes=[action.action for action in multi.actions],
        )
        business_labels = apply_history_decision_to_business_labels(
            business_labels,
            history_decision,
            sender_type=req.sender_type,
            action_codes=[action.action for action in multi.actions],
        )

    # Stage 4b: ConfidenceScorerV2
    conf_result = score_confidence_v2(multi, cleaning.cleaned_body, risk_threshold=cfg["review_risk_threshold"])
    # 合并 RuleValidator 已设置的 needs_review（取 OR）
    final_needs_review = multi.needs_review or conf_result.needs_review
    final_review_reasons = []
    if multi.needs_review and multi.review_reason:
        final_review_reasons.append(multi.review_reason)
    final_review_reasons.extend(
        r for r in conf_result.review_reasons
        if r not in final_review_reasons
    )

    # Stage 5: ContextUpdaterV2 — 写入 multi_label_recent_events
    try:
        await _persist_v2(req, cleaning, multi, conf_result, final_needs_review, final_review_reasons, thread_ctx, model_name, db, auth_events=auth_events, raw_extraction=raw_extraction, business_labels=business_labels, history_decision=history_decision)
    except Exception:
        await db.rollback()
        raise

    # 重新查询返回完整结果
    row = await db.execute(
        select(EmailLabelingResult)
        .where(EmailLabelingResult.email_id == req.email_id)
        .order_by(EmailLabelingResult.version.desc())
        .limit(1)
    )
    result_row = row.scalar_one_or_none()
    if result_row is None:
        raise RuntimeError(f"Failed to persist result for email {req.email_id}")

    await db.refresh(result_row)
    return _to_response(result_row)


# ── 业务标签派生 ───────────────────────────────────────────


async def _derive_business_labels(req: LabelEmailRequest, multi, auth_events, db) -> list:
    """业务标签派生（动作 + 摘要 + ERP权威 + 时序上下文；规则为主、多标签）。

    声明式规则见 pipeline.business_labels。开发期任何异常都直接抛出，避免把业务标签错误隐藏成空结果。
    """
    from src.agents.agt_email_labeling.src.pipeline.business_labels import (
        compute_business_signals, derive_business_labels,
    )
    cust = getattr(req, "customer_id", None)
    now = getattr(req, "sent_at", None)
    # emails.sent_at 是 naive timestamp;req.sent_at 可能带时区 → 统一去时区,避免比较报错
    now_naive = now.replace(tzinfo=None) if (now is not None and getattr(now, "tzinfo", None)) else now
    prior = []
    if cust and now_naive is not None:
        rows = (await db.execute(text(
            "SELECT sender_type, sent_at FROM email_labeling_emails "
            "WHERE customer_id = :c AND sent_at < :now "
            "AND NOT COALESCE(excluded_from_timeline, false)"
        ), {"c": cust, "now": now_naive})).fetchall()
        prior = [(s, d) for s, d in rows]
    # 本线程之前是否已发过报价(send_quotation)→ 用于"报价跟进"业务标签
    thread_quote = False
    tid = getattr(req, "thread_id", None)
    if tid and now_naive is not None:
        qrow = (await db.execute(text(
            "SELECT 1 FROM email_labeling_results r "
            "JOIN email_labeling_emails e ON e.email_id = r.email_id "
            "WHERE r.thread_id = :t AND e.sent_at < :now "
            "AND r.facts->'multi_label'->'actions' @> '[{\"action\":\"send_quotation\"}]' LIMIT 1"
        ), {"t": tid, "now": now_naive})).fetchone()
        thread_quote = qrow is not None
    sig = compute_business_signals(now_naive, prior, auth_events,
                                   thread_had_quotation_before=thread_quote)
    acts = [a.action for a in (multi.actions or [])]
    facts = multi.current_facts.model_dump() if getattr(multi, "current_facts", None) else {}
    return derive_business_labels(acts, getattr(req, "sender_type", "") or "", multi.summary or "", sig, facts=facts)


# ── 持久化 ─────────────────────────────────────────────────


async def _persist_v2(
    req: LabelEmailRequest,
    cleaning,
    multi: "MultiLabelResult",
    conf_result,
    final_needs_review: bool,
    final_review_reasons: list,
    thread_ctx: "ThreadContextV2",
    model_name: str,
    db: AsyncSession,
    auth_events: Optional[list] = None,
    raw_extraction: Optional[dict] = None,
    business_labels: Optional[list] = None,
    history_decision: Optional[HistoryDecision] = None,
) -> None:
    """持久化 V2 结果：写入 email + result + multi_label_recent_events。"""
    await _upsert_email(req, cleaning, db)
    version = await _next_version(req.email_id, db)

    primary_topic = multi.primary_topic or ""
    display = TOPIC_DISPLAY_MAP.get(primary_topic, primary_topic)
    first_action = multi.actions[0].display if multi.actions else ""
    summary = multi.summary or ""

    result_row = EmailLabelingResult(
        email_id=req.email_id,
        thread_id=req.thread_id,
        version=version,
        sender_type=req.sender_type,
        email_summary=summary,
        email_topic=primary_topic,
        email_topic_display=display,
        sub_action=first_action or None,
        label_reason=_build_reason(multi),
        email_label=display,
        primary_event=primary_topic,
        secondary_events=[a.action for a in multi.actions],
        facts={
            "multi_label": multi.model_dump(),
            "multi_label_version": "v2",
            # 冻结抽取：规则/封顶/派生之前的 LLM 原始结构化输出。供后处理离线重放(不再调 LLM)。
            **({"raw_extraction": raw_extraction} if raw_extraction else {}),
            # provenance：保证"一次产生、可复现"——同 input_hash 可重放确定性后处理(replay)，
            # 或换 prompt/模型重抽(re-extract)。raw_llm_output 已存于 multi_label 内。
            "provenance": {
                "input_hash": hashlib.sha256(
                    (cleaning.cleaned_body or "").encode("utf-8")
                ).hexdigest()[:16],
                "model_name": model_name or "",
                "prompt_version": "multi_label_v2",
                "cleaner_version": "cleaner_v2",
                "parser_version": "parser_v2",
                "schema_version": multi.schema_version,
            },
            # 权威业务事件（下单/发货）：留存事件流，供回放、审计、动作校验和重放上下文对照
            **({"authoritative_events": auth_events} if auth_events else {}),
            # 业务标签（动作之上的派生场景标签：冷启动开发/老客户召回 等；多标签、规则为主）
            **({"business_labels": business_labels} if business_labels else {}),
            **({"n122_history": {
                "i01_eligible": history_decision.i01_eligible,
                "o03_eligible": history_decision.o03_eligible,
                "linked_o01_email_id": history_decision.linked_o01_email_id,
                "prior_email_count": history_decision.prior_email_count,
                "prior_effective_customer_reply_count": history_decision.prior_effective_customer_reply_count,
                "days_since_last_valid_contact": history_decision.days_since_last_valid_contact,
                "has_pre_cutoff_formal_order": history_decision.has_pre_cutoff_formal_order,
                "history_incomplete": history_decision.history_incomplete,
                "source_gaps": list(history_decision.source_gaps),
                "affected_candidates": list(history_decision.affected_candidates),
            }} if history_decision is not None else {}),
        },
        risk_signals=list(multi.risk_signals or []),
        opportunity_signals=[],
        confidence_score=conf_result.score,
        confidence_level=conf_result.level,
        confidence_detail=conf_result.detail.model_dump(),
        evidence=[t.evidence for t in multi.topics if t.evidence],
        ambiguity_flags=[],
        needs_review=final_needs_review,
        review_reasons=final_review_reasons,
        model_name=model_name,
        prompt_version="multi_label_v2",
        state_machine_version="",
    )
    db.add(result_row)

    # 写入 multi_label_recent_events（带 sent_at + customer_id，供客户级层跨线程时间截断）
    customer_key = _derive_customer_email(req)
    entry = derive_multi_label_event(
        email_id=req.email_id,
        sender_type=req.sender_type,
        multi=multi,
        excluded_from_context=False,
        sent_at=req.sent_at,
        customer_id=customer_key,
    )
    await _append_multi_event(req.thread_id, entry, db, customer_id=customer_key)

    await db.commit()


async def _append_multi_event(
    thread_id: str,
    entry: dict,
    db: AsyncSession,
    customer_id: Optional[str] = None,
) -> None:
    """向 thread.multi_label_recent_events 追加条目。

    建 thread 时即挂 customer_id（客户编码），历史 NULL 也顺手回填——
    否则客户级上下文按 thread.customer_id 跨线程聚合时会断键（见 README 两层模型）。
    """
    row = await db.execute(
        select(EmailLabelingThread).where(EmailLabelingThread.thread_id == thread_id)
    )
    thread = row.scalar_one_or_none()
    if thread is None:
        thread = EmailLabelingThread(
            thread_id=thread_id,
            customer_id=customer_id,
            multi_label_recent_events=[entry],
        )
        db.add(thread)
        return

    if customer_id and not thread.customer_id:
        thread.customer_id = customer_id
    events = list(thread.multi_label_recent_events or [])
    events.append(entry)
    thread.multi_label_recent_events = events[-MAX_RECENT_EVENTS:]


# ── 辅助函数 ───────────────────────────────────────────────


def _format_context_v2(ctx: "ThreadContextV2") -> str:
    """将 ThreadContextV2 格式化为 LLM 可读的上下文文本。

    只从 V2 结构化字段生成，不读任何旧标签。
    """
    parts = []
    if ctx.context_missing:
        return "无历史上下文"

    # 只给**事实性**近期上下文（实体/事实），不做漏斗阶段推断。
    # 最近实体
    if ctx.recent_entities_text:
        parts.append(f"最近实体: {ctx.recent_entities_text}")

    # 中可信事实(邮件明确陈述过的业务里程碑,参考；可信度低于 ERP 权威事实)
    if ctx.recent_facts_text:
        parts.append(f"本线程邮件已明确(参考): {ctx.recent_facts_text}")

    # 注：跨线程/跨会话信息已移到「客户全局上下文」段（_format_customer_context），此处只管本会话。
    return " | ".join(parts) if parts else "无相关历史上下文"


def _merge_context(thread_text: str, auth_text: str) -> str:
    """合并线程上下文与权威事实上下文。"""
    if not auth_text:
        return thread_text
    if not thread_text or thread_text in ("无历史上下文", "无相关历史上下文"):
        return auth_text
    return thread_text + "\n" + auth_text


def _format_customer_context(ctx: "CustomerContextV2", auth_text: str) -> str:
    """渲染「客户全局上下文」段：跨会话里程碑(中可信) + ERP 权威事实(高可信)。

    护栏①防污染：只放客户级状态/里程碑与 ERP 事件，**不放**跨会话的产品实体。
    （客户级里程碑/ERP 的时间截断已分别在 build_customer_context / auth_events 切片里完成。）
    """
    parts = []
    if ctx and not ctx.context_missing:
        if ctx.other_thread_count > 0:
            parts.append(f"该客户另有 {ctx.other_thread_count} 段历史会话")
        if ctx.milestone_facts_text:
            parts.append(f"客户级里程碑(跨会话·截至本邮件·中可信): {ctx.milestone_facts_text}")
    if auth_text:
        parts.append(auth_text)
    return "\n".join(parts) if parts else ""


def _cap_topics(multi: "MultiLabelResult", max_n: int = 3) -> None:
    """主题精度控制（召回安全）：

    1. **先剪纯噪声主题**：既无原文依据、又无动作规范归属支撑、且非 primary 的"提及型"主题
       直接丢——这正是过度枚举的来源（提升精度，且几乎不丢真实主题/recall）。
    2. 剪完若仍 > max_n（极少见，真有多业务），再按支撑度保留 top-max_n。

    与"硬截断 top-3"相比：硬截断会把弱支撑但正确的主题也砍掉（伤 recall）；
    这里只砍"无任何支撑"的噪声，真实主题（有依据/有动作）一律保留。
    """
    from src.agents.agt_email_labeling.src.pipeline.action_definitions import ACTION_TOPIC_MAP
    topics = multi.topics or []
    if len(topics) <= 1:
        return
    action_topics = {ACTION_TOPIC_MAP.get(a.action) for a in (multi.actions or [])}

    def _supported(t) -> bool:
        if t.topic == multi.primary_topic:
            return True
        if getattr(t, "evidence", None) and str(t.evidence).strip():
            return True
        if t.topic in action_topics:
            return True
        # LLM 自标 role=primary 的弱支撑主题也保留(它自认是这封的主要意图,是真信号)。
        # 离线 cap 重放验证:多标签召回 +1.5pt,topics/封 1.57→1.67(未滥用),时间/客户留出均不退。
        return getattr(t, "role", "") == "primary"

    kept = [t for t in topics if _supported(t)]
    if not kept:                      # 安全：绝不清空
        kept = topics
    if len(kept) > max_n:             # 真有多业务才软截断
        def _score(t) -> int:
            return (100 if t.topic == multi.primary_topic else 0) \
                + (2 if (getattr(t, "evidence", None) and str(t.evidence).strip()) else 0) \
                + (2 if t.topic in action_topics else 0)
        kept = sorted(kept, key=_score, reverse=True)[:max_n]
    multi.topics = kept


def _normalize_topic_roles(multi: "MultiLabelResult") -> None:
    """把 topics[] 的 primary 角色对齐到权威 primary_topic：恰一个 primary，其余 secondary。

    修「双 primary」「无 primary」结构工件——这些来自 LLM 自标 + 规则注入的角色冲突。
    只改 role 字段，不增删主题 → 多标签覆盖(gold ∈ topic 集合 ∪ primary)不变；
    作用是让 ConfidenceScorer 的 primary_item 与系统真实主标一致，避免置信度算在错的主标上。
    """
    if not multi.topics:
        return
    pt = multi.primary_topic
    matched = False
    for t in multi.topics:
        if t.topic == pt and not matched:
            t.role = "primary"
            matched = True
        else:
            t.role = "secondary"
    # primary_topic 不在 topics 里(理论上 cap 后不会发生)：保底把第一个设为 primary，避免无 primary。
    if not matched:
        multi.topics[0].role = "primary"


def _build_reason(multi: "MultiLabelResult") -> str:
    """从 multi_label 生成 label_reason 摘要。"""
    parts = []
    if multi.primary_topic:
        parts.append(f"primary_topic={multi.primary_topic}")
    if multi.needs_review and multi.review_reason:
        parts.append(f"review={multi.review_reason}")
    if multi.topics:
        topics_str = ", ".join(
            f"{t.topic}{'*' if t.role == 'primary' else ''}" for t in multi.topics
        )
        parts.append(f"topics=[{topics_str}]")
    return "; ".join(parts) if parts else "multi_label_v2"


def _append_system_decision_trace(multi: "MultiLabelResult", auth_events: Optional[list] = None) -> None:
    """把确定性后处理写入可审计依据链，避免把系统规则误认为模型 CoT。"""
    from src.agents.agt_email_labeling.src.schemas import DecisionRuleTrace

    existing = {
        (getattr(item, "rule", ""), getattr(item, "effect", ""))
        for item in (getattr(multi.decision_trace, "rule_overrides", []) or [])
    }

    def add(rule: str, effect: str) -> None:
        key = (rule, effect)
        if not effect or key in existing:
            return
        multi.decision_trace.rule_overrides.append(DecisionRuleTrace(rule=rule, effect=effect))
        existing.add(key)

    if multi.review_reason:
        add("rule_validator", multi.review_reason)
    if auth_events:
        add("authoritative_events", f"附带 {len(auth_events)} 条业务权威事件；已按客户摘要/活跃窗口/显式命中压缩后参与模型上下文")
    if multi.primary_topic:
        add("primary_topic_deriver", f"系统从 topics/actions 派生主主题：{multi.primary_topic}")
