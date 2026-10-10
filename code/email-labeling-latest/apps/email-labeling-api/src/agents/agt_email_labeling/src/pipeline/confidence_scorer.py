import re
from typing import TYPE_CHECKING, List, Optional

from src.agents.agt_email_labeling.src.schemas import ConfidenceDetail, ConfidenceResult

if TYPE_CHECKING:
    from src.agents.agt_email_labeling.src.schemas import MultiLabelResult

_HIGH_THRESHOLD = 0.75
_LOW_THRESHOLD = 0.50


def _score_to_level(score: float) -> str:
    if score >= _HIGH_THRESHOLD:
        return "high"
    if score >= _LOW_THRESHOLD:
        return "medium"
    return "low"


# ── V2 Multi-Label 置信度打分 ─────────────────────────────────────────────────

def _has_evidence(item) -> bool:
    """标签是否携带原文引用（可验证的落地信号）。"""
    ev = getattr(item, "evidence", None)
    return bool(ev and str(ev).strip())


def _action_topic(action_item) -> str:
    """动作的**规范归属主题**：用确定性的 ACTION_TOPIC_MAP（taxonomy 1:1），
    而非 LLM 逐封自评的 topic_ref（后者是判断类输出，易幻觉，不采信）。"""
    from src.agents.agt_email_labeling.src.pipeline.action_definitions import ACTION_TOPIC_MAP
    return ACTION_TOPIC_MAP.get(getattr(action_item, "action", None) or "") or ""


def _topic_supported(topic_item, actions: list) -> bool:
    """主题是否有落地支撑：自带原文依据，或有动作的规范归属指向它。"""
    if _has_evidence(topic_item):
        return True
    return any(_action_topic(a) == topic_item.topic for a in actions)


