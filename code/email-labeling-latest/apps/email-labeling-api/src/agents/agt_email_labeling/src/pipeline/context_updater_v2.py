"""Stage 7: ContextUpdaterV2 — 只写 multi_label_recent_events。

核心原则：
  - 只从 MultiLabelResult（topics / actions / 非权威 entities / 非权威 current_facts）派生
  - 不读 legacy email_topic / sub_action / primary_event / recent_events
  - SystemFilter 命中的邮件标记 excluded_from_context=True，不参与业务聚合
  - 订单/样品单/发货发生事实只来自 authoritative_events，不写入模型派生 recent_event
"""
from typing import Any, Dict, List, Optional

from src.agents.agt_email_labeling.src.schemas import MultiLabelResult


def derive_multi_label_event(
    email_id: str,
    sender_type: str,
    multi: MultiLabelResult,
    excluded_from_context: bool = False,
    sent_at: Any = None,
    customer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """从 multi_label_result 派生 V2 recent_event 条目。

    输出写入 EmailLabelingThread.multi_label_recent_events。
    ThreadContextBuilderV2（会话层）与 CustomerContextBuilderV2（客户级层）消费这个结构。
    sent_at / customer_id 用于客户级层**跨线程按时间截断**（历史条目缺这俩时由读取侧 join 邮件表兜底）。
    """
    entities = multi.entities.model_dump()
    for field_name in ("orders", "payments", "logistics"):
        entities[field_name] = []
    return {
        "schema_version": "multi_label_recent_event_v1",
        "email_id": email_id,
        "customer_id": customer_id or "",
        "sent_at": sent_at.isoformat() if hasattr(sent_at, "isoformat") else (sent_at or None),
        "sender_type": sender_type,
        "primary_topic": multi.primary_topic or "",
        "topics": [t.model_dump() for t in multi.topics],
        "actions": [a.model_dump() for a in multi.actions],
        "entities": entities,
        "current_facts": multi.current_facts.model_dump(),
        "evidence": _best_evidence(multi),
        "excluded_from_context": excluded_from_context,
    }


def _best_evidence(multi: MultiLabelResult, max_snippets: int = 3) -> str:
    """从 multi_label 各字段收集最佳 evidence 片段。"""
    snippets: List[str] = []

    for t in multi.topics:
        if t.evidence and len(snippets) < max_snippets:
            snippets.append(f"topic[{t.topic}]: {t.evidence[:120]}")

    for a in multi.actions:
        if a.evidence and len(snippets) < max_snippets:
            snippets.append(f"action[{a.action}]: {a.evidence[:120]}")

    for field_name in ("products", "prices", "payments", "logistics", "documents", "orders", "meetings"):
        entities_list = getattr(multi.entities, field_name, [])
        for ent in entities_list[:2]:
            ev = getattr(ent, "evidence", None) or ""
            if ev and len(snippets) < max_snippets:
                snippets.append(f"{field_name}: {ev[:120]}")

    return " | ".join(snippets)
