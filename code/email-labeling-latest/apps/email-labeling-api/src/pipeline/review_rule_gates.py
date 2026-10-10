from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


_WORD_RE = re.compile(r"[a-z0-9]+", re.I)
_CURRENT_MESSAGE_SPLIT_RE = re.compile(
    r"(?im)^\s*(?:from|van|de|sent|verzonden|aan|to|cc|subject|onderwerp)\s*:",
)
_EXPLICIT_MEETING_TIME_RE = re.compile(
    r"\b(?:at\s*)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b|"
    r"\b(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+\d{1,2}(?:st|nd|rd|th)?\b",
    re.I,
)
_SCHEDULED_CALL_RE = re.compile(
    r"\bset\s+up\s+a\s+call\b.{0,80}\bby\s+(?:monday|tuesday|wednesday|thursday|friday)\b|"
    r"\bby\s+(?:monday|tuesday|wednesday|thursday|friday)\b.{0,80}\bset\s+up\s+a\s+call\b",
    re.I,
)
_ACTIVE_ONLINE_MEETING_RE = re.compile(
    r"\b(?:computer|laptop|zoom|teams|skype|meeting|call)\b.{0,80}\b(?:connecting|join(?:ing)?\s+in|join\b)|"
    r"\b(?:connecting|join(?:ing)?\s+in|join\b).{0,80}\b(?:computer|laptop|zoom|teams|skype|meeting|call)\b",
    re.I,
)
_BUSINESS_VISIT_WITH_INVITATION_LETTER_RE = re.compile(
    r"\b(?:schengen\s+visa|apply\s+(?:the\s+)?visa|invitation\s+letter)\b.{0,160}\b(?:visit|meeting|available|business\s+trip)\b|"
    r"\b(?:visit|meeting|available|business\s+trip)\b.{0,160}\b(?:schengen\s+visa|apply\s+(?:the\s+)?visa|invitation\s+letter)\b",
    re.I,
)
_WAREHOUSE_VISIT_PLAN_RE = re.compile(
    r"\b(?:come\s+that\s+way|come|visit|coming)\b[\s\S]{0,220}\b(?:warehouse|products?)\b|"
    r"\b(?:warehouse|products?)\b[\s\S]{0,220}\b(?:come\s+that\s+way|come|visit|coming)\b",
    re.I,
)
_SHIP_EARLIER_RE = re.compile(r"\bship\b.{0,40}\bearlier\b", re.I)
_SHIP_OUT_RE = re.compile(r"\b(?:ship|send(?:ing)?)\s+(?:out\s+)?(?:it|the\s+\w+|\w+)\b", re.I)
_FULFILLMENT_STATUS_RE = re.compile(
    r"\bstatus\s+of\s+(?:the\s+)?(?:samples?|containers?|orders?|shipments?)(?:\s+and\s+(?:samples?|containers?|orders?|shipments?))*\b",
    re.I,
)
_DELIVERY_DEADLINE_RE = re.compile(
    r"\b(?:need|needs|needed)\b.{0,40}\b(?:products?|goods?|items?|frames?|samples?)\b.{0,40}\b(?:maximum|by|before)\b|"
    r"\b(?:need|needs|needed)\b.{0,40}\b(?:products?|goods?|items?|frames?|samples?)\b.{0,40}(?:a\.s\.a\.p\.|asap)(?=\W|$)|"
    r"\bhand\s+it\s+over\b.{0,80}\bby\b",
    re.I,
)
_FASTEST_DELIVERY_RE = re.compile(r"\bfastest\s+possible\s+delivery\b", re.I)
_STOCK_PUSH_RE = re.compile(r"\benough\s+(?:frames?|stock|inventory)\s+for\s+around\s+\d+\s+days\b.{0,80}\bpush\b", re.I)
_CLEAN_DELIVERY_TIME_REQUEST_RE = re.compile(
    r"\bwhen\b.{0,80}\b(?:delivery|deliver(?:ed|y)?|ship(?:ped|ment|ping)?|send(?:ing)?|arriv(?:e|al))\b|"
    r"\b(?:delivery|deliver(?:ed|y)?|ship(?:ped|ment|ping)?|send(?:ing)?|arriv(?:e|al))\b.{0,80}\b(?:when|within|earlier|as soon as possible|a\\.s\\.a\\.p\\.|asap|maximum)\b|"
    r"\b(?:can|could)\s+you\b.{0,80}\b(?:deliver|ship|send)\b.{0,80}\b(?:within|earlier|as soon as possible|a\\.s\\.a\\.p\\.|asap)\b",
    re.I,
)
_DELIVERY_TIMING_QUESTION_RE = re.compile(
    r"\bstill\s+on\s+time\b|"
    r"\b(?:when|what\s+date)\b.{0,100}\b(?:delivered?|delivery|ship(?:ped|ment|ping)?|leave|arrive|arriving)\b|"
    r"\b(?:delivered?|delivery|ship(?:ped|ment|ping)?|leave|arrive|arriving)\b.{0,100}\b(?:when|what\s+date|on\s+time|to\s+our\s+warehouse)\b|"
    r"\b(?:container|shipment|order)\b.{0,80}\b(?:leave|leaves|leaving)\b.{0,40}\b(?:on|before)\b",
    re.I,
)
_NEXT_ORDER_CONTAINER_LEAVE_DEADLINE_RE = re.compile(
    r"\bnext\s+order\b[\s\S]{0,800}\b(?:container|shipment)\b.{0,80}\b(?:need|needs|must|should)\s+to\s+leave\b.{0,80}\bbefore\b",
    re.I,
)

