from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from src.agents.agt_email_labeling.src.pipeline.action_definitions import ACTION_NORMALIZE
from src.pipeline.business_label_taxonomy import get_business_label_taxonomy
from src.pipeline.review_rule_gates import p0_missing_requirements


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _actions_from_facts(facts: Mapping[str, Any]) -> list[dict[str, str]]:
    multi_label = _dict(facts.get("multi_label"))
    raw_extraction = _dict(facts.get("raw_extraction"))
    raw_actions = multi_label.get("actions")
    if raw_actions is None:
        raw_actions = raw_extraction.get("actions")

    actions: list[dict[str, str]] = []
    for item in _list(raw_actions):
        if not isinstance(item, dict):
            continue
        raw_code = item.get("action") or item.get("label")
        if not raw_code:
            continue
        canonical_code = ACTION_NORMALIZE.get(str(raw_code), str(raw_code))
        actions.append(
            {
                "action": canonical_code,
                "raw_action": str(raw_code),
                "evidence": str(item.get("evidence") or ""),
            }
        )
    return actions


def _current_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    multi_label = _dict(facts.get("multi_label"))
    raw_extraction = _dict(facts.get("raw_extraction"))
    return _dict(multi_label.get("current_facts") or raw_extraction.get("current_facts"))


_NEW_PRODUCT_MARKERS = (
    re.compile(r"\bnew\s+(?:product|model|series|solution|release|released|launch|launched|version)\b", re.I),
    re.compile(r"\bnew\s+(?:style|design|designs|arrival|arrivals|item|items|product\s+line)\b", re.I),
    re.compile(r"\b(?:launch|launching|launched|keeping\s+launch|show|showing|share|sharing|introduce|introducing)\s+.{0,50}\bnew\s+(?:designs?|arrivals?|products?)\b", re.I),
    re.compile(r"\b(?:updated|upgraded|advanced\s+and\s+updated)\s+products?\b", re.I),
    re.compile(r"\bimproved\s+version\b", re.I),
    re.compile(r"\b(?:developed|developing)\s+.{0,50}\b(?:motor|technology|model|series|frame|desk)\b", re.I),
    re.compile(r"\badvanced\s+.{0,40}\btechnology\b", re.I),
    re.compile(r"\b(?:newly|recently)\s+(?:released|launched|developed)\b", re.I),
    re.compile(r"\brecently\s+.{0,40}\barr(?:iv|vi)ed\b.{0,40}\bnew\s+item", re.I),
    re.compile(r"\brecently\s+.{0,40}\bdeveloped\b.{0,40}\b(?:model|models|series)\b", re.I),
    re.compile(r"\bwe\s+have\s+launched\b", re.I),
    re.compile(r"\bnew\s+economical\b", re.I),
    re.compile(r"\binnovative\s+version\b", re.I),
    re.compile(r"\blatest\s+(?:product|model|version|series|solution)\b", re.I),
    re.compile(r"新品|最新款|新款|新产品"),
)


def _has_strong_new_product_signal(*parts: Any) -> bool:
    text = "\n".join(str(part or "") for part in parts)
    return any(pattern.search(text) for pattern in _NEW_PRODUCT_MARKERS)


def _has_new_product_promotion_signal(
    *,
    facts: Mapping[str, Any],
    context: Mapping[str, Any],
    current_facts: Mapping[str, Any],
    matched_entries: list[dict[str, str]],
) -> bool:
    return current_facts.get("promotes_new_product_in_email") is True or _has_strong_new_product_signal(
        facts.get("email_subject"),
        context.get("email_subject"),
        context.get("email_text"),
        *(entry["evidence"] for entry in matched_entries),
    )


def _context_satisfied(requirement: str, sender_type: str, context: Mapping[str, Any]) -> bool:
    if requirement.startswith("sender_direction:"):
        required_direction = requirement.split(":", 1)[1]
        return required_direction == "both" or sender_type == required_direction
    if ":" not in requirement:
        return bool(context.get(requirement))
    key, expected = requirement.split(":", 1)
    actual = context.get(key)
    if expected == "true":
        return actual is True
    if expected == "false":
        return actual is False
    if expected.startswith(">="):
        return actual is not None and actual >= int(expected[2:])
    return str(actual) == expected


