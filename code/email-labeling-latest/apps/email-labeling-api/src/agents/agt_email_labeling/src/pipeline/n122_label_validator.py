"""Deterministic business boundary checks for the N122 v8 result.

The LLM decides which current-mail events are candidates.  This module checks
those candidates against the frozen v8 rules and records a visible failure for
an impossible label.  It deliberately does not use the legacy topic cap or
primary/secondary normalization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.agents.agt_email_labeling.src.pipeline.n122_label_spec import load_n122_spec


_SPEC = load_n122_spec(Path(__file__).resolve().parents[2] / "resources" / "n122_v8.json")
N122_RULE_IDS = tuple(rule["rule_id"] for rule in _SPEC.payload["rules"])
N122_ACTIVE_CODES = frozenset(_SPEC.active_codes)

# Every frozen rule has a named enforcement point.  The manifest is kept next
# to the executable checks so a newly added workbook rule cannot silently fall
# through to the generic parser contract.
N122_RULE_ENFORCEMENT = {
    "R01": "current_body_evidence",
    "R02": "direction_and_code_scope",
    "R03": "parallel_labels_without_legacy_cap",
    "R04": "independent_label_evidence",
    "R05": "business_event_over_courtesy",
    "R06": "recommendation_hard_trigger",
    "R07": "objective_product_boundary",
    "R08": "cold_start_history_gate",
    "R09": "first_reply_history_gate",
    "R10": "detail_vs_requirement_boundary",
    "R11": "directional_requirement_boundary",
    "R12": "recommendation_and_quote_coexistence",
    "R13": "sample_request_boundary",
    "R14": "sample_confirmation_boundary",
    "R14A": "sample_inquiry_vs_execution",
    "R14B": "sample_followup_boundary",
    "R15": "logistics_vs_quotation_boundary",
    "R16": "shipment_intent_boundary",
    "R17": "confirmed_delay_boundary",
    "R18": "payment_state_boundary",
    "R19": "refusal_pause_termination_boundary",
    "R20": "document_and_nda_boundary",
    "R20A": "meeting_vs_visit_boundary",
    "R21": "short_confirmation_boundary",
    "R22": "short_confirmation_context_gate",
    "R23": "system_noise_and_duplicate_boundary",
    "R24": "confidence_contract",
    "R25": "evidence_priority_order",
    "R26": "price_inquiry_boundary",
    "R27": "onsite_sample_boundary",
    "R28": "past_event_followup_boundary",
    "R29": "formal_order_recall_gate",
    "R30": "lifecycle_and_current_event_coexistence",
    "R31": "recommendation_vs_product_introduction",
    "R32": "meeting_execution_boundary",
    "R33": "executed_quote_boundary",
    "R34": "fulfillment_logistics_expression_boundary",
    "R35": "completed_payment_boundary",
}
if set(N122_RULE_ENFORCEMENT) != set(N122_RULE_IDS):  # pragma: no cover - spec drift guard
    raise RuntimeError("N122 rule enforcement manifest is out of sync with frozen v8 rules")


class N122BoundaryError(ValueError):
    """A candidate label violates a frozen N122 boundary."""


@dataclass(frozen=True)
class N122BoundaryContext:
    sender_type: str
    current_body: str
    subject: str = ""
    history_complete: bool = False
    has_prior_quotation: bool = False
    has_formal_order: bool = False
    has_prior_order: bool = False
    has_prior_cold_start: bool = False
    days_since_last_contact: int | None = None
    duplicate: bool = False
    duplicate_of: str | None = None
    unique_prior_object: bool = False
    system_noise: bool = False


_PRICE_TERMS = re.compile(
    r"\b(?:prices?|pricing|quot(?:e|ed|ation|ations)|quoatation|moq|minimum quantity|fob|cif|exw|freights?|shipping costs?|transport costs?|sea freight|warehouse prices?|proforma invoice|invoices?|PI)\b",
    re.I,
)
_FULFILLMENT_TERMS = re.compile(
    r"\b(?:shipped|ship|on the ship|on transport|tracking|track(?:ing)?\s+and\s+trace|tracking number|etd|eta|container|20gp|40hq|load(?:ed|ing)?|loading|pick[- ]?up|delivery(?:\s+to|\s+within)?|deliver|delivery date|pallet|cbm|cargo|warehouse|arrival|ready for shipment|not ready|transit|on the way|come to)\b|装柜|提货|发货|上船|运输状态|交期|托盘|收货",
    re.I,
)
_RECOMMEND_TERMS = re.compile(
    r"\b(?:recommend|suggest|offer|propose|match(?:es)?|suitable|best price|consider\s+(?:using|ordering)|change it to|alternative|replace|for your project|can meet your needs|introduce|share (?:with|our)|new (?:product|model|frame)|like to|catalog)\b|推荐|建议|适合|改用|替代|推介|介绍|新品",
    re.I,
)
_OBJECTIVE_PRODUCT_TERMS = re.compile(
    r"\b(?:structure|motor|control box|upgraded|configuration|performance|specification|specs|same as|better price)\b|结构|电机|控制盒|升级|配置|性能",
    re.I,
)
_SAMPLE_REQUEST_TERMS = re.compile(r"\b(?:need|request|require|send|would like|samples?|sampel|handset)\b|索样|样品.*(?:需要|寄|请求)", re.I)
_SAMPLE_CONFIRM_TERMS = re.compile(r"\b(?:confirm|agreed|accept|perfect|address|quantity|color|fee|best|fine|okay|ok|put|add|ship|随货)\b|确认|同意|地址|数量|颜色|费用", re.I)
_SAMPLE_EXECUTION_TERMS = re.compile(r"\b(?:prepare|preparing|make|making|invoice|proforma|dispatch|send out|ship)\b|制样|开票|准备|寄送", re.I)
_MEETING_TERMS = re.compile(r"\b(?:meeting|call|teams|zoom|webex|skype|appointment|agenda|calendar|invite|invitation|see you|schedule|speak to you|\d{1,2}:\d{2})\b|会议|电话|视频|见面|日程", re.I)
_VISIT_TERMS = re.compile(r"\b(?:visit|factory|showroom|office|onsite|on-site|exhibition|booth|stand|tour|warehouse|business trip)\b|拜访|工厂|展厅|办公室|现场|展会|展位|参观", re.I)
_PAYMENT_DONE_TERMS = re.compile(
    r"\b(?:paid|has been paid|did (?:the )?payment|made (?:the )?payment|payment(?:s)? has been (?:just )?done|payment(?:s)? have been (?:just )?done|payment(?:s)? (?:were|was|are|is) (?:just )?(?:done|paid|received)|payment received|paid in full|full payment|remitted)\b"
    r"|\b(?:payment|rest payment|balance payment|payment of)\b.{0,50}\b(?:has\s+(?:just\s+)?been|was|is)\s+(?:just\s+)?(?:done|paid|received)\b"
    r"|已付款|已支付|付款完成|付清",
    re.I,
)
_PAYMENT_FUTURE_TERMS = re.compile(r"\b(?:will pay|plan to pay|arrange payment|payment date|payment status|finance department|later payment|延期付款)\b|安排付款|付款日期|付款状态|财务|延期付款", re.I)
_DELAY_TERMS = re.compile(r"\b(?:delayed|delay|postponed|cannot meet|later than promised|not ready as scheduled|延期|延迟)\b", re.I)
_SHIP_INTENT_TERMS = re.compile(
    r"\b(?:will ship|ship(?:ped)?|dispatch(?:ed)?|load(?:ed|ing)?|loading|pick[- ]?up|etd|tracking number|track(?:ing)?\s+and\s+trace|fedex|dhl|ups|gls|courier|bill of lading|telex release|commercial invoice|packing list|b/l|bl|追踪号|发货|装柜|提货)\b",
    re.I,
)
_DOC_TERMS = re.compile(r"\b(?:drawing|manual|certificate|contract|nda|authorization|datasheet|document|资料|图纸|手册|证书|合同|授权)\b", re.I)
_PRICE_INQUIRY_TERMS = re.compile(r"\b(?:how much|prices?|pricing|quotes?|quotations?|moq|minimum quantity|freights?|shipping costs?|transport costs?|costs?|采购成本)\b|多少钱|价格|报价|起订量|运费", re.I)
_SHORT_ACK_TERMS = re.compile(r"^(?:yes|ok|okay|thanks?|thank you|got it|received|noted|好的|收到|谢谢|感谢|明白)[.!。！,， ]*$", re.I)
_DETAIL_TERMS = re.compile(r"\b(?:function|feature|material|performance|difference|compatible|compatibility|structure|specification|specs|details?|questions?|what|which|how|where|sellings?)\b|功能|结构|材料|性能|差异|适配|规格|细节|问题", re.I)
_REQUIREMENT_TERMS = re.compile(r"\b(?:need|requirement|application|quantity|qty|packaging|package|custom|modify|change|color|black|white|silver|ral|logo|改制|用途|需求|数量|包装|定制)\b|需求|用途|数量|包装|改制|定制|颜色|尺寸", re.I)
_REFUSAL_TERMS = re.compile(r"\b(?:not interested|not interesting|not what we(?:'re| are|’re) looking for|decline|reject|refuse|do not want|won't take|cancel)\b|不感兴趣|没兴趣|拒绝|不要|取消", re.I)
_PAUSE_TERMS = re.compile(r"\b(?:on hold|pause|postpone|wait|come back later|review later|later|when ready|when we are ready|when accepted|accepted (?:his|the) offer|in the future|暂缓|搁置|以后再决定)\b|暂缓|搁置|等待决定|之后回复", re.I)
_TERMINATION_TERMS = re.compile(r"\b(?:terminate|end our cooperation|stop cooperation|no longer work together|close the account)\b|终止合作|结束合作|不再合作", re.I)
_SUPPLY_TERMS = re.compile(r"\b(?:stock|stocks|inventory|available|availability|supply|production|produce|produced|lead time|ready|readiness|warehouse|in stock|out of stock|can provide|deliver|MOQ|not feasible|quantity reaches|customized printing)\b|库存|供货|生产|周期|就绪|现货|缺货|供给", re.I)
_AFTER_SALES_TERMS = re.compile(r"\b(?:install|installation|use|usage|quality|defect|damage|damaged|missing|shortage|repair|replace|replacement|support|solve|solution|troubleshoot|problem|issue|complaint|manual)\b|安装|使用|质量|缺件|损坏|维修|替换|售后|问题|投诉|解决", re.I)
_HANDOVER_TERMS = re.compile(r"\b(?:handover|hand over|new contact|new colleague|responsible|take over|taking over|in charge|my colleague)\b|交接|新负责人|联系人|接手|负责", re.I)
_ORDER_TERMS = re.compile(r"\b(?:order|purchase order|PO|order confirmation|order change|quantity|qty|packing|prepare|prepare(?:d|s|ing)?|produce|produced|production|bag|washers|address|container)\b|订单|采购单|数量|包装|地址|改订单", re.I)
_ORDER_CONFIRMATION_TERMS = re.compile(r"\b(?:signed\s+(?:PI|proforma invoice)|attached\s+(?:the\s+)?signed\s+PI|agreed(?:\s+for|\s+on)?|confirmed(?:\s+the)?)\b|签署|签字|同意|确认", re.I)
_INTEREST_TERMS = re.compile(r"\b(?:interested|interest|plan to buy|looking for|need|project|budget|would like|consider|deal|volumes?|build (?:great )?volumes?)\b|感兴趣|采购计划|意向|项目|预算|想要|需要", re.I)
_FEEDBACK_TERMS = re.compile(r"\b(?:feedback|evaluate|evaluation|review|assessment|decision|project progress|update|market test|check if|option for the future|accepted (?:his|the) offer|offer acceptance)\b|反馈|评估|评价|决定|进展|测试结果|市场", re.I)
_NEGOTIATION_TERMS = re.compile(r"\b(?:lower price|better price|discount|cheaper|too high|not interesting|serious prices?|compare|comparison|bargain|negotiate|competitive price)\b|降价|折扣|更便宜|太高|比较|议价|竞争力", re.I)
_REPURCHASE_TERMS = re.compile(r"\b(?:reorder|repeat order|next order|again|additional order|future orders?)\b|复购|再次下单|下一单|追加订单|未来订单", re.I)
_CANCEL_TERMS = re.compile(r"\b(?:cancel|return|exchange|refund)\b|取消订单|退货|换货|退款", re.I)
_CHANNEL_TERMS = re.compile(r"\b(?:distributor|dealer|agency|agent|partner|cooperation|channel)\b|经销|代理|渠道|合作伙伴", re.I)
_FOLLOWUP_TERMS = re.compile(r"\b(?:follow up|remind|reminder|any news|any update|status|progress|reply|respond|hear from|feedback|confirm|check)\b|跟进|提醒|进展|回复|消息|状态|反馈|确认|检查", re.I)
_COMMERCIAL_TERMS = re.compile(r"\b(?:payment terms|payment method|terms|MOQ|minimum order|incoterm|FOB|CIF|EXW|freight|shipping cost|delivery terms|credit|lead time|cost|\d+\s+days?\s+from|CI)\b|付款条件|付款方式|贸易条款|起订量|运费|交期|成本|商务条件", re.I)
_NEGATED_NEED = re.compile(r"\b(?:do not need|don't need|no need|not interested|not looking for|no requirement)\b|不需要|无需|没有需求|不感兴趣", re.I)
_VISIT_PAST = re.compile(r"\b(?:visited|met|glad to meet|after (?:our|the) meeting|exhibition last|after the exhibition|after our visit|already came)\b|已拜访|拜访后|展会后|见过|来访过", re.I)
_VISIT_FUTURE = re.compile(r"\b(?:visit|come to|available on|arrange a visit|business trip|tour)\b|拜访|来访|参观|行程", re.I)
_SUBSTANTIVE_REPLY = re.compile(r"\b(?:price|quote|order|sample|product|need|project|meeting|visit|payment|shipment|specification|feedback|interested|confirm|agree|request)\b|价格|报价|订单|样品|产品|需求|项目|会议|拜访|付款|发货|规格|反馈|确认|同意|需要", re.I)
_CANCELLING_MEETING = re.compile(r"\b(?:cancel|postpone)\s+(?:the\s+)?(?:meeting|call|appointment)\b|取消会议|推迟会议", re.I)
_PRODUCT_FEEDBACK_TERMS = re.compile(r"\b(?:right|perfect|mistake|enough|approved|works?|not the same|suitable|correct|(?:all|is|are) good)\b|正确|完美|错误|足够|批准|适用|不一致", re.I)
_CLARIFICATION_TERMS = re.compile(r"\b(?:if you prefer|your (?:main )?concern|please confirm|would you like|can you|which option|height range|configuration|tolerance|let me know|adjustments?|size|thickness)\b|如果您偏好|请确认|配置|范围|公差", re.I)


def _has_sample_execution_evidence(text: str) -> bool:
    """Return true only when execution language is tied to a sample object.

    A message can discuss shipping ordinary products and separately ask whether
    a sample should be tested.  The generic ``ship`` token must not turn that
    inquiry into an executed-sample event.
    """
    return bool(
        re.search(
            r"\b(?:sample|samples|sampel)\b[^.!?\n]{0,100}\b(?:prepare|preparing|make|making|invoice|proforma|dispatch|send out|ship(?:ped)?|寄送|制样|开票|准备)\b"
            r"|\b(?:prepare|preparing|make|making|invoice|proforma|dispatch|send out|ship(?:ped)?|寄送|制样|开票|准备)\b[^.!?\n]{0,100}\b(?:sample|samples|sampel)\b",
            text,
            re.I,
        )
    )


def _codes(output: Any) -> list[str]:
    return [str(item.code) for item in getattr(output, "labels", ())]


def _body(context: N122BoundaryContext) -> str:
    # Business evidence always comes from the current body.  Subject is
    # handled only in the explicit meeting special case below.
    return context.current_body.strip()


def _has(code: str, codes: set[str]) -> bool:
    return code in codes


def _raise(code: str, rule: str, reason: str) -> None:
    raise N122BoundaryError(f"{rule}: {code}: {reason}")


def _review(output: Any, rule: str, reason: str) -> None:
    """Record an ambiguous boundary for review without inventing a label."""
    output.validation_issues.append(f"{rule}: {reason}")
    output.review_reasons.append(f"{rule}_boundary_uncertain")


def _make_context(
    *, sender_type: str, current_body: str, subject: str = "", context: Mapping[str, Any] | None = None, **kwargs: Any
) -> N122BoundaryContext:
    values = dict(context or {})
    values.update(kwargs)
    return N122BoundaryContext(
        sender_type=str(sender_type or "").strip().lower(),
        current_body=str(current_body or ""),
        subject=str(subject or ""),
        history_complete=bool(values.get("history_complete", values.get("n122_email_history_complete", False))),
        has_prior_quotation=bool(values.get("has_prior_quotation", values.get("thread_had_quotation_before", False))),
        has_formal_order=bool(values.get("has_formal_order", values.get("formal_order", False))),
        has_prior_order=bool(values.get("has_prior_order", values.get("has_formal_order", values.get("formal_order", False)))),
        has_prior_cold_start=bool(values.get("has_prior_cold_start", values.get("prior_cold_start", False))),
        days_since_last_contact=values.get("days_since_last_contact"),
        duplicate=bool(values.get("duplicate", values.get("is_duplicate", False))),
        duplicate_of=(str(values.get("duplicate_of") or values.get("duplicate_of_email_id") or "").strip() or None),
        unique_prior_object=bool(values.get("unique_prior_object", values.get("has_unique_prior_object", False))),
        system_noise=bool(values.get("system_noise", False)),
    )


def validate_n122_boundaries(
    output: Any,
    *,
    current_body: str,
    sender_type: str,
    subject: str = "",
    context: Mapping[str, Any] | None = None,
    **context_values: Any,
):
    """Validate a parsed ``N122LabelOutput`` against the v8 business rules.

    The function returns the same output object with local audit fields filled.
    Contradictory candidate labels raise ``N122BoundaryError`` so routing can
    send the original result to review without silently deleting evidence.
    """
    ctx = _make_context(sender_type=sender_type, current_body=current_body, subject=subject, context=context, **context_values)
    from src.agents.agt_email_labeling.src.schemas import N122LabelOutput

    if not isinstance(output, N122LabelOutput):
        raise N122BoundaryError("output must be N122LabelOutput")
    codes = _codes(output)
    code_set = set(codes)

    def prune_or_raise(code: str, rule: str, reason: str) -> None:
        """Remove an unsupported secondary candidate, but fail for a sole label.

        A model may return one valid business event together with a second code
        whose evidence is absent from the current body.  Keeping the valid
        event and recording the removal is auditable; accepting a sole
        unsupported code would hide a boundary error, so that case remains a
        hard failure.
        """
        if len(code_set) > 1:
            output.labels = [item for item in output.labels if item.code != code]
            code_set.discard(code)
            output.validation_issues.append(f"{rule}: {code} removed; {reason}")
            return
        _raise(code, rule, reason)

    if len(codes) != len(code_set):
        raise N122BoundaryError("R03: duplicate N122 label code")
    if not code_set or not code_set <= N122_ACTIVE_CODES:
        raise N122BoundaryError("R02: unknown N122 label code")
    expected_direction = "业务员→客户" if ctx.sender_type in {"salesperson", "sales", "业务员"} else "客户→业务员"
    if output.direction != expected_direction:
        _raise("direction", "R02", "current sender direction does not match output")
    for item in output.labels:
        spec = _SPEC.labels_by_code[item.code]
        if spec["direction"] not in {output.direction, "通用"}:
            _raise(item.code, "R02", "label direction is not valid for current sender")
        if not item.evidence.strip() or item.evidence not in ctx.current_body:
            # Context evidence is permitted by the parser only for C01/I13;
            # the boundary layer still requires a current-message anchor when
            # the row is not a short confirmation.
            if item.code not in {"C01", "I13"} and not (item.code == "O23" and item.evidence in ctx.subject):
                _raise(item.code, "R01/R04", "label has no independent current-body evidence")

    text = _body(ctx)
    quote_only = bool(_PRICE_TERMS.search(text)) and not bool(_FULFILLMENT_TERMS.search(text))

    # R06/R07/R31: recommendation and objective product introduction are separate.
    if _has("O04", code_set):
        if not _RECOMMEND_TERMS.search(text) or (_OBJECTIVE_PRODUCT_TERMS.search(text) and not _RECOMMEND_TERMS.search(text)):
            if len(code_set) > 1:
                output.labels = [item for item in output.labels if item.code != "O04"]
                code_set.discard("O04")
                output.validation_issues.append("R31: O04 removed; no current recommendation or choice action")
            else:
                _raise("O04", "R06/R07/R31", "no current recommendation or choice action")
    if _has("O27", code_set) and _RECOMMEND_TERMS.search(text):
        prune_or_raise("O27", "R31", "current message contains a recommendation/choice action")

    # A product-upgrade notice may quote a marketing price without being a
    # separate quotation event.  The frozen N122 example for this boundary is
    # O27 only ("better price $..."), while an attached quotation/PI or FOB/CIF
    # offer remains eligible for O07.  Remove only this deterministic overlap;
    # all other contradictory candidates still fail loudly below.
    if _has("O27", code_set) and _has("O07", code_set):
        marketing_price = bool(re.search(r"\b(?:better|best|competitive)\s+price\b|更优惠价格|更有竞争力的价格", text, re.I))
        quote_artifact = bool(re.search(r"\b(?:quote|quotation|proforma|PI|FOB|CIF|EXW|price\s+list)\b|报价单|报价列表", text, re.I))
        if marketing_price and not quote_artifact:
            output.labels = [item for item in output.labels if item.code != "O07"]
            code_set.discard("O07")
            output.validation_issues.append("R33: O07 removed from objective product-upgrade price mention")

    # R12/R33: a sent quote needs an actual price/quote artifact or amount.
    if _has("O07", code_set) and not (_PRICE_TERMS.search(text) or re.search(r"\$|€|£|\b\d+[,.]?\d*\s*(?:usd|eur|rmb|pcs|pc)\b", text, re.I)):
        _raise("O07", "R12/R33", "no executed price, quote, PI, or price attachment evidence")

    # R15/R16/R34: transport pricing is not fulfillment logistics.
    if (code_set & {"O21", "I16"}) and quote_only:
        for logistics_code in ("O21", "I16"):
            if _has(logistics_code, code_set):
                prune_or_raise(logistics_code, "R15/R34", "freight/FOB/CIF is a quotation condition without fulfillment evidence")
    shipment_with_container = bool(
        re.search(r"\b(?:send|sending)\b[^.?!\n]{0,80}\b(?:container|shipment|cargo)\b", text, re.I)
    )
    if _has("O14", code_set) and not (_SHIP_INTENT_TERMS.search(text) or shipment_with_container):
        prune_or_raise("O14", "R16", "no explicit shipment intent, shipment fact, ETD, or tracking evidence")
    if _has("O13", code_set) and not _DELAY_TERMS.search(text):
        if len(code_set) > 1:
            output.labels = [item for item in output.labels if item.code != "O13"]
            code_set.discard("O13")
            output.validation_issues.append("R17: O13 removed; no confirmed delay evidence")
        else:
            _raise("O13", "R17", "no confirmed delay evidence")

    # Remaining O/I labels receive the same minimum evidence treatment.  These
    # checks stop an empty or merely quoted body from passing a business label.
    if _has("O26", code_set) and not _SUPPLY_TERMS.search(text):
        prune_or_raise("O26", "R07", "no normal supply, stock, production, or lead-time evidence")
    if _has("O16", code_set) and not (_AFTER_SALES_TERMS.search(text) and re.search(r"\b(?:will|can|please|send|check|help|solution|解决|处理)\b|方案|处理", text, re.I)):
        _raise("O16", "R07", "no issue plus handling action evidence")
    if _has("O17", code_set) and not (re.search(r"\b(?:out of stock|cannot provide|unavailable|not available(?: for sale)?|cannot be sold|not completed|alternative|replace|change it to|cancel)\b|缺货|无法供货|替代|取消", text, re.I)):
        _raise("O17", "R07", "no supply restriction or alternative evidence")
    if _has("O18", code_set) and not re.search(r"\b(?:happy holiday|merry christmas|happy new year|greeting|best wishes|how are you|keep you posted|if you have any question|thanks? for (?:your )?confirmation|well noted|kindly noted)\b|节日|问候|祝福|客情", text, re.I):
        prune_or_raise("O18", "R07", "no relationship-maintenance evidence")
    if _has("O19", code_set) and not _HANDOVER_TERMS.search(text):
        prune_or_raise("O19", "R07", "no handover or responsible-contact change evidence")
    if _has("O11", code_set) and not _ORDER_TERMS.search(text):
        _raise("O11", "R07", "no concrete order or execution-detail evidence")
    if _has("O12", code_set) and not (_PAYMENT_DONE_TERMS.search(text) or _PAYMENT_FUTURE_TERMS.search(text) or re.search(r"\b(?:payment|pay|remittance|bank|water slip|invoice)\b|付款|汇款|水单|发票", text, re.I)):
        _raise("O12", "R18", "no payment follow-up or receipt evidence")
    if _has("I02", code_set) and not _INTEREST_TERMS.search(text):
        prune_or_raise("I02", "R07", "no current purchase interest or plan evidence")
    if _has("I03", code_set) and not _FEEDBACK_TERMS.search(text):
        prune_or_raise("I03", "R07", "no opportunity evaluation or progress evidence")
    if _has("I09", code_set) and not _NEGOTIATION_TERMS.search(text):
        if len(code_set) > 1:
            output.labels = [item for item in output.labels if item.code != "I09"]
            code_set.discard("I09")
            output.validation_issues.append("R07: I09 removed; no negotiation or comparison evidence")
        else:
            _raise("I09", "R07", "no negotiation or comparison evidence")
    if _has("I14", code_set) and not (_ORDER_TERMS.search(text) or _ORDER_CONFIRMATION_TERMS.search(text)):
        prune_or_raise("I14", "R07", "no customer order confirmation or change evidence")
    if _has("I17", code_set) and not _REPURCHASE_TERMS.search(text):
        prune_or_raise("I17", "R07", "no repeat-purchase evidence")
    if _has("I17", code_set) and not ctx.has_prior_order:
        prune_or_raise("I17", "R07", "repeat-purchase label requires a prior order fact")
    if _has("I18", code_set) and not _AFTER_SALES_TERMS.search(text):
        _raise("I18", "R07", "no customer after-sales issue or support question")
    if _has("I19", code_set) and not (re.search(r"\b(?:sample|samples|test|tested|testing|feedback|result|trial)\b|样品|测试|试用|反馈|结果", text, re.I) or _PRODUCT_FEEDBACK_TERMS.search(text)):
        prune_or_raise("I19", "R07", "no product or sample test feedback evidence")
    if _has("I20", code_set) and not re.search(r"\b(?:quality|defect|defective|damaged|broken|missing|complaint|not the same)\b|质量|缺陷|损坏|缺件|投诉|不一致", text, re.I):
        if len(code_set) > 1:
            output.labels = [item for item in output.labels if item.code != "I20"]
            code_set.discard("I20")
            output.validation_issues.append("R07: I20 removed; no quality or damage complaint evidence")
        else:
            _raise("I20", "R07", "no quality or damage complaint evidence")
    if _has("I21", code_set) and (_CANCELLING_MEETING.search(text) or not _CANCEL_TERMS.search(text)):
        _raise("I21", "R19", "no order return/exchange/cancellation evidence")
    if _has("I22", code_set) and not _CHANNEL_TERMS.search(text):
        prune_or_raise("I22", "R07", "no channel or cooperation inquiry evidence")
    if _has("I23", code_set) and not _FOLLOWUP_TERMS.search(text):
        prune_or_raise("I23", "R07", "no customer chase or progress request evidence")
    if _has("I24", code_set) and not _COMMERCIAL_TERMS.search(text):
        prune_or_raise("I24", "R07", "no commercial-condition evidence")
    if _has("O05", code_set) and not (_CHANNEL_TERMS.search(text) or re.search(r"\b(?:exhibition|exhibit|trade show|fair|booth|stand|invite)\b|展会|展览|展位|邀请", text, re.I)):
        _raise("O05", "R07", "no channel or exhibition development evidence")
    if _has("O06", code_set) and not (_REQUIREMENT_TERMS.search(text) or _CLARIFICATION_TERMS.search(text)):
        if len(code_set) > 1:
            output.labels = [item for item in output.labels if item.code != "O06"]
            code_set.discard("O06")
            output.validation_issues.append("R11: O06 removed; no salesperson requirement clarification evidence")
        else:
            _raise("O06", "R11", "no salesperson requirement clarification evidence")
    if _has("O08", code_set) and not (_FOLLOWUP_TERMS.search(text) or _PRICE_TERMS.search(text)):
        _raise("O08", "R08", "no quotation follow-up evidence")
    if _has("O08", code_set) and not ctx.has_prior_quotation:
        _review(output, "R08", "quotation history was not supplied; follow-up inheritance needs review")
    if _has("O20", code_set) and (not _SAMPLE_REQUEST_TERMS.search(text) or _has_sample_execution_evidence(text)) and not (code_set & {"O09", "O14", "O22"}):
        prune_or_raise("O20", "R14A", "no sample inquiry or sample execution was confused with inquiry")

    # R10/R11: product detail and requirement confirmation are separate
    # directions.  The checks are intentionally evidence based and allow both
    # labels when a message contains two independent questions.
    if _has("I06", code_set) and not _DETAIL_TERMS.search(text):
        _review(output, "R10", "no explicit product detail or compatibility wording")
    positive_requirement_text = _NEGATED_NEED.sub(" ", text)
    if _has("I07", code_set) and _NEGATED_NEED.search(text) and not _REQUIREMENT_TERMS.search(positive_requirement_text):
        _raise("I07", "R11", "a negative/no-need statement is not a customer requirement")
    if _has("I07", code_set) and not _REQUIREMENT_TERMS.search(text):
        _review(output, "R11", "no explicit customer requirement wording")

    # R19: a refusal, pause, and relationship termination carry different
    # meanings and must not be inferred from one another.
    if _has("I30", code_set) and (_CANCELLING_MEETING.search(text) or not _REFUSAL_TERMS.search(text)):
        prune_or_raise("I30", "R19", "no explicit refusal of a product, proposal, or project")
    if _has("I04", code_set) and (
        not _PAUSE_TERMS.search(text)
        or (bool(_PRICE_TERMS.search(text)) and not bool(_INTEREST_TERMS.search(text) or _RECOMMEND_TERMS.search(text)))
    ):
        prune_or_raise("I04", "R19", "no explicit temporary hold or wait decision")
    if _has("I05", code_set) and not _TERMINATION_TERMS.search(text):
        _raise("I05", "R19", "no explicit termination of the business relationship")

    # R18/R35: completed payment and future payment communication are distinct.
    if _has("I15", code_set) and not _PAYMENT_DONE_TERMS.search(text):
        prune_or_raise("I15", "R18/R35", "no completed-payment evidence")
    if _has("I26", code_set) and (
        _PAYMENT_DONE_TERMS.search(text)
        or not (
            _PAYMENT_FUTURE_TERMS.search(text)
            or re.search(r"\b(?:pay|payment|remit|remittance|balance|installment|due)\b|付款|汇款|尾款|余款|分期", text, re.I)
        )
    ):
        prune_or_raise("I26", "R18/R35", "I26 requires a future payment date/status or payment-delay communication")

    # R13/R14/R14A/R14B: sample request, confirmation, execution and follow-up.
    if _has("I12", code_set) and not (_SAMPLE_REQUEST_TERMS.search(text) and not _SAMPLE_CONFIRM_TERMS.search(text)):
        _review(output, "R13/R14", "sample request and confirmation wording are ambiguous")
    if _has("I27", code_set) and not (_SAMPLE_REQUEST_TERMS.search(text) and _SAMPLE_CONFIRM_TERMS.search(text)):
        prune_or_raise("I27", "R14", "sample confirmation requires a sample object plus acceptance/arrangement evidence")
    if _has("O09", code_set) and not _SAMPLE_EXECUTION_TERMS.search(text):
        _review(output, "R14A", "sample arrangement execution wording is incomplete")
    if _has("O20", code_set) and _has_sample_execution_evidence(text) and not (code_set & {"O09", "O14", "O22"}):
        _raise("O20", "R14A", "sample execution should use O09")
    if _has("O22", code_set) and not (
        re.search(r"\b(?:sample|samples)\b|样品", text, re.I)
        and re.search(r"\b(?:received|tested|testing|feedback|opinion|comments|update|confirm|confirmed|order|news)\b|收到|测试|反馈|意见|跟进|确认|消息", text, re.I)
    ):
        prune_or_raise("O22", "R14B", "sample follow-up requires a sample object and receipt/test/update evidence")

    # R20/R20A/R27/R28/R32: documents, meeting, visit and onsite sample boundaries.
    if code_set & {"O15", "I10", "I11"} and not _DOC_TERMS.search(text):
        document_codes = code_set & {"O15", "I10", "I11"}
        if len(code_set) > len(document_codes):
            output.labels = [item for item in output.labels if item.code not in document_codes]
            code_set.difference_update(document_codes)
            output.validation_issues.append("R20: document label removed; no explicit document/NDA action")
        else:
            _review(output, "R20", "document/NDA action wording is incomplete")
    meeting_link = bool(
        (
            re.search(r"\b(?:teams|zoom|webex)\b", text, re.I)
            and re.search(r"\b(?:link|join|invite|invitation)\b|加入|链接|入会", text, re.I)
        )
        or re.search(r"\b(?:send|share) (?:the )?(?:meeting )?link\b|\blink soon\b", text, re.I)
    )
    if _has("O10", code_set) and not (
        (_MEETING_TERMS.search(text) or meeting_link)
        and (
            meeting_link
            or re.search(r"\b(?:schedule|scheduled|available|confirm|confirmed|date|time|at\s+\d|invite|invitation|calendar|arrange|use\s+skype|plan|today|tomorrow|soon)\b|安排|确认|时间|日期|邀请", text, re.I)
        )
    ):
        prune_or_raise("O10", "R20A/R32", "online meeting label requires a meeting object and schedule/confirmation")
    if _has("I13", code_set):
        if _SHORT_ACK_TERMS.fullmatch(ctx.current_body.strip()) and not ctx.unique_prior_object:
            _raise("R22", "R22", "short confirmation requires one uniquely identifiable prior business object")
        relative_time = bool(
            re.search(
                r"\b(?:in\s+(?:about\s+)?\d+(?:[,.]\d+)?\s*(?:hours?|hrs?|minutes?|mins?)|already\s+in\s+(?:the\s+)?(?:meeting|call)|join\s+(?:the\s+)?(?:meeting|call)|speak\s+to\s+you\s+in\s+(?:\d+|one|an?)\s*(?:hours?|hrs?|minutes?|mins?)?)\b",
                text,
                re.I,
            )
        )
        if not (
            (_SHORT_ACK_TERMS.fullmatch(ctx.current_body.strip()) and ctx.unique_prior_object)
            or (
                _MEETING_TERMS.search(text)
                and (
                    re.search(r"\b(?:confirm|confirmed|available|date|time|at\s+\d|schedule|scheduled|agree|good|fine|works|plan|today|tomorrow|soon)\b|确认|时间|日期|可以|同意", text, re.I)
                    or relative_time
                )
            )
        ):
            prune_or_raise("I13", "R20A/R32", "customer meeting confirmation requires schedule/availability evidence")
    visit_evidence_text = f"{text}\n{ctx.subject}"
    if _has("O23", code_set) and not (
        _VISIT_TERMS.search(visit_evidence_text)
        and re.search(r"\b(?:invite|invitation|available|date|time|schedule|arrange|come|visit)\b|邀请|安排|时间|日期", visit_evidence_text, re.I)
        and not _VISIT_PAST.search(text)
    ):
        prune_or_raise("O23", "R20A", "visit invitation requires a future visit arrangement")
    if _has("I28", code_set) and not (
        _VISIT_TERMS.search(text)
        and re.search(r"\b(?:confirm|confirmed|available|date|time|schedule|scheduled|agree|good|fine|works|come)\b|确认|时间|日期|可以|同意", text, re.I)
        and not re.search(r"\b(?:will visit|will come|plan to visit|planning to visit)\b|将拜访|计划拜访", text, re.I)
    ):
        prune_or_raise("I28", "R20A", "customer visit acceptance requires confirmation/availability, not a bare future plan")
    if _has("O25", code_set) and not (
        _VISIT_PAST.search(text)
        or re.search(r"\b(?:nice meeting|thank you for visiting|after (?:our|the) (?:meeting|visit|exhibition))\b|会议后|拜访后|展会后", text, re.I)
    ):
        prune_or_raise("O25", "R28", "past-event follow-up requires evidence that the visit or exhibition already happened")
    if code_set & {"O24", "I29"} and not (_VISIT_TERMS.search(text) and re.search(r"sample|样品|product|实物|show", text, re.I)):
        _review(output, "R27", "onsite sample wording is incomplete")

    # R26: inquiry must be a customer question about commercial conditions.
    if _has("I08", code_set) and ctx.sender_type != "customer":
        _raise("I08", "R26", "I08 is customer direction only")
    if _has("I08", code_set) and not _PRICE_INQUIRY_TERMS.search(text):
        prune_or_raise("I08", "R26", "no price, MOQ, freight, or cost inquiry")

    # R08/R09/R29: lifecycle labels need explicit history facts from the caller.
    if _has("O01", code_set) and not (_RECOMMEND_TERMS.search(text) and _SUBSTANTIVE_REPLY.search(text)):
        _raise("O01", "R08", "cold-start label requires a current product recommendation/wakeup event")
    if _has("O01", code_set) and not ctx.history_complete:
        # Frozen phase-0 history is incomplete; keep the label but make the
        # limitation visible to downstream routing.
        output.validation_issues.append("R08: history incomplete; O01 confidence is bounded by visible history")
        output.review_reasons.append("history_incomplete")
    if _has("O03", code_set) and not (
        ctx.has_formal_order
        and (ctx.days_since_last_contact or 0) >= 90
        and (_RECOMMEND_TERMS.search(text) or _FOLLOWUP_TERMS.search(text) or _INTEREST_TERMS.search(text))
    ):
        prune_or_raise("O03", "R29", "formal order plus 90-day silence is not present in context")
    if _has("I01", code_set) and not _SUBSTANTIVE_REPLY.search(text):
        _raise("I01", "R09", "first reply requires a substantive current business response")
    if _has("I01", code_set) and not ctx.has_prior_cold_start:
        _raise("I01", "R09", "first reply requires a prior same-customer O01 cold-start event")
    if _has("I01", code_set) and not ctx.history_complete:
        output.validation_issues.append("R09: history incomplete; I01 cannot be fully verified")
        output.review_reasons.append("history_incomplete")

    # R22: a bare yes/ok can inherit one uniquely identifiable prior object;
    # without that object it remains a review candidate instead of a guessed
    # business label.
    if _SHORT_ACK_TERMS.fullmatch(ctx.current_body.strip()) and len(code_set) == 1 and not _has("C01", code_set):
        if not ctx.unique_prior_object:
            _raise("R22", "R22", "short confirmation requires one uniquely identifiable prior business object")
        output.validation_issues.append("R22: inherited label confidence is bounded by prior context")
        output.review_reasons.append("inherited_from_prior_context")

    # R21/R23/R05: system and courtesy labels cannot coexist with a concrete event.
    if _has("C02", code_set) and len(code_set) > 1:
        _raise("C02", "R23/R05", "system noise cannot coexist with business labels")
    if _has("C01", code_set) and len(code_set) > 1:
        # C01 is only the neutral short-confirmation outcome.  When the same
        # current message has an evidence-backed business event, retaining C01
        # creates a false conflict and dilutes the actionable label.
        output.labels = [item for item in output.labels if item.code != "C01"]
        code_set.discard("C01")
        output.validation_issues.append("R21: C01 removed; concrete business event takes precedence")
    if ctx.duplicate and not ctx.duplicate_of:
        _raise("C02", "R23", "duplicate metadata requires the explicit original email linkage")
    if (ctx.system_noise or ctx.duplicate) and not _has("C02", code_set):
        output.validation_issues.append("R23: system/duplicate metadata requires C02 or original-label linkage")
        output.review_reasons.append("system_or_duplicate")

    # R24/R25: confidence is already checked by the extractor; repeat the
    # lower-bound relation here so later callers cannot bypass the contract.
    expected_overall = min(item.confidence for item in output.labels)
    if output.overall_confidence != expected_overall:
        # Boundary pruning above can remove the label that originally set the
        # minimum.  Recompute this derived field rather than invalidating the
        # remaining evidence-backed labels.
        output.overall_confidence = expected_overall
        output.confidence_level = "high" if expected_overall >= 0.90 else "medium" if expected_overall >= 0.75 else "low"
        output.validation_issues.append("R24: overall confidence recomputed after boundary pruning")
    # R25/R30 are preservation checks: evidence ordering decides whether a
    # candidate is accepted, while lifecycle labels remain alongside the
    # current event and are never used to replace it.
    if code_set & {"O01", "O03", "I01"} and len(code_set) == 1:
        output.validation_issues.append("R30: lifecycle label has no current event label")
        output.review_reasons.append("lifecycle_without_current_event")
    if output.validation_issues:
        output.validation_issues = list(dict.fromkeys(output.validation_issues))
    if output.review_reasons:
        output.review_reasons = list(dict.fromkeys(output.review_reasons))
    return output


def boundary_coverage_report(spec: Any = _SPEC, rows: Mapping[str, Mapping[str, Any]] | None = None, references: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Report real frozen positive coverage for every rule.

    A rule is ``未覆盖`` when none of its declared output codes appears in the
    frozen reference rows.  No synthetic rows are created to turn it green.
    """
    refs = references or {}
    codes_by_email = {
        email_id: {code.strip() for code in str(row.get("标签代码（多选）", "")).split("；") if code.strip()}
        for email_id, row in refs.items()
    }
    report: dict[str, Any] = {"total_rules": len(spec.payload["rules"]), "rules": {}}
    for rule in spec.payload["rules"]:
        output_codes = set(re.findall(r"\b[OIC]\d{2}\b", str(rule.get("output", ""))))
        matched = sorted(email_id for email_id, codes in codes_by_email.items() if output_codes & codes)
        report["rules"][rule["rule_id"]] = {
            "status": "covered" if matched else "未覆盖",
            "output_codes": sorted(output_codes),
            "reference_email_ids": matched,
            "source": rule["source"],
        }
    return report


def n122_label_records(output: Any) -> list[dict[str, Any]]:
    """Convert the v8 labels for persistence without legacy cap/role fields."""
    return [
        {
            "code": item.code,
            "name": item.name,
            "evidence": item.evidence,
            "confidence": item.confidence,
            "context_refs": list(item.context_refs),
        }
        for item in output.labels
    ]
