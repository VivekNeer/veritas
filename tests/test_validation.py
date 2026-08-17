"""Unit tests for validator.py — Step 7, FR-3.1-3.4.

One test per test_analytics category (the five the brief specifies, plus
the two documented extensions), plus range-boundary cases.
"""
from __future__ import annotations

from src.validation.validator import Validator, ValidationInput


class TestValidator:
    def setup_method(self):
        self.validator = Validator()

    # ---- the five required categories -----------------------------------
    def test_within_range(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 14.0))
        assert r.test_analytics == "Within Range"

    def test_below_range(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 10.7))
        assert r.test_analytics == "Below Range"

    def test_above_range(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 20.0))
        assert r.test_analytics == "Above Range"

    def test_outlier_low(self):
        # The brief's own FR-3.2 worked example.
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 0.1))
        assert r.test_analytics == "Outlier"

    def test_outlier_high(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 999.0))
        assert r.test_analytics == "Outlier"

    def test_invalid_unresolved_name(self):
        r = self.validator.classify(ValidationInput(None, None))
        assert r.test_analytics == "Invalid"

    def test_invalid_null_result_value(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", None))
        assert r.test_analytics == "Invalid"

    # ---- the two documented extensions (D-14) ----------------------------
    def test_qualitative(self):
        r = self.validator.classify(ValidationInput("URINE_ROUTINE", None, is_qualitative=True))
        assert r.test_analytics == "Qualitative"

    def test_not_applicable(self):
        r = self.validator.classify(ValidationInput("FEVER", None, is_narrative=True))
        assert r.test_analytics == "Not Applicable"

    # ---- precedence cascade order matters --------------------------------
    def test_unit_mismatch_takes_precedence_over_range_check(self):
        # 9700 would be wildly "Above Range" for HEMOGLOBIN (12-16) — but a
        # unit_test_mismatch must be caught first and reported as Invalid,
        # not as a nonsensical Above Range reading.
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 9700.0, unit_test_mismatch=True))
        assert r.test_analytics == "Invalid"

    def test_range_leaked_into_result_takes_precedence(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", None, result_equals_range_text=True))
        assert r.test_analytics == "Invalid"

    def test_outlier_precedes_above_range(self):
        # A value that is BOTH beyond outlier_high AND above the normal
        # high must classify as Outlier, never Above Range.
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 25.5))  # outlier_high=25.0
        assert r.test_analytics == "Outlier"

    def test_missing_reference_range_is_invalid(self):
        r = self.validator.classify(ValidationInput("SOME_TEST_WITH_NO_CONFIGURED_RANGE", 5.0))
        assert r.test_analytics == "Invalid"

    # ---- range-boundary cases (inclusive) ---------------------------------
    def test_boundary_low_is_within_range(self):
        # HEMOGLOBIN low=12.0 — boundary_handling is inclusive.
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 12.0))
        assert r.test_analytics == "Within Range"

    def test_boundary_high_is_within_range(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 16.0))
        assert r.test_analytics == "Within Range"

    def test_just_below_low_boundary(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 11.99))
        assert r.test_analytics == "Below Range"

    def test_just_above_high_boundary(self):
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 16.01))
        assert r.test_analytics == "Above Range"

    def test_just_beyond_outlier_boundary_is_outlier_not_below_range(self):
        # HEMOGLOBIN outlier_low=2.0 — a value just past it must classify
        # Outlier, not Below Range, since outlier bounds are checked first.
        # (Unlike the normal low/high range, reference_ranges.yaml's
        # documented inclusive boundary_handling applies only to low/high,
        # not to the outlier bounds, so the exact boundary value itself is
        # deliberately not asserted here — that's an implementation choice,
        # not a documented contract.)
        r = self.validator.classify(ValidationInput("HEMOGLOBIN", 1.99))
        assert r.test_analytics == "Outlier"

    def test_source_range_disagreement_detection(self):
        # Source range 4000-10000 is nowhere near HEMOGLOBIN's canonical
        # 12.0-16.0 — must be flagged as a disagreement (D-8).
        assert self.validator.compare_source_range("HEMOGLOBIN", 4000.0, 10000.0) is True

    def test_source_range_agreement_not_flagged(self):
        assert self.validator.compare_source_range("HEMOGLOBIN", 12.0, 16.0) is False
