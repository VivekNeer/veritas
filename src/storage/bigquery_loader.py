"""Storage layer — FR-4.1, FR-4.2, FR-4.3 · NFR-3.2, NFR-4.2.

Two backends behind one interface, selected by `STORAGE_BACKEND`:

  * BigQueryBackend — the real target. `standardised_records` +
    `dead_letter` tables in a BigQuery dataset, idempotent via a
    delete-by-id then load (avoids the streaming-buffer delete restriction
    that would otherwise block same-day re-runs).
  * SQLiteBackend — a same-schema local stand-in so the pipeline, tests,
    and UI are runnable end to end without live GCP credentials. This is a
    pragmatic addition beyond the original plan, disclosed in
    ASSUMPTIONS.md (T-1a). It is NOT a replacement for BigQuery in
    production — it exists purely to make this submission runnable and
    verifiable without gcloud auth in hand.

Idempotency (NFR-3.2): every row's `id` is a deterministic hash of the
fields in `document_type_mappings.yaml > deduplication.row_identity_fields`.
Re-running the pipeline over the same inputs re-derives the same ids, so the
upsert never creates duplicate rows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config_loader import get_document_type_mappings

logger = logging.getLogger("veritas.storage")

DEAD_LETTER_COLUMNS = ["source_file", "document_id", "error_reason", "raw_json", "failed_at"]


def compute_row_id(row: dict, id_fields: list[str]) -> str:
    parts = [str(row.get(f, "")) for f in id_fields]
    canonical = "||".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def dead_letter_id(row: dict) -> str:
    """Stable id for a dead-letter row — deliberately excludes `failed_at`,
    which changes on every re-run and would otherwise defeat idempotency
    (the same malformed row would accumulate a fresh entry each run)."""
    stable_fields = {k: v for k, v in row.items() if k != "failed_at"}
    canonical = json.dumps(stable_fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def row_identity_fields() -> list[str]:
    return get_document_type_mappings()["deduplication"]["row_identity_fields"]


@dataclass
class LoadSummary:
    records_loaded: int
    dead_letters_loaded: int
    backend: str


class SQLiteBackend:
    """Local stand-in for BigQuery — same logical schema, upsert-by-id."""

    def __init__(self, db_path: str = "local_warehouse.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._ensure_tables_exist: dict[str, set] = {}

    def _union_columns(self, rows: list[dict]) -> list[str]:
        """Column order must be deterministic across calls (insertion order
        of first appearance) — never a set(), which reorders randomly and
        would misalign the positional `?` placeholders below."""
        seen: dict[str, None] = {}
        for row in rows:
            for k in row.keys():
                seen.setdefault(k, None)
        return list(seen.keys())

    def _ensure_table(self, table: str, all_cols: list[str]) -> None:
        existing = self._ensure_tables_exist.get(table)
        if existing is None:
            placeholders = ", ".join(f'"{k}" TEXT' for k in all_cols if k != "id")
            self.conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}" (id TEXT PRIMARY KEY, {placeholders})'
            )
            self.conn.commit()
            existing = {row[1] for row in self.conn.execute(f'PRAGMA table_info("{table}")')}
            self._ensure_tables_exist[table] = existing

        for col in all_cols:
            if col != "id" and col not in existing:
                self.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" TEXT')
                existing.add(col)
        self.conn.commit()

    def load_records(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        cols = self._union_columns(rows)
        self._ensure_table("standardised_records", cols)
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        sql = f'INSERT OR REPLACE INTO "standardised_records" ({col_list}) VALUES ({placeholders})'
        values = [[json.dumps(r.get(c)) if isinstance(r.get(c), (list, dict)) else r.get(c) for c in cols] for r in rows]
        self.conn.executemany(sql, values)
        self.conn.commit()
        return len(rows)

    def load_dead_letters(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        stamped = [{"id": dead_letter_id(r), **r} for r in rows]
        cols = self._union_columns(stamped)
        self._ensure_table("dead_letter", cols)
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(f'"{c}"' for c in cols)
        sql = f'INSERT OR REPLACE INTO "dead_letter" ({col_list}) VALUES ({placeholders})'
        values = [[row.get(c) for c in cols] for row in stamped]
        self.conn.executemany(sql, values)
        self.conn.commit()
        return len(rows)

    def fetch_all(self, table: str) -> list[dict]:
        try:
            cur = self.conn.execute(f'SELECT * FROM "{table}"')
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in cur.fetchall()]

    def row_count(self, table: str) -> int:
        try:
            cur = self.conn.execute(f'SELECT COUNT(*) FROM "{table}"')
            return cur.fetchone()[0]
        except sqlite3.OperationalError:
            return 0


class BigQueryBackend:
    """Real BigQuery target. Requires GCP_PROJECT_ID / BQ_DATASET and
    `gcloud auth application-default login`. Not exercised against a live
    project in this development environment — see ASSUMPTIONS.md S-2."""

    def __init__(self, project_id: str, dataset: str):
        from google.cloud import bigquery

        self.bigquery = bigquery
        self.client = bigquery.Client(project=project_id)
        self.dataset = dataset
        self.project_id = project_id
        self._ensure_dataset()

    def _ensure_dataset(self) -> None:
        dataset_ref = f"{self.project_id}.{self.dataset}"
        try:
            self.client.get_dataset(dataset_ref)
        except Exception:
            ds = self.bigquery.Dataset(dataset_ref)
            ds.location = os.environ.get("BQ_LOCATION", "US")
            self.client.create_dataset(ds, exists_ok=True)

    def _table_ref(self, table: str) -> str:
        return f"{self.project_id}.{self.dataset}.{table}"

    def _ensure_table(self, table: str, rows: list[dict]) -> None:
        table_ref = self._table_ref(table)
        try:
            self.client.get_table(table_ref)
            return
        except Exception:
            pass
        # Union across every row, not just rows[0] — a schema inferred from a
        # single row silently drops columns any other row happens to add.
        column_types: dict[str, str] = {}
        for row in rows:
            for k, v in row.items():
                if k not in column_types:
                    column_types[k] = "FLOAT64" if isinstance(v, float) else "STRING"
        schema = [
            self.bigquery.SchemaField(k, t, mode="NULLABLE") for k, t in column_types.items()
        ]
        self.client.create_table(self.bigquery.Table(table_ref, schema=schema))

    def _delete_existing_ids(self, table: str, ids: list[str]) -> None:
        if not ids:
            return
        table_ref = self._table_ref(table)
        query = f'DELETE FROM `{table_ref}` WHERE id IN UNNEST(@ids)'
        job_config = self.bigquery.QueryJobConfig(
            query_parameters=[self.bigquery.ArrayQueryParameter("ids", "STRING", ids)]
        )
        try:
            self.client.query(query, job_config=job_config).result()
        except Exception as e:
            logger.warning("bigquery_delete_before_load_failed table=%s error=%s", table, e)

    def load_records(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        self._ensure_table("standardised_records", rows)
        self._delete_existing_ids("standardised_records", [r["id"] for r in rows])
        job_config = self.bigquery.LoadJobConfig(
            source_format=self.bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=self.bigquery.WriteDisposition.WRITE_APPEND,
        )
        job = self.client.load_table_from_json(rows, self._table_ref("standardised_records"), job_config=job_config)
        job.result()
        return len(rows)

    def load_dead_letters(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        stamped = [{"id": dead_letter_id(r), **r} for r in rows]
        self._ensure_table("dead_letter", stamped)
        self._delete_existing_ids("dead_letter", [r["id"] for r in stamped])
        job_config = self.bigquery.LoadJobConfig(
            source_format=self.bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=self.bigquery.WriteDisposition.WRITE_APPEND,
        )
        job = self.client.load_table_from_json(stamped, self._table_ref("dead_letter"), job_config=job_config)
        job.result()
        return len(rows)

    def fetch_all(self, table: str) -> list[dict]:
        try:
            return [dict(r) for r in self.client.list_rows(self._table_ref(table))]
        except Exception:
            return []

    def row_count(self, table: str) -> int:
        try:
            query = f'SELECT COUNT(*) as c FROM `{self._table_ref(table)}`'
            return list(self.client.query(query).result())[0]["c"]
        except Exception:
            return 0


def get_backend():
    backend = os.environ.get("STORAGE_BACKEND", "sqlite").lower()
    if backend == "bigquery":
        project_id = os.environ["GCP_PROJECT_ID"]
        dataset = os.environ.get("BQ_DATASET", "veritas_claims")
        return BigQueryBackend(project_id, dataset)
    db_path = os.environ.get("SQLITE_PATH", "local_warehouse.db")
    return SQLiteBackend(db_path)


class Loader:
    """Thin orchestration wrapper: stamps ids + timestamps, then hands rows
    to whichever backend is configured."""

    def __init__(self, backend=None):
        self.backend = backend or get_backend()
        self.id_fields = row_identity_fields()

    def load(self, rows: list[dict], dead_letters: list[dict]) -> LoadSummary:
        now = datetime.now(timezone.utc).isoformat()
        for r in rows:
            r.setdefault("processed_at", now)
            r["id"] = compute_row_id(r, self.id_fields)

        n_records = self.backend.load_records(rows)
        n_dead = self.backend.load_dead_letters(dead_letters)
        backend_name = type(self.backend).__name__
        logger.info("load_complete records=%d dead_letters=%d backend=%s", n_records, n_dead, backend_name)
        return LoadSummary(n_records, n_dead, backend_name)
