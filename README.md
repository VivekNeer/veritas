# Veritas Claims — Medical Data Standardisation Pipeline

An ingestion and standardisation pipeline for medical claims data: raw JSON reports from
heterogeneous clinical sources in, validated canonical records in BigQuery out, with an
operational UI for pipeline health and flagged-record review.

Built as an engineering assignment for Niveus Solutions (part of NTT DATA).

---

## The Problem

Veritas Claims processes 200,000+ medical reports daily from 500+ clinical sources, each
with its own field names, units, date formats, and test naming conventions. Four
consequences follow, and this pipeline addresses each:

| Business problem | Where it is solved |
|---|---|
| Analysts cannot compare lab values because `Haemoglobin` and `Hemoglobin` are different tests | `src/standardisation/test_name_normaliser.py` |
| Automated adjudication fails on non-numeric result fields | `src/standardisation/numeric_converter.py` |
| Outlier lab values slip through undetected, causing incorrect claim approvals | `src/validation/validator.py` |
| Regulatory reporting is inconsistent because demographics are not standardised | `src/standardisation/demographic_normaliser.py` |

---

## What This Does — Before and After

Real rows from a full pipeline run against all 5 supplied sample files:

| Raw input | Canonical output | How |
|---|---|---|
| `test_name: "aemoglobin"` (OCR-truncated) | `test_name_canonical: "HEMOGLOBIN"` | exact match — the truncation is a pre-catalogued dictionary variant, confidence `1.00` |
| `test_name: "tal WBC Count"`, `unit: "mil/cu.mm"`, `result: "5.45"` | `WHITE_BLOOD_CELL_COUNT`, `result_value: 5450000.0`, `unit_canonical: "cells/cu.mm"` | OCR-truncated name resolved, then the brief's own worked unit-conversion example applied |
| `test_name: "BP"`, `result: "100/60 mmHg"` | two rows: `BLOOD_PRESSURE_SYSTOLIC = 100.0`, `BLOOD_PRESSURE_DIASTOLIC = 60.0 mmHg` | compound vital split, `normalization_method: "derived_compound_split"` |
| `test_name: "aemoglobin"`, `unit: "cell/cu.mm"`, `range: "4000-10000"`, `result: "9700"` | `test_analytics: "Invalid"`, flag `unit_test_mismatch`, unit and value **unchanged** | WBC data on a haemoglobin row — flagged, never auto-corrected (D-6) |

**Measured on the supplied sample data:** 94.2% of clinical rows resolved to canonical
test names (262 of 278 `lab_report` rows: 255 exact, 3 fuzzy, 2 embedded-value extraction,
2 derived compound-vital splits), against NFR-4.1's 98% target — across 72 canonical
tests and 287 configured variants. The 16 unresolved rows are, on inspection, all
panel-header strings carrying real but misattributed values, not genuine coverage gaps;
full breakdown in [`ASSUMPTIONS.md` D-15](ASSUMPTIONS.md).

---

