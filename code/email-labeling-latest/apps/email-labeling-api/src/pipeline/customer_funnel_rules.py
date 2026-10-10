from __future__ import annotations


CUSTOMER_FUNNEL_RULE_VERSION = "customer_funnel_v2026_07_03_p0_review_tightening"

FUNNEL_STAGES = ("L1", "L2", "L3", "L4", "L5")
STAGE_NAMES = {
    "L1": "营销触达",
    "L2": "首次回复",
    "L3": "询盘深化",
    "L4": "送样",
    "L5": "正式成交",
}
STAGE_RANK = {stage: index + 1 for index, stage in enumerate(FUNNEL_STAGES)}

SYSTEM_NOISE_ACTIONS = {
    "auto_reply",
    "bounce",
    "empty",
}

L1_MARKETING_ACTIONS = {
    "product_outreach",
    "outreach_exhibition",
    "visit_followup",
    "distributor_proposal",
}
L1_MARKETING_LABELS = {
    "cold_start_development",
    "customer_recall",
    "new_product_promotion",
}

L2_REPLY_ACTIONS = {
    "first_reply",
}
L2_REPLY_LABELS = {
    "first_contact",
}

L3_INQUIRY_ACTIONS = {
    "price_inquiry",
    "requirement_clarification",
    "document_request",
    "spec_discussion",
    "product_detail_inquiry",
    "distributor_inquiry",
    "price_negotiation",
    "sample_request",
    "sample_confirmation",
    "sample_feedback",
    "offline_invitation",
    "offline_visit",
    "online_meeting",
    "nda_contract",
}
L3_INQUIRY_LABELS = {
    "biz_product_detail_inquiry",
    "biz_distributor_inquiry",
    "biz_price_inquiry",
    "biz_price_bargaining",
    "biz_requirement_clarification",
    "biz_spec_discussion",
    "biz_document_request",
    "biz_nda_contract",
    "biz_sample_request",
    "biz_sample_confirmation",
    "biz_offline_invitation",
    "biz_onsite_sample_viewing",
    "biz_online_meeting",
    "biz_sample_feedback",
}

L4_EXECUTED_SAMPLE_ACTIONS = {
    "sample_shipment",
}
L4_LOGISTICS_ACTIONS = {
    "shipment_notice",
    "logistics_tracking",
}
L4_LOGISTICS_LABELS = {
    "biz_shipment_notice",
    "biz_logistics_tracking",
}
L4_SAMPLE_TEXT_MARKERS = {
    "sample",
    "samples",
    "样品",
}
L4_EXECUTED_TEXT_MARKERS = {
    "shipped",
    "ship out",
    "sent by",
    "tracking",
    "dhl",
    "ups",
    "fedex",
    "tnt",
    "寄出",
    "发出",
    "运单",
    "物流",
}

L5_FORMAL_ORDER_ACTIONS = {
    "order_confirmation",
    "order_payment",
}
L5_FORMAL_ORDER_LABELS = {
    "biz_order_confirmation",
    "biz_order_payment",
}
L5_EXCLUDED_SAMPLE_ACTIONS = {
    "sample_pi_issued",
    "sample_payment",
}
L5_SAMPLE_TEXT_MARKERS = {
    "sample pi",
    "sample payment",
    "sample order",
    "样品pi",
    "样品付款",
    "样品订单",
}