def score_confidence_v2(multi: "MultiLabelResult", cleaned_body: str,
                        risk_threshold: Optional[float] = None) -> ConfidenceResult:
    """V2 multi-label 置信度打分——**完全基于落地信号，不使用任何 LLM 自评置信度**。

    LLM 自报的数值置信度校准差、易幻觉，已从整条链路移除。置信度改由系统从可验证的
    结构化信号合成，3 个维度加权：
      evidence_coverage (55%) + action_alignment (25%) + topic_clarity (20%)

    注：实体抽取属"额外拓展信息（有更好、没有也行）"，**不再纳入置信度**——
    实体仍正常抽取并落库，只是不作为打标确定性的依据。
    """
    topics = multi.topics or []
    actions = multi.actions or []

    primary_item = next((t for t in topics if t.role == "primary"), None)
    primary_topic = multi.primary_topic or (primary_item.topic if primary_item else "")

    # ── 1. 证据覆盖率 (55%) ───────────────────────────────────────────────────
    # 最可信的落地信号：每个标签是否带原文引用（可验证），而非模型自评数字。
    items = [*topics, *actions]
    items_with_evidence = sum(1 for it in items if _has_evidence(it))
    evidence_score = items_with_evidence / max(len(items), 1)

    # ── 2. 动作-话题对齐 (25%) ────────────────────────────────────────────────
    # 用动作的规范归属（ACTION_TOPIC_MAP）判断是否对齐主话题，不采信 LLM 的 topic_ref。
    if not actions:
        alignment_score = 0.6  # 无动作（纯客套/通知），中性
    elif primary_topic and any(_action_topic(a) == primary_topic for a in actions):
        alignment_score = 1.0
    else:
        alignment_score = 0.5  # 有动作但都不指向主话题

    # ── 3. 话题清晰度 (20%) ───────────────────────────────────────────────────
    # 结构化判定（不依赖置信度）：主话题是否被其它"有落地支撑"的话题竞争。
    if len(topics) <= 1:
        clarity_score = 1.0
    elif not (primary_item and _topic_supported(primary_item, actions)):
        clarity_score = 0.5  # 主话题自身无支撑
    else:
        competitors = sum(
            1 for t in topics
            if t.role != "primary" and _topic_supported(t, actions)
        )
        clarity_score = 1.0 if competitors == 0 else (0.7 if competitors == 1 else 0.5)

    # ── 加权合成 ──────────────────────────────────────────────────────────────
    weighted = (
        evidence_score  * 0.55
        + alignment_score * 0.25
        + clarity_score   * 0.20
    )
    score = round(weighted, 4)
    level = _score_to_level(score)

    # ── 复核决策：数据校准的风险分 + 结构性硬触发 ──────────────────────────────
    # 旧的「score < 阈值」通用闸门已被证据证伪：对错误无区分力（审 9%→80% 漏网几乎不变）。
    # 改用按 golden 校准的 P(标错) 风险分（话题先验 + 低置信 + 业务员 + 单动作），
    # 在同等复核量下错误召回 32%→64%、漏网 9.6%→5.2%（LOO-CV）。
    review_reasons: List[str] = []

    # 1) 结构性硬触发（与置信度无关，永远有效）
    if not topics:
        review_reasons.append("无话题标签")
    if len(cleaned_body.strip()) < 20:
        review_reasons.append("邮件正文过短")
    # RuleValidator 命中的确定性坏模式（如方向反了）
    if multi.needs_review and multi.review_reason:
        review_reasons.append(multi.review_reason)
    if (
        getattr(multi, "sender_type", "") == "salesperson"
        and any(a.action == "follow_up_reminder" for a in actions)
        and _looks_like_salesperson_resend_missing_context(cleaned_body, actions)
    ):
        review_reasons.append("业务员邮件像是在请求补发缺失历史邮件，不应简单判成「催回复」")

    # 2) 校准风险闸门（话题/置信/发件方/动作数 → P(标错)；阈值来自运行时配置）
    from src.agents.agt_email_labeling.src.pipeline.review_calibration import should_review
    risk_flag, risk = should_review(
        primary_topic, score, getattr(multi, "sender_type", "") or "", len(actions),
        threshold=risk_threshold,
    )
    if risk_flag:
        review_reasons.append(
            f"校准复核风险高（P(标错)≈{risk:.0%}，话题={primary_topic or '—'}）"
        )

    needs_review = bool(review_reasons)

    return ConfidenceResult(
        score=score,
        level=level,
        detail=ConfidenceDetail(
            # 字段复用 V3 名称，V2 语义（已无 LLM 自评置信度）：
            llm_event_confidence=0.0,                       # 已移除 LLM 自评置信度
            candidate_gap=round(clarity_score, 4),          # 话题清晰度
            evidence_strength=round(evidence_score, 4),     # 证据覆盖率
            transition_reasonableness=round(alignment_score, 4),  # 动作对齐
            context_completeness=0.0,                       # 实体维度已移除（实体不再纳入置信度）
            weighted_score=score,
        ),
        needs_review=needs_review,
        review_reasons=review_reasons,
    )


def _looks_like_salesperson_resend_missing_context(cleaned_body: str, actions: list) -> bool:
    """识别"业务员请求客户补发缺失历史邮件/上下文"的可疑催回复场景。"""
    parts = [cleaned_body or ""]
    for action in actions or []:
        evidence = getattr(action, "evidence", "") or ""
        if evidence:
            parts.append(evidence)
    text = "\n".join(parts).lower()
    asks_resend = bool(re.search(r"\b(?:re-?send|send\s+me\s+again|forward\s+me)\b", text))
    mentions_mail = bool(re.search(r"\b(?:email|e-mail|mail|letter|message)\b", text))
    missing_context = bool(re.search(
        r"\b(?:july|didn'?t\s+receive|not\s+receive|only\s+email\s+i\s+received|check\s+and\s+reply)\b",
        text,
    ))
    return asks_resend and mentions_mail and missing_context