def _hard_context_contradicted(requirement: str, sender_type: str, context: Mapping[str, Any]) -> bool:
    if requirement.startswith("sender_direction:"):
        required_direction = requirement.split(":", 1)[1]
        return required_direction != "both" and sender_type != required_direction
    if ":" not in requirement:
        return False
    key, expected = requirement.split(":", 1)
    actual = context.get(key)
    if actual is None:
        return False
    if key == "customer_replied_before" and expected in {"true", "false"}:
        return actual is not (expected == "true")
    if key == "has_formal_order" and expected in {"true", "false"}:
        return actual is not (expected == "true")
    if key == "days_since_last_contact" and expected.startswith(">="):
        return actual < int(expected[2:])
    return False


def _has_hard_context_contradiction(
    *,
    required_context: list[str],
    sender_type: str,
    context: Mapping[str, Any],
) -> bool:
    return any(
        _hard_context_contradicted(requirement, sender_type, context)
        for requirement in required_context
    )


def _matched_entries(actions: list[dict[str, str]], trigger_actions: list[str]) -> list[dict[str, str]]:
    trigger_set = set(trigger_actions)
    return [
        action
        for action in actions
        if action["action"] in trigger_set
    ]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _evidence_items(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in entries:
        if not entry["evidence"]:
            continue
        items.append(
            {
                "source": "multi_label.actions",
                "action": entry["action"],
                "raw_action": entry["raw_action"],
                "text": entry["evidence"],
            }
        )
    return items


_PRODUCT_EVIDENCE_REQUIRED_LABELS = {
    "new_product_promotion",
    "cold_start_development",
    "customer_recall",
    "biz_send_quotation",
    "biz_product_detail_inquiry",
    "biz_price_inquiry",
    "biz_price_bargaining",
    "biz_requirement_clarification",
    "biz_spec_discussion",
    "biz_sample_request",
    "biz_sample_confirmation",
    "biz_sample_feedback",
}


def _product_evidence_summary(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "checked": context.get("product_evidence_checked") is True,
        "count": int(context.get("product_evidence_count") or 0),
        "matched_count": int(context.get("product_evidence_matched_count") or 0),
        "missing_in_erp_count": int(context.get("product_evidence_missing_in_erp_count") or 0),
        "category_missing_count": int(context.get("product_evidence_category_missing_count") or 0),
        "has_product_evidence": context.get("has_product_evidence") is True,
        "has_matched_product_evidence": context.get("has_matched_product_evidence") is True,
        "has_product_category_evidence": context.get("has_product_category_evidence") is True,
        "items": _list(context.get("product_evidence_items")),
    }


def _product_evidence_missing_requirements(
    *,
    spec_code: str,
    context: Mapping[str, Any],
) -> list[str]:
    if spec_code not in _PRODUCT_EVIDENCE_REQUIRED_LABELS:
        return []
    summary = _product_evidence_summary(context)
    missing: list[str] = []
    if not summary["checked"]:
        missing.append("product_evidence_checked:true")
        return missing
    if not summary["has_product_evidence"]:
        missing.append("product_evidence:present")
    if not summary["has_matched_product_evidence"]:
        missing.append("product_evidence:erp_matched")
    if not summary["has_product_category_evidence"]:
        missing.append("product_evidence:category_present")
    return missing


_NON_SUBSTANTIVE_ACTIONS = {
    "read_receipt",
    "auto_reply",
    "bounce",
    "short_reply",
    "other",
}
_SOFT_PI_OFFER_RE = re.compile(
    r"\bdo\s+you\s+need\b.{0,60}\bissue\s+pi\b|\bissue\s+pi\b.{0,60}\bto\s+you\s+first\b",
    re.I,
)
_GENERIC_COOPERATION_ONLY_RE = re.compile(
    r"\b(?:hope\s+to\s+cooperate|potential\s+cooperation)\b",
    re.I,
)


def _has_substantive_customer_action(actions: list[dict[str, str]], sender_type: str) -> bool:
    if sender_type != "customer":
        return False
    return any(
        action["action"] not in _NON_SUBSTANTIVE_ACTIONS and bool(action["evidence"])
        for action in actions
    )


def _substantive_customer_evidence(actions: list[dict[str, str]], sender_type: str) -> list[dict[str, str]]:
    if sender_type != "customer":
        return []
    entries = [
        action
        for action in actions
        if action["action"] not in _NON_SUBSTANTIVE_ACTIONS and bool(action["evidence"])
    ]
    return _evidence_items(entries[:1])


def _is_soft_pi_offer_action(action: dict[str, str]) -> bool:
    return action["action"] == "order_confirmation" and _SOFT_PI_OFFER_RE.search(action["evidence"] or "") is not None


def _has_explicit_quotation_followup(actions: list[dict[str, str]]) -> bool:
    return any(action["action"] == "quotation_followup" and bool(action["evidence"]) for action in actions)


def _has_quotation_followup_context(
    *,
    spec_code: str,
    actions: list[dict[str, str]],
    sender_type: str,
    context: Mapping[str, Any],
) -> bool:
    if spec_code != "follow_up_chase":
        return False
    action_codes = {action["action"] for action in actions}
    return (
        sender_type == "salesperson"
        and "follow_up_reminder" in action_codes
        and context.get("thread_had_quotation_before") is True
    )


def _excluded_condition_hits(
    *,
    excluded_conditions: list[str],
    actions: list[dict[str, str]],
    spec_code: str,
    sender_type: str,
    context: Mapping[str, Any],
) -> list[str]:
    action_codes = {action["action"] for action in actions}
    hits: list[str] = []
    for condition in excluded_conditions:
        if condition.startswith("late_stage_action:"):
            late_stage_action = condition.split(":", 1)[1]
            if (
                spec_code == "quotation_followup"
                and late_stage_action == "order_confirmation"
                and _has_explicit_quotation_followup(actions)
                and all(
                    action["action"] != "order_confirmation" or _is_soft_pi_offer_action(action)
                    for action in actions
                )
            ):
                continue
            if late_stage_action in action_codes:
                hits.append(condition)
            continue
        if condition.startswith("non_quotation_action:"):
            non_quotation_action = condition.split(":", 1)[1]
            if spec_code == "quotation_followup" and _has_explicit_quotation_followup(actions):
                continue
            if non_quotation_action in action_codes:
                hits.append(condition)
            continue
    return hits


def _has_confirmed_quotation_followup_decision(
    *,
    actions: list[dict[str, str]],
    sender_type: str,
    context: Mapping[str, Any],
) -> bool:
    for spec in get_business_label_taxonomy():
        if spec["code"] != "quotation_followup":
            continue
        if not _matched_entries(actions, spec["trigger_actions"]):
            return False
        for requirement in spec["required_context"]:
            if not _context_satisfied(requirement, sender_type, context):
                return False
        return not _excluded_condition_hits(
            excluded_conditions=spec["excluded_conditions"],
            actions=actions,
            spec_code=spec["code"],
            sender_type=sender_type,
            context=context,
        )
    return False


def _should_suppress_quotation_followup_candidate(
    *,
    matched_entries: list[dict[str, str]],
    excluded_hits: list[str],
) -> bool:
    matched_actions = {entry["action"] for entry in matched_entries}
    return (
        matched_actions == {"follow_up_reminder"}
        and bool(excluded_hits)
    )


def _should_suppress_after_sales_candidate(*, spec_code: str, p0_requirements: list[str]) -> bool:
    return (
        spec_code in {"biz_after_sales_support", "biz_after_sales_inquiry"}
        and "p0_post_delivery_support_requires_issue_complaint_return_defect_or_repair" in p0_requirements
    )


def _should_suppress_generic_online_meeting_candidate(
    *,
    spec_code: str,
    matched_entries: list[dict[str, str]],
    p0_requirements: list[str],
) -> bool:
    if spec_code != "biz_online_meeting":
        return False
    if "p0_meeting_arrangement_requires_time_place_schedule_or_agenda" not in p0_requirements:
        return False
    evidence_text = "\n".join(entry["evidence"].lower() for entry in matched_entries)
    return (
        "further talk" in evidence_text
        or "initial conversation" in evidence_text
    )


def _should_suppress_generic_distributor_proposal_candidate(
    *,
    spec_code: str,
    matched_entries: list[dict[str, str]],
    p0_requirements: list[str],
) -> bool:
    if spec_code != "biz_distributor_proposal":
        return False
    if "p0_partnership_distribution_requires_strategic_partnership_without_quantity_list" not in p0_requirements:
        return False
    evidence_text = "\n".join(entry["evidence"] for entry in matched_entries)
    return _GENERIC_COOPERATION_ONLY_RE.search(evidence_text) is not None


def derive_business_label_decisions(
    *,
    facts: Mapping[str, Any],
    sender_type: str,
    email_id: str | None = None,
    needs_review: bool = False,
    context: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    actions = _actions_from_facts(facts)
    current_facts = _current_facts(facts)
    context_values = {
        "email_subject": facts.get("email_subject") or "",
        "email_text": facts.get("email_text") or "",
        **dict(context or {}),
    }
    has_confirmed_quotation_followup = _has_confirmed_quotation_followup_decision(
        actions=actions,
        sender_type=sender_type,
        context=context_values,
    )
    out: list[dict[str, Any]] = []

    for spec in get_business_label_taxonomy():
        matched = _matched_entries(actions, spec["trigger_actions"])
        if spec["trigger_actions"] and not matched:
            continue
        if _has_hard_context_contradiction(
            required_context=spec["required_context"],
            sender_type=sender_type,
            context=context_values,
        ):
            continue
        if spec["code"] == "new_product_promotion" and not _has_new_product_promotion_signal(
            facts=facts,
            context=context_values,
            current_facts=current_facts,
            matched_entries=matched,
        ):
            continue
        if spec["code"] == "quotation_followup" and context_values.get("thread_had_quotation_before") is False:
            continue
        if spec["code"] == "follow_up_chase" and has_confirmed_quotation_followup:
            continue
        if spec["code"] == "first_contact" and not _has_substantive_customer_action(actions, sender_type):
            continue
        if spec["code"] == "biz_order_confirmation" and matched and all(
            _is_soft_pi_offer_action(entry) for entry in matched
        ):
            continue

        missing_requirements: list[str] = []
        for fact_code in spec["required_facts"]:
            if fact_code == "erp_order_confirmed" and context_values.get("has_formal_order") is True:
                continue
            if fact_code == "promotes_new_product_in_email" and _has_new_product_promotion_signal(
                facts=facts,
                context=context_values,
                current_facts=current_facts,
                matched_entries=matched,
            ):
                continue
            if current_facts.get(fact_code) is not True:
                missing_requirements.append(fact_code)
        for requirement in spec["required_context"]:
            if not _context_satisfied(requirement, sender_type, context_values):
                missing_requirements.append(requirement)

        if not spec["trigger_actions"] and missing_requirements:
            continue

        evidence = _evidence_items(matched)
        if spec["code"] == "first_contact" and not evidence:
            evidence = _substantive_customer_evidence(actions, sender_type)
        if spec["trigger_actions"] and not evidence:
            missing_requirements.append("evidence")
        excluded_hits = _excluded_condition_hits(
            excluded_conditions=spec["excluded_conditions"],
            actions=actions,
            spec_code=spec["code"],
            sender_type=sender_type,
            context=context_values,
        )
        if spec["code"] == "quotation_followup" and _should_suppress_quotation_followup_candidate(
            matched_entries=matched,
            excluded_hits=excluded_hits,
        ):
            continue
        missing_requirements.extend(excluded_hits)
        p0_requirements = p0_missing_requirements(
            label_code=spec["code"],
            matched_entries=matched,
            context=context_values,
        )
        if _should_suppress_after_sales_candidate(
            spec_code=spec["code"],
            p0_requirements=p0_requirements,
        ):
            continue
        if _should_suppress_generic_online_meeting_candidate(
            spec_code=spec["code"],
            matched_entries=matched,
            p0_requirements=p0_requirements,
        ):
            continue
        if _should_suppress_generic_distributor_proposal_candidate(
            spec_code=spec["code"],
            matched_entries=matched,
            p0_requirements=p0_requirements,
            ):
            continue
        missing_requirements.extend(p0_requirements)
        missing_requirements.extend(
            _product_evidence_missing_requirements(
                spec_code=spec["code"],
                context=context_values,
            )
        )

        status = "review_required" if missing_requirements else "confirmed"
        trigger_reason = spec["confirmed_rule"] if status == "confirmed" else spec["review_required_rule"]
        evidence_text = evidence[0]["text"] if evidence else ""
        product_evidence_summary = _product_evidence_summary(context_values)

        out.append(
            {
                "email_id": email_id,
                "code": spec["code"],
                "display": spec["display"],
                "decision_status": status,
                "evidence_text": evidence_text,
                "evidence_items": evidence,
                "trigger_reason": trigger_reason,
                "candidate_rule": spec["candidate_rule"],
                "confirmed_rule": spec["confirmed_rule"],
                "review_required_rule": spec["review_required_rule"],
                "rule_version": spec["rule_version"],
                "matched_actions": _unique([entry["action"] for entry in matched]),
                "matched_raw_actions": _unique([entry["raw_action"] for entry in matched]),
                "missing_requirements": _unique(missing_requirements),
                "no_evidence": not bool(evidence),
                "source_email_needs_review": bool(needs_review),
                "product_evidence_summary": product_evidence_summary,
            }
        )

    return out
