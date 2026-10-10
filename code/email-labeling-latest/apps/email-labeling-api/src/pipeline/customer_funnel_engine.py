from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from hashlib import sha256
from typing import Any

from src.agents.agt_email_labeling.src.pipeline.action_definitions import ACTION_NORMALIZE
from src.pipeline.customer_funnel_rules import (
    CUSTOMER_FUNNEL_RULE_VERSION,
    FUNNEL_STAGES,
    L1_MARKETING_ACTIONS,
    L1_MARKETING_LABELS,
    L2_REPLY_ACTIONS,
    L2_REPLY_LABELS,
    L3_INQUIRY_ACTIONS,
    L3_INQUIRY_LABELS,
    L4_EXECUTED_SAMPLE_ACTIONS,
    L4_EXECUTED_TEXT_MARKERS,
    L4_LOGISTICS_ACTIONS,
    L4_LOGISTICS_LABELS,
    L4_SAMPLE_TEXT_MARKERS,
    L5_EXCLUDED_SAMPLE_ACTIONS,
    L5_FORMAL_ORDER_ACTIONS,
    L5_FORMAL_ORDER_LABELS,
    L5_SAMPLE_TEXT_MARKERS,
    STAGE_NAMES,
    STAGE_RANK,
    SYSTEM_NOISE_ACTIONS,
)
from src.pipeline.review_rule_gates import is_l5_sample_payment_context, text_context


@dataclass(frozen=True)
class EmailFunnelInput:
    customer_id: str
    email_id: str | None
    thread_id: str | None
    sender_type: str
    sent_at: datetime | None
    needs_review: bool
    facts: dict[str, Any]
    result_version: int | None = None
    subject: str = ""
    cleaned_body: str = ""


@dataclass(frozen=True)
class CustomerFunnelEvent:
    customer_id: str
    source_type: str
    source_id: str
    email_id: str
    thread_id: str | None
    stage: str
    stage_name: str
    occurred_at: datetime | None
    evidence_text: str
    evidence_hash: str
    fact_strength: str
    trigger_reason: str
    rule_version: str
    frozen_result_version: str
    decision_status: str = "confirmed"
    detected_at: datetime | None = None


