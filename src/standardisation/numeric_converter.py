"""Numeric conversion — FR-2.3.

Populates `result_value` (nullable float) while always preserving
`result_text` verbatim. Also parses the source `range` field into
`range_low` / `range_high`, always retaining `range_text`.

Every case here is drawn from the real, OCR-damaged sample data — see
Part 1 §2.5 of the (local, gitignored) planning notes and ASSUMPTIONS.md D-3.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HL_QUALIFIER_PREFIX = re.compile(r"^\s*[HL]\s+(?=\d)")
_LEADING_NUMBER = re.compile(r"^\s*([+-]?\d+\.?\d*)\s*%?\s*(.*)$")
_ALL_NUMBERS = re.compile(r"\d+\.?\d*")
_COMPOUND_TWO_NUMBERS = re.compile(r"^\s*([+-]?\d+\.?\d*)\s*/\s*([+-]?\d+\.?\d*)\s*([^\d/]*)$")

NULL_EQUIVALENTS = {"", "N/A", "NA", "na", "NIL", "nil", "-", "--", "null", "None", "N/a"}


@dataclass
class ResultParse:
    result_value: float | None
    result_text: str | None
    unit_fallback: str | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class RangeParse:
    range_low: float | None
    range_high: float | None
    range_text: str | None
    flags: list[str] = field(default_factory=list)


def _is_null_equivalent(s: str | None) -> bool:
    return s is None or s.strip() in NULL_EQUIVALENTS


def parse_range(raw_range: str | None) -> RangeParse:
    text = raw_range if raw_range not in (None,) else None
    if _is_null_equivalent(raw_range):
        return RangeParse(None, None, text)

    numbers = _ALL_NUMBERS.findall(raw_range)
    if len(numbers) == 2:
        low, high = float(numbers[0]), float(numbers[1])
        if low > high:
            low, high = high, low
        return RangeParse(low, high, text)
    if len(numbers) == 1:
        return RangeParse(None, None, text, flags=["range_malformed_single_value"])
    return RangeParse(None, None, text, flags=["range_unparseable"] if numbers or raw_range.strip() else [])


def detect_range_leaked_into_result(raw_result: str | None, raw_range: str | None) -> bool:
    """`result: "1.5-4.5"` where the range itself leaked into the result field."""
    if raw_result is None or raw_range is None:
        return False
    return raw_result.strip() != "" and raw_result.strip() == raw_range.strip() and "-" in raw_result


def parse_result(raw_result: str | None, raw_range: str | None = None) -> ResultParse:
    text = raw_result
    if _is_null_equivalent(raw_result):
        return ResultParse(None, text)

    if detect_range_leaked_into_result(raw_result, raw_range):
        return ResultParse(None, text, flags=["range_leaked_into_result"])

    # Lab-convention High/Low flag prefixed to the value ("H 0.7 %", "L 3.8
    # million/cumm") — strip it before numeric extraction, but flag it since
    # it duplicates the analytics classification we compute independently.
    working = raw_result
    hl_flag = []
    if _HL_QUALIFIER_PREFIX.match(working):
        working = _HL_QUALIFIER_PREFIX.sub("", working)
        hl_flag = ["high_low_qualifier_stripped"]

    m = _LEADING_NUMBER.match(working)
    if not m:
        # Pure free text, no leading numeric — e.g. "B/L AE+", "NEGATIVE".
        return ResultParse(None, text, flags=["non_numeric_free_text"])

    number_str, remainder = m.groups()
    try:
        value = float(number_str)
    except ValueError:
        return ResultParse(None, text, flags=["non_numeric_free_text"])

    remainder = remainder.strip()
    had_percent = "%" in working[: len(number_str) + 2]
    unit_fallback = "%" if had_percent else (remainder or None)

    flags = list(hl_flag)
    if remainder and not had_percent:
        flags.append("qualifier_or_unit_text_in_result")
    return ResultParse(value, text, unit_fallback=unit_fallback, flags=flags)


def is_template_placeholder_row(row: dict, junk_cfg: dict) -> bool:
    """A row is a template placeholder if any field's value equals its own
    field name — the unfilled extraction template shipped as-is."""
    self_ref = junk_cfg.get("self_referential_values", {})
    for field_name, literal_values in self_ref.items():
        value = row.get(field_name)
        if value is not None and str(value) in literal_values:
            return True
    return False


def is_non_clinical_string_row(raw_test_name: str | None, junk_cfg: dict) -> bool:
    """Catches OCR bleed from letterheads, footers, and adjacent columns
    landing directly in the test_name field — e.g. "lac/cmm" (a unit
    string), "NEGATIVE" / "ABSENT" (a result, not a name), or a lab's
    marketing tagline. These are junk rows, not unresolved test names, and
    must not be counted against NFR-4.1's coverage target."""
    if not raw_test_name:
        return False
    known = {s.lower() for s in junk_cfg.get("non_clinical_strings", [])}
    return raw_test_name.strip().lower() in known


def detect_compound_vital(raw_test_name: str, raw_result: str | None, test_dictionary: dict) -> list[tuple[str, float]] | None:
    """Rows like `test_name: "BP", result: "100/60 mmHg"` map to TWO
    canonical tests (systolic/diastolic) via `derived_from` in
    test_dictionary.yaml, rather than being coerced into one float.

    Returns a list of (canonical_test_name, value) if this row's raw name
    matches a configured compound source, and its result is genuinely two
    numbers joined by the configured delimiter. Returns None otherwise, in
    which case the row is treated as an ordinary single-value result.
    """
    if not raw_result or not raw_test_name:
        return None

    components: dict[str, tuple[str, str]] = {}  # component -> (canonical, delimiter)
    for canonical, spec in test_dictionary.get("tests", {}).items():
        derived = spec.get("derived_from")
        if derived and derived.get("source_variant", "").lower() == raw_test_name.strip().lower():
            components[derived["component"]] = (canonical, derived["delimiter"])

    if not components:
        return None

    delimiter = next(iter(components.values()))[1]
    if delimiter != "/":
        return None  # only "/" compound splitting is implemented

    m = _COMPOUND_TWO_NUMBERS.match(raw_result)
    if not m:
        return None

    first, second, _trailing = m.groups()
    ordered = sorted(components.items())  # deterministic: diastolic, systolic
    values = {"systolic": float(first), "diastolic": float(second)}
    return [(canonical, values[component]) for component, (canonical, _d) in ordered if component in values]
