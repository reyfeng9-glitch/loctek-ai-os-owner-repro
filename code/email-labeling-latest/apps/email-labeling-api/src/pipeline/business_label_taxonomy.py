from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agents.agt_email_labeling.src.pipeline.action_definitions import (
    ACTION_NORMALIZE,
    ACTION_PRIORITY,
)
from src.agents.agt_email_labeling.src.pipeline.business_labels import business_labels_for_display


BUSINESS_LABEL_TAXONOMY_VERSION = "business_label_taxonomy_v2026_07_08_product_evidence_gate"
BUSINESS_LABEL_TAXONOMY_UPDATED_AT = "2026-07-08T00:00:00+00:00"


def _aliases_by_action() -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for raw_code, canonical_code in ACTION_NORMALIZE.items():
        aliases.setdefault(canonical_code, []).append(raw_code)
    return {
        code: sorted(set(values))
        for code, values in aliases.items()
    }


def _priority(trigger_actions: list[str]) -> int:
    if not trigger_actions:
        return 0
    return max(ACTION_PRIORITY[action] for action in trigger_actions)


def _required_str(item: Mapping[str, Any], key: str) -> str:
    value = item[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"business label taxonomy field {key} must be a non-empty string")
    return value


def _required_str_list(item: Mapping[str, Any], key: str) -> list[str]:
    value = item[key]
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise TypeError(f"business label taxonomy field {key} must be a list[str]")
    return list(value)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _context_for(item: Mapping[str, Any]) -> list[str]:
    direction = _required_str(item, "direction")
    required_context = _required_str_list(item, "required_context")
    direction_context = [] if direction == "both" else [f"sender_direction:{direction}"]
    return _unique([*direction_context, *required_context])


def _build_taxonomy_item(item: Mapping[str, Any], aliases: Mapping[str, list[str]]) -> dict[str, Any]:
    trigger_actions = _required_str_list(item, "trigger_actions")
    trigger_action_aliases = {
        action: list(aliases.get(action, [action]))
        for action in trigger_actions
    }
    return {
        "code": _required_str(item, "id"),
        "display": _required_str(item, "display"),
        "group": _required_str(item, "group"),
        "direction": _required_str(item, "direction"),
        "priority": _priority(trigger_actions),
        "trigger_actions": trigger_actions,
        "trigger_action_aliases": trigger_action_aliases,
        "required_facts": _required_str_list(item, "required_facts"),
        "required_context": _context_for(item),
        "excluded_conditions": _required_str_list(item, "excluded_conditions"),
        "evidence_policy": (
            "confirmed 必须有真实 action/fact/context/evidence 支撑；缺 evidence、缺必需事实、"
            "缺必需上下文或命中 needs_review 时只能输出 review_required。"
        ),
        "compatible_labels": _required_str_list(item, "compatible_labels"),
        "conflict_labels": _required_str_list(item, "conflict_labels"),
        "candidate_rule": _required_str(item, "condition_text"),
        "confirmed_rule": _required_str(item, "rationale"),
        "review_required_rule": (
            "触发动作命中但 evidence、required_facts、required_context、排除条件或复核状态任一未满足时，"
            "保留候选并进入复核，不输出 confirmed。"
        ),
        "rule_version": BUSINESS_LABEL_TAXONOMY_VERSION,
        "positive_examples": [],
        "negative_examples": [],
        "boundary_examples": [],
        "coverage_count": None,
        "precision": None,
        "recall": None,
        "review_overturn_rate": None,
        "candidate_to_confirmed_rate": None,
        "confusion_pairs": [],
        "no_evidence_hit_count": None,
        "last_updated_at": BUSINESS_LABEL_TAXONOMY_UPDATED_AT,
    }


def get_business_label_taxonomy() -> list[dict[str, Any]]:
    aliases = _aliases_by_action()
    return [
        _build_taxonomy_item(item, aliases)
        for item in business_labels_for_display()
    ]


def get_business_label_spec(code: str) -> dict[str, Any] | None:
    for item in get_business_label_taxonomy():
        if item["code"] == code:
            return item
    return None


def business_label_codes() -> set[str]:
    return {item["code"] for item in get_business_label_taxonomy()}
