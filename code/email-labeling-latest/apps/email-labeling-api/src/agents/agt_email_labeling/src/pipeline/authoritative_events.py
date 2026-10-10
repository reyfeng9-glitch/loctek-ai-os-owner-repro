"""权威业务事件（订单 / 样品订单的下单与发货）—— 用于**时序消歧 + 动作校验**的事实依据。

真实成交事实（ERP 的下单 / 发货）是权威动作。我们用它对照邮件抽出的动作，核对动作抽得准不准
；同时由 authoritative_context.py 按"客户摘要 + 活跃窗口 + 显式命中"压缩后进入模型上下文，
帮助判断当前邮件是否在谈已有订单的履约/物流/售后。不产出业务阶段、不单调锁定、不替代原文意图。
当前没有业务 API：订单是**测试数据**，由 inject_authoritative_orders.py 解析订单 MD →
结构化 fixture（authoritative_orders.json）→ 按时间切片**注入测试集**（每封邮件的
authoritative_events）。

本模块只提供一个**纯函数** events_for（给定某客户订单 + 时间点 → 截至该时刻的事件），
供注入脚本构造测试集使用。**不做运行时文件加载、不做降级兜底**——线上接入 ERP/API 后，
由 API 直接提供同形状事件，这段时间切片逻辑可复用。

事件格式：
    {
      "type": "order_confirmed"|"order_shipped"|"sample_order_confirmed"|"sample_order_shipped",
      "order_no": str,
      "date": "YYYY-MM-DD",
      "source": str,
      "order_info": {"products": [...], "item_details": [...], ...}
    }
"""
import os
from datetime import date, datetime
from typing import List, Optional

# 订单 fixture 落盘位置（结构化测试数据；运行时不加载，仅注入脚本写入/查看）
_ORDERS_PATH = os.path.join(os.path.dirname(__file__), "authoritative_orders.json")
_SAMPLE_ORDERS_PATH = os.path.join(os.path.dirname(__file__), "authoritative_sample_orders.json")


def _as_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()


def events_for(customer_email: Optional[str], as_of, orders: dict) -> List[dict]:
    """某客户截至 as_of（含）已发生的权威事件。

    orders: {customer_email: {code, name, orders:[{order_no, order_date, actual_ship_day,...}]}}
    order_date      ≤ as_of → order_confirmed（测试集里 order_date 按下单时间处理）
    actual_ship_day ≤ as_of → order_shipped
    """
    if not customer_email:
        return []
    cust = orders.get(customer_email)
    if not cust:
        return []
    as_of_d = _as_date(as_of)
    if as_of_d is None:
        return []
    events: List[dict] = []
    for o in cust.get("orders", []):
        order_info = _order_info(o)
        od = _as_date(o.get("order_date"))
        if od and od <= as_of_d:
            events.append({"type": "order_confirmed", "order_no": o.get("order_no"),
                           "date": o.get("order_date"), "source": "erp_test",
                           "order_info": order_info})
        sd = _as_date(o.get("actual_ship_day"))
        if sd and sd <= as_of_d:
            events.append({"type": "order_shipped", "order_no": o.get("order_no"),
                           "date": o.get("actual_ship_day"), "source": "erp_test",
                           "order_info": order_info})
    return events


def sample_events_for(customer_email: Optional[str], as_of, sample_orders: dict) -> List[dict]:
    """某客户截至 as_of（含）已发生的样品订单权威事件。

    sample_orders 与 orders 同形状，但事件类型独立：
    order_date      ≤ as_of → sample_order_confirmed
    actual_ship_day ≤ as_of → sample_order_shipped
    """
    if not customer_email:
        return []
    cust = sample_orders.get(customer_email) or sample_orders.get(str(customer_email).lower())
    if not cust:
        return []
    as_of_d = _as_date(as_of)
    if as_of_d is None:
        return []
    events: List[dict] = []
    for o in cust.get("orders", []):
        order_info = _order_info(o)
        order_info["authority_event_family"] = "sample_order"
        od = _as_date(o.get("order_date") or o.get("orderdate"))
        if od and od <= as_of_d:
            events.append({"type": "sample_order_confirmed", "order_no": o.get("order_no"),
                           "date": (o.get("order_date") or o.get("orderdate")), "source": "sample_erp_test",
                           "order_info": order_info})
        sd = _as_date(o.get("actual_ship_day"))
        if sd and sd <= as_of_d:
            events.append({"type": "sample_order_shipped", "order_no": o.get("order_no"),
                           "date": o.get("actual_ship_day"), "source": "sample_erp_test",
                           "order_info": order_info})
    return events


def _order_info(order: dict) -> dict:
    items = order.get("items") or []
    products = []
    item_details = []
    amount_total = 0
    quantity_total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        sku = str(item.get("sku") or "").strip()
        qty = item.get("sku_num")
        amount = item.get("sku_amount")
        if isinstance(amount, (int, float)):
            amount_total += amount
        if isinstance(qty, (int, float)):
            quantity_total += qty
        if sku:
            products.append(f"{sku}{f' × {qty}' if qty is not None else ''}")
        item_details.append({
            "sku": sku,
            "sku_num": qty,
            "sku_amount": amount,
        })
    return {
        "order_refs": [order.get("order_no")] if order.get("order_no") else [],
        "products": products,
        "items": products,
        "item_details": item_details,
        "amount_total": amount_total,
        "quantity_total": quantity_total,
        "salesman_name": order.get("salesman_name") or "",
    }
