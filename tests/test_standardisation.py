"""Unit tests for the standardisation modules — Step 7.

Per the brief and 02_IMPLEMENTATION_STEPS.md, tests are scoped to exactly
standardisation and validation; nothing else is required. Demographic
normalisation is proven here with synthetic values (ASSUMPTIONS.md D-11)
since the supplied samples arrive pre-redacted.
"""
from __future__ import annotations

from src.standardisation.demographic_normaliser import (
    is_redacted,
    normalise_age,
    normalise_date,
    normalise_gender,
    normalise_redactable_field,
    tokenise_identifier,
)
from src.standardisation.numeric_converter import (
    detect_compound_vital,
    detect_range_leaked_into_result,
    is_non_clinical_string_row,
    is_template_placeholder_row,
    parse_range,
    parse_result,
)
from src.standardisation.test_name_normaliser import TestNameNormaliser as NameNormaliser
from src.standardisation.unit_harmoniser import UnitHarmoniser
from src.config_loader import get_document_type_mappings, get_test_dictionary, get_unit_conversions


# --------------------------------------------------------------------------
# test_name_normaliser — FR-2.1
# --------------------------------------------------------------------------
class TestTestNameNormaliser:
    def setup_method(self):
        self.normaliser = NameNormaliser()

    def test_exact_match(self):
        result = self.normaliser.normalise("HEMOGLOBIN", "12.0")
        assert result.test_name_canonical == "HEMOGLOBIN"
        assert result.normalization_method == "exact"
        assert result.normalization_confidence == 1.0

    def test_exact_match_case_insensitive(self):
        result = self.normaliser.normalise("haemoglobin", "12.0")
        assert result.test_name_canonical == "HEMOGLOBIN"
        assert result.normalization_method == "exact"

    def test_fuzzy_match_on_ocr_truncated_name(self):
        # "aemoglobin" is a leading-character OCR truncation seen in the
        # real samples; it is listed as an exact variant, so this asserts
        # the dictionary + preprocessing resolve it deterministically.
        result = self.normaliser.normalise("aemoglobin", "9700")
        assert result.test_name_canonical == "HEMOGLOBIN"
        assert result.normalization_method in ("exact", "fuzzy")
        assert result.normalization_confidence >= 0.85

    def test_fuzzy_match_below_dictionary_coverage(self):
        # A near-miss NOT in the dictionary's variant list at all, close
        # enough to fuzzy-resolve above threshold.
        result = self.normaliser.normalise("Haemoglobinn", "10.0")
        assert result.normalization_method == "fuzzy"
        assert result.test_name_canonical == "HEMOGLOBIN"
        assert result.normalization_confidence >= 0.85

    def test_unresolved_below_threshold(self):
        result = self.normaliser.normalise("XYZ_NOT_A_REAL_TEST_QWERTY", "5")
        assert result.test_name_canonical is None
        assert result.normalization_method == "unresolved"
        assert "unresolved_test_name" in result.flags

    def test_embedded_value_extraction(self):
        result = self.normaliser.normalise("Na + (127)", "")
        assert result.test_name_canonical == "SODIUM"
        assert result.normalization_method == "extracted"
        assert result.normalization_confidence == 0.70
        assert result.extracted_value == 127.0

    def test_embedded_value_extraction_only_fires_when_result_empty(self):
        # Same shape, but result is already populated — extraction must not
        # override a genuine result.
        result = self.normaliser.normalise("Na + (127)", "140")
        assert result.extracted_value is None
        assert result.normalization_method != "extracted"

    def test_composite_multi_analyte_row_flagged_not_split(self):
        result = self.normaliser.normalise("LFT ( SGOT - 38, SGPT -14, ALP - 127)", "")
        assert result.is_composite_row is True
        assert "composite_multi_analyte_row" in result.flags

    def test_dictionary_collision_reconciled(self):
        # HAEMOGLOBIN and HEMOGLOBIN both claimed "Hb" in the supplied
        # reference dictionary (ASSUMPTIONS.md D-5) — both variants must
        # resolve to the single reconciled canonical entry.
        assert self.normaliser.normalise("HAEMOGLOBIN", "12").test_name_canonical == "HEMOGLOBIN"
        assert self.normaliser.normalise("HEMOGLOBIN", "12").test_name_canonical == "HEMOGLOBIN"

    def test_qualitative_test_flagged(self):
        result = self.normaliser.normalise("urine R/M", "normal")
        assert result.is_qualitative is True
        assert result.test_name_canonical == "URINE_ROUTINE"

    def test_narrative_test_flagged(self):
        result = self.normaliser.normalise("Fever", "Present")
        assert result.is_narrative is True

    def test_variant_with_method_annotation_matches_preprocessed_form(self):
        # Regression: dictionary variants that themselves contain a method
        # annotation ("PUS CELLS(light microscopy)/hpf") must be indexed
        # under the SAME preprocessing applied to incoming names, or an
        # exact match can never fire.
        result = self.normaliser.normalise("PUS CELLS(light microscopy)/hpf", "0")
        assert result.test_name_canonical == "URINE_PUS_CELLS"
        assert result.normalization_method == "exact"


