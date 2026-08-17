"""Ingestion layer — FR-1.1, FR-1.2, FR-1.3 · NFR-3.1, NFR-3.2.

Reads raw JSON envelopes from GCS (or a local folder in LOCAL_MODE), parses
only the shared envelope (never clinical field names — that is the config
layer's job), fans out `data.responseDetails[]` into one logical record per
entry, and deduplicates at two levels before handing records downstream.

Ingestion knows nothing about test names, units, or clinical fields. That
boundary is what makes FR-1.3 (schema-on-read) real rather than asserted.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config_loader import get_document_type_mappings

logger = logging.getLogger("veritas.ingestion")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_path(obj: Any, dotted_path: str) -> Any:
    """Resolve a dotted path against a nested dict. Missing -> None, never raises."""
    if dotted_path == "":
        return obj
    cur = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class DeadLetter:
    source_file: str
    document_id: str | None
    error_reason: str
    raw_json: str | None = None
    failed_at: str = field(default_factory=_now_iso)


@dataclass
class RawRecord:
    """One fanned-out logical record from `data.responseDetails[]`."""
    document_id: str | None
    trace_id: str | None
    correlation_id: str | None
    classifier: str
    record_type: str
    payload: dict
    source_file: str
    meta: dict
    payload_hash: str
    ingested_at: str = field(default_factory=_now_iso)


def _iter_local_files(folder: Path) -> Iterator[tuple[str, bytes]]:
    for p in sorted(folder.glob("*.json")):
        yield str(p), p.read_bytes()


def _iter_gcs_files(bucket_name: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    for blob in bucket.list_blobs(prefix=prefix):
        if blob.name.endswith(".json"):
            yield f"gs://{bucket_name}/{blob.name}", blob.download_as_bytes()


def _extract_meta(document: dict, mapping: dict) -> dict:
    meta_cfg = mapping["envelope"]["meta_details"]
    raw_list = _get_path(document, meta_cfg["path"]) or []
    key_field = meta_cfg["key_field"]
    value_field = meta_cfg["value_field"]
    key_map = meta_cfg["key_map"]

    meta = {out_name: None for out_name in key_map.values()}
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        k = item.get(key_field)
        v = item.get(value_field)
        out_name = key_map.get(k)
        if out_name:
            meta[out_name] = v
    return meta


def parse_envelope(document: dict, source_file: str, mapping: dict) -> tuple[list[RawRecord], list[DeadLetter]]:
    """Parses one file's envelope and fans out its logical records.

    Returns (records, dead_letters). Never raises — malformed structure
    becomes a dead-letter entry so one bad file can't block a batch (NFR-3.1).
    """
    records: list[RawRecord] = []
    dead_letters: list[DeadLetter] = []

    envelope = mapping["envelope"]
    identity = envelope["identity_map"]
    trace_id = _get_path(document, identity["trace_id"])
    correlation_id = _get_path(document, identity["correlation_id"])
    document_id = _get_path(document, identity["document_id"])

    response_details = _get_path(document, envelope["records_path"])
    if not isinstance(response_details, list):
        dead_letters.append(DeadLetter(
            source_file=source_file,
            document_id=document_id,
            error_reason=f"missing_or_malformed_{envelope['records_path'].replace('.', '_')}",
            raw_json=json.dumps(document, default=str)[:5000],
        ))
        return records, dead_letters

    meta = _extract_meta(document, mapping)
    profiles = mapping["profiles"]
    classifier_field = envelope["classifier_field"]
    payload_field = envelope["payload_field"]
    status_field = envelope["status_field"]

    seen_intra_file: set[tuple[str, str]] = set()
    dedup_cfg = mapping["deduplication"]

    for entry in response_details:
        if not isinstance(entry, dict):
            dead_letters.append(DeadLetter(
                source_file=source_file, document_id=document_id,
                error_reason="response_detail_entry_not_an_object",
            ))
            continue

        classifier = entry.get(classifier_field)
        status = entry.get(status_field)
        payload = entry.get(payload_field)

        if classifier not in profiles:
            dead_letters.append(DeadLetter(
                source_file=source_file, document_id=document_id,
                error_reason=f"unknown_classifier:{classifier}",
                raw_json=json.dumps(entry, default=str)[:5000],
            ))
            continue

        if status != "success" or not isinstance(payload, dict):
            dead_letters.append(DeadLetter(
                source_file=source_file, document_id=document_id,
                error_reason=f"record_status_not_success:{status}",
                raw_json=json.dumps(entry, default=str)[:5000],
            ))
            continue

        phash = _payload_hash(payload)

        if dedup_cfg.get("intra_file", {}).get("enabled"):
            key = (classifier, phash)
            if key in seen_intra_file:
                logger.info(
                    "correlation_id=%s intra_file_duplicate_suppressed classifier=%s",
                    correlation_id, classifier,
                )
                continue
            seen_intra_file.add(key)

        record_type = profiles[classifier]["record_type"]
        records.append(RawRecord(
            document_id=document_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
            classifier=classifier,
            record_type=record_type,
            payload=payload,
            source_file=source_file,
            meta=meta,
            payload_hash=phash,
        ))

    return records, dead_letters


def ingest(local_mode: bool, source: str) -> tuple[list[RawRecord], list[DeadLetter]]:
    """Reads every JSON file from `source` (local folder or GCS bucket name),
    parses envelopes, fans out records, and applies cross-file dedup across
    the batch. Cross-*run* idempotency is the storage layer's job (deterministic
    row id + MERGE) — see storage/bigquery_loader.py.
    """
    mapping = get_document_type_mappings()
    all_records: list[RawRecord] = []
    all_dead_letters: list[DeadLetter] = []

    file_iter = _iter_local_files(Path(source)) if local_mode else _iter_gcs_files(source)

    for source_file, raw_bytes in file_iter:
        try:
            document = json.loads(raw_bytes)
        except json.JSONDecodeError as e:
            all_dead_letters.append(DeadLetter(
                source_file=source_file, document_id=None,
                error_reason=f"malformed_json:{e}",
            ))
            continue

        records, dead_letters = parse_envelope(document, source_file, mapping)
        all_records.extend(records)
        all_dead_letters.extend(dead_letters)

    # Cross-file dedup within this ingestion batch (FR-1.2, level 2).
    cross_cfg = mapping["deduplication"]["cross_file"]
    if cross_cfg.get("enabled"):
        seen: set[tuple] = set()
        deduped: list[RawRecord] = []
        for r in all_records:
            key = (r.document_id, r.record_type, r.payload_hash)
            if key in seen:
                logger.info(
                    "correlation_id=%s cross_file_duplicate_suppressed document_id=%s",
                    r.correlation_id, r.document_id,
                )
                continue
            seen.add(key)
            deduped.append(r)
        all_records = deduped

    logger.info("ingestion_complete records=%d dead_letters=%d", len(all_records), len(all_dead_letters))
    return all_records, all_dead_letters