_STRONG_LOGISTICS_MARKERS = {
    "shipment",
    "shipping",
    "shipped",
    "tracking",
    "customs",
    "clearance",
    "eta",
    "etd",
    "bill of lading",
    "vessel",
    "freight",
    "forwarder",
    "dhl",
    "fedex",
    "ups",
    "tnt",
    "air waybill",
    "awb",
    "运单",
    "清关",
    "发货",
    "物流",
}
_DELIVERY_ONLY_MARKERS = {
    "delivery",
    "deliver",
    "delivered",
    "warehouse",
    "交付",
    "送达",
}
_ORDER_DISCUSSION_MARKERS = {
    "quantity",
    "quantities",
    "qty",
    "price",
    "quotation",
    "quote",
    "specification",
    "spec",
    "model",
    "sku",
    "pi",
    "next order",
    "pcs",
    "black",
    "white",
    "silver",
    "aluminium",
    "数量",
    "价格",
    "规格",
    "型号",
    "订单",
}
_PARTNERSHIP_MARKERS = {
    "exclusive",
    "exclusivity",
    "agent",
    "agency",
    "distributor",
    "distribution",
    "dealer",
    "reseller",
    "territory",
    "partnership",
    "sole agent",
    "cooperation",
    "经销",
    "代理",
    "分销",
    "独家",
    "区域",
}
_STRATEGIC_PARTNERSHIP_INTENT_MARKERS = {
    "sole agent",
    "authorized distributor",
    "distributor",
    "distribut",
    "dealer",
    "reseller",
    "brand dealer",
    "dealer business",
    "distributor policy",
    "distributor agreement",
    "sell the brand",
    "sell flexispot",
    "sell in name of",
    "use and sell",
    "brand name",
    "经销",
    "代理",
    "分销",
    "独家",
}
_EXISTING_PARTNER_CONTEXT_MARKERS = {
    "we had exclusive seller",
    "we have exclusive seller",
    "we had an exclusive seller",
    "we have an exclusive seller",
    "we already have exclusive seller",
    "we already have an exclusive seller",
    "now we had exclusive seller",
    "now we have exclusive seller",
    "已有独家",
    "已有代理",
    "已有经销",
}
_STOCK_UPFRONT_RE = re.compile(r"\bbuy\b.{0,40}\bstocks?\b.{0,40}\bup\s*front\b", re.I)
_RESELLER_PRICE_MARGIN_RE = re.compile(
    r"\b(?:retailer|dealer|reseller)\s+price\b.{0,120}\bmargin\b|"
    r"\bmargin\b.{0,120}\b(?:retailer|dealer|reseller)\s+price\b",
    re.I,
)
_CUSTOMER_EXCLUSIVITY_REQUEST_RE = re.compile(
    r"\b(?:need|want|request|ask(?:ing)?\s+for)\b.{0,80}\bexclusiv(?:e|ity)\b|"
    r"\bexclusiv(?:e|ity)\b.{0,100}\b(?:can\s+you\s+give|give\s+this|if\s+you\s+can\s+adjust|buy\s+this\s+product|no\s+problem\s+for\s+us)\b|"
    r"\b(?:territory|region|country)\b.{0,80}\bexclusiv(?:e|ity)\b",
    re.I,
)
_AFTER_SALES_ISSUE_MARKERS = {
    "issue",
    "problem",
    "complaint",
    "return",
    "defect",
    "repair",
    "damage",
    "damaged",
    "broken",
    "missing",
    "shortage",
    "replacement",
    "made a mistake",
    "do made a mistake",
    "recover my mistake",
    "sorry about my mistake",
    "lack of",
    "not what we received",
    "wrong color",
    "not working",
    "not good",
    "not match",
    "can't be used",
    "cannot be used",
    "can not be used",
    "need asap the shorter",
    "need the shorter",
    "need shorter",
    "not the same as in the cataloque",
    "not the same as in the catalogue",
    "not the same as the others",
    "not exactly",
    "cannot lower",
    "can't lower",
    "cannot work",
    "too short",
    "too complicated",
    "difficulties",
    "droppes down",
    "drops down",
    "does not visually fit",
    "do not accept",
    "doesn’t accept",
    "doesn't accept",
    "warranty",
    "malfunction",
    "投诉",
    "问题",
    "退货",
    "换货",
    "维修",
    "损坏",
    "缺件",
}
_RECEIVED_MARKERS = {
    "received",
    "got",
    "arrived",
    "delivered",
    "收到",
    "已收",
}
_THANKS_MARKERS = {
    "thanks",
    "thank you",
    "thx",
    "best",
    "谢谢",
    "感谢",
}
_MEETING_LOGISTICS_MARKERS = {
    "schedule",
    "arrange",
    "appointment",
    "agenda",
    "meeting agenda",
    "meeting time",
    "invitation schedule",
    "business visit",
    "business trip",
    "plan this",
    "date",
    "time",
    "next monday",
    "next tuesday",
    "next wednesday",
    "next thursday",
    "next friday",
    "zoom",
    "teams",
    "skype",
    "video call",
    "video meeting",
    "videocall",
    "meeting link",
    "in 30 minutes",
    "in one hour",
    "speak to you",
    "in person",
    "address",
    "adresse",
    "booth",
    "hall",
    "fair",
    "exhibit",
    "exhibition",
    "orgatec",
    "visit you",
    "visit us",
    "visit our booth",
    "will visit",
    "be visiting",
    "meet with you",
    "meet there",
    "see you",
    "happy to meet",
    "come to",
    "factory visit",
    "warehouse visit",
    "visit the warehouse",
    "visit our warehouse",
    "安排",
    "日程",
    "议程",
    "时间",
    "地点",
    "会议",
    "拜访",
}
_PAST_MEETING_REFERENCE_MARKERS = {
    "when you visited",
    "thanks for the meeting yesterday",
    "meeting yesterday",
    "we talked about",
    "visited us in",
}
_SAMPLE_CONTEXT_MARKERS = {"sample", "samples", "样品"}
_SAMPLE_ORDER_SIGNAL_MARKERS = {
    "payment",
    "payments",
    "paid",
    "transfer",
    "pi",
    "order",
    "付款",
    "支付",
    "订单",
}