# --------------------------------------------------------------------------
# numeric_converter — FR-2.3
# --------------------------------------------------------------------------
class TestNumericConverter:
    def test_plain_numeric(self):
        assert parse_result("9700", None).result_value == 9700.0

    def test_numeric_with_trailing_unit_text(self):
        p = parse_result("98.6 degree F", None)
        assert p.result_value == 98.6
        assert p.unit_fallback == "degree F"

    def test_numeric_with_percent_qualifier(self):
        p = parse_result("99% On Room Air", None)
        assert p.result_value == 99.0

    def test_high_low_qualifier_stripped(self):
        p = parse_result("H 0.7 %", None)
        assert p.result_value == 0.7
        assert "high_low_qualifier_stripped" in p.flags

    def test_range_leaked_into_result_detected(self):
        assert detect_range_leaked_into_result("1.5-4.5", "1.5-4.5") is True
        p = parse_result("1.5-4.5", "1.5-4.5")
        assert p.result_value is None
        assert "range_leaked_into_result" in p.flags

    def test_free_text_result_is_invalid_numeric(self):
        p = parse_result("B/L AE+", None)
        assert p.result_value is None
        assert "non_numeric_free_text" in p.flags

    def test_range_parses_low_high(self):
        r = parse_range("4000-10000")
        assert r.range_low == 4000.0
        assert r.range_high == 10000.0

    def test_range_single_value_is_malformed(self):
        r = parse_range("1.31")
        assert r.range_low is None
        assert r.range_high is None
        assert "range_malformed_single_value" in r.flags

    def test_range_with_to_separator(self):
        r = parse_range("1.005 TO 1.030")
        assert r.range_low == 1.005
        assert r.range_high == 1.030

    def test_template_placeholder_row_detected(self):
        junk_cfg = get_document_type_mappings()["junk_detection"]
        row = {"test_name": "test_name", "result": "result", "range": "range",
               "unit": "unit", "test_analytics": "low/normal/high"}
        assert is_template_placeholder_row(row, junk_cfg) is True

    def test_real_row_not_flagged_as_template(self):
        junk_cfg = get_document_type_mappings()["junk_detection"]
        row = {"test_name": "Haemoglobin", "result": "10.7", "range": "12-16",
               "unit": "g/dL", "test_analytics": "normal"}
        assert is_template_placeholder_row(row, junk_cfg) is False

    def test_non_clinical_string_in_test_name(self):
        junk_cfg = get_document_type_mappings()["junk_detection"]
        assert is_non_clinical_string_row("NEGATIVE", junk_cfg) is True
        assert is_non_clinical_string_row("Haemoglobin", junk_cfg) is False

    def test_compound_vital_split(self):
        td = get_test_dictionary()
        result = detect_compound_vital("BP", "100/60 mmHg", td)
        assert result is not None
        values = dict(result)
        assert values["BLOOD_PRESSURE_SYSTOLIC"] == 100.0
        assert values["BLOOD_PRESSURE_DIASTOLIC"] == 60.0

    def test_non_compound_single_value_not_split(self):
        td = get_test_dictionary()
        assert detect_compound_vital("Pulse", "114/min", td) is None


