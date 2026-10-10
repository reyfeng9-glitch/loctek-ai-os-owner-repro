"""CustomerContextBuilderV2 —— 跨该客户**全部线程**聚合「状态/里程碑」，按时间截断。

与 thread_context_v2（单会话）正交，构成方案B 的「客户级上下文层」。详见 agent README。

两条护栏（务必遵守）：
  ① 防污染：只聚**状态/里程碑**（_MILESTONE_FACTS：已报价/有质量投诉/…），
     **不聚产品实体**——产品级细节留在会话层，避免 A 会话的产品串进 B 会话。
  ② 防泄露：只取 **sent_at < 当前邮件**的其他线程事件（跨线程时间截断）。
     重抽按客户时序串行时，这条防止把"未来"泄露给当前邮件。

订单/样品/发货「是否真实发生」仍只来自 authoritative_events（ERP），不从邮件 LLM facts 推断。
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.agt_email_labeling.src.models import EmailLabelingEmail, EmailLabelingThread
from src.agents.agt_email_labeling.src.schemas import CustomerContextV2
# 里程碑事实映射单一真相源：与会话层共用，避免两套定义漂移。
from src.agents.agt_email_labeling.src.pipeline.thread_context_v2 import _MILESTONE_FACTS
from src.services.n122_history_service import (
    HistoryCompleteness,
    HistoryDecision,
    HistoryEmail,
    load_and_evaluate_history,
)

# 扫描上限（防止大客户每标一封都全量遍历兄弟线程；输出本就有界，这里界住"读"的量）：
_MAX_THREADS = 50          # cap②-a：最多加载最近活跃的 N 个其他线程
_MAX_SCAN_EVENTS = 60      # cap②-b：里程碑聚合最多看最近 K 条事件（按 sent_at 倒序）


def _as_dt(v: Any) -> Optional[datetime]:
    """统一成可比较的 naive datetime（datetime / date / ISO 字符串都接）。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


async def build_customer_context(
    customer_id: Optional[str],
    as_of: Any,
    current_thread_id: str,
    db: AsyncSession,
) -> CustomerContextV2:
    """构建客户级上下文：跨该客户**其他**线程、截至 as_of 的里程碑聚合。

    customer_id：客户编码（精确实体键）。无编码 → 空上下文。
    as_of：当前邮件 sent_at；只聚合早于它的事件（护栏②）。
    """
    if not customer_id:
        return CustomerContextV2(context_missing=True)

    as_of_dt = _as_dt(as_of)

    # cap②-a：只加载该客户最近活跃的 N 个其他线程（每线程事件本就 ≤MAX_RECENT_EVENTS）。
    rows = await db.execute(
        select(EmailLabelingThread.thread_id, EmailLabelingThread.multi_label_recent_events)
        .where(EmailLabelingThread.customer_id == customer_id)
        .where(EmailLabelingThread.thread_id != current_thread_id)
        .order_by(EmailLabelingThread.updated_at.desc())
        .limit(_MAX_THREADS)
    )
    thread_events = rows.all()
    if not thread_events:
        return CustomerContextV2(customer_id=customer_id, context_missing=True)

    # 收集其他线程的事件条目；缺 sent_at 的（历史数据）记下 email_id 待 join 兜底。
    pending: List[tuple] = []          # (thread_id, entry, sent_at|None)
    need_ids: List[str] = []
    for tid, events in thread_events:
        for e in (events or []):
            if e.get("excluded_from_context"):
                continue
            sa = _as_dt(e.get("sent_at"))
            if sa is None and e.get("email_id"):
                need_ids.append(e["email_id"])
            pending.append((tid, e, sa))

    sent_map: Dict[str, Any] = {}
    if need_ids:
        erows = await db.execute(
            select(EmailLabelingEmail.email_id, EmailLabelingEmail.sent_at)
            .where(EmailLabelingEmail.email_id.in_(need_ids))
        )
        sent_map = {eid: sa for eid, sa in erows.all()}

    # 时间截断（护栏②）：只留 sent_at < as_of 的事件。
    valid: List[tuple] = []            # (sent_at, thread_id, current_facts)
    for tid, e, sa in pending:
        if sa is None:
            sa = _as_dt(sent_map.get(e.get("email_id")))
        if sa is None:
            continue                   # 定不了时间 → 跳过（宁缺毋泄露）
        if as_of_dt is not None and not (sa < as_of_dt):
            continue                   # 只取当前邮件之前
        valid.append((sa, tid, e.get("current_facts") or {}))

    other_thread_count = len({tid for _, tid, _ in valid})

    # cap②-b：按时间倒序只取最近 K 条聚合里程碑；cap①：里程碑种类集齐即停。
    valid.sort(key=lambda x: x[0], reverse=True)
    milestones: List[str] = []         # 有序去重
    for _, _, cf in valid[:_MAX_SCAN_EVENTS]:
        for key, label in _MILESTONE_FACTS.items():
            if cf.get(key) and label not in milestones:
                milestones.append(label)
        if len(milestones) >= len(_MILESTONE_FACTS):
            break

    return CustomerContextV2(
        customer_id=customer_id,
        context_missing=not valid,
        other_thread_count=other_thread_count,
        milestone_facts_text="、".join(milestones),
    )


async def build_n122_complete_history(
    *,
    current: HistoryEmail,
    authoritative_events: List[Dict[str, Any]],
    completeness: HistoryCompleteness,
    db: AsyncSession,
) -> HistoryDecision:
    """Dedicated uncapped N122 history path for I01/O03 decisions.

    The ordinary customer context remains bounded because it is prompt context.
    This path instead reads compact metadata from the entire pre-cutoff customer
    timeline and returns only auditable decision signals.
    """
    return await load_and_evaluate_history(
        current=current,
        authoritative_events=authoritative_events,
        completeness=completeness,
        db=db,
    )
