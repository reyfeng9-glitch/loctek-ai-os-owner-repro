# -*- coding: utf-8 -*-
"""发件方向约束(动作方向)—— 声明式单一真相源。

被流水线(rule_validator)强制执行,也被配置页 API 读取展示(GET /config/direction-rules)。

原理:外贸 B2B 往来里,某些动作**只可能由一方发出**。发件方向不符即方向性误判
(人工 Review 报告「发件方向错误」占 21%,仅次于主题冲突)。
  - 业务员专有动作出现在客户邮件 → 剔除(客户不会发报价/做交接/发货通知/冷推…)。
  - 客户专有动作出现在业务员邮件 → 剔除(业务员不会"首次回复",也不会向自己"询价/问产品/复购/售后咨询")。

只锁**方向确凿**的动作;双方都可能发的(sample_request 业务员可转述、order_payment 业务员可确认、
price_negotiation 双方都谈、distributor_inquiry 业务员可洽谈经销)一律不锁,避免误删真实信号 / 丢覆盖。
"""

from src.agents.agt_email_labeling.src.pipeline.action_definitions import ACTION_DISPLAY_MAP
from src.agents.agt_email_labeling.src.pipeline.topic_definitions import TOPIC_DISPLAY_MAP

# 只可能由业务员发出的动作:客户邮件上出现即方向性错误。
SALES_ONLY_ACTIONS = frozenset({
    "send_quotation", "sales_handover", "shipment_notice", "sample_shipment",
    "quotation_followup", "product_outreach", "outreach_exhibition",
})

# 只可能由客户发出的动作:业务员邮件上出现即方向性错误。
# 「首次回复」是客户专有;「询价 / 产品细节咨询 / 复购咨询 / 售后咨询」都是买方问询(卖方负责回答,不问询)。
# 注:distributor_inquiry(经销咨询)不锁 —— 业务员邮件常合法洽谈经销(golden 实证:gold=partnership 共 2 例)。
CUSTOMER_ONLY_ACTIONS = frozenset({
    "first_reply", "price_inquiry", "product_detail_inquiry",
    "repurchase_inquiry", "after_sales_inquiry",
})


# 主题方向:某些主题只可能由某一方"产出"。客户邮件上的 quotation(报价)实为"询价"。
#   - 同封已有其它主题 → 直接剔除 quotation(让位给真实业务主题);
#   - quotation 是唯一主题 → remap 成 fallback(requirement_discussion,询价归此),不留空。
# 数据验证:客户邮件带 quotation 共 56 封,golden 中 gold=quotation 的 0 封 → 剔除/remap 零覆盖风险。
TOPIC_DIRECTION = [
    {
        "sender": "customer",
        "topic": "quotation",
        "fallback_topic": "requirement_discussion",
        "title": "客户邮件 报价→询价(需求/规格沟通)",
        "rationale": "客户不发报价、只会询价。客户邮件上的 quotation 实为'索要报价/询价':"
                     "同封已有其它业务主题则直接剔除 quotation;若 quotation 是唯一主题则 remap 成"
                     " requirement_discussion(询价归此),不留空。",
    },
]


def apply_direction_filter(multi) -> None:
    """按发件方剔除方向不可能的动作 + 修正方向不可能的主题(原地修改)。纯精度修复。"""
    sender = getattr(multi, "sender_type", "") or ""
    if sender == "customer":
        multi.actions = [a for a in multi.actions if a.action not in SALES_ONLY_ACTIONS]
    elif sender == "salesperson":
        multi.actions = [a for a in multi.actions if a.action not in CUSTOMER_ONLY_ACTIONS]

    # 主题方向修正
    for rule in TOPIC_DIRECTION:
        if sender != rule["sender"]:
            continue
        topic = rule["topic"]
        if not any(t.topic == topic for t in multi.topics):
            continue
        others = [t for t in multi.topics if t.topic != topic]
        if others:
            multi.topics = others                      # 有其它主题 → 剔除方向错的 quotation
        else:
            for t in multi.topics:                     # quotation 是唯一主题 → remap,不留空
                if t.topic == topic:
                    t.topic = rule["fallback_topic"]


def direction_rules_for_display() -> list:
    """配置页展示视图:补动作/主题中文名,便于前端直接渲染。

    每条规则带 kind:'action'(剔除方向错的动作)或 'topic'(剔除/remap 方向错的主题)。"""
    def _names(codes):
        return [{"code": c, "display": ACTION_DISPLAY_MAP.get(c, c)} for c in sorted(codes)]
    rules = [
        {
            "id": "sales_only_on_customer",
            "kind": "action",
            "sender": "customer",
            "sender_display": "客户邮件",
            "drop_actions": _names(SALES_ONLY_ACTIONS),
            "title": "客户邮件 ⊄ 业务员专有动作",
            "rationale": "客户不会发报价 / 做销售交接 / 发货通知 / 样品发货 / 冷推 / 老客户唤醒等;"
                         "这些动作出现在客户邮件上即方向性误判,剔除。",
        },
        {
            "id": "customer_only_on_sales",
            "kind": "action",
            "sender": "salesperson",
            "sender_display": "业务员邮件",
            "drop_actions": _names(CUSTOMER_ONLY_ACTIONS),
            "title": "业务员邮件 ⊄ 客户专有动作",
            "rationale": "「首次回复」是客户专有;询价 / 产品细节咨询 / 复购咨询 / 售后咨询 都是买方问询"
                         "(卖方负责回答,不问询)。出现在业务员邮件上即方向性误判,剔除。"
                         "注:经销咨询不锁——业务员常合法洽谈经销。",
        },
    ]
    for r in TOPIC_DIRECTION:
        rules.append({
            "id": f"topic_dir_{r['sender']}_{r['topic']}",
            "kind": "topic",
            "sender": r["sender"],
            "sender_display": "客户邮件" if r["sender"] == "customer" else "业务员邮件",
            "drop_actions": [],
            "topic_remap": {
                "from": r["topic"],
                "from_display": TOPIC_DISPLAY_MAP.get(r["topic"], r["topic"]),
                "to": r["fallback_topic"],
                "to_display": TOPIC_DISPLAY_MAP.get(r["fallback_topic"], r["fallback_topic"]),
            },
            "title": r["title"],
            "rationale": r["rationale"],
        })
    return rules