def text_context(*parts: Any) -> str:
    values: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            values.append(part)
            continue
        if isinstance(part, Mapping):
            values.extend(str(value) for value in part.values() if value is not None)
            continue
        if isinstance(part, Iterable):
            values.extend(str(value) for value in part if value is not None)
            continue
        values.append(str(part))
    return "\n".join(values)


def _lower(text: str) -> str:
    return text.lower()


def current_message_text(text: str) -> str:
    match = _CURRENT_MESSAGE_SPLIT_RE.search(text)
    return text[:match.start()].strip() if match else text


def _contains_any(text: str, markers: set[str]) -> bool:
    lowered = _lower(text)
    return any(marker in lowered for marker in markers)


def _contains_order_discussion_marker(text: str) -> bool:
    lowered = _lower(text)
    for marker in _ORDER_DISCUSSION_MARKERS:
        if re.search(r"[\u4e00-\u9fff]", marker):
            if marker in lowered:
                return True
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])"
        if re.search(pattern, lowered):
            return True
    return False


def has_order_quantity_list(text: str) -> bool:
    lowered = _lower(text)
    quantity_lines = re.findall(r"(?im)^\s*(?:[-*]\s*)?.{0,80}?\b\d+\s*x\b", text)
    qty_units = re.findall(r"\b\d+\s*(?:pcs|pieces|sets|cartons|containers)\b", lowered)
    return len(quantity_lines) >= 2 or len(qty_units) >= 2 or (
        "next order" in lowered and bool(re.search(r"\b\d+\s*x\b", lowered))
    )


