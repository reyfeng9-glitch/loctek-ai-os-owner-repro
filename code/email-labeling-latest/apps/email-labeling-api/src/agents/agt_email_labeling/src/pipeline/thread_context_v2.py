"""ThreadContextBuilderV2 — 只读 multi_label_recent_events。

核心原则：
  - 只消费 MultiLabelRecentEvent 的 topics / actions / 非权威实体 / 非权威事实
  - **不读取任何旧字段**：email_topic / sub_action / primary_event / legacy recent_events
  - excluded_from_context=True 的条目完全跳过
  - 订单/样品单/发货事实只来自 authoritative_events，不从 LLM facts/entities 继承
"""
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.agt_email_labeling.src.models import EmailLabelingEmail, EmailLabelingThread
from src.agents.agt_email_labeling.src.schemas import ThreadContextV2
from src.services.n122_history_service import HistoryDecision

_MAX_EVENTS = 20


def n122_history_context_refs(decision: HistoryDecision) -> List[str]:
    """Expose compact audit refs without placing historical bodies in prompts."""
    refs: List[str] = []
    if decision.linked_o01_email_id:
        refs.append(f"linked_o01:{decision.linked_o01_email_id}")
    if decision.has_pre_cutoff_formal_order:
        refs.append("authoritative_order:pre_cutoff_confirmed")
    if decision.days_since_last_valid_contact is not None:
        refs.append(f"last_valid_contact_gap_days:{decision.days_since_last_valid_contact}")
    refs.extend(f"source_gap:{gap}" for gap in decision.source_gaps)
    return refs


async def build_thread_context_v2(
    thread_id: str,
    current_email_id: str,
    db: AsyncSession,
    customer_email: Optional[str] = None,
) -> ThreadContextV2:
    """构建 V2 线程上下文。只从 multi_label_recent_events 聚合。"""
    thread_row = await db.execute(
        select(EmailLabelingThread).where(EmailLabelingThread.thread_id == thread_id)
    )
    thread = thread_row.scalar_one_or_none()

    if thread is None:
        ctx = ThreadContextV2(thread_id=thread_id, context_missing=True)
        ctx.customer_ever_contacted = await _check_cross_thread(
            thread_id, customer_email, db
        )
        return ctx

    raw_events: List[Dict[str, Any]] = list(thread.multi_label_recent_events or [])
    # 过滤掉当前邮件（避免读到正在处理的邮件自己的上下文）
    filtered = [e for e in raw_events if e.get("email_id") != current_email_id]
    filtered = await _filter_excluded_email_events(filtered, db)

    events = _deserialize_events(filtered)
    ctx = _aggregate_context(thread_id, events)
    ctx.customer_ever_contacted = await _check_cross_thread(
        thread_id, customer_email, db
    )
    return ctx


async def _filter_excluded_email_events(
    events: List[Dict[str, Any]],
    db: AsyncSession,
) -> List[Dict[str, Any]]:
    """过滤已被数据治理软排除的邮件事件。

    这里是上下文污染防线：重复邮件虽然不物理删除，但不能继续进入后续邮件的
    thread context。否则 UI 看起来干净了，LLM 仍会在历史上下文里读到脏事件。
    """
    email_ids = [e.get("email_id") for e in events if e.get("email_id")]
    if not email_ids:
        return events
    rows = await db.execute(
        select(EmailLabelingEmail.email_id)
        .where(EmailLabelingEmail.email_id.in_(email_ids))
        .where(EmailLabelingEmail.excluded_from_timeline == True)
    )
    excluded_ids = {row[0] for row in rows.fetchall()}
    if not excluded_ids:
        return events
    return [e for e in events if e.get("email_id") not in excluded_ids]


# ── 内部：反序列化 + 聚合 ─────────────────────────────────


def _deserialize_events(
    raw: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """过滤 excluded 条目，保留最近 _MAX_EVENTS 条。"""
    active = [e for e in raw if not e.get("excluded_from_context", False)]
    return active[-_MAX_EVENTS:]


def _aggregate_context(
    thread_id: str,
    events: List[Dict[str, Any]],
) -> ThreadContextV2:
    """从 events 列表聚合 ThreadContextV2。

    只提供**事实性近期上下文**（近期实体 + 近期事实），供 LLM 参考。
    不再聚合单调漏斗里程碑（has_ever_quoted/paid/...）——已放弃业务漏斗，以动作为主。
    """
    # 最近实体缓存（给 prompt 用）
    recent_entities: Dict[str, List[str]] = {
        "products": [], "documents": [], "prices": [],
    }

    for ev in events[-10:]:  # 最近 10 条参与聚合
        entities = ev.get("entities", {}) or {}
        for entity_type in recent_entities:
            items = entities.get(entity_type, []) or []
            for item in items[:3]:
                name = _entity_label(entity_type, item)
                if name and name not in recent_entities[entity_type]:
                    recent_entities[entity_type].append(name)

    return ThreadContextV2(
        thread_id=thread_id,
        context_missing=False,
        multi_label_events=events,
        recent_entities_text=_format_entities(recent_entities),
        recent_facts_text=_format_recent_facts(events[-5:]),
    )


async def _check_cross_thread(
    thread_id: str,
    customer_email: Optional[str],
    db: AsyncSession,
) -> bool:
    """检查客户在其他线程是否有 V2 上下文。"""
    if not customer_email:
        return False
    other = await db.execute(
        select(EmailLabelingThread)
        .where(EmailLabelingThread.customer_id == customer_email)
        .where(EmailLabelingThread.thread_id != thread_id)
        .where(
            EmailLabelingThread.multi_label_recent_events != None,
            EmailLabelingThread.multi_label_recent_events != [],
        )
        .limit(1)
    )
    return other.scalar_one_or_none() is not None


# ── 辅助函数 ───────────────────────────────────────────────


def _safe_topic_names(topics_raw: Any) -> List[str]:
    if not topics_raw or not isinstance(topics_raw, list):
        return []
    return [t.get("topic", "") if isinstance(t, dict) else str(t) for t in topics_raw]


def _safe_action_names(actions_raw: Any) -> List[str]:
    if not actions_raw or not isinstance(actions_raw, list):
        return []
    return [a.get("action", "") if isinstance(a, dict) else str(a) for a in actions_raw]


def _entity_label(entity_type: str, item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    if entity_type == "products":
        return item.get("product_name") or item.get("sku") or ""
    if entity_type in ("documents",):
        return item.get("document_type") or item.get("document_name") or ""
    if entity_type == "prices":
        return f"{item.get('currency','')} {item.get('price_value','')}".strip()
    return ""


def _format_entities(entities: Dict[str, List[str]]) -> str:
    parts: List[str] = []
    for etype, names in entities.items():
        if names:
            parts.append(f"{etype}: {', '.join(names[:5])}")
    return " | ".join(parts) if parts else ""


# 中可信事实：从邮件**明确陈述**抽出的非订单/非样品事实。
# 订单/样品/发货是否真实发生只认 authoritative_events，不能由 LLM current_facts 传给后续邮件。
_MILESTONE_FACTS = {
    "quality_complaint_in_email": "有质量投诉",
    "price_quoted_in_email": "已报价",
}


def _format_recent_facts(events: List[Dict[str, Any]]) -> str:
    """近期邮件里**明确陈述过**的业务里程碑(去重、可读、中可信)。供时序消歧参考。"""
    seen: List[str] = []
    for ev in events:
        cf = ev.get("current_facts", {}) or {}
        for key, label in _MILESTONE_FACTS.items():
            if cf.get(key) and label not in seen:
                seen.append(label)
    return "、".join(seen)
