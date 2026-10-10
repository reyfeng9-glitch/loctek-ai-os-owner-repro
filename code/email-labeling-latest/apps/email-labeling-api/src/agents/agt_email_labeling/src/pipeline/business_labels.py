# -*- coding: utf-8 -*-
"""业务标签派生层 —— 在 action 之上,用「动作 + 发件方向 + 邮件摘要 + ERP权威数据 + 时序上下文」派生业务场景标签。

对齐业务侧《外销业务邮件标签库》(飞书 wiki):分两类
  - **派生型**(_DERIVED):不能仅靠单个动作确定,需 动作+方向+ERP/时序/语义(冷启动/老客户召回/新品/首次接触/报价跟进/催回复)。
  - **直接映射型**(_DIRECT_MAP):业务标签 ≈ 触发动作 + 发件方向(文档「直接映射」行),如 发送报价/询价/确认订单…

特点(按需求确定):
  - **多标签**:一封邮件可命中多个业务标签;
  - **规则为主 + 边界用 LLM**:能用 (动作+方向+ERP+上下文) 确定的走声明式规则(零 LLM、可审计);
    需要语义判断的边界场景挂 `boundary` 钩子(预留,见 derive_business_labels)。
  - **声明式、可扩展**:陆续补充只在 _DERIVED / _DIRECT_MAP 增条目;配置页同源展示。

与已移除的「漏斗状态机」不同:这是**每封邮件自身**的业务场景标签,动作为主、方向+ERP+上下文消歧,
不是跨邮件推断"客户当前阶段"的状态机。

—— 上下文信号(由 compute_business_signals 从线程/客户历史 + auth_events 算出)——
  has_formal_order        : 该客户截至本邮件已有 ERP 正式订单(order_confirmed)
  customer_replied_before : 本客户在本邮件之前是否已回复过(= 是否已过"首次回复")
  days_since_last_contact : 距上次联系(该客户上一封邮件)的天数;无历史则 None
"""
from datetime import datetime
from typing import Optional


# ── 上下文信号计算(纯函数,离线/在线共用)─────────────────────────────────────
def _parse_dt(s) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def compute_business_signals(email_sent_at, prior_emails, auth_events,
                             thread_had_quotation_before: bool = False) -> dict:
    """
    email_sent_at : 本邮件 sent_at(ISO 字符串或 datetime)
    prior_emails  : 该客户在本邮件**之前**的邮件 [(sender_type, sent_at), ...](已排除 system_noise/auto-reply)
    auth_events   : 截至本邮件的 ERP 权威事件列表(已 as_of 切片)
    thread_had_quotation_before : 本**线程**在本邮件之前是否已发出过报价(send_quotation)
    """
    has_order = any((e or {}).get("type") == "order_confirmed" for e in (auth_events or []))
    replied = any((s or "") == "customer" for s, _ in (prior_emails or []))
    now = _parse_dt(email_sent_at)
    gap = None
    prev_dts = [_parse_dt(d) for _, d in (prior_emails or []) if _parse_dt(d)]
    if now and prev_dts:
        last = max(prev_dts)
        gap = (now - last).days
    return {
        "has_formal_order": has_order,
        "customer_replied_before": replied,
        "days_since_last_contact": gap,
        "thread_had_quotation_before": bool(thread_had_quotation_before),
    }


# 已进入"成交后/履约"阶段的动作:出现这些 → 邮件主线已过报价阶段,不应再判"报价跟进"。
_LATE_STAGE_ACTIONS = frozenset({
    "order_confirmation", "order_payment", "order_detail_confirmation", "order_post_doc",
    "shipment_notice", "logistics_tracking", "delivery_followup", "shipment_delay",
    "after_sales_support", "after_sales_inquiry", "quality_complaint",
})
_NON_QUOTATION_FOLLOWUP_ACTIONS = frozenset({
    "offline_invitation", "offline_visit", "online_meeting",
})


# ── 业务标签:对齐业务侧《外销业务邮件标签库》(单一真相源)──────────────────────
# 每条:id / display / when(actions:set, sender:str, summary:str, sig:dict)->bool / condition_text / rationale。
def _dir_ok(direction: str, sender: str) -> bool:
    return direction == "both" or (sender or "") == direction


