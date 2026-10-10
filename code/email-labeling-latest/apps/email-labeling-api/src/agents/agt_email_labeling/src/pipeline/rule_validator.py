"""Stage 3: RuleValidator — multi_label 一致性校验。

职责：
  - 补漏：current_facts 有强信号时确保对应 topic 存在
  - 降置信：检测到矛盾时降低 action/topic 的 confidence
  - needs_review：无法自动修正时标记人工复核
  - 不是重新做 HardOverride（不直接修改 primary_topic）
"""
import re
from typing import List, Optional

from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TOPIC_PRIORITY
from src.agents.agt_email_labeling.src.pipeline import taxonomy
from src.agents.agt_email_labeling.src.pipeline.lexicons import NO_ACTION_COURTESY as _NO_ACTION_COURTESY
from src.agents.agt_email_labeling.src.pipeline.topic_consistency import apply_topic_exclusions
from src.agents.agt_email_labeling.src.pipeline.direction_rules import apply_direction_filter
from src.pipeline.review_rule_gates import (
    has_after_sales_issue,
    is_delivery_confirmation_without_issue,
    is_fulfillment_logistics_confirmable,
    is_meeting_arrangement_confirmable,
    is_partnership_distribution_confirmable,
    text_context,
)
from src.agents.agt_email_labeling.src.schemas import (
    ActionItem, CurrentEmailFacts, MultiLabelResult, ThreadContextV2, TopicItem,
)

# current_facts 字段 → 应确保存在的 topic / action（单一真相源：taxonomy.py）
_FACT_TO_TOPIC: dict = dict(taxonomy.FACT_TO_TOPIC)
_FACT_TO_ACTION: dict = dict(taxonomy.FACT_TO_ACTION)

# 话题组合规则：某些 topic 共存时需要明确 delivery 上下文
_DELIVERY_DEPENDENT_TOPICS = frozenset({"post_delivery_support"})




