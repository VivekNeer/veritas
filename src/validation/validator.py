"""Validation & analytics classification — FR-3.1 – FR-3.4.

Implements the precedence cascade from `config/reference_ranges.yaml`
exactly, evaluated top to bottom with the first match winning. Order
matters: a physiologically implausible value must be caught as `Outlier`
before it could be reported merely `Above Range`.

Configured reference ranges are the authority over the source's own `range`
field, which is demonstrably unreliable in the sample data — see
ASSUMPTIONS.md D-8.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config_loader import get_reference_ranges


@dataclass
class ValidationInput:
    test_name_canonical: str | None
    result_value: float | None
    is_junk_row: bool = False
    unit_test_mismatch: bool = False
    result_equals_range_text: bool = False
    is_narrative: bool = False
    is_qualitative: bool = False


@dataclass
class ValidationResult:
    test_analytics: str
    flags: list[str] = field(default_factory=list)
    source_range_disagrees: bool = False


class Validator:
    def __init__(self, reference_ranges: dict | None = None):
        cfg = reference_ranges or get_reference_ranges()
        self.ranges = cfg["ranges"]
        self.defaults = cfg["defaults"]["when_no_range_defined"]
        self.comparison_cfg = cfg["source_range_comparison"]

    def classify(self, inp: ValidationInput) -> ValidationResult:
        if inp.is_junk_row:
            return ValidationResult("Invalid", ["template_placeholder_or_non_clinical_row"])

        if inp.test_name_canonical is None:
            return ValidationResult("Invalid", ["unresolved_test_name"])

        if inp.unit_test_mismatch:
            return ValidationResult("Invalid", ["unit_test_mismatch"])

        if inp.result_equals_range_text:
            return ValidationResult("Invalid", ["range_leaked_into_result"])

        if inp.is_narrative:
            return ValidationResult("Not Applicable")

        if inp.is_qualitative:
            return ValidationResult("Qualitative")

        if inp.result_value is None:
            return ValidationResult("Invalid", ["result_value_null"])

        ref = self.ranges.get(inp.test_name_canonical)
        if ref is None:
            flags = [self.defaults["flag"]]
            return ValidationResult("Invalid", flags)

        v = inp.result_value
        if v < ref["outlier_low"] or v > ref["outlier_high"]:
            return ValidationResult("Outlier", ["physiologically_implausible"])
        if v < ref["low"]:
            return ValidationResult("Below Range")
        if v > ref["high"]:
            return ValidationResult("Above Range")
        return ValidationResult("Within Range")

    def compare_source_range(self, test_name_canonical: str | None, source_low: float | None,
                              source_high: float | None) -> bool:
        """Configured ranges are the authority (D-8), but systematic
        disagreement with the source's own range is itself a quality signal,
        surfaced per-source in the UI (FR-5.4)."""
        if not self.comparison_cfg.get("enabled") or test_name_canonical is None:
            return False
        ref = self.ranges.get(test_name_canonical)
        if ref is None or source_low is None or source_high is None:
            return False
        tolerance = self.comparison_cfg["tolerance_pct"] / 100.0
        low_ok = abs(source_low - ref["low"]) <= tolerance * max(abs(ref["low"]), 1e-9)
        high_ok = abs(source_high - ref["high"]) <= tolerance * max(abs(ref["high"]), 1e-9)
        return not (low_ok and high_ok)
