"""权威业务事件上下文 Builder。

把 ERP/订单系统给出的订单/样品订单下单、发货事实，压缩成给 LLM 使用的三层上下文：

1. lifetime summary：客户是否成交过、最近一次下单/发货、订单数。
2. active events：仍在合理业务窗口内的订单/发货事件。
3. matched events：当前邮件显式提到的订单号/SKU 等事件，不受时间窗限制。

原则：权威事件用于时序消歧，不替代当前邮件自身意图；详细事件有窗口和上限，避免老订单污染新邮件。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional


ORDER_ACTIVE_DAYS = 180
ORDER_AFTER_SHIP_DAYS = 30
SHIP_ACTIVE_DAYS = 120
EXPLICIT_LOOKBACK_DAYS = 365
MAX_DETAILED_EVENTS = 8


def build_authoritative_context(
    auth_events: Optional[List[Dict[str, Any]]],
    *,
    as_of: Any = None,
    subject: str = "",
    body: str = "",
    max_events: int = MAX_DETAILED_EVENTS,
) -> str:
    """生成给模型看的权威事实上下文。

    auth_events 通常已经是"截至当前邮件 sent_at 已发生"的切片；这里再做上下文压缩：
    - 永远保留客户级摘要；
    - 只展开活跃窗口内的详细事件；
    - 邮件显式提到订单号/SKU 时，相关事件优先展开。
    """
    events = _normalize_events(auth_events)
    if not events:
        return ""

    as_of_date = _as_date(as_of) or _latest_event_date(events) or date.today()
    order_ship_dates = _order_ship_dates(events)
    text = f"{subject}\n{body}".lower()

    confirmed = [event for event in events if event["type"] == "order_confirmed"]
    shipped = [event for event in events if event["type"] == "order_shipped"]
    sample_confirmed = [event for event in events if event["type"] == "sample_order_confirmed"]
    sample_shipped = [event for event in events if event["type"] == "sample_order_shipped"]
    latest_order = max((event["date"] for event in confirmed), default=None)
    latest_ship = max((event["date"] for event in shipped), default=None)
    latest_sample_order = max((event["date"] for event in sample_confirmed), default=None)
    latest_sample_ship = max((event["date"] for event in sample_shipped), default=None)

    summary_parts = []
    if confirmed:
        summary_parts.append(f"历史下单 {len(_unique_orders(confirmed))} 笔，最近下单 {latest_order.isoformat()}")
    if shipped:
        summary_parts.append(f"历史发货 {len(_unique_orders(shipped))} 笔，最近发货 {latest_ship.isoformat()}")
    if sample_confirmed:
        summary_parts.append(f"历史样品下单 {len(_unique_orders(sample_confirmed))} 笔，最近样品下单 {latest_sample_order.isoformat()}")
    if sample_shipped:
        summary_parts.append(f"历史样品发货 {len(_unique_orders(sample_shipped))} 笔，最近样品发货 {latest_sample_ship.isoformat()}")
    if not summary_parts:
        return ""

    matched = []
    active = []
    for event in events:
        if _is_explicitly_mentioned(event, text, as_of_date):
            matched.append(event)
        elif _is_active_event(event, as_of_date, order_ship_dates):
            active.append(event)

    detailed = _dedupe_events([*matched, *active])
    detailed = sorted(
        detailed,
        key=lambda event: (
            1 if event in matched else 0,
            event["date"],
            event.get("order_no") or "",
        ),
        reverse=True,
    )[:max_events]

    lines = [
        "ERP权威业务事实 / Authoritative ERP context:",
        f"- 客户摘要 / customer summary: {'；'.join(summary_parts)}。",
    ]
    if detailed:
        lines.append(
            "- 详细参与规则 / detail policy: 显式提到的订单优先；未显式提到时，只展开仍在业务有效期内的下单/发货事件。"
        )
        for event in detailed:
            reason = "explicit_match" if event in matched else _active_reason(event, as_of_date, order_ship_dates)
            lines.append(f"- {format_authoritative_event(event)}；参与原因 / reason: {reason}")
    else:
        lines.append("- 本封邮件未命中仍需展开的具体订单事件，仅使用客户级历史摘要。")
    lines.append("使用边界 / boundary: 这些是真实业务事实，只用于判断是否涉及已有订单的履约/物流/售后；当前邮件意图仍以邮件原文为准。")
    return "\n".join(lines)


def _normalize_events(auth_events: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in auth_events or []:
        if not isinstance(raw, dict):
            continue
        event_type = raw.get("type")
        if event_type not in {"order_confirmed", "order_shipped", "sample_order_confirmed", "sample_order_shipped"}:
            continue
        event_date = _as_date(raw.get("date") or raw.get("effective_at") or raw.get("order_date") or raw.get("actual_ship_day"))
        if not event_date:
            continue
        order_no = str(raw.get("order_no") or raw.get("order_ref") or "").strip()
        key = (event_type, order_no, event_date.isoformat())
        if key in seen:
            continue
        seen.add(key)
        event = dict(raw)
        event["type"] = event_type
        event["date"] = event_date
        event["order_no"] = order_no
        out.append(event)
    return sorted(out, key=lambda event: event["date"])


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _latest_event_date(events: List[Dict[str, Any]]) -> Optional[date]:
    return max((event["date"] for event in events), default=None)


def _unique_orders(events: List[Dict[str, Any]]) -> set[str]:
    return {event.get("order_no") or event["date"].isoformat() for event in events}


def _order_ship_dates(events: List[Dict[str, Any]]) -> Dict[str, date]:
    out: Dict[str, date] = {}
    for event in events:
        if event["type"] not in {"order_shipped", "sample_order_shipped"}:
            continue
        order_no = event.get("order_no") or ""
        if not order_no:
            continue
        out[order_no] = max(out.get(order_no, event["date"]), event["date"])
    return out


def _is_active_event(event: Dict[str, Any], as_of: date, order_ship_dates: Dict[str, date]) -> bool:
    event_date = event["date"]
    if event_date > as_of:
        return False
    if event["type"] in {"order_shipped", "sample_order_shipped"}:
        return as_of <= event_date + timedelta(days=SHIP_ACTIVE_DAYS)
    order_no = event.get("order_no") or ""
    ship_date = order_ship_dates.get(order_no)
    if ship_date:
        return as_of <= ship_date + timedelta(days=ORDER_AFTER_SHIP_DAYS)
    return as_of <= event_date + timedelta(days=ORDER_ACTIVE_DAYS)


def _active_reason(event: Dict[str, Any], as_of: date, order_ship_dates: Dict[str, date]) -> str:
    if event["type"] in {"order_shipped", "sample_order_shipped"}:
        days = (as_of - event["date"]).days
        return f"active_shipment_window_{days}d/{SHIP_ACTIVE_DAYS}d"
    order_no = event.get("order_no") or ""
    ship_date = order_ship_dates.get(order_no)
    if ship_date:
        days = (as_of - ship_date).days
        return f"order_after_ship_window_{days}d/{ORDER_AFTER_SHIP_DAYS}d"
    days = (as_of - event["date"]).days
    return f"active_order_window_{days}d/{ORDER_ACTIVE_DAYS}d"


def _is_explicitly_mentioned(event: Dict[str, Any], text: str, as_of: date) -> bool:
    if not text:
        return False
    order_no = (event.get("order_no") or "").strip().lower()
    if order_no and order_no in text:
        return True
    for token in _event_tokens(event):
        if token and re.search(rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])", text):
            event_date = event["date"]
            # SKU/弱 token 只在一年内允许唤起详细事件，防止陈年 SKU 污染。
            if (as_of - event_date).days <= EXPLICIT_LOOKBACK_DAYS:
                return True
    return False


def _event_tokens(event: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    order_info = event.get("order_info") or {}
    for key in ("items", "products", "order_refs"):
        values = order_info.get(key) or []
        if isinstance(values, list):
            tokens.extend(str(value).split("×")[0].strip() for value in values)
    for item in order_info.get("item_details") or []:
        if isinstance(item, dict) and item.get("sku"):
            tokens.append(str(item["sku"]))
    return [token for token in tokens if len(token) >= 4]


def _dedupe_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for event in events:
        key = (event["type"], event.get("order_no") or "", event["date"].isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def format_authoritative_event(event: Dict[str, Any]) -> str:
    label_map = {
        "order_confirmed": "下单 / order confirmed",
        "order_shipped": "发货 / order shipped",
        "sample_order_confirmed": "样品下单 / sample order confirmed",
        "sample_order_shipped": "样品发货 / sample order shipped",
    }
    label = label_map.get(event["type"], event["type"])
    order_no = event.get("order_no") or "unknown order"
    order_info = event.get("order_info") or {}
    products = [str(item) for item in (order_info.get("products") or order_info.get("items") or []) if item]
    product_text = f"，货物 / goods: {', '.join(products[:3])}" if products else ""
    if len(products) > 3:
        product_text += f" +{len(products) - 3} more"
    amount = order_info.get("amount_total")
    amount_text = f"，金额 / amount: {amount}" if amount else ""
    return f"{label}: {order_no} @ {event['date'].isoformat()}{product_text}{amount_text}"