def rule_validate(
    multi: MultiLabelResult,
    thread_ctx: Optional[ThreadContextV2] = None,
    cleaned_body: str = "",
    enable_outreach_backstop: bool = True,
) -> MultiLabelResult:
    """修正 multi_label 输出中的明显矛盾。直接修改入参并返回。"""
    existing_topics = {t.topic for t in multi.topics}
    existing_actions = {a.action for a in multi.actions}

    # ── 1. current_facts → 补 topic ──
    cf = multi.current_facts
    for fact_field, topic in _FACT_TO_TOPIC.items():
        if getattr(cf, fact_field, False) and topic not in existing_topics:
            multi.topics.append(TopicItem(
                topic=topic,
                role="secondary",
                evidence=f"auto-validated from {fact_field}",
            ))
            existing_topics.add(topic)

    # ── 2. current_facts → 补 action ──
    for fact_field, action in _FACT_TO_ACTION.items():
        if getattr(cf, fact_field, False) and action not in existing_actions:
            multi.actions.append(ActionItem(
                action=action,
                evidence=f"auto-validated from {fact_field}",
            ))
            existing_actions.add(action)

    # ── 3. 收货 + 文件/支持请求 → 补 post_delivery_support ──
    # 收货后客户索要文件（manual/CDR/certificate）或提出支持请求，属于售后场景
    if cf.delivery_received_in_email and (
        cf.document_requested_in_email or cf.document_provided_in_email
    ):
        _ensure_topic(multi, "post_delivery_support", role="secondary")
        existing_topics.add("post_delivery_support")

    # ── 4. 空 topics → 兜底 ──
    if not multi.topics:
        if _looks_like_no_action_reply(cleaned_body):
            multi.topics.append(TopicItem(topic="no_action_reply", role="primary"))
        else:
            multi.needs_review = True
            multi.review_reason = _append_reason(multi.review_reason, "empty topics, could not auto-derive")

    # ── 5. post_delivery_support 需要本封邮件的售后/交付事实支撑（动作为主，不看漏斗历史）──
    if "post_delivery_support" in existing_topics:
        has_after_sales_evidence = (
            cf.delivery_received_in_email
            or cf.quality_complaint_in_email
            or any(a.action in ("quality_complaint", "after_sales_support", "after_sales_inquiry")
                   for a in multi.actions)
        )
        if not has_after_sales_evidence:
            _demote_topic(multi, "post_delivery_support")
            multi.needs_review = True
            multi.review_reason = _append_reason(
                multi.review_reason,
                "post_delivery_support without after-sales evidence",
            )

    # ── 6. quality_issue 必须与 quality_complaint 或类似 action 对应 ──
    if "quality_issue" in existing_topics and not cf.quality_complaint_in_email:
        has_quality_action = any(
            a.action in ("quality_complaint",) for a in multi.actions
        )
        if not has_quality_action:
            _demote_topic(multi, "quality_issue")

    # ── 7. other action 净化 ──
    # 如果 other 跟具体业务 action 共存 → 过滤掉 other（纯噪声）
    # 如果 other 是唯一 action → 按无实质内容处理
    _SPECIFIC_ACTIONS = {
        a.action for a in multi.actions
        if a.action not in ("other", "auto_reply", "bounce", "empty", "short_reply")
    }
    _NON_OTHER_ACTIONS = [a for a in multi.actions if a.action != "other"]
    has_other = "other" in existing_actions
    if has_other:
        if _SPECIFIC_ACTIONS:
            multi.actions = _NON_OTHER_ACTIONS
        elif len(_NON_OTHER_ACTIONS) > 0:
            # 除 other 外还有 auto_reply/bounce/empty/short_reply → 仅去掉 other
            multi.actions = _NON_OTHER_ACTIONS
        else:
            # other 是唯一 action
            if _looks_like_no_action_reply(cleaned_body):
                multi.actions = [
                    ActionItem(
                        action="short_reply",
                        topic_ref="no_action_reply",
                        evidence="converted from other by RuleValidator",
                    ),
                ]
                _ensure_topic(multi, "no_action_reply", role="primary")
            else:
                multi.needs_review = True
                multi.review_reason = _append_reason(
                    multi.review_reason,
                    "only action is 'other', requires review",
                )
    # 刷新 action 集合，防止后续规则读到脏状态
    existing_actions = {a.action for a in multi.actions}

    # ── 7d. 人工复核 P0 收紧：4 类高影响误判必须有明确证据 ──
    _apply_p0_review_rule_gates(multi, cleaned_body)
    existing_actions = {a.action for a in multi.actions}

    # ── 7b. 唯一硬兜底：业务员**纯冷推/展会**里的"指示价" ≠ 报价 ──
    # 数据证明：通用 prompt 准则压不住这一类（LLM 顽固"见价即报价"）。
    # 这是**唯一**允许的确定性补丁——针对证明性顽固、高频高影响的头号系统性误判，划一道闸门。
    # 红线：硬兜底 ≤1-2 条且须有数据支撑，不得增长成 per-pattern 补丁列表。
    #
    # 收窄（人工审计 32 封 golden 错）：触发集原为去掉 outreach_new_product，只留 cold_email/exhibition。
    # 批1 动作中性化后 cold_email 并入 product_outreach（= 冷+新品+召回合并），无法再在动作层区分"冷 teaser
    # vs 推新品附真实报价"。为不退回"误删真实新品报价"，触发集只保留纯 teaser 的 outreach_exhibition。
    _OUTREACH = {"outreach_exhibition"}
    if enable_outreach_backstop and multi.sender_type == "salesperson":
        if any(a.action in _OUTREACH for a in multi.actions) and \
                any(a.action == "send_quotation" for a in multi.actions):
            multi.actions = [a for a in multi.actions if a.action != "send_quotation"]
            _demote_topic(multi, "quotation")
            existing_actions = {a.action for a in multi.actions}

    # ── 7c. 业务员要求客户补发缺失历史邮件 ≠ 催回复 ──
    # 典型句式："Please re-send me the email in July, then I will check and reply..."
    # 这是业务员向客户索取缺失上下文，不是业务员"催客户回复"。
    if multi.sender_type == "salesperson" and "follow_up_reminder" in existing_actions:
        if _looks_like_salesperson_resend_missing_context(cleaned_body):
            multi.actions = [
                ActionItem(
                    action="requirement_clarification",
                    display="需求确认",
                    topic_ref="requirement_discussion",
                    evidence="salesperson asks customer to resend missing prior email for checking",
                ) if a.action == "follow_up_reminder" else a
                for a in multi.actions
            ]
            _ensure_topic(multi, "requirement_discussion", role="primary")
            _demote_topic(multi, "follow_up")
            multi.summary = "业务员说明未收到客户7月邮件，请客户补发该邮件以便核查并回复。"
            existing_actions = {a.action for a in multi.actions}

    # ── 8. order_detail_confirmation 需要合理 action/entity 支撑 ──
    if "order_detail_confirmation" in existing_topics:
        has_doc_entity = bool(
            multi.entities.documents or multi.entities.orders
        )
        has_doc_action = any(
            a.action in (
                "order_post_doc", "document_request", "order_detail_confirmation",
            ) for a in multi.actions
        )
        has_doc_fact = (
            cf.document_provided_in_email or cf.document_requested_in_email
            or cf.order_detail_confirmed_in_email
        )
        if not (has_doc_entity or has_doc_action or has_doc_fact):
            _demote_topic(multi, "order_detail_confirmation")

    # ── 末尾清理(在所有规则 append 之后,才能兜住后加的动作)──
    # 0) 主题自洽性闸门:强制执行声明式互斥规则(单一真相源见 topic_consistency.TOPIC_EXCLUSIONS,
    #    配置页同源展示)。当前主要拦截 no_action_reply 与具体业务主题的逻辑矛盾。
    apply_topic_exclusions(multi)

    # a) 动作去重(LLM/规则偶尔重复同一动作)
    seen, deduped = set(), []
    for a in multi.actions:
        if a.action not in seen:
            seen.add(a.action)
            deduped.append(a)
    multi.actions = deduped
    # b) 发件方向过滤(声明式单一真相源见 direction_rules,配置页同源展示):
    #    客户邮件剔业务员专有动作;业务员邮件剔客户专有动作(如 first_reply / 各类买方问询)。纯精度修复。
    apply_direction_filter(multi)

    # c) 主题去重(同名只保留一个,若任一同名带 primary 角色则保留 primary 那个)。
    #    与动作去重对称:LLM 偶尔把同一主题列两次(如 meeting_arrangement[prim]+[seco]),
    #    会虚增 item 数、污染置信度的 evidence 覆盖。纯结构净化,不改主题集合 → 不影响多标签覆盖。
    by_topic: dict = {}
    for t in multi.topics:
        if t.topic not in by_topic or t.role == "primary":
            by_topic[t.topic] = t
    multi.topics = list(by_topic.values())

    return multi