# --------------------------------------------------------------------------
# unit_harmoniser — FR-2.4
# --------------------------------------------------------------------------
class TestUnitHarmoniser:
    def setup_method(self):
        self.harmoniser = UnitHarmoniser()

    def test_alias_normalises_without_changing_value(self):
        r = self.harmoniser.harmonise("gm/di", "HEMOGLOBIN", 10.7)
        assert r.unit_canonical == "g/dL"
        assert r.result_value == 10.7

    def test_conversion_factor_applied(self):
        # The brief's own worked example: mil/cu.mm -> cells/cu.mm, x1e6
        r = self.harmoniser.harmonise("mil/cu.mm", "WHITE_BLOOD_CELL_COUNT", 5.45)
        assert r.unit_canonical == "cells/cu.mm"
        assert r.result_value == 5_450_000.0
        assert "unit_converted" in r.flags

    def test_missing_unit_assumes_canonical(self):
        r = self.harmoniser.harmonise("", "HEMOGLOBIN", 10.7)
        assert r.unit_canonical == "g/dL"
        assert "unit_missing_assumed_canonical" in r.flags

    def test_invalid_unit_that_is_actually_a_test_name(self):
        r = self.harmoniser.harmonise("atelet Count", "PLATELET_COUNT", 1.31)
        assert "invalid_unit" in r.flags
        assert r.unit_canonical == "lakhs/cu.mm"

    def test_unit_test_mismatch_flagged_not_corrected(self):
        # The real aemoglobin/WBC row: HEMOGLOBIN with cell/cu.mm unit.
        r = self.harmoniser.harmonise("cell/cu.mm", "HEMOGLOBIN", 9700.0, 4000.0, 10000.0)
        assert "unit_test_mismatch" in r.flags
        assert r.result_value == 9700.0        # unchanged — never auto-corrected
        assert r.unit_canonical == "cell/cu.mm"  # original retained, not overwritten


# --------------------------------------------------------------------------
# demographic_normaliser — FR-2.5 (proven by synthetic values, D-11)
# --------------------------------------------------------------------------
class TestDemographicNormaliser:
    def test_redaction_detected(self):
        assert is_redacted("[AGE REDACTED]") is True
        assert is_redacted("[UHID-REDACTED]") is True
        assert is_redacted("45") is False

    def test_redactable_field_stores_null_not_literal(self):
        result = normalise_redactable_field("[PATIENT NAME REDACTED]")
        assert result.value is None
        assert result.redacted is True

    def test_age_composite_format(self):
        r = normalise_age("33Y11M265D")
        assert r.age_years == round(33 + 11 / 12 + 265 / 365.25, 2)

    def test_age_plain_years(self):
        assert normalise_age("45").age_years == 45.0

    def test_age_redacted(self):
        r = normalise_age("[AGE REDACTED]")
        assert r.age_years is None
        assert r.redacted is True

    def test_gender_variants_canonicalise(self):
        for raw in ("M", "Male", "m", "1"):
            assert normalise_gender(raw).value == "MALE"
        for raw in ("F", "Female", "f", "2"):
            assert normalise_gender(raw).value == "FEMALE"

    def test_gender_redacted(self):
        assert normalise_gender("[GENDER REDACTED]").value is None

    def test_date_dd_mm_yyyy(self):
        assert normalise_date("09-10-2025").iso_date == "2025-10-09"

    def test_date_dd_mon_yyyy(self):
        assert normalise_date("07-Oct-2025").iso_date == "2025-10-07"

    def test_date_placeholder_rejected(self):
        r = normalise_date("DD/MM/YYYY")
        assert r.iso_date is None
        assert "placeholder_date" in r.flags

    def test_date_junk_not_coerced(self):
        r = normalise_date("LAB10945")
        assert r.iso_date is None
        assert "date_unparseable" in r.flags

    def test_tokenise_identifier_is_stable_and_not_reversible_literal(self):
        h1 = tokenise_identifier("John Doe")
        h2 = tokenise_identifier("John Doe")
        assert h1 == h2
        assert h1 != "John Doe"

    def test_tokenise_identifier_skips_redacted(self):
        assert tokenise_identifier("[UHID-REDACTED]") is None
