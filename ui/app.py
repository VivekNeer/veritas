"""Operational UI — Step 6, FR-5.1-5.4.

Four tabs: pipeline dashboard, record inspector, flagged-record queue, and
per-source quality summary. Reads from whichever backend STORAGE_BACKEND
selects (local SQLite by default, real BigQuery when configured) — the UI
talks to the same Loader interface the pipeline writes through, so nothing
here changes when you point it at real GCP. See README.md > Setup.

Functional over polished, per the brief's own framing: st.metric,
st.dataframe, st.selectbox, st.bar_chart are enough.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.bigquery_loader import get_backend  # noqa: E402

st.set_page_config(page_title="Veritas Claims — Pipeline Ops", layout="wide")

NUMERIC_COLUMNS = ["result_value", "range_low", "range_high", "normalization_confidence", "row_seq"]
NON_FLAGGED_ANALYTICS = {"Within Range", "Not Applicable"}


@st.cache_data(ttl=30)
def load_records() -> pd.DataFrame:
    backend = get_backend()
    rows = backend.fetch_all("standardised_records")
    df = pd.DataFrame(rows)
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@st.cache_data(ttl=30)
def load_dead_letters() -> pd.DataFrame:
    backend = get_backend()
    rows = backend.fetch_all("dead_letter")
    return pd.DataFrame(rows)


def load_run_summary() -> dict | None:
    path = os.environ.get("RUN_SUMMARY_PATH", "run_summary.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


records_df = load_records()
dead_letter_df = load_dead_letters()
run_summary = load_run_summary()

st.title("Veritas Claims — Pipeline Operations")
st.caption(
    f"Storage backend: `{os.environ.get('STORAGE_BACKEND', 'sqlite')}` · "
    f"{len(records_df)} standardised rows · {len(dead_letter_df)} dead-letter rows"
)

if st.button("Refresh data"):
    load_records.clear()
    load_dead_letters.clear()
    st.rerun()

if records_df.empty:
    st.warning("No standardised records yet — run `python -m src.pipeline` first.")
    st.stop()

tab_dashboard, tab_inspector, tab_flagged, tab_quality = st.tabs(
    ["Dashboard", "Record Inspector", "Flagged Queue", "Source Quality"]
)

# --------------------------------------------------------------- Dashboard
with tab_dashboard:
    st.subheader("Pipeline health (FR-5.1)")

    if run_summary:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Files received", run_summary.get("files_read", "—"))
        c2.metric("Rows processed", run_summary.get("rows_written", "—"))
        c3.metric("Dead-lettered", run_summary.get("dead_letters", "—"))
        c4.metric("Flagged", run_summary.get("flagged", "—"))
        c5.metric("Duplicates suppressed", run_summary.get("duplicates_suppressed", "—"))
        st.caption(f"Last run: {run_summary.get('started_at', '—')} → {run_summary.get('finished_at', '—')}")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Rows processed", len(records_df))
        c2.metric("Dead-lettered", len(dead_letter_df))
        analytics = records_df["test_analytics"]
        flagged = (analytics.notna() & ~analytics.isin(NON_FLAGGED_ANALYTICS)).sum()
        c3.metric("Flagged", int(flagged))
        st.caption("No run_summary.json found — showing counts derived from the warehouse tables only.")

    st.markdown("##### Records by analytics category")
    counts = records_df["test_analytics"].value_counts()
    st.bar_chart(counts)

    if not dead_letter_df.empty:
        st.markdown("##### Dead-letter reasons")
        st.bar_chart(dead_letter_df["error_reason"].value_counts())

# ----------------------------------------------------------- Record Inspector
with tab_inspector:
    st.subheader("Record inspector (FR-5.2)")

    doc_ids = sorted(records_df["document_id"].dropna().unique().tolist())
    selected_doc = st.selectbox("document_id", doc_ids)

    doc_rows = records_df[records_df["document_id"] == selected_doc]

    col_json, col_table = st.columns([1, 2])
    with col_json:
        st.markdown("**Raw JSON**")
        raw = doc_rows["raw_json"].dropna().iloc[0] if not doc_rows["raw_json"].dropna().empty else None
        if raw:
            try:
                st.json(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                st.text(raw)
        else:
            st.info("No raw JSON retained for this record.")

    with col_table:
        st.markdown("**Standardised rows**")
        display_cols = [c for c in [
            "record_type", "test_name_original", "test_name_canonical", "result_value",
            "result_text", "unit_canonical", "test_analytics", "normalization_method",
            "normalization_confidence", "medicine", "dose", "frequency", "flags",
        ] if c in doc_rows.columns]
        st.dataframe(doc_rows[display_cols], use_container_width=True, hide_index=True)

# --------------------------------------------------------------- Flagged Queue
with tab_flagged:
    st.subheader("Flagged records (FR-5.3)")
    st.caption("Every row where test_analytics != 'Within Range', sorted by normalisation confidence.")

    # notna() first: discharge_summary/medication rows carry no test_analytics
    # at all (they aren't test results), and None != "Within Range" is also
    # True in pandas, which would otherwise pull every non-lab row in here.
    flagged_df = records_df[
        records_df["test_analytics"].notna() & (records_df["test_analytics"] != "Within Range")
    ].copy()
    flagged_df = flagged_df.sort_values("normalization_confidence", na_position="first")

    analytics_options = sorted(flagged_df["test_analytics"].dropna().unique().tolist())
    selected_analytics = st.multiselect("Filter by category", analytics_options, default=analytics_options)
    filtered = flagged_df[flagged_df["test_analytics"].isin(selected_analytics)]

    display_cols = [c for c in [
        "document_id", "source_system", "test_name_original", "test_name_canonical",
        "result_value", "unit_original", "test_analytics", "normalization_method",
        "normalization_confidence", "flags",
    ] if c in filtered.columns]
    st.dataframe(filtered[display_cols], use_container_width=True, hide_index=True)
    st.caption(f"{len(filtered)} flagged rows shown (of {len(flagged_df)} total).")

# ------------------------------------------------------------- Source Quality
with tab_quality:
    st.subheader("Per-source quality summary (FR-5.4)")
    st.caption(
        "No true clinic identifier exists in the source data — grouped by `source_system` "
        "(FASTTRACK / ARTEMIS), the closest available proxy. See ASSUMPTIONS.md D-2."
    )

    lab_rows = records_df[records_df["record_type"] == "lab_report"].copy()
    sources = sorted(set(lab_rows["source_system"].dropna().unique()) | set(dead_letter_df.get("source_system", pd.Series(dtype=str)).dropna().unique()))

    quality_rows = []
    for source in sources:
        src_rows = lab_rows[lab_rows["source_system"] == source]
        src_dead = dead_letter_df[dead_letter_df.get("source_system") == source] if not dead_letter_df.empty else dead_letter_df
        total_attempted = len(src_rows) + len(src_dead)
        unresolved = (src_rows["normalization_method"] == "unresolved").sum()
        missing_unit = (src_rows["unit_original"].isna() | (src_rows["unit_original"] == "")).sum()
        quality_rows.append({
            "source_system": source,
            "clinical_rows": len(src_rows),
            "error_rate_pct": round(len(src_dead) / total_attempted * 100, 1) if total_attempted else 0.0,
            "unresolved_name_rate_pct": round(unresolved / len(src_rows) * 100, 1) if len(src_rows) else 0.0,
            "missing_unit_rate_pct": round(missing_unit / len(src_rows) * 100, 1) if len(src_rows) else 0.0,
        })

    quality_df = pd.DataFrame(quality_rows)
    st.dataframe(quality_df, use_container_width=True, hide_index=True)

    if run_summary:
        st.metric("Duplicates suppressed (whole run, not broken out per source)",
                   run_summary.get("duplicates_suppressed", "—"))
        st.caption(
            "Duplicate detection runs before source_system is attributable per suppressed "
            "row (a dropped duplicate is never turned into an output row), so this figure "
            "is tracked per pipeline run rather than per source — a disclosed scope "
            "simplification, not a missing feature."
        )