# ① 派生型:不能仅靠单个动作确定,需 动作 + 方向 + ERP/时序/语义。
_DERIVED = [
    {
        "id": "cold_start_development",
        "display": "冷启动开发",
        "trigger_actions": ["product_outreach"],
        "required_facts": [],
        "required_context": ["customer_replied_before:false"],
        "excluded_conditions": [],
        "compatible_labels": ["new_product_promotion"],
        "conflict_labels": ["customer_recall"],
        "when": lambda actions, sender, summary, sig: (
            sender == "salesperson"
            and "product_outreach" in actions
            and not sig.get("customer_replied_before")
        ),
        "condition_text": "业务员 + 含产品推介动作(product_outreach) + 客户尚未首次回复",
        "rationale": "业务员在客户**首次回复前**的产品推介(建立联系)。'冷'是关系上下文,由 customer_replied_before 判,"
                     "不再焊进动作名。自动回复已上游短路排除。",
    },
    {
        "id": "customer_recall",
        "display": "老客户召回",
        "trigger_actions": ["product_outreach"],
        "required_facts": ["erp_order_confirmed"],
        "required_context": ["has_formal_order:true", "days_since_last_contact:>=60"],
        "excluded_conditions": [],
        "compatible_labels": ["new_product_promotion"],
        "conflict_labels": ["cold_start_development"],
        "when": lambda actions, sender, summary, sig: (
            sender == "salesperson"
            and sig.get("has_formal_order")
            and "product_outreach" in actions
            and (sig.get("days_since_last_contact") or 0) >= 60
        ),
        "condition_text": "业务员 + 该客户有 ERP 正式订单 + 含产品推介动作 + 距上次联系 ≥60 天",
        "rationale": "对**有正式订单(ERP)**的老客户,在 ≥60 天沉默后再次产品推介/唤醒。'老客户'由 ERP+时序判,不焊进动作名。",
    },
    {
        "id": "new_product_promotion",
        "display": "新品推介",
        "trigger_actions": ["product_outreach"],
        "required_facts": ["promotes_new_product_in_email"],
        "required_context": [],
        "excluded_conditions": [],
        "compatible_labels": ["cold_start_development", "customer_recall"],
        "conflict_labels": [],
        "when": lambda actions, sender, summary, sig: (
            sender == "salesperson"
            and "product_outreach" in actions
            and sig.get("promotes_new_product")
        ),
        "condition_text": "业务员 + 含产品推介动作 + 本封明确推介新品/最新款(promotes_new_product fact)",
        "rationale": "推介的是**新品/最新款**(客户之前未推过/未购买过)。'新品'是语义信号(LLM 置 promotes_new_product fact),"
                     "与冷/老客户正交——可与冷启动开发/老客户召回同时命中(多标签)。",
    },
    {
        "id": "first_contact",
        "display": "首次接触",
        "trigger_actions": [],
        "required_facts": [],
        "required_context": ["customer_replied_before:false"],
        "excluded_conditions": [],
        "compatible_labels": [],
        "conflict_labels": [],
        "when": lambda actions, sender, summary, sig: (
            sender == "customer" and not sig.get("customer_replied_before")
        ),
        "condition_text": "客户邮件 + 该客户此前无更早邮件(本封是客户首次回复/首次来邮)",
        "rationale": "客户在本关系里**第一次**主动来邮/回复 —— 关系建立的关键转化节点。"
                     "时序由 customer_replied_before 判定,不再靠 first_reply 动作焊死'首次'。",
    },
    {
        "id": "quotation_followup",
        "display": "报价跟进",
        "trigger_actions": ["follow_up_reminder", "quotation_followup"],
        "required_facts": [],
        "required_context": ["thread_had_quotation_before:true"],
        "excluded_conditions": [
            *[f"late_stage_action:{action}" for action in sorted(_LATE_STAGE_ACTIONS)],
            *[f"non_quotation_action:{action}" for action in sorted(_NON_QUOTATION_FOLLOWUP_ACTIONS)],
        ],
        "compatible_labels": [],
        "conflict_labels": ["follow_up_chase"],
        "when": lambda actions, sender, summary, sig: (
            sender == "salesperson"
            and bool(actions & {"follow_up_reminder", "quotation_followup"})
            and sig.get("thread_had_quotation_before")
            and not (actions & _LATE_STAGE_ACTIONS)
        ),
        "condition_text": "业务员 + 含催回复/报价跟进动作 + 本线程此前已发过报价 + 本封未进入成交后/履约动作",
        "rationale": "业务员在**已报价**之后的跟进催进展。'跟进对象=报价'是上下文(线程已发报价),"
                     "不再焊进 quotation_followup 动作名。",
    },
    {
        "id": "follow_up_chase",
        "display": "催回复",
        "trigger_actions": ["follow_up_reminder"],
        "required_facts": [],
        "required_context": [],
        "excluded_conditions": ["quotation_followup_context"],
        "compatible_labels": [],
        "conflict_labels": ["quotation_followup"],
        "when": lambda actions, sender, summary, sig: (
            "follow_up_reminder" in actions
            and not (
                (sender or "") == "salesperson"
                and sig.get("thread_had_quotation_before")
                and not (actions & _LATE_STAGE_ACTIONS)
            )
        ),
        "condition_text": "含催回复动作(follow_up_reminder)的泛泛催进展;若专催报价单则归「报价跟进」",
        "rationale": "无特定标的物的泛泛催回复/问进展;与「报价跟进」互斥(后者专催报价单),由报价线程上下文区分。",
    },
]


