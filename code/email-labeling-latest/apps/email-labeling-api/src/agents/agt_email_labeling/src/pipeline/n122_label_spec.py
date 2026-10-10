from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPECTED_ACTIVE = {"O": 27, "I": 29, "C": 4}
EXPECTED_RULES = 38
EXPECTED_CONFIRMED_EXAMPLES = 16
REQUIRED_LABEL_FIELDS = ("code", "direction", "scene", "name", "definition", "trigger", "boundary", "priority")
EXPECTED_ACTIVE_CODES = frozenset(
    [f"O{i:02d}" for i in range(1, 28)]
    + [f"I{i:02d}" for i in range(1, 31) if i != 25]
    + [f"C{i:02d}" for i in range(1, 5)]
)


class N122SpecError(ValueError):
    pass


@dataclass(frozen=True)
class N122LabelSpec:
    payload: dict[str, Any]
    spec_hash: str

    @property
    def active_labels(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload["active_labels"])

    @property
    def active_codes(self) -> frozenset[str]:
        return frozenset(label["code"] for label in self.active_labels)

    @property
    def labels_by_code(self) -> dict[str, dict[str, Any]]:
        return {label["code"]: label for label in self.active_labels}


def _canonical_hash(payload: dict[str, Any]) -> str:
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_n122_spec(payload: dict[str, Any]) -> N122LabelSpec:
    if payload.get("schema_version") != "n122-label-spec-v8":
        raise N122SpecError("unsupported or missing schema_version")
    if payload.get("mapping_usage") != "reference_only_requires_original_email_judgment":
        raise N122SpecError("old/new mappings must remain reference-only")
    labels = payload.get("active_labels")
    if not isinstance(labels, list):
        raise N122SpecError("active_labels must be a list")
    codes = [str(label.get("code") or "") for label in labels]
    if len(codes) != len(set(codes)):
        raise N122SpecError("active labels contain duplicate codes")
    if set(codes) != EXPECTED_ACTIVE_CODES:
        raise N122SpecError("active labels contain unknown or empty codes")
    counts = {prefix: sum(code.startswith(prefix) for code in codes) for prefix in EXPECTED_ACTIVE}
    if counts != EXPECTED_ACTIVE:
        raise N122SpecError(f"active label counts mismatch: {counts}")
    if "I25" in codes or any(code.startswith("X") for code in codes):
        raise N122SpecError("I25 and X extension codes are not active")
    for label in labels:
        missing = [field for field in REQUIRED_LABEL_FIELDS if not str(label.get(field) or "").strip()]
        if missing:
            raise N122SpecError(f"label {label.get('code')} missing required fields: {missing}")
        prefix = label["code"][0]
        expected_direction = {"O": "业务员→客户", "I": "客户→业务员", "C": "通用"}[prefix]
        if label["direction"] != expected_direction:
            raise N122SpecError(f"label {label['code']} direction mismatch")

    rules = payload.get("rules")
    examples = payload.get("confirmed_examples")
    extensions = payload.get("inactive_extensions")
    differences = payload.get("old_new_mappings")
    if not isinstance(rules, list) or len(rules) != EXPECTED_RULES:
        raise N122SpecError(f"expected {EXPECTED_RULES} rules")
    rule_ids = [str(rule.get("rule_id") or "") for rule in rules]
    if len(rule_ids) != len(set(rule_ids)) or any(not rule_id for rule_id in rule_ids):
        raise N122SpecError("rule IDs must be unique and non-empty")
    rules_by_id = {rule["rule_id"]: rule for rule in rules}
    if not isinstance(examples, list) or len(examples) != EXPECTED_CONFIRMED_EXAMPLES:
        raise N122SpecError(f"expected {EXPECTED_CONFIRMED_EXAMPLES} confirmed examples")
    if not isinstance(extensions, list) or {entry.get("code") for entry in extensions} != {f"X{i:02d}" for i in range(1, 14)}:
        raise N122SpecError("inactive extensions must be X01-X13")
    if not isinstance(differences, list) or len(differences) != 43:
        raise N122SpecError("old/new mapping must preserve all 43 dictionary rows")
    for group_name, entries in (
        ("active_labels", labels), ("rules", rules), ("confirmed_examples", examples),
        ("old_new_mappings", differences), ("inactive_extensions", extensions),
    ):
        for entry in entries:
            source = entry.get("source")
            if not isinstance(source, dict) or not str(source.get("sheet") or "").strip() or not isinstance(source.get("row"), int) or source["row"] < 2:
                raise N122SpecError(f"{group_name} item missing sheet/row provenance")
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list) or not conflicts:
        raise N122SpecError("workbook conflict ledger is required")
    for conflict in conflicts:
        if not all(conflict.get(key) for key in ("conflict_id", "source_refs", "content", "impact", "resolution")):
            raise N122SpecError("conflict entry is incomplete")
        source_refs = conflict["source_refs"]
        if not isinstance(source_refs, list) or not source_refs:
            raise N122SpecError("conflict source_refs must be a non-empty list")
        for source_ref in source_refs:
            if (
                not isinstance(source_ref, dict)
                or not isinstance(source_ref.get("sheet"), str)
                or not source_ref["sheet"].strip()
                or not isinstance(source_ref.get("row"), int)
                or source_ref["row"] < 2
            ):
                raise N122SpecError("conflict source reference must include a valid sheet and row")
            if "rule_id" in source_ref and (
                not isinstance(source_ref["rule_id"], str) or not source_ref["rule_id"].strip()
            ):
                raise N122SpecError("conflict rule_id source reference must be non-empty")
            if "rule_id" in source_ref:
                referenced_rule = rules_by_id.get(source_ref["rule_id"])
                if referenced_rule is None or referenced_rule.get("source") != {
                    "sheet": source_ref["sheet"], "row": source_ref["row"]
                }:
                    raise N122SpecError("conflict rule_id does not match its rule provenance")
            if "email_id" in source_ref and (
                not isinstance(source_ref["email_id"], str) or not source_ref["email_id"].strip()
            ):
                raise N122SpecError("conflict email_id source reference must be non-empty")

    content = {key: value for key, value in payload.items() if key != "spec_hash"}
    computed_hash = _canonical_hash(content)
    declared_hash = payload.get("spec_hash")
    if declared_hash is not None and declared_hash != computed_hash:
        raise N122SpecError("spec_hash does not match content")
    normalized = dict(content)
    normalized["spec_hash"] = computed_hash
    return N122LabelSpec(payload=normalized, spec_hash=computed_hash)


def load_n122_spec(path: Path) -> N122LabelSpec:
    return validate_n122_spec(json.loads(path.read_text(encoding="utf-8")))
