"""Orchestration — Step 5. Wires ingestion -> standardisation -> validation
-> storage and emits a run summary for the UI.

Structured logging carries `correlation_id` on every line (NFR-5.2) so a
single record's journey is traceable end to end — the payload already
supplies both `traceId` and `correlationId`; the pipeline propagates them
rather than generating its own.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.config_loader import (
    get_document_type_mappings,
    get_reference_ranges,
    get_test_dictionary,
    get_unit_conversions,
)
from src.ingestion.gcs_reader import RawRecord, ingest
from src.path_utils import get_path
from src.standardisation.demographic_normaliser import (
    normalise_age,
    normalise_date,
    normalise_gender,
    normalise_redactable_field,
)
from src.standardisation.numeric_converter import (
    detect_compound_vital,
    is_non_clinical_string_row,
    is_template_placeholder_row,
    parse_range,
    parse_result,
)
from src.standardisation.test_name_normaliser import TestNameNormaliser
from src.standardisation.unit_harmoniser import UnitHarmoniser
from src.storage.bigquery_loader import Loader
from src.validation.validator import Validator, ValidationInput

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("veritas.pipeline")


@dataclass
class RunSummary:
    files_processed_local_mode: bool
    files_read: int = 0
    records_seen: int = 0
    rows_written: int = 0
    dead_letters: int = 0
    flagged: int = 0
    unresolved_test_names: int = 0
    duplicates_suppressed: int = 0
    by_analytics: dict = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    backend: str | None = None

    def to_dict(self) -> dict:
        return {**self.__dict__}


NON_FLAGGED_ANALYTICS = {"Within Range", "Not Applicable"}

# Every emitted row carries this full canonical column set (nulled where not
# applicable) rather than a per-record-type subset. This is what makes the
# long-format table actually uniform in a warehouse: a lab_report row and a
# discharge_summary row are the same shape, just sparsely populated — see
# ASSUMPTIONS.md D-18 for the consolidated-column reasoning.
CANONICAL_COLUMNS = [
    # lineage
    "document_id", "record_type", "file_source", "trace_id", "correlation_id",
    "source_system", "claim_no", "nt_code", "consumer_client_id",
    "destination_identifier", "case_id", "ingested_at", "row_seq",
    # standardisation core (lab_report rows only)
    "test_name_original", "test_name_canonical", "result_value", "result_text",
    "unit_canonical", "unit_original", "range_low", "range_high", "range_text",
    "test_analytics", "source_test_analytics", "normalization_method",
    "normalization_confidence", "page_no", "flags",
    # patient / clinical context
    "patient_name", "age_years", "age_text", "age_flags", "gender", "uhid",
    "hospital_name", "lab_or_hospital_name", "doctor_name", "bill_date",
    "reports_date", "admission_date", "discharge_date", "diagnosis",
    "brief_history", "general_examinations", "recommendations",
    "hospital_address", "ward", "post_discharge_advice",
    "course_during_hospitalisation",
    # medication (discharge_summary rows only)
    "medicine", "dose", "frequency", "medicine_type", "other_med_inj_investigations",
    # audit trail (FR-4.3) — the full classifier payload this record came
    # from, so any row's decision can be traced back to source without
    # re-opening the original file. Duplicated per row of the same record;
    # a real BigQuery table absorbs this trivially at this data volume.
    "raw_json",
]


def finalize_row(row: dict) -> dict:
    """Ensures every row carries exactly CANONICAL_COLUMNS — fills gaps with
    None and drops anything unexpected, so the storage layer always sees a
    uniform schema regardless of which record type produced the row."""
    return {col: row.get(col) for col in CANONICAL_COLUMNS}


class Pipeline:
    def __init__(self):
        self.mapping = get_document_type_mappings()
        self.test_dictionary = get_test_dictionary()
        self.normaliser = TestNameNormaliser(self.test_dictionary)
        self.harmoniser = UnitHarmoniser(
            get_unit_conversions(), self.test_dictionary, get_reference_ranges()
        )
        self.validator = Validator(get_reference_ranges())
        self.junk_cfg = self.mapping["junk_detection"]

    def _dead_letter(self, r: RawRecord, reason: str, raw_item) -> dict:
        return {
            "source_file": r.source_file, "document_id": r.document_id,
            "error_reason": reason, "raw_json": str(raw_item),
            "source_system": r.meta.get("source_system"),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }

    # ---------------------------------------------------------- header/meta
    def _extract_header(self, payload: dict, profile: dict) -> dict:
        header_cfg = profile.get("header", {})
        source = get_path(payload, header_cfg.get("source_path", ""))
        source = source if isinstance(source, dict) else {}
        out = {}
        for src_field, out_field in header_cfg.get("field_map", {}).items():
            out[out_field] = source.get(src_field)
        return out

    def _lineage_fields(self, r: RawRecord) -> dict:
        meta = r.meta
        return {
            "document_id": r.document_id,
            "record_type": r.record_type,
            "file_source": r.source_file,
            "trace_id": r.trace_id,
            "correlation_id": r.correlation_id,
            "source_system": meta.get("source_system") or self.mapping["envelope"]["source_identity_fallback"],
            "claim_no": meta.get("claim_no"),
            "nt_code": meta.get("nt_code"),
            "consumer_client_id": meta.get("consumer_client_id"),
            "destination_identifier": meta.get("destination_identifier"),
            "case_id": meta.get("case_id"),
            "ingested_at": r.ingested_at,
            "raw_json": json.dumps(r.payload, default=str),
        }

    def _normalise_demographics(self, header: dict) -> dict:
        out = dict(header)
        if "age_text" in out:
            age_result = normalise_age(out.get("age_text"))
            out["age_years"] = age_result.age_years
            out["age_flags"] = ",".join(age_result.flags) or None
        if "gender" in out:
            gender_result = normalise_gender(out.get("gender"))
            out["gender"] = gender_result.value
        for field_name in ("patient_name", "uhid", "doctor_name", "hospital_name",
                           "lab_or_hospital_name", "hospital_address"):
            if field_name in out:
                out[field_name] = normalise_redactable_field(out.get(field_name)).value
        for date_field in ("reports_date", "bill_date", "admission_date", "discharge_date"):
            if date_field in out:
                out[date_field] = normalise_date(out.get(date_field)).iso_date
        return out

    # -------------------------------------------------------------- lab_report
    def _process_lab_report(self, r: RawRecord, lineage: dict) -> tuple[list[dict], list[dict]]:
        profile = self.mapping["profiles"]["lab_report"]
        header = self._extract_header(r.payload, profile)
        header = self._normalise_demographics(header)

        line_items_cfg = profile["line_items"]
        items = get_path(r.payload, line_items_cfg["source_path"]) or []
        field_map = line_items_cfg["field_map"]

        rows: list[dict] = []
        dead_letters: list[dict] = []

        for raw_item in items:
            item = {out: raw_item.get(src) for src, out in field_map.items()}
            junk_probe = {
                "test_name": item.get("test_name_original"),
                "result": item.get("result_text_original"),
                "range": item.get("range_text_original"),
                "unit": item.get("unit_original"),
                "test_analytics": item.get("source_test_analytics"),
            }
            if is_template_placeholder_row(junk_probe, self.junk_cfg):
                dead_letters.append(self._dead_letter(r, self.junk_cfg["drop_reason"], raw_item))
                continue

            raw_name = item.get("test_name_original") or ""
            raw_result = item.get("result_text_original")
            raw_range = item.get("range_text_original")
            raw_unit = item.get("unit_original")

            if is_non_clinical_string_row(raw_name, self.junk_cfg):
                dead_letters.append(self._dead_letter(r, "non_clinical_string_in_test_name", raw_item))
                continue

            compound = detect_compound_vital(raw_name, raw_result, self.test_dictionary)
            range_parse = parse_range(raw_range)

            if compound:
                for canonical, value in compound:
                    rows.append(self._build_test_row(
                        lineage, header, item, test_name_canonical=canonical,
                        normalization_method="derived_compound_split", normalization_confidence=1.0,
                        result_value=value, range_parse=range_parse, extra_flags=["compound_vital_split"],
                        is_qualitative=False, is_narrative=False,
                    ))
                continue

            norm = self.normaliser.normalise(raw_name, raw_result)

            if norm.is_panel_header:
                # Structure, not a measurement (D-12) — excluded from
                # test-level metrics rather than counted as unresolved.
                dead_letters.append(self._dead_letter(r, "panel_header_row", raw_item))
                continue

            result_parse = parse_result(raw_result, raw_range)
            result_value = norm.extracted_value if norm.extracted_value is not None else result_parse.result_value

            harmonised = self.harmoniser.harmonise(
                raw_unit, norm.test_name_canonical, result_value,
                range_parse.range_low, range_parse.range_high,
            )

            flags = list(norm.flags) + list(result_parse.flags) + list(harmonised.flags) + list(range_parse.flags)

            validation = self.validator.classify(ValidationInput(
                test_name_canonical=norm.test_name_canonical,
                result_value=harmonised.result_value,
                unit_test_mismatch="unit_test_mismatch" in harmonised.flags,
                result_equals_range_text="range_leaked_into_result" in result_parse.flags,
                is_narrative=norm.is_narrative,
                is_qualitative=norm.is_qualitative,
            ))
            source_disagrees = self.validator.compare_source_range(
                norm.test_name_canonical, range_parse.range_low, range_parse.range_high
            )
            if source_disagrees:
                flags.append("source_range_disagrees_with_canonical")

            row = {
                **lineage, **header,
                "test_name_original": raw_name,
                "test_name_canonical": norm.test_name_canonical,
                "result_value": harmonised.result_value,
                "result_text": raw_result,
                "unit_canonical": harmonised.unit_canonical,
                "unit_original": raw_unit,
                "range_low": range_parse.range_low,
                "range_high": range_parse.range_high,
                "range_text": range_parse.range_text,
                "test_analytics": validation.test_analytics,
                "source_test_analytics": item.get("source_test_analytics"),
                "normalization_method": norm.normalization_method,
                "normalization_confidence": norm.normalization_confidence,
                "page_no": item.get("page_no"),
                "flags": ",".join(sorted(set(flags))) or None,
            }
            rows.append(row)

        return self._finalize_rows(rows), dead_letters

    def _finalize_rows(self, rows: list[dict]) -> list[dict]:
        """Stamps a per-record row_seq (needed because compound-split rows
        and medication rows can otherwise share identical row_identity_fields)
        and pads every row out to CANONICAL_COLUMNS."""
        out = []
        for seq, row in enumerate(rows):
            row = dict(row)
            row["row_seq"] = seq
            out.append(finalize_row(row))
        return out

    def _build_test_row(self, lineage, header, item, *, test_name_canonical, normalization_method,
                         normalization_confidence, result_value, range_parse, extra_flags,
                         is_qualitative, is_narrative) -> dict:
        validation = self.validator.classify(ValidationInput(
            test_name_canonical=test_name_canonical, result_value=result_value,
            is_qualitative=is_qualitative, is_narrative=is_narrative,
        ))
        canonical_unit = self.normaliser.canonical_unit_for(test_name_canonical)
        return {
            **lineage, **header,
            "test_name_original": item.get("test_name_original"),
            "test_name_canonical": test_name_canonical,
            "result_value": result_value,
            "result_text": item.get("result_text_original"),
            "unit_canonical": canonical_unit,
            "unit_original": item.get("unit_original"),
            "range_low": range_parse.range_low,
            "range_high": range_parse.range_high,
            "range_text": range_parse.range_text,
            "test_analytics": validation.test_analytics,
            "source_test_analytics": item.get("source_test_analytics"),
            "normalization_method": normalization_method,
            "normalization_confidence": normalization_confidence,
            "page_no": item.get("page_no"),
            "flags": ",".join(sorted(set(extra_flags))) or None,
        }

    # -------------------------------------------------------- discharge_summary
    def _process_discharge_summary(self, r: RawRecord, lineage: dict) -> tuple[list[dict], list[dict]]:
        profile = self.mapping["profiles"]["discharge_summary"]
        header = self._extract_header(r.payload, profile)
        header = self._normalise_demographics(header)

        free_text_cfg = profile.get("free_text_lists", {})
        for src_field, out_field in free_text_cfg.items():
            values = get_path(r.payload, src_field) or []
            header[out_field] = "; ".join(str(v) for v in values) if values else None

        med_cfg = profile.get("medications", {})
        medications = get_path(r.payload, med_cfg.get("source_path", "")) or []
        med_field_map = med_cfg.get("field_map", {})

        rows: list[dict] = []
        if not medications:
            rows.append({**lineage, **header})
            return self._finalize_rows(rows), []

        for raw_med in medications:
            med_row = {out: raw_med.get(src) for src, out in med_field_map.items()}
            rows.append({**lineage, **header, **med_row})
        return self._finalize_rows(rows), []

    # ------------------------------------------------------------------- run
    def run(self, local_mode: bool | None = None, source: str | None = None) -> RunSummary:
        local_mode = local_mode if local_mode is not None else os.environ.get("LOCAL_MODE", "true").lower() == "true"
        source = source or os.environ.get("SAMPLE_DATA_DIR" if local_mode else "GCS_BUCKET", "sample-data")

        logger.info("pipeline_run_start local_mode=%s source=%s", local_mode, source)
        raw_records, envelope_dead_letters, ingest_stats = ingest(local_mode, source)

        summary = RunSummary(
            files_processed_local_mode=local_mode,
            files_read=ingest_stats.files_read,
            records_seen=len(raw_records),
            duplicates_suppressed=ingest_stats.intra_file_duplicates + ingest_stats.cross_file_duplicates,
        )
        all_rows: list[dict] = []
        all_dead_letters: list[dict] = [
            {"source_file": dl.source_file, "document_id": dl.document_id,
             "error_reason": dl.error_reason, "raw_json": dl.raw_json,
             "source_system": dl.source_system, "failed_at": dl.failed_at}
            for dl in envelope_dead_letters
        ]

        for r in raw_records:
            lineage = self._lineage_fields(r)
            logger.info("correlation_id=%s processing document_id=%s record_type=%s",
                        r.correlation_id, r.document_id, r.record_type)
            if r.record_type == "lab_report":
                rows, dead_letters = self._process_lab_report(r, lineage)
            elif r.record_type == "discharge_summary":
                rows, dead_letters = self._process_discharge_summary(r, lineage)
            else:
                rows, dead_letters = [], []
            all_rows.extend(rows)
            all_dead_letters.extend(dead_letters)

        for row in all_rows:
            analytics = row.get("test_analytics")
            if analytics:
                summary.by_analytics[analytics] = summary.by_analytics.get(analytics, 0) + 1
                if analytics not in NON_FLAGGED_ANALYTICS:
                    summary.flagged += 1
            if row.get("normalization_method") == "unresolved":
                summary.unresolved_test_names += 1

        loader = Loader()
        load_summary = loader.load(all_rows, all_dead_letters)

        summary.rows_written = load_summary.records_loaded
        summary.dead_letters = len(all_dead_letters)
        summary.backend = load_summary.backend
        summary.finished_at = datetime.now(timezone.utc).isoformat()

        logger.info(
            "pipeline_run_complete rows_written=%d dead_letters=%d flagged=%d unresolved=%d "
            "duplicates_suppressed=%d backend=%s",
            summary.rows_written, summary.dead_letters, summary.flagged,
            summary.unresolved_test_names, summary.duplicates_suppressed, summary.backend,
        )
        self._write_run_summary(summary)
        return summary

    def _write_run_summary(self, summary: RunSummary) -> None:
        """Persisted so the UI's Dashboard tab can show run-level metrics
        (duplicates suppressed, files read) that aren't reconstructable from
        the warehouse tables alone — sanctioned explicitly by the brief:
        "reading from BigQuery (or the run-summary file if BQ latency
        annoys you)"."""
        path = os.environ.get("RUN_SUMMARY_PATH", "run_summary.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary.to_dict(), fh, indent=2)


def main() -> None:
    summary = Pipeline().run()
    print(summary)


if __name__ == "__main__":
    main()