# ② 直接映射型:业务标签 ≈ 触发动作 + 发件方向(源:业务侧《外销业务邮件标签库》「直接映射」行)。
# (label_id, 中文名, 触发动作 code, 方向: salesperson|customer|both)
_DIRECT_MAP = [
    # ① 业务员专用
    ("biz_send_quotation", "发送报价", "send_quotation", "salesperson"),
    ("biz_sales_handover", "业务交接", "sales_handover", "salesperson"),
    ("biz_relationship_care", "客情维护", "holiday_greeting", "salesperson"),
    ("biz_distributor_proposal", "经销推荐", "distributor_proposal", "salesperson"),
    ("biz_delivery_delay", "交期延迟", "shipment_delay", "salesperson"),
    ("biz_after_sales_support", "售后支持", "after_sales_support", "salesperson"),
    # ② 客户专用
    ("biz_product_detail_inquiry", "产品细节咨询", "product_detail_inquiry", "customer"),
    ("biz_distributor_inquiry", "经销咨询", "distributor_inquiry", "customer"),
    ("biz_price_inquiry", "询价", "price_inquiry", "customer"),
    ("biz_price_bargaining", "压价比价", "price_negotiation", "customer"),
    ("biz_delivery_followup", "催促交期", "delivery_followup", "customer"),
    ("biz_after_sales_inquiry", "售后咨询", "after_sales_inquiry", "customer"),
    # ③ 双向
    ("biz_requirement_clarification", "需求确认", "requirement_clarification", "both"),
    ("biz_spec_discussion", "规格沟通", "spec_discussion", "both"),
    ("biz_document_request", "文件请求", "document_request", "both"),
    ("biz_nda_contract", "合同/NDA签", "nda_contract", "both"),
    ("biz_sample_request", "样品请求", "sample_request", "both"),
    ("biz_sample_confirmation", "样品确认", "sample_confirmation", "both"),
    ("biz_offline_invitation", "线下邀请拜访", "offline_invitation", "both"),
    ("biz_onsite_sample_viewing", "现场看样", "offline_visit", "both"),
    ("biz_online_meeting", "线上会议", "online_meeting", "both"),
    ("biz_order_confirmation", "确认订单", "order_confirmation", "both"),
    ("biz_order_payment", "确认付款", "order_payment", "both"),
    ("biz_order_post_doc", "订单后资料", "order_post_doc", "both"),
    ("biz_shipment_notice", "发货通知", "shipment_notice", "both"),
    ("biz_logistics_tracking", "物流跟踪", "logistics_tracking", "both"),
    ("biz_sample_feedback", "样品测试反馈", "sample_feedback", "both"),
    ("biz_repurchase_intent", "复购意向", "repurchase_inquiry", "both"),
    ("biz_quality_complaint", "质量投诉", "quality_complaint", "both"),
    ("biz_order_cancellation", "取消订单", "order_cancellation", "both"),
    ("biz_short_no_business", "短回复无实质业务", "short_reply", "both"),
    # B 段:对应新增动作(需重抽才会在数据上 populate)
    ("biz_out_of_stock", "缺货/无法供货告知", "out_of_stock_notice", "salesperson"),
    ("biz_damage_shortage", "缺件损坏反馈", "damage_shortage_report", "both"),
    ("biz_return_exchange", "退换货处理", "return_exchange", "both"),
    ("biz_pause_hold", "暂停搁置", "pause_hold", "both"),
    ("biz_terminate", "终止合作", "terminate_cooperation", "both"),
    ("biz_visit_followup", "展会/拜访跟进", "visit_followup", "salesperson"),
]