def has_multiple_product_codes(text: str) -> bool:
    return len(set(re.findall(r"\b[A-Z]{1,4}\d{2,}[A-Z0-9]*(?:\([A-Z0-9]+\))?(?:-[A-Z0-9]+)*\b", text))) >= 2


def has_after_sales_issue(text: str) -> bool:
    return _contains_any(current_message_text(text), _AFTER_SALES_ISSUE_MARKERS)


def is_delivery_confirmation_without_issue(text: str) -> bool:
    current_text = current_message_text(text)
    return (
        _contains_any(current_text, _RECEIVED_MARKERS)
        and _contains_any(current_text, _THANKS_MARKERS)
        and not has_after_sales_issue(current_text)
    )


def is_mixed_order_list_shipping_request(text: str) -> bool:
    current_text = current_message_text(text)
    return (
        _contains_any(current_text, {"provide information about our order", "push ingenor"})
        and _contains_any(current_text, {"with these products", "next order"})
        and (
            has_order_quantity_list(current_text)
            or has_multiple_product_codes(current_text)
        )
    )


def is_fulfillment_logistics_confirmable(*, action: str, evidence: str, context_text: str) -> bool:
    if action == "document_request" and _contains_any(evidence, {"pi", "quotation", "quote"}):
        return False
    evidence_text = current_message_text(evidence or "")
    current_context = current_message_text(context_text or "")
    combined = text_context(evidence_text, current_context)
    if (
        _CLEAN_DELIVERY_TIME_REQUEST_RE.search(evidence_text)
        and not has_order_quantity_list(evidence_text)
        and not has_multiple_product_codes(evidence_text)
    ):
        return True
    if _SHIP_EARLIER_RE.search(evidence_text):
        return True
    if _SHIP_OUT_RE.search(evidence_text):
        return True
    if _FULFILLMENT_STATUS_RE.search(evidence_text):
        return True
    if _DELIVERY_DEADLINE_RE.search(evidence_text):
        return True
    if _FASTEST_DELIVERY_RE.search(evidence_text):
        return True
    if _STOCK_PUSH_RE.search(evidence_text):
        return True
    if _DELIVERY_TIMING_QUESTION_RE.search(evidence_text) and not is_mixed_order_list_shipping_request(combined):
        return True
    if _NEXT_ORDER_CONTAINER_LEAVE_DEADLINE_RE.search(combined):
        return True
    if _contains_any(evidence_text, _STRONG_LOGISTICS_MARKERS):
        return True
    if _contains_any(combined, _STRONG_LOGISTICS_MARKERS) and not has_order_quantity_list(combined):
        return True
    if _contains_any(evidence_text, _DELIVERY_ONLY_MARKERS):
        return not (
            _contains_order_discussion_marker(combined)
            or has_order_quantity_list(combined)
        )
    return False


def is_partnership_distribution_confirmable(*, evidence: str, context_text: str) -> bool:
    evidence_text = current_message_text(evidence or "")
    combined = text_context(evidence_text, context_text)
    has_partnership_marker = _contains_any(combined, _PARTNERSHIP_MARKERS)
    has_strategic_intent_in_evidence = _contains_any(evidence_text, _STRATEGIC_PARTNERSHIP_INTENT_MARKERS)
    has_distribution_economics = (
        (
            _STOCK_UPFRONT_RE.search(evidence_text) is not None
            and _contains_any(combined, {"margin", "own brand", "retailer price", "dealer price", "reseller price"})
        )
        or _RESELLER_PRICE_MARGIN_RE.search(evidence_text) is not None
        or (
            _contains_any(evidence_text, {"stock in small quantities", "suggested retailer price"})
            and _contains_any(evidence_text, {"margin", "final say on retailer price"})
        )
    )
    if not has_partnership_marker and not has_strategic_intent_in_evidence and not has_distribution_economics:
        return False
    if _contains_any(evidence_text, _EXISTING_PARTNER_CONTEXT_MARKERS):
        return False
    if _CUSTOMER_EXCLUSIVITY_REQUEST_RE.search(evidence_text):
        return True
    if has_strategic_intent_in_evidence or has_distribution_economics:
        return True
    return not has_order_quantity_list(combined)