# ── 辅助函数 ───────────────────────────────────────────────


def _looks_like_no_action_reply(body: str) -> bool:
    """判断是否属于无实质业务的短回复（多语言客套词表见 lexicons.NO_ACTION_COURTESY）。"""
    if not body:
        return True
    cleaned = body.strip().lower()
    word_count = len(cleaned.split())
    if word_count < 10:
        return True
    # 纯致谢/确认收到（含英文多词短语 + 多语言单词客套）
    thank_phrases = {"thank you", "got it", "will check", "let me check"}
    if cleaned.rstrip(".!?,;") in thank_phrases:
        return True
    courtesy = {w.lower() for w in _NO_ACTION_COURTESY}
    words = {w.strip(".!?,;") for w in cleaned.split()}
    if words and words.issubset(courtesy):
        return True
    return False


def _looks_like_salesperson_resend_missing_context(body: str) -> bool:
    """业务员请求客户补发缺失历史邮件/信件，用于纠正 follow_up_reminder 方向误判。"""
    if not body:
        return False
    text = body.lower()
    asks_resend = bool(re.search(r"\b(?:re-?send|send\s+me\s+again|forward\s+me)\b", text))
    mentions_mail = bool(re.search(r"\b(?:email|e-mail|mail|letter|message)\b", text))
    missing_context = bool(re.search(r"\b(?:july|didn'?t\s+receive|not\s+receive|only\s+email\s+i\s+received|check\s+and\s+reply)\b", text))
    return asks_resend and mentions_mail and missing_context


def n122_rule_validate(
    n122_output,
    *,
    cleaned_body: str,
    sender_type: str,
    subject: str = "",
    context: Optional[dict] = None,
    **context_values,
):
    """Run the frozen N122 v8 boundary validator.

    This adapter is deliberately separate from :func:`rule_validate`: the
    legacy V2 topic/action repair continues to operate on its own schema, while
    N122 labels remain parallel, un-capped, and free of primary/secondary
    normalization.
    """
    from src.agents.agt_email_labeling.src.pipeline.n122_label_validator import (
        validate_n122_boundaries,
    )

    return validate_n122_boundaries(
        n122_output,
        current_body=cleaned_body,
        sender_type=sender_type,
        subject=subject,
        context=context,
        **context_values,
    )