# 现场看样暂映射 offline_visit(无独立动作)。
# auto_reply / 退信 / 已读回执 / 空邮件 属 system_noise,上游短路、不派生业务标签。


def _make_direct(label_id: str, display: str, action_code: str, direction: str) -> dict:
    def when(actions, sender, summary, sig, _a=action_code, _d=direction):
        return _a in actions and _dir_ok(_d, sender)
    dir_txt = {"salesperson": " + 发件方=业务员", "customer": " + 发件方=客户", "both": ""}[direction]
    return {
        "id": label_id, "display": display, "when": when, "direction": direction,
        "trigger_actions": [action_code],
        "required_facts": [],
        "required_context": [],
        "excluded_conditions": [],
        "compatible_labels": [],
        "conflict_labels": [],
        "condition_text": f"含动作「{display}」({action_code}){dir_txt}",
        "rationale": "直接映射:该动作在对应发件方向上即为此业务标签(业务侧《外销业务邮件标签库》)。",
    }


# 单一真相源(派生型在前,直接映射型在后;配置页同源)。
BUSINESS_LABELS = _DERIVED + [_make_direct(*x) for x in _DIRECT_MAP]


def derive_business_labels(actions, sender: str, summary: str, signals: dict, facts: dict = None) -> list:
    """规则为主派生业务标签(多标签)。返回 [{id, display}, ...]。

    facts: 本封 current_facts(用于把语义信号如 promotes_new_product_in_email 并入判定)。
    边界(boundary)钩子:未来给某条 spec 加 'boundary' 语义判断时,可在此对"规则判不准"的
    候选调用 LLM;当前均为确定性规则,无需 LLM。
    """
    aset = set(actions or ())
    sig = dict(signals or {})
    if facts:
        sig["promotes_new_product"] = bool(facts.get("promotes_new_product_in_email"))
    out = []
    for spec in BUSINESS_LABELS:
        if spec["when"](aset, sender or "", summary or "", sig):
            out.append({"id": spec["id"], "display": spec["display"]})
    return out


# 派生型业务标签的发件方向(用于配置页按方向分组,与业务侧文档三组一致)。
_DERIVED_DIRECTION = {
    "cold_start_development": "salesperson", "customer_recall": "salesperson",
    "new_product_promotion": "salesperson", "quotation_followup": "salesperson",
    "follow_up_chase": "both", "first_contact": "customer",
}
_GROUP_CN = {"salesperson": "业务员专用", "customer": "客户专用", "both": "双向"}


def business_labels_for_display() -> list:
    """配置页展示视图(不含不可序列化的 when 闭包),带 direction/group 便于按方向分组。"""
    out = []
    for s in BUSINESS_LABELS:
        direction = s.get("direction") or _DERIVED_DIRECTION.get(s["id"], "both")
        out.append({
            "id": s["id"],
            "display": s["display"],
            "direction": direction,
            "group": _GROUP_CN.get(direction, "双向"),
            "trigger_actions": list(s["trigger_actions"]),
            "required_facts": list(s["required_facts"]),
            "required_context": list(s["required_context"]),
            "excluded_conditions": list(s["excluded_conditions"]),
            "compatible_labels": list(s["compatible_labels"]),
            "conflict_labels": list(s["conflict_labels"]),
            "condition_text": s["condition_text"],
            "rationale": s["rationale"],
        })
    return out


def derive_n122_business_labels(n122_output) -> list[dict]:
    """Return N122 v8 records for persistence/display without legacy shaping.

    ``derive_business_labels`` remains the compatibility adapter for the old
    action based field.  The N122 result is already independently judged by
    code, evidence and confidence, so this function preserves every label and
    never applies the legacy topic cap or a primary/secondary role.
    """
    from src.agents.agt_email_labeling.src.pipeline.n122_label_validator import (
        n122_label_records,
    )

    return n122_label_records(n122_output)