def is_meeting_arrangement_confirmable(*, evidence: str, context_text: str) -> bool:
    evidence_text = current_message_text(evidence or "")
    current_context = current_message_text(context_text or "")
    combined = text_context(evidence_text, current_context)
    context_has_explicit_schedule = (
        "invitation schedule" in _lower(current_context)
        or _EXPLICIT_MEETING_TIME_RE.search(current_context) is not None
    )
    if _contains_any(combined, _PAST_MEETING_REFERENCE_MARKERS) and not _contains_any(evidence_text, _MEETING_LOGISTICS_MARKERS):
        return False
    return (
        _contains_any(evidence_text, _MEETING_LOGISTICS_MARKERS)
        or _EXPLICIT_MEETING_TIME_RE.search(evidence_text) is not None
        or _SCHEDULED_CALL_RE.search(evidence_text) is not None
        or _ACTIVE_ONLINE_MEETING_RE.search(evidence_text) is not None
        or _BUSINESS_VISIT_WITH_INVITATION_LETTER_RE.search(combined) is not None
        or _WAREHOUSE_VISIT_PLAN_RE.search(combined) is not None
        or (
            _contains_any(evidence_text, {"invitation", "schedule"})
            and context_has_explicit_schedule
        )
        or (
            _contains_any(current_context, _MEETING_LOGISTICS_MARKERS)
            and _contains_any(evidence_text, {"visit", "meeting", "meet", "coming", "come", "in person", "see you"})
        )
    )


def is_l5_sample_payment_context(text: str) -> bool:
    lowered = _lower(text)
    has_sample = any(marker in lowered for marker in _SAMPLE_CONTEXT_MARKERS)
    has_order_signal = any(marker in lowered for marker in _SAMPLE_ORDER_SIGNAL_MARKERS)
    return has_sample and has_order_signal


def p0_missing_requirements(
    *,
    label_code: str,
    matched_entries: list[dict[str, str]],
    context: Mapping[str, Any],
) -> list[str]:
    email_context = text_context(context.get("email_subject"), context.get("email_text"))
    email_body_context = text_context(context.get("email_text"))
    missing: list[str] = []
    if label_code in {"biz_delivery_followup"}:
        if not any(
            is_fulfillment_logistics_confirmable(
                action=entry["action"],
                evidence=entry["evidence"],
                context_text=email_body_context,
            )
            for entry in matched_entries
        ):
            missing.append("p0_fulfillment_logistics_requires_shipment_tracking_customs_or_clean_delivery_context")
    if label_code in {"biz_distributor_inquiry", "biz_distributor_proposal"}:
        if not any(
            is_partnership_distribution_confirmable(
                evidence=entry["evidence"],
                context_text=email_context,
            )
            for entry in matched_entries
        ):
            missing.append("p0_partnership_distribution_requires_strategic_partnership_without_quantity_list")
    if label_code in {"biz_after_sales_support", "biz_after_sales_inquiry"}:
        if not any(has_after_sales_issue(text_context(entry["evidence"], email_context)) for entry in matched_entries):
            missing.append("p0_post_delivery_support_requires_issue_complaint_return_defect_or_repair")
    if label_code in {"biz_offline_invitation", "biz_onsite_sample_viewing", "biz_online_meeting", "biz_visit_followup"}:
        if not any(
            is_meeting_arrangement_confirmable(
                evidence=entry["evidence"],
                context_text=email_context,
            )
            for entry in matched_entries
        ):
            missing.append("p0_meeting_arrangement_requires_time_place_schedule_or_agenda")
    return missing