def _apply_p0_review_rule_gates(multi: MultiLabelResult, cleaned_body: str) -> None:
    """2026-07-03 人工复核 P0 规则：无明确证据的高影响误判不得继续支撑主标。"""
    context = text_context(cleaned_body, multi.summary)

    if any(t.topic == "post_delivery_support" for t in multi.topics):
        has_issue = has_after_sales_issue(context) or any(
            a.action in ("quality_complaint", "after_sales_support", "after_sales_inquiry")
            and has_after_sales_issue(text_context(a.evidence, context))
            for a in multi.actions
        )
        if not has_issue or is_delivery_confirmation_without_issue(context):
            multi.actions = [
                a for a in multi.actions
                if a.action not in ("after_sales_support", "after_sales_inquiry", "repurchase_inquiry")
            ]
            _drop_topic(multi, "post_delivery_support")
            _ensure_topic(multi, "fulfillment_logistics", role="secondary")
            multi.needs_review = True
            multi.review_reason = _append_reason(
                multi.review_reason,
                "post_delivery_support requires issue/problem/complaint/return/defect/repair evidence",
            )

    if any(t.topic == "fulfillment_logistics" for t in multi.topics):
        logistics_actions = {
            "delivery_followup", "shipment_notice", "logistics_tracking",
            "shipment_delay", "out_of_stock_notice",
        }
        supported = [
            a for a in multi.actions
            if a.action in logistics_actions
            and is_fulfillment_logistics_confirmable(
                action=a.action,
                evidence=a.evidence,
                context_text=context,
            )
        ]
        if not supported:
            multi.actions = [a for a in multi.actions if a.action not in logistics_actions]
            _drop_topic(multi, "fulfillment_logistics")
            if any(a.action in ("order_detail_confirmation", "requirement_clarification", "spec_discussion") for a in multi.actions):
                _ensure_topic(multi, "requirement_discussion", role="secondary")
            multi.needs_review = True
            multi.review_reason = _append_reason(
                multi.review_reason,
                "fulfillment_logistics requires shipment/tracking/customs/delivery evidence beyond quantity/order/price/spec discussion",
            )

    if any(t.topic == "partnership_distribution" for t in multi.topics):
        partnership_actions = {"distributor_inquiry", "distributor_proposal"}
        supported = [
            a for a in multi.actions
            if a.action in partnership_actions
            and is_partnership_distribution_confirmable(evidence=a.evidence, context_text=context)
        ]
        if not supported:
            multi.actions = [a for a in multi.actions if a.action not in partnership_actions]
            _drop_topic(multi, "partnership_distribution")
            _ensure_topic(multi, "requirement_discussion", role="secondary")
            multi.needs_review = True
            multi.review_reason = _append_reason(
                multi.review_reason,
                "partnership_distribution requires exclusive/agent/distributor/territory/partnership evidence not only quantity list",
            )

    if any(t.topic == "meeting_arrangement" for t in multi.topics):
        meeting_actions = {
            "offline_invitation", "offline_visit", "online_meeting",
            "exhibition_visit", "visit_followup", "outreach_exhibition",
        }
        supported = [
            a for a in multi.actions
            if a.action in meeting_actions
            and is_meeting_arrangement_confirmable(evidence=a.evidence, context_text=context)
        ]
        if not supported:
            multi.actions = [a for a in multi.actions if a.action not in meeting_actions]
            _drop_topic(multi, "meeting_arrangement")
            if any(a.action in ("product_detail_inquiry", "spec_discussion", "requirement_clarification") for a in multi.actions):
                _ensure_topic(multi, "requirement_discussion", role="secondary")
            multi.needs_review = True
            multi.review_reason = _append_reason(
                multi.review_reason,
                "meeting_arrangement requires time/date/schedule/visit/meeting/agenda arrangement evidence",
            )


def _demote_topic(multi: MultiLabelResult, topic: str) -> None:
    """将被矛盾规则命中的 topic 从 primary 降级为 secondary。

    （原先还会下调 confidence，但 LLM 自评置信度已移除，降级仅作用于 role。）
    """
    for t in multi.topics:
        if t.topic == topic and t.role == "primary":
            t.role = "secondary"


def _drop_topic(multi: MultiLabelResult, topic: str) -> None:
    multi.topics = [t for t in multi.topics if t.topic != topic]


def _ensure_topic(multi: MultiLabelResult, topic: str, role: str = "secondary") -> None:
    """确保指定 topic 存在，不存在则追加。"""
    if not any(t.topic == topic for t in multi.topics):
        multi.topics.append(TopicItem(
            topic=topic,
            role=role,
            evidence=f"auto-validated by RuleValidator ({role})",
        ))


def _append_reason(existing: str, new: str) -> str:
    if existing:
        return existing + "; " + new
    return new