@dataclass(frozen=True)
class CustomerFunnelState:
    customer_id: str | None = None
    current_active_stage: str | None = None
    deepest_stage: str | None = None
    actual_milestones: list[CustomerFunnelEvent] = field(default_factory=list)
    main_funnel_counted_as: list[str] = field(default_factory=list)
    actual_milestone_counted_as: list[str] = field(default_factory=list)
    confirmed_event_count: int = 0


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _labels(facts: dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    for item in _list(facts.get("business_labels")):
        if isinstance(item, dict):
            code = item.get("id") or item.get("code")
            if code:
                labels.add(str(code))
    return labels


def _action_entries(facts: dict[str, Any]) -> list[dict[str, str]]:
    multi_label = _dict(facts.get("multi_label"))
    raw_extraction = _dict(facts.get("raw_extraction"))
    raw_actions = multi_label.get("actions")
    if raw_actions is None:
        raw_actions = raw_extraction.get("actions")
    entries: list[dict[str, str]] = []
    for item in _list(raw_actions):
        if not isinstance(item, dict):
            continue
        raw_code = item.get("action") or item.get("label")
        if not raw_code:
            continue
        code = ACTION_NORMALIZE.get(str(raw_code).strip().lower().replace("-", "_"), str(raw_code))
        entries.append(
            {
                "action": code,
                "raw_action": str(raw_code),
                "evidence": str(item.get("evidence") or ""),
            }
        )
    return entries


def _actions(entries: list[dict[str, str]]) -> set[str]:
    return {entry["action"] for entry in entries}


def _evidence_for(entries: list[dict[str, str]], trigger_actions: set[str]) -> str:
    for entry in entries:
        if entry["action"] in trigger_actions and entry["evidence"]:
            return entry["evidence"]
    return ""


def _contains_any(text: str, markers: set[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _hash_evidence(email_id: str, stage: str, evidence_text: str) -> str:
    digest_source = f"{email_id}|{stage}|{evidence_text}".encode("utf-8")
    return sha256(digest_source).hexdigest()


def _frozen_result_version(item: EmailFunnelInput) -> str:
    business_label_version = item.facts.get("business_label_rule_version")
    if business_label_version:
        return str(business_label_version)
    if item.result_version is not None:
        return f"email_labeling_results.version:{item.result_version}"
    return "email_labeling_results.facts"


def _event(
    *,
    item: EmailFunnelInput,
    stage: str,
    source_id: str,
    evidence_text: str,
    fact_strength: str,
    trigger_reason: str,
) -> CustomerFunnelEvent:
    if not evidence_text:
        raise ValueError(f"customer funnel event {stage} requires evidence_text for email {item.email_id}")
    return CustomerFunnelEvent(
        customer_id=item.customer_id,
        source_type="email_labeling_result",
        source_id=source_id,
        email_id=item.email_id,
        thread_id=item.thread_id,
        stage=stage,
        stage_name=STAGE_NAMES[stage],
        occurred_at=item.sent_at,
        evidence_text=evidence_text,
        evidence_hash=_hash_evidence(item.email_id, stage, evidence_text),
        fact_strength=fact_strength,
        trigger_reason=trigger_reason,
        rule_version=CUSTOMER_FUNNEL_RULE_VERSION,
        frozen_result_version=_frozen_result_version(item),
    )


def authoritative_event_to_customer_funnel_event(
    *,
    customer_id: str,
    event_type: str,
    source_primary_key: str,
    event_date: date | datetime | str,
    evidence: dict[str, Any],
    source_system: str,
    source_table: str,
    source_rule_version: str,
) -> CustomerFunnelEvent:
    occurred_at = _event_date_to_datetime(event_date)
    stage = _authoritative_event_stage(event_type)
    event_ref = _authoritative_event_ref(event_type, evidence, source_primary_key)
    evidence_text = f"ERP {event_type} {event_ref} on {occurred_at.date().isoformat()}"
    source_id = f"{source_system}.{source_table}:{source_primary_key}"
    return CustomerFunnelEvent(
        customer_id=customer_id,
        source_type="email_labeling_authoritative_event",
        source_id=source_id,
        email_id=None,
        thread_id=None,
        stage=stage,
        stage_name=STAGE_NAMES[stage],
        occurred_at=occurred_at,
        evidence_text=evidence_text,
        evidence_hash=_hash_evidence(source_id, stage, evidence_text),
        fact_strength="authoritative",
        trigger_reason=f"ERP {event_type} authoritative event from business system",
        rule_version=CUSTOMER_FUNNEL_RULE_VERSION,
        frozen_result_version=f"email_labeling_authoritative_events:{source_rule_version}",
    )


def authoritative_order_confirmed_event_to_l5(
    *,
    customer_id: str,
    source_primary_key: str,
    event_date: date | datetime | str,
    evidence: dict[str, Any],
    source_system: str,
    source_table: str,
    source_rule_version: str,
) -> CustomerFunnelEvent:
    return authoritative_event_to_customer_funnel_event(
        customer_id=customer_id,
        event_type="order_confirmed",
        source_primary_key=source_primary_key,
        event_date=event_date,
        evidence=evidence,
        source_system=source_system,
        source_table=source_table,
        source_rule_version=source_rule_version,
    )


def _authoritative_event_stage(event_type: str) -> str:
    if event_type in {"sample_order_confirmed", "order_shipped"}:
        return "L4"
    if event_type in {"order_confirmed", "payment_confirmed"}:
        return "L5"
    raise ValueError(f"Unsupported authoritative customer funnel event type: {event_type}")


def _authoritative_event_ref(event_type: str, evidence: dict[str, Any], source_primary_key: str) -> str:
    if event_type in {"order_confirmed", "sample_order_confirmed"}:
        return str(evidence.get("order_no") or source_primary_key)
    if event_type == "payment_confirmed":
        return str(evidence.get("exchange_id") or evidence.get("export_order_no") or source_primary_key)
    if event_type == "order_shipped":
        return str(evidence.get("ship_id") or evidence.get("linked_order_nos") or source_primary_key)
    return source_primary_key


def _event_date_to_datetime(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    parsed = datetime.fromisoformat(str(value)[:10])
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _l4_text_is_executed_sample(text: str) -> bool:
    return _contains_any(text, L4_SAMPLE_TEXT_MARKERS) and _contains_any(text, L4_EXECUTED_TEXT_MARKERS)


def _summary_context(facts: dict[str, Any]) -> str:
    multi_label = _dict(facts.get("multi_label"))
    raw_extraction = _dict(facts.get("raw_extraction"))
    return text_context(
        multi_label.get("summary"),
        raw_extraction.get("summary"),
        facts.get("summary"),
    )


def _l5_text_is_sample_only(text: str) -> bool:
    return _contains_any(text, L5_SAMPLE_TEXT_MARKERS) or is_l5_sample_payment_context(text)


def derive_customer_funnel_events(
    item: EmailFunnelInput,
    current_state: CustomerFunnelState,
) -> list[CustomerFunnelEvent]:
    if item.needs_review:
        return []
    entries = _action_entries(item.facts)
    actions = _actions(entries)
    if actions & SYSTEM_NOISE_ACTIONS:
        return []

    labels = _labels(item.facts)
    events: list[CustomerFunnelEvent] = []
    milestone_stages = {event.stage for event in current_state.actual_milestones}
    has_l1 = "L1" in milestone_stages or current_state.deepest_stage is not None
    needs_first_reply_event = item.sender_type == "customer" and has_l1 and "L2" not in milestone_stages

    l5_evidence = _evidence_for(entries, L5_FORMAL_ORDER_ACTIONS)
    l5_context = text_context(
        l5_evidence,
        item.subject,
        item.cleaned_body,
        item.facts.get("email_subject"),
        item.facts.get("email_text"),
        _summary_context(item.facts),
    )
    if (labels & L5_FORMAL_ORDER_LABELS or actions & L5_FORMAL_ORDER_ACTIONS) and not (
        actions & L5_EXCLUDED_SAMPLE_ACTIONS or _l5_text_is_sample_only(l5_context)
    ):
        if needs_first_reply_event:
            events.append(
                _event(
                    item=item,
                    stage="L2",
                    source_id=f"{item.email_id}:L2",
                    evidence_text=l5_evidence or "customer effective first reply before formal order event",
                    fact_strength="evidence",
                    trigger_reason="customer effective first reply; auto replies and system noise are excluded",
                )
            )
        events.append(
            _event(
                item=item,
                stage="L5",
                source_id=f"{item.email_id}:L5",
                evidence_text=l5_evidence or "formal order/payment business label confirmed",
                fact_strength="authoritative",
                trigger_reason="formal PO/PI/order payment confirmation; sample PI/payment is excluded",
            )
        )
        return events

    l4_sample_evidence = _evidence_for(entries, L4_EXECUTED_SAMPLE_ACTIONS)
    if actions & L4_EXECUTED_SAMPLE_ACTIONS:
        events.append(
            _event(
                item=item,
                stage="L4",
                source_id=f"{item.email_id}:L4",
                evidence_text=l4_sample_evidence,
                fact_strength="executed",
                trigger_reason="sample shipment action confirms sample was sent",
            )
        )
        return events

    l4_logistics_evidence = _evidence_for(entries, L4_LOGISTICS_ACTIONS)
    if (labels & L4_LOGISTICS_LABELS or actions & L4_LOGISTICS_ACTIONS) and _l4_text_is_executed_sample(l4_logistics_evidence):
        strength = "executed" if _l4_text_is_executed_sample(l4_logistics_evidence) else "authoritative"
        if needs_first_reply_event:
            events.append(
                _event(
                    item=item,
                    stage="L2",
                    source_id=f"{item.email_id}:L2",
                    evidence_text=l4_logistics_evidence or "customer effective first reply before logistics event",
                    fact_strength="evidence",
                    trigger_reason="customer effective first reply; auto replies and system noise are excluded",
                )
            )
        events.append(
            _event(
                item=item,
                stage="L4",
                source_id=f"{item.email_id}:L4",
                evidence_text=l4_logistics_evidence or "shipment/logistics business label confirmed",
                fact_strength=strength,
                trigger_reason="shipment or tracking evidence confirms an executed delivery event",
            )
        )
        return events

    l3_evidence = _evidence_for(entries, L3_INQUIRY_ACTIONS)
    if labels & L3_INQUIRY_LABELS or actions & L3_INQUIRY_ACTIONS:
        if needs_first_reply_event:
            events.append(
                _event(
                    item=item,
                    stage="L2",
                    source_id=f"{item.email_id}:L2",
                    evidence_text=l3_evidence or "customer effective first reply before inquiry event",
                    fact_strength="evidence",
                    trigger_reason="customer effective first reply; auto replies and system noise are excluded",
                )
            )
        events.append(
            _event(
                item=item,
                stage="L3",
                source_id=f"{item.email_id}:L3",
                evidence_text=l3_evidence or "inquiry/deepening business label confirmed",
                fact_strength="evidence",
                trigger_reason="customer or business discussion moved into inquiry/deepening context",
            )
        )
        return events

    l2_evidence = _evidence_for(entries, L2_REPLY_ACTIONS)
    if item.sender_type == "customer" and (needs_first_reply_event or labels & L2_REPLY_LABELS or actions & L2_REPLY_ACTIONS):
        events.append(
            _event(
                item=item,
                stage="L2",
                source_id=f"{item.email_id}:L2",
                evidence_text=l2_evidence or "customer effective reply business event confirmed",
                fact_strength="evidence",
                trigger_reason="customer effective first reply; auto replies and system noise are excluded",
            )
        )
        return events

    l1_evidence = _evidence_for(entries, L1_MARKETING_ACTIONS)
    if item.sender_type == "salesperson" and (labels & L1_MARKETING_LABELS or actions & L1_MARKETING_ACTIONS):
        events.append(
            _event(
                item=item,
                stage="L1",
                source_id=f"{item.email_id}:L1",
                evidence_text=l1_evidence or "salesperson marketing outreach business label confirmed",
                fact_strength="evidence",
                trigger_reason="salesperson sends real marketing/product outreach",
            )
        )

    return events


def _deepest(stages: list[str]) -> str | None:
    if not stages:
        return None
    return max(stages, key=lambda stage: STAGE_RANK[stage])


def _main_counted_as(deepest_stage: str | None) -> list[str]:
    if deepest_stage is None:
        return []
    deepest_rank = STAGE_RANK[deepest_stage]
    return [stage for stage in FUNNEL_STAGES if STAGE_RANK[stage] <= deepest_rank]


def fold_customer_funnel_state(
    events: list[CustomerFunnelEvent],
    initial: CustomerFunnelState | None = None,
) -> CustomerFunnelState:
    base = initial or CustomerFunnelState()
    confirmed_events = [event for event in events if event.decision_status == "confirmed"]
    all_milestones = [*base.actual_milestones]
    seen_stages = {event.stage for event in all_milestones}
    for event in sorted(confirmed_events, key=lambda item: (item.occurred_at or datetime.min, item.email_id)):
        if event.stage not in seen_stages:
            all_milestones.append(event)
            seen_stages.add(event.stage)

    active = confirmed_events[-1].stage if confirmed_events else base.current_active_stage
    deepest = _deepest([event.stage for event in all_milestones] + ([base.deepest_stage] if base.deepest_stage else []))
    return CustomerFunnelState(
        customer_id=base.customer_id or (confirmed_events[-1].customer_id if confirmed_events else None),
        current_active_stage=active,
        deepest_stage=deepest,
        actual_milestones=all_milestones,
        main_funnel_counted_as=_main_counted_as(deepest),
        actual_milestone_counted_as=sorted({event.stage for event in all_milestones}, key=lambda stage: STAGE_RANK[stage]),
        confirmed_event_count=base.confirmed_event_count + len(confirmed_events),
    )
