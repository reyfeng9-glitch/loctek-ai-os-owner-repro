"""邮件打标分类法 —— 主题 / 动作 / 事实 的【单一真相源】+ 关联关系。

为什么有这个文件
================
此前主题、动作、事实分散在多个文件、各自又是 4~5 个"按字符串并联的字典"
（DISPLAY / PRIORITY / NORMALIZE / TOPIC_MAP / FACT_TO_*），加一个标签要改很多处、
极易漏配且没有完整性校验；主题与动作/事实之间的关联也是隐式、跨文件的。

本文件把三者合并为**声明式注册表**：每个主题/动作/事实只声明一次，
其余所有并联字典与关联关系都从这里**自动派生**，并在 import 期做
完整性 + 一致性断言（漏配/非法直接报错，而不是静默出错）。

如何扩展
========
- 加主题：在 TOPICS 追加一个 TopicSpec（code/display/priority/aliases）。
- 加动作：在 ACTIONS 追加一个 ActionSpec，必须指定 topic（父主题，须是合法 TopicSpec.code）。
- 加事实：在 FACTS 追加一个 FactSpec，可选 topic/action 表示"命中该事实应确保存在的主题/动作"。
- 别名 aliases：LLM 可能输出的各种变体（含中文/同义词）；归一化用，必须全局唯一。
- 改完跑：tests/email_labeling_taxonomy_spec.py（含与 CurrentEmailFacts 字段的一致性校验）。

本模块只依赖标准库（不引 pydantic），保证可独立导入与离线测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TopicSpec:
    code: str
    display: str
    priority: int          # 派生 primary_topic 的优先级（越大越优先）
    aliases: tuple = ()     # LLM 输出变体 → 归一化到 code


@dataclass(frozen=True)
class ActionSpec:
    code: str
    display: str
    topic: str             # 父主题（必须是某 TopicSpec.code）—— 主题↔动作的关联
    priority: int
    aliases: tuple = ()


@dataclass(frozen=True)
class FactSpec:
    code: str              # 须与 schemas.CurrentEmailFacts 的字段同名
    desc: str              # prompt 中对该事实的描述
    topic: str | None = None    # 命中该事实应确保存在的主题（关联）
    action: str | None = None   # 命中该事实应确保存在的动作（关联）


# ─────────────────────────── 主题 ───────────────────────────
TOPICS: list[TopicSpec] = [
    TopicSpec('system_noise', '系统/无效邮件', 100, ('auto_reply', 'bounce', 'undeliverable', 'automatic_reply')),
    TopicSpec('no_action_reply', '无实质业务回复', 30, ('no_reply', 'short_reply', 'not_actionable')),
    TopicSpec('marketing_promotion', '营销/产品推广', 40, ('promotion', 'marketing', 'cold_email', 'new_product')),
    TopicSpec('follow_up', '跟进/唤醒', 35, ('follow_up_reminder', '催回复', 'wake_up')),
    TopicSpec('meeting_arrangement', '会议/拜访安排', 45, ('meeting', 'visit', 'online_meeting', 'offline_meeting', 'warehouse_visit', 'exhibition')),
    TopicSpec('requirement_discussion', '需求/规格沟通', 50, ('spec', 'specification', 'product_detail', 'tech_discussion')),
    TopicSpec('quotation', '报价/价格方案', 75, ('quote', 'pricing', 'send_quote', '报价', 'quotation_sent', 'price_quote')),
    TopicSpec('price_negotiation', '价格异议/谈判', 70, ()),
    TopicSpec('sample_preparation', '样品申请/备样', 55, ('sample', 'sample_request', 'sample_confirm')),
    TopicSpec('order_payment_confirmation', '订单/付款确认', 85, ('payment', 'payment_confirmation', 'order', 'order_confirmation', 'pi_po')),
    TopicSpec('order_detail_confirmation', '订单资料/配置确认', 65, ('manual',)),
    TopicSpec('fulfillment_logistics', '生产/发货/物流履约', 80, ('fulfillment', 'logistics', 'delivery', 'shipment', 'tracking')),
    TopicSpec('post_delivery_support', '交付后支持', 60, ('after_sales', 'after_sale', '售后', 'after_sales_service')),
    TopicSpec('quality_issue', '质量异常/投诉', 95, ('quality', 'complaint', 'defect')),
    TopicSpec('cancellation_pause', '取消/暂停/终止', 90, ('cancel', 'cancellation')),
    # ── 新增（2026-06-02 校验报告建议）──
    TopicSpec('partnership_distribution', '经销/代理合作', 68,
              ('distributor', 'dealer', 'reseller', 'distribution', 'agency', 'wholesale', 'sole_agent', '经销', '代理', '分销')),
]

# ─────────────────────────── 动作 ───────────────────────────
ACTIONS: list[ActionSpec] = [
    # 中性化(批1):冷启动/新品/老客户召回 三个"上下文型"动作合并为中性「产品推介」。
    # 冷/新/召回 的区别交给业务标签层(冷启动开发/新品推介/老客户召回)。旧 code 全列为别名,老数据自动归一。
    ActionSpec('product_outreach', '产品推介', 'marketing_promotion', 40,
               ('outreach_new_product', 'new_product', 'new_product_recommendation', 'new_arrival', '新品推荐',
                'outreach_cold_email', 'cold_email', 'cold_outreach',
                'customer_recall', 'wake_up', 'recall_interest', '老客户唤醒',
                'product_intro', 'product_promotion', '产品推介', '产品介绍')),
    ActionSpec('outreach_exhibition', '展会邀约', 'meeting_arrangement', 40, ('exhibition_invite', 'exhibition', 'trade_show')),
    ActionSpec('follow_up', '业务跟进', 'follow_up', 28, ('followup', 'following_up', '跟进')),
    ActionSpec('follow_up_reminder', '催回复', 'follow_up', 30, ('reminder', '催回复')),
    ActionSpec('sales_handover', '业务交接', 'follow_up', 24, ('handover', 'taking_over', '业务交接')),
    ActionSpec('holiday_greeting', '节日问候', 'follow_up', 21,
               ('holiday', 'season_greeting', 'seasons_greetings', 'christmas', 'xmas',
                'new_year', 'happy_new_year', 'festive', 'merry', '节日问候', '节日祝福')),
    ActionSpec('first_reply', '首次回复', 'follow_up', 26, ('first_reply', '首次回复')),
    ActionSpec('price_inquiry', '询价', 'requirement_discussion', 50, ('inquiry', 'price_inquiry', 'ask_price', '询价', 'quote_me', 'send_price')),
    ActionSpec('requirement_clarification', '需求确认', 'requirement_discussion', 53, ('requirement', 'requirement_clarification', '需求确认')),
    ActionSpec('document_request', '文件请求', 'requirement_discussion', 45, ('document_request', 'request_doc', '文件请求')),
    ActionSpec('spec_discussion', '规格沟通', 'requirement_discussion', 55, ('spec', 'specification', '规格')),
    ActionSpec('product_detail_inquiry', '产品细节咨询', 'requirement_discussion', 48, ('product_detail', 'product_inquiry', '产品细节')),
    ActionSpec('send_quotation', '发送报价', 'quotation', 72, ('send_quote', 'send_quotation', '报价', 'quotation_sent', 'price_quote', 'pricing', 'price')),
    ActionSpec('price_negotiation', '价格谈判', 'price_negotiation', 70, ('price_objection', 'price_negotiation', 'nego', '价格谈判')),
    ActionSpec('quotation_followup', '报价跟进', 'follow_up', 20, ('quotation_followup', 'quote_followup', '报价跟进')),
    ActionSpec('sample_request', '样品请求', 'sample_preparation', 35, ('sample', 'sample_request', '样品')),
    ActionSpec('sample_confirmation', '样品确认', 'sample_preparation', 34, ('sample_confirmed', 'sample_confirm', '样品确认')),
    ActionSpec('sample_feedback', '样品反馈', 'sample_preparation', 32, ('sample_feedback', '样品反馈')),
    ActionSpec('sample_shipment', '样品发货', 'sample_preparation', 68, ('sample_shipped', '样品发货')),
    ActionSpec('sample_pi_issued', '样品PI开具', 'order_payment_confirmation', 74, ('sample_pi', '样品PI')),
    ActionSpec('sample_payment', '样品付款', 'order_payment_confirmation', 74, ('sample_pi_payment', '样品付款')),
    ActionSpec('offline_invitation', '线下邀请', 'meeting_arrangement', 60, ('invite_visit', 'offline_invitation', '线下邀请')),
    ActionSpec('offline_visit', '线下拜访', 'meeting_arrangement', 60, ('visit', 'offline_visit', 'warehouse_visit', 'factory_visit', '线下拜访')),
    ActionSpec('online_meeting', '线上会议', 'meeting_arrangement', 36, ('meeting', 'online_meeting', 'video_call', 'zoom', 'teams', '线上会议')),
    ActionSpec('exhibition_visit', '展会拜访', 'meeting_arrangement', 58, ()),
    ActionSpec('order_confirmation', '确认订单', 'order_payment_confirmation', 76, ('order', 'order_confirmed', 'confirm_order', '确认订单', 'pi_po', 'formal_order')),
    ActionSpec('order_detail_confirmation', '订单资料确认', 'order_detail_confirmation', 75, ('order_detail', '订单资料')),
    ActionSpec('order_payment', '确认付款', 'order_payment_confirmation', 85, ('payment', 'payment_confirmed', 'payment_confirmation', '确认付款')),
    ActionSpec('order_cancellation', '取消订单', 'cancellation_pause', 90, ('cancel', 'cancellation', '取消订单')),
    ActionSpec('order_post_doc', '订单后资料确认', 'order_detail_confirmation', 52, ('order_post_doc', 'post_order_doc', 'manual_doc', 'document_post_doc')),
    ActionSpec('delivery_followup', '催促交期', 'fulfillment_logistics', 78, ('delivery', 'delivery_followup', '催促交期')),
    ActionSpec('shipment_notice', '发货通知', 'fulfillment_logistics', 82, ('shipment', 'shipment_notice', '发货通知')),
    ActionSpec('logistics_tracking', '物流跟踪', 'fulfillment_logistics', 80, ('tracking', '物流')),
    ActionSpec('shipment_delay', '交期延迟通知', 'fulfillment_logistics', 78, ('delay', 'shipment_delay', '交期延迟')),
    ActionSpec('after_sales_support', '售后支持', 'post_delivery_support', 65, ('售后支持', 'after_sales_service')),
    ActionSpec('after_sales_inquiry', '售后咨询', 'post_delivery_support', 65, ('售后', 'after_sales', '售后咨询')),
    ActionSpec('quality_complaint', '质量投诉', 'quality_issue', 95, ('quality', 'complaint', 'defect', 'quality_issue', '质量投诉')),
    ActionSpec('repurchase_inquiry', '复购咨询', 'post_delivery_support', 63, ('repurchase', '复购')),
    # ── 新增(2026-06:对齐业务侧《外销业务邮件标签库》B 段,补缺的动作)──
    ActionSpec('out_of_stock_notice', '缺货/无法供货告知', 'fulfillment_logistics', 79,
               ('out_of_stock', 'cannot_supply', 'no_stock', 'unable_to_supply', '缺货', '无法供货', '无货', '不能加柜')),
    ActionSpec('damage_shortage_report', '缺件损坏反馈', 'quality_issue', 93,
               ('damage', 'shortage', 'missing_parts', 'broken', 'damaged', '缺件', '损坏', '漏发')),
    ActionSpec('return_exchange', '退换货处理', 'quality_issue', 92,
               ('return', 'exchange', 'rma', 'replacement', '退货', '换货', '退换')),
    ActionSpec('pause_hold', '暂停搁置', 'cancellation_pause', 88,
               ('pause', 'hold', 'on_hold', 'shelve', '暂停', '搁置')),
    ActionSpec('terminate_cooperation', '终止合作', 'cancellation_pause', 89,
               ('terminate', 'end_cooperation', 'end_partnership', '终止合作', '终止')),
    ActionSpec('visit_followup', '展会/拜访跟进', 'meeting_arrangement', 57,
               ('visit_followup', 'exhibition_followup', 'post_visit_followup', '展会跟进', '拜访跟进', '展后跟进')),
    ActionSpec('auto_reply', '自动回复', 'system_noise', 100, ('auto_reply', 'automatic_reply', 'out_of_office', '自动回复')),
    ActionSpec('bounce', '退信', 'system_noise', 100, ('bounce', 'undeliverable', '退信')),
    ActionSpec('empty', '空邮件', 'system_noise', 100, ('empty', '空邮件')),
    ActionSpec('short_reply', '短回复无实质业务', 'no_action_reply', 10, ('short_reply', 'no_action_reply', 'not_actionable')),
    ActionSpec('other', '其他', 'no_action_reply', 5, ('其他', 'other')),
    # ── 新增（2026-06-02 校验报告建议）──
    ActionSpec('distributor_inquiry', '经销咨询', 'partnership_distribution', 52,
               ('distributor_inquiry', 'reseller_price', 'distribution_price', 'dealer_price', 'become_dealer', 'become_distributor', '经销咨询', '代理咨询')),
    ActionSpec('distributor_proposal', '经销推介', 'partnership_distribution', 50,
               ('distributor_proposal', 'brand_dealer', 'dealer_program', 'dealership_offer', '经销推介')),
    ActionSpec('price_increase_notice', '涨价通知', 'quotation', 71,
               ('price_increase', 'price_increase_notice', 'price_adjustment', 'raise_price', 'price_change', '涨价', '涨价通知')),
    ActionSpec('nda_contract', 'NDA/合同签署', 'requirement_discussion', 47,
               ('nda', 'non_disclosure', 'nda_agreement', 'sign_agreement', 'contract', 'agreement', '保密协议', '合同')),
]

# ─────────────────────────── 事实 ───────────────────────────
# code 必须与 schemas.CurrentEmailFacts 的字段一一对应（由测试断言）。
FACTS: list[FactSpec] = [
    FactSpec('order_confirmed_in_email', '确认下单/PO/PI', 'order_payment_confirmation', 'order_confirmation'),
    FactSpec('payment_confirmed_in_email', '确认已付款', 'order_payment_confirmation', 'order_payment'),
    FactSpec('shipment_mentioned_in_email', '提到发货/tracking', None, None),
    FactSpec('delivery_received_in_email', '已收货/已交付', 'fulfillment_logistics', None),
    FactSpec('sample_mentioned_in_email', '提到样品', None, None),
    FactSpec('sample_pi_in_email', '样品PI/样品付款安排', 'order_payment_confirmation', 'sample_pi_issued'),
    FactSpec('tracking_number_in_email', '具体运单号/tracking number', 'fulfillment_logistics', None),
    FactSpec('order_detail_confirmed_in_email', '订单资料确认(manual/CDR/packaging)', 'order_detail_confirmation', 'order_detail_confirmation'),
    FactSpec('document_requested_in_email', '索取文件(manual/certificate/drawing)', None, None),
    FactSpec('document_provided_in_email', '提供文件', None, None),
    FactSpec('quality_complaint_in_email', '质量投诉', 'quality_issue', 'quality_complaint'),
    FactSpec('price_quoted_in_email', '报价含具体金额', 'quotation', 'send_quotation'),
    FactSpec('price_inquiry_in_email', '客户问价', 'requirement_discussion', 'price_inquiry'),
    FactSpec('price_objection_in_email', '价格异议/嫌贵/压价', 'price_negotiation', 'price_negotiation'),
    FactSpec('promotes_new_product_in_email', '推介中明确是新品/最新款(new/latest product)', None, None),
    FactSpec('onsite_visit_in_email', '线下拜访(warehouse/factory visit)', 'meeting_arrangement', None),
    FactSpec('online_meeting_in_email', '线上会议(teams/zoom/video call)', 'meeting_arrangement', None),
]


# ─────────────────── 派生：并联字典（保持向后兼容）───────────────────
TOPIC_CODES: list[str] = [t.code for t in TOPICS]
ACTION_CODES: list[str] = [a.code for a in ACTIONS]
FACT_CODES: list[str] = [f.code for f in FACTS]

TOPIC_DISPLAY: dict[str, str] = {t.code: t.display for t in TOPICS}
TOPIC_PRIORITY: dict[str, int] = {t.code: t.priority for t in TOPICS}
ACTION_DISPLAY: dict[str, str] = {a.code: a.display for a in ACTIONS}
ACTION_TOPIC: dict[str, str] = {a.code: a.topic for a in ACTIONS}
ACTION_PRIORITY: dict[str, int] = {a.code: a.priority for a in ACTIONS}
FACT_DESC: dict[str, str] = {f.code: f.desc for f in FACTS}
FACT_TO_TOPIC: dict[str, str] = {f.code: f.topic for f in FACTS if f.topic}
FACT_TO_ACTION: dict[str, str] = {f.code: f.action for f in FACTS if f.action}


def _build_normalize(specs) -> dict[str, str]:
    """别名 → code；同时把 code 自身也登记为别名（恒等），并检测冲突。"""
    out: dict[str, str] = {}
    for s in specs:
        for alias in (s.code, *s.aliases):
            key = alias.strip().lower().replace("-", "_")
            if key in out and out[key] != s.code:
                raise ValueError(
                    f"别名冲突: {alias!r} 同时映射到 {out[key]!r} 和 {s.code!r}"
                )
            out[key] = s.code
    return out


TOPIC_NORMALIZE: dict[str, str] = _build_normalize(TOPICS)
ACTION_NORMALIZE: dict[str, str] = _build_normalize(ACTIONS)


# ─────────────────── 关联关系（正向查询）───────────────────
def actions_of_topic(topic_code: str) -> list[str]:
    """某主题下挂的所有动作。"""
    return [a.code for a in ACTIONS if a.topic == topic_code]


def facts_of_topic(topic_code: str) -> list[str]:
    """命中后应确保该主题存在的所有事实。"""
    return [f.code for f in FACTS if f.topic == topic_code]


def topic_of_action(action_code: str) -> str | None:
    return ACTION_TOPIC.get(action_code)


# ─────────────────── import 期完整性 + 一致性断言 ───────────────────
def _validate() -> None:
    topic_set = set(TOPIC_CODES)
    action_set = set(ACTION_CODES)
    # 1. code 唯一
    for label, codes in (("topic", TOPIC_CODES), ("action", ACTION_CODES), ("fact", FACT_CODES)):
        dup = {c for c in codes if codes.count(c) > 1}
        if dup:
            raise ValueError(f"{label} code 重复: {sorted(dup)}")
    # 2. 每个动作的父主题合法
    bad = {a.code: a.topic for a in ACTIONS if a.topic not in topic_set}
    if bad:
        raise ValueError(f"动作的父主题非法（不在 TOPICS 中）: {bad}")
    # 3. 事实关联的 topic/action 合法
    for f in FACTS:
        if f.topic is not None and f.topic not in topic_set:
            raise ValueError(f"事实 {f.code} 关联的 topic 非法: {f.topic}")
        if f.action is not None and f.action not in action_set:
            raise ValueError(f"事实 {f.code} 关联的 action 非法: {f.action}")
    # 4. 每个主题至少有一个动作挂靠（保证 ACTION_PRIORITY 兜底能派生到它）
    topics_with_action = {a.topic for a in ACTIONS}
    orphan = topic_set - topics_with_action
    if orphan:
        raise ValueError(f"以下主题没有任何动作挂靠: {sorted(orphan)}")


_validate()