## Architecture Summary
Architecture Presentation: [Google Slides Presentation](https://docs.google.com/presentation/d/1PSQgC79A32b5MLFcEBq_UUoT5sYkeWS3/edit?slide=id.p1#slide=id.p1)
<img src="docs/architecture_diagram.svg" alt="Veritas Claims solution architecture: GCS bucket into ingestion, a four-stage transform driven by config/*.yaml, BigQuery storage split into standardised_records and dead_letter, and a Streamlit ops UI reading from BigQuery." width="100%">

Three decisions shape the design: schema-on-read at ingestion with schema-on-write at the
warehouse; micro-batch rather than streaming, because the SLA is 15 minutes and batch is
cheaper; and **flag, never silently fix** — the source data is OCR-derived and damaged,
and inferring corrections would produce clean-looking output that misrepresents clinical
values.

Full detail: [`docs/architecture_narrative.md`](docs/architecture_narrative.md).

---

## Setup

### Prerequisites
- Python 3.10+
- A Google account (a GCP project with billing enabled is only needed for the
  optional real-cloud path below — everything runs locally without one)

### Install
```bash
git clone https://github.com/VivekNeer/veritas.git
cd veritas
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Run locally — no GCP required
The default `.env` values (`LOCAL_MODE=true`, `STORAGE_BACKEND=sqlite`) read
JSON from `sample-data/` and write to a local `local_warehouse.db` SQLite
file with the identical schema BigQuery would use. This is the fastest way
to verify the pipeline and is what every number in this README was measured
against.

```bash
python -m src.pipeline    # runs the pipeline end to end
streamlit run ui/app.py   # operational UI, reads local_warehouse.db
pytest                    # unit tests
```

---

### Optional: pointing at real GCP

The architecture is designed for GCS + BigQuery (see `docs/architecture_narrative.md`);
local SQLite is a disclosed stand-in for development, not a replacement. To
run against the real thing:

**1. Install the gcloud CLI** (skip if already installed)
Follow https://cloud.google.com/sdk/docs/install for your OS, then:
```bash
gcloud init
```
This walks you through logging into your Google account and picking or
creating a project interactively.

**2. Create (or select) a GCP project**
```bash
gcloud projects create veritas-claims-demo --name="Veritas Claims Demo"
gcloud config set project veritas-claims-demo
```
A project needs billing enabled to use GCS/BigQuery beyond the always-free
tier — attach a billing account in the Console
(`https://console.cloud.google.com/billing`) if this is a fresh project.
At the volume of this assignment (5 sample files, a few hundred rows),
actual spend is effectively $0 — GCS and BigQuery both have generous free
tiers (5GB storage, 1TB queries/month).

**3. Enable the two APIs this project uses**
```bash
gcloud services enable storage.googleapis.com bigquery.googleapis.com
```

**4. Authenticate the pipeline's Python client libraries**
```bash
gcloud auth application-default login
```
This opens a browser login and stores credentials where
`google-cloud-storage` / `google-cloud-bigquery` look for them automatically
— no service account JSON key needed for local development.

**5. Create the GCS bucket**
Bucket names are globally unique across all of GCP, so pick a suffix (your
project ID is a safe default):
```bash
export GCP_PROJECT_ID="veritas-claims-demo"
export GCS_BUCKET="veritas-claims-raw-${GCP_PROJECT_ID}"

gcloud storage buckets create gs://$GCS_BUCKET \
  --project=$GCP_PROJECT_ID \
  --location=US \
  --uniform-bucket-level-access
```

**6. Upload the sample files**
```bash
gcloud storage cp sample-data/*.json gs://$GCS_BUCKET/
```
The pipeline's GCS reader lists every `.json` blob in the bucket — no
folder structure is required for this dataset (see ASSUMPTIONS.md D-1 on
why there's no per-clinic prefixing here).

**7. Create the BigQuery dataset**
```bash
bq --location=US mk --dataset ${GCP_PROJECT_ID}:veritas_claims
```
The two tables inside it (`standardised_records`, `dead_letter`) are
created automatically by the pipeline on first run — nothing to do here.

**8. Point the pipeline at it**
Edit `.env`:
```bash
LOCAL_MODE=false
STORAGE_BACKEND=bigquery
GCP_PROJECT_ID=veritas-claims-demo
GCS_BUCKET=veritas-claims-raw-veritas-claims-demo
BQ_DATASET=veritas_claims
BQ_LOCATION=US
```
Or run one-off without editing `.env`:
```bash
LOCAL_MODE=false STORAGE_BACKEND=bigquery python -m src.pipeline
```
You can also mix the two — e.g. `LOCAL_MODE=true STORAGE_BACKEND=bigquery`
reads from `sample-data/` locally but writes to real BigQuery, which is a
convenient way to sanity-check the BigQuery path without re-uploading files
every time you change something.

**9. Verify**
```bash
bq query --use_legacy_sql=false \
  'SELECT test_analytics, COUNT(*) FROM veritas_claims.standardised_records GROUP BY 1'
```
Point the Streamlit UI at the same `.env` and it reads from BigQuery
instead of SQLite with no code change — the UI talks to whichever backend
`STORAGE_BACKEND` selects.

**No code changes are required at any point in this section.** The
LOCAL_MODE / STORAGE_BACKEND split was built specifically so switching
between local development and real GCP is purely an environment-variable
change — see `src/storage/bigquery_loader.py` for the two backends behind
one interface.

**Tearing it down** (avoid stray billing on an otherwise-idle project):
```bash
gcloud storage rm -r gs://$GCS_BUCKET
bq rm -r -d ${GCP_PROJECT_ID}:veritas_claims
```

---

## Key Design Decisions

**Long-format output, resolving a conflict in the brief.** FR-2.2 describes five columns
per test; the supplied schema file is long-format. We followed the schema file — 72
canonical tests would need 360 columns in the wide form, most null on any given report.
All five conceptual fields are preserved as row columns. `[ASSUMPTIONS.md D-17]`

**Every mapping decision is auditable.** Each row records `normalization_method` (exact /
fuzzy / extracted / unresolved) and `normalization_confidence`. You can always answer why
a given raw name became a given canonical name, and the unresolved queue is triageable by
score.

**We reconciled the supplied dictionary rather than propagating it.**
`Clinical_name_standardization.pdf` lists `HAEMOGLOBIN` and `HEMOGLOBIN` as separate
canonical names, both claiming `Hb` as a variant — an ambiguous mapping. Same for
`CREATININE` / `Serum Creatinine`. Merged, with LOINC as the production answer.
`[ASSUMPTIONS.md D-5]`

**Mismatched rows are flagged, never corrected.** Some rows carry another analyte's unit
and range — `aemoglobin` with `range: 4000-10000` and `unit: cell/cu.mm` is WBC data on a
haemoglobin row. It is technically possible to infer the real test from the unit. We
don't: that would rewrite a clinical record on a guess and destroy the evidence that the
source extraction is misaligned. `[ASSUMPTIONS.md D-6]`

**Configured ranges override source-provided ranges.** FR-3.1 asks for validation against
*medically accepted* ranges, and the source `range` field is demonstrably unreliable.
Both are retained and their disagreement is itself a reported quality signal.
`[ASSUMPTIONS.md D-8]`

**Zero-code onboarding is real, not asserted.** Adding a test, variant, unit alias,
reference range, or new source profile is a config edit. A disabled `_EXAMPLE_NEW_CLINIC`
profile ships in `config/document_type_mappings.yaml` as a worked example.

---

## Adding a New Test Variant (NFR-2.1 walkthrough)

A clinic starts sending `"Hgb%"` for haemoglobin. To support it:

```yaml
# config/test_dictionary.yaml
HEMOGLOBIN:
  canonical_unit: "g/dL"
  variants:
    - "HEMOGLOBIN"
    - "Hgb%"        # <-- add this line
```

Re-run the pipeline. No code change, no redeploy. The same pattern applies to unit
aliases, reference ranges, and whole source profiles.

---

## Repository Layout

```
config/     four YAML files driving every mapping decision
src/        ingestion, standardisation, validation, storage, orchestration
ui/         Streamlit operational interface
sample-data/  provided sample files (+ synthetic edge cases, labelled)
tests/      unit tests for standardisation and validation
docs/       architecture narrative, diagram, assumptions
```

---

## Requirements Coverage

| Req | Status | Notes |
|---|---|---|
| FR-1.1 Multi-source ingestion | ✅ Built | GCS + local mode, verified both paths |
| FR-1.2 Duplicate detection | ✅ Built | Intra-file + cross-file, configurable; file 5's real duplicate caught |
| FR-1.3 Schema flexibility | ✅ Built | Schema-on-read, per-classifier profiles |
| FR-2.1 Test name normalisation | ✅ Built | Exact → fuzzy → unresolved, method + confidence recorded; 94.2% coverage |
| FR-2.2 Fixed column schema | ✅ Built (reinterpreted) | Long-format, see D-17 |
| FR-2.3 Numeric conversion | ✅ Built | All documented edge cases handled; 64 tests |
| FR-2.4 Unit harmonisation | ✅ Built | Alias + factor conversion, mismatch flagged not corrected |
| FR-2.5 Demographic normalisation | ✅ Built | Proven by synthetic tests; source PII pre-redacted |
| FR-2.6 Medicine mapping | ⬜ Omitted | See S-1 — OCR quality makes it unsafe |
| FR-3.1 Range validation | ✅ Built | Config ranges are authority over source ranges |
| FR-3.2 Outlier detection | ✅ Built | Separate bounds from range bounds; brief's own worked example verified |
| FR-3.3 Analytics classification | ✅ Built | Plus two states, see D-14 |
| FR-3.4 Incorrect value flagging | ✅ Built | Folded into `Invalid` with specific reason flags |
| FR-4.1 Structured DB load | ✅ Built | Real BigQuery client + local SQLite stand-in behind one interface |
| FR-4.2 Error logging | ✅ Built | Dead-letter table with reason, idempotent |
| FR-4.3 Audit trail | ✅ Built | Full raw JSON retained per row |
| FR-5.1 Pipeline dashboard | ✅ Built | Streamlit, `st.metric` + bar chart |
| FR-5.2 Record inspector | ✅ Built | Raw JSON beside standardised rows |
| FR-5.3 Flagged records review | ✅ Built | Sortable, filterable by category |
| FR-5.4 Clinic-level summary | ✅ Built | Grouped by `source_system`, see D-2 |
| NFR-2.1 Zero-code onboarding | ✅ Built | Config-driven; walkthrough above |
| NFR-3.1 Fault tolerance | ✅ Built | Per-record isolation, verified on malformed inputs |
| NFR-3.2 Idempotency | ✅ Built | Deterministic id + upsert; verified 342/342 and 84/84 distinct across re-runs |
| NFR-4.2 Data lineage | ✅ Built | Trace/correlation IDs, timestamps, source path on every row |
| NFR-5.2 Structured logging | ✅ Built | Correlation ID on every log line |

Remaining NFRs are addressed in the architecture narrative, as the brief specifies.

---

## Known Limitations

- Runs as a local process against real GCS and BigQuery; Pub/Sub, Cloud Run, and Dataflow
  are designed but not deployed `[S-2]`
- Reference ranges are general adult intervals; several samples are paediatric `[D-10]`
- Medicine name mapping omitted — the medication data is too OCR-damaged for dictionary
  mapping to be safe `[S-1]`
- No authentication on the operational UI `[S-6]`
- Throughput claims are argued analytically, not load-tested `[S-7]`
- Config is not schema-versioned for historical reprocessing `[S-3]`
- Deterministic fuzzy matching has an observed false-positive mode: one of three fuzzy
  matches in the sample run scored a **perfect** confidence (1.00) on a wrong match, purely
  from a single shared token between two unrelated strings — the exact weakness embeddings
  would fix, and dangerous precisely because it wouldn't surface in a confidence-sorted
  review queue `[T-3a]`
- 16 lab_report rows (5.8%) remain unresolved — all inspected individually and confirmed to
  be panel-header captions carrying real, misattributed values rather than genuine
  dictionary gaps `[D-15]`

Full reasoning for each: [`ASSUMPTIONS.md`](ASSUMPTIONS.md).
