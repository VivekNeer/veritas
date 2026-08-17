# Assumptions

This document records the assumptions, judgment calls, and deliberate omissions made
while building this pipeline. Per the assignment brief, it is treated as a core
deliverable rather than an appendix.

Where a simplification was made to fit the time budget, it is stated plainly along with
what the production version would require.

---

## 1. Business Assumptions

**B-1. Standardisation is a permanent requirement, not a migration.**
The brief describes 500+ sources each with their own conventions. We assume upstream
convergence is not achievable — clinics will not be made to conform. The pipeline is
therefore built to absorb heterogeneity indefinitely rather than to bridge a temporary
gap. This drives the config-over-code design throughout.

**B-2. Flagged records are reviewed, not discarded.**
The brief specifies an ops review queue (FR-5.3), so we assume a human team acts on
flags. Consequently the pipeline never silently drops a clinical record: anything
unparseable is retained with a reason and surfaced. The only rows removed from the
analytics table entirely are template placeholders containing no clinical content
(see D-4).

**B-3. Incorrect data is more costly than missing data.**
In claims adjudication, a wrong lab value can approve a fraudulent claim or deny a
legitimate one. We therefore prefer to mark a value `Invalid` and route it for review
rather than infer a plausible-looking correction. This principle is the basis for
decision D-6.

**B-4. Volume is stated at 200k files/day, not 200k records/day.**
Each file may contain multiple logical records — the samples contain 1–3. Our throughput
reasoning uses files as the ingestion unit and records as the transform unit.

**B-5. The 15-minute p95 latency target implies micro-batch, not streaming.**
A 15-minute SLA does not require per-event streaming. We assume scheduled micro-batches
(or event-triggered small batches) are acceptable, which materially reduces cost and
operational complexity. See the architecture narrative for the trade-off.

---

## 2. Technical Assumptions

**T-1. Google Cloud Platform, deliberately.**
GCS for landing, BigQuery for the canonical store. The brief permits equivalents, but
BigQuery is named first and suits the workload: columnar, append-heavy, analytics-facing,
with schema evolution support.

**T-2. The prototype runs locally; the architecture is cloud-native.**
Ingestion reads from a real GCS bucket and loads to a real BigQuery dataset, but
orchestration is a local Python process rather than a deployed Dataflow job or Cloud Run
service. This is a conscious time-budget trade. The production topology
(GCS → Pub/Sub → Cloud Run → Dataflow → BigQuery) is documented in the architecture
narrative. Nothing in the module structure assumes local execution; the transform
functions are pure and would lift into Beam DoFns without redesign.

**T-3. Deterministic fuzzy matching over LLM-based resolution.**
`rapidfuzz` with a configurable threshold, not Vertex AI or Gemini. Reasons: results are
reproducible and unit-testable; there is no API latency or cost per record; and
correctness does not depend on an external service being available. At production scale
and for genuinely novel variants, text embeddings would outperform string similarity —
that is the documented upgrade path, not the prototype default. Note that every match
records its method and confidence, so an embedding tier could be added as a fourth tier
without changing the output schema.

**T-3a. Observed weakness: token_set_ratio can over-score on a single shared word.**
On the full sample run, one of three fuzzy matches was wrong: the panel-header string
`"COMPLETE BLOOD COUNT (CBC) , WHOLE BLOOD EDTA"` matched `URINE_BLOOD` (variant `"URINE
BLOOD"`) at **confidence 1.00** — a perfect score, purely because both strings contain the
token "BLOOD" and `token_set_ratio` scores on token-set overlap rather than semantic
meaning. This is the concrete failure mode T-3 already names as fuzzy matching's weak
spot, and it is a genuinely dangerous one: the row is *maximally confident* and so would
sort to the bottom of a confidence-ordered review queue, not the top, exactly where an
ops reviewer would never think to look. Left uncorrected here deliberately — patching the
scorer this late risked destabilising the other two (correct) fuzzy matches and the 64
passing tests without a broader tuning pass to validate against. Recorded as a concrete,
observed data point for why embeddings are the real production answer, not a guess.

**T-4. Streamlit for the operational UI.**
The brief asks for a lightweight, functional UI and explicitly states it need not be
production-grade. Streamlit reads directly from BigQuery, lives in the repo, and requires
no separate frontend build. A production ops console would be a proper web application
with authentication and role-based access.

**T-5. Idempotency via deterministic row identity.**
Each output row's `id` is a hash of `(document_id, record_type, test_name_original,
page_no, result_text, row_seq)`. `row_seq` was added during implementation: none of the
other fields vary between two medication rows in the same discharge_summary encounter (no
test_name/page_no/result_text exist on those rows at all), so without it every medication
row for one document collapsed onto the same id. Verified idempotent by re-running the
full pipeline twice against all 5 samples — 342/342 and 84/84 distinct ids both times, no
duplicate rows. Key set: `document_type_mappings.yaml > deduplication.row_identity_fields`.

**T-6. Configuration is data, not code.**
All four YAML files are read at runtime. Adding a test, a variant spelling, a unit
alias, a reference range, or an entire new source profile requires no code change and no
redeploy. This is the assignment's one non-functional requirement marked as required for
implementation (NFR-2.1).

**T-7. Config is not schema-versioned in the prototype.**
`document_type_mappings.yaml` carries a `schema_version` field, but historical
reprocessing against a prior config version is not implemented. See Scope Exclusions S-3.

---

## 3. Data Assumptions

These arise from direct inspection of the five supplied sample files.

**D-1. Variation is by document classifier, not by clinic.**
The brief describes per-clinic field-name variation. The supplied data does not exhibit
this: all five files share one envelope (`traceId` → `data.responseDetails[]`), and the
payload shape varies by `classifier` — `lab_report` (nested `basic_info` +
`report_details[]`, snake_case) or `discharge_summary` (flat camelCase + medication
lists). No clinic identifier exists anywhere in the payload.

We built the config layer on the axis the data actually exhibits, while keeping an
open-ended `source_profiles` block — with a worked, disabled example — so that genuine
per-source divergence is a config-only addition. We chose not to fabricate five fictional
clinic formats to match the brief's description.

**D-2. `source_system` is the closest available proxy for "clinic".**
Observed values: `FASTTRACK`, `ARTEMIS`. Per-source quality metrics (FR-5.4) group by
this field. If a true clinic identifier exists upstream, it should be added to
`metaDetails` and the grouping key changed in config.

**D-3. The source data is OCR-derived and materially damaged.**
This is the defining characteristic of the dataset and shaped most of the design.
Observed and handled:

| Defect | Real example from samples | Handling |
|---|---|---|
| Leading characters truncated | `aemoglobin`, `tal WBC Count`, `atelet Count`, `ematocrit HCT`, `eutrophils` | Fuzzy match; `normalization_method: fuzzy` with score recorded |
| Columns misaligned across analytes | `test_name: "aemoglobin"` with `range: "4000-10000"`, `unit: "cell/cu.mm"` — WBC data on a haemoglobin row | Flagged `unit_test_mismatch`, **not** corrected (see D-6) |
| Range leaked into the result field | `result: "1.5-4.5"`, `result: "9.0-13.0"` | Detected by comparing result to range; `Invalid` |
| Unit column contains a test name | `unit: "atelet Count"` | Nulled, flagged, canonical unit assumed |
| Unit column contains a letterhead | `unit: "Expertise. Empowering you."` | Nulled, flagged |
| Entire unit column collapsed into one cell | `"g/dL, %, cells/cu.mm, cells/cu.mm, %, %, ..."` | Detected by multi-unit pattern; unrecoverable at row grain, flagged |
| Unfilled template rows | `test_name: "test_name"`, `result: "result"`, `reports_date: "DD/MM/YYYY"` | Dropped to dead-letter (see D-4) |
| Result carries unit or qualifier text | `"98.6 degree F"`, `"123 mg/dl"`, `"99% On Room Air"` | Leading numeric extracted; full string retained in `result_text` |
| Compound vitals in one field | `"100/60 mmHg"`, `"114/min"` | See D-7 |
| Values embedded in the test name | `"Na + (127)"`, `"KFT - sr creatinine - 0.3"` | Name stripped for matching; value recovered only when the result field is empty, flagged `value_recovered_from_test_name` |
| Multiple analytes in one name | `"LFT ( SGOT - 38, SGPT -14, ALP - 127)"` | Flagged `composite_multi_analyte_row`, not split |
| Free-text where numeric expected | `"B/L AE+"`, `"S1S2+"`, `"diffuse tenderness +"` | `result_value` null, text retained, `Invalid` |
| Non-date in a date field | `bill_date: "LAB10945"` | Null with original retained |
| Garbled medication rows | `medicine: "Spor 977"`, `dose: "bud- ob of"` | See S-1 |

**D-4. Template placeholder rows are dropped, not flagged.**
Rows where a value equals its own field name (`result: "result"`) contain no clinical
information and are artefacts of an unfilled extraction template. These go to the
dead-letter store with reason `template_placeholder_or_non_clinical_row` rather than
into the analytics table, where they would corrupt every aggregate. This is the single
exception to B-2.

**D-5. The supplied canonical dictionary is internally inconsistent; we reconciled it.**
`Clinical_name_standardization.pdf` lists:
- `HAEMOGLOBIN` and `HEMOGLOBIN` as **separate** canonical names, both claiming `Hb` as a
  variant — an ambiguous mapping that cannot be resolved deterministically as supplied.
- `CREATININE` and `Serum Creatinine` as separate canonical names for the same analyte,
  differing only by specimen qualifier.
- Mixed casing conventions (`SODIUM` vs `Platelet Count`) that would fragment grouping.

We merged the true synonyms into one canonical entry each and normalised casing to
UPPER_SNAKE. In production this should be resolved against a standard terminology —
LOINC for lab observations — rather than a hand-maintained dictionary, which is exactly
the class of drift this collision demonstrates.

**D-6. Mismatched unit/range rows are flagged, never auto-corrected.**
Where the resolved test's expected unit contradicts the row's stated unit, it is
technically possible to infer the "real" test from the unit and range. We deliberately do
not. Doing so would silently rewrite a clinical record on the basis of a guess, and would
destroy the evidence that the source extraction is misaligned. The platform's job is to
surface upstream data quality, not to launder it. These rows are marked `Invalid` with a
`unit_test_mismatch` flag and routed to the ops queue.

**D-7. Compound vitals are split where unambiguous, flagged otherwise.**
`"100/60 mmHg"` maps to two canonical tests (systolic and diastolic) via the
`derived_from` config block: the raw test name must match a configured
`source_variant` ("BP") AND the result must parse as two numbers joined by the configured
delimiter. A single-number result on the same raw name (`"BP": "100"`, seen once in the
samples) does not match the two-number pattern and falls through to ordinary single-value
resolution instead — verified against the real sample data, where this exact case occurs.
Ambiguous or non-matching compounds are left with `result_value` from ordinary numeric
extraction and `result_text` retained, never coerced.

**D-8. Configured reference ranges override source-provided ranges.**
The `range` field in the samples is demonstrably unreliable (single values where
intervals are expected, ranges belonging to other analytes, unfilled templates). FR-3.1
asks for validation against *medically accepted* ranges, so `reference_ranges.yaml` is
the authority. The source range is still parsed into `range_low`/`range_high` and
compared; systematic disagreement for a given source is itself reported as a quality
signal.

**D-9. Source-provided `test_analytics` is ignored.**
Observed values include `"normal"` on out-of-range results and the literal string
`"low/normal/high"`. We compute our own classification and retain the original for
comparison, reporting the disagreement rate in the UI.

**D-10. Reference ranges are general adult intervals.**
Real intervals are stratified by age, sex, and assay method, and are laboratory-specific.
Several samples are clearly paediatric, where adult intervals do not apply. Using a
single adult interval set is a prototype simplification; production requires
per-laboratory, demographically stratified intervals. This is a known source of false
range flags in the current output.

**D-11. PII arrives pre-redacted, so demographic normalisation is proven by test.**
`age`, `gender`, `patient_name`, `doctor_name`, `uhid`, and `claim_no` all arrive as
`[X REDACTED]` literals. The pipeline detects this pattern and stores null plus a
`redacted` flag rather than treating the literal as a value. Consequently FR-2.5 cannot
be demonstrated on the supplied data — the age, gender, and date normalisers are proven
by unit tests against synthetic values instead. Any identifier that does arrive
unredacted is hashed.

**D-12. Panel headers are structure, not measurements.**
Rows such as `"LIVER FUNCTION TEST (LFT), SERUM"` and
`"COMPLETE BLOOD COUNT (CBC), WHOLE BLOOD EDTA"` are section captions with no result.
They are identified and excluded from test-level metrics rather than counted as
unresolved names.

**D-13. Qualitative and narrative tests are not classification failures.**
Dengue NS1, malaria antigens, Widal, urine microscopy, and imaging findings are
non-numeric by design. Marking them `Invalid` would flood the ops queue with correct
data. They receive `Qualitative` or `Not Applicable` — two states beyond the five FR-3.3
enumerates. See D-14.

**D-14. Two classification states were added beyond the brief's five.**
FR-3.3 enumerates Within Range, Above Range, Below Range, Outlier, Invalid. We added
`Qualitative` (tests that are legitimately non-numeric) and `Not Applicable` (narrative
findings and nursing-chart items with no clinical interval). Both are additive; the five
specified values retain exactly their specified meanings. Collapsing these into `Invalid`
would have made the flag unusable for its stated purpose.

**D-15. Measured coverage against the supplied samples.**
Against the 278 `lab_report` rows produced across the five files (after junk/placeholder/
panel-header rows are routed to dead-letter rather than counted as clinical rows at all —
see D-4, D-12): 255 resolved by exact match (91.7%), 3 by fuzzy match (1.1%), 2 by
embedded-value extraction (0.7%), 2 by derived compound-vital split (0.7%), for **94.2%
overall coverage** (262/278). 16 rows remain unresolved (5.8%) — inspected individually,
every one is a panel-header-shaped string carrying a real, misattributed analyte value
(the same column-misalignment defect as D-3's headline example, just on a caption row
instead of a test row), correctly retained and queued for review rather than guessed at.
NFR-4.1 targets 98% within 30 days of a source going live; 94.2% on five OCR-damaged
samples on day one is consistent with that trajectory once the review-queue-to-dictionary
feedback loop (T-3) runs for real. The dictionary ships with 72 canonical tests and 287
variants. One of the three fuzzy matches was itself a false positive — see T-3a.

**D-16. Duplicate detection is exercisable on the supplied data.**
`Sample_JSON_file5.json` contains the same `discharge_summary` payload twice within
`responseDetails`. Files 1 and 3 are structurally identical with differing identifiers.
Both the intra-file and cross-file dedup paths are therefore demonstrated on real input
rather than synthetic cases.

**D-17. The target output is long-format, resolving a conflict in the brief.**
FR-2.2 describes exactly five columns per test (`Test_Name`, `Test_Name_Result`,
`Test_Name_Range`, `Test_Name_Unit`, `Test_Name_Analytics`), implying a wide table. The
supplied `Ourput-table-ideal-schema.csv` is long-format: one row per test result with
`test_name_canonical`, `result_value`, `range_low`/`range_high`, `unit_canonical`,
`test_analytics`.

We followed the schema file. It is the more specific artefact, and the wide form does not
scale — 72 canonical tests would require 360 columns, and the real world has thousands of
analytes, most null on any given report. All five conceptual fields are preserved; they
are expressed as columns of a row rather than as a column group. Pivoting to the wide
form for a fixed panel is a single SQL view over this table if it is ever required.

**D-18. Duplicate and legacy columns in the supplied schema were consolidated.**
The provided schema contains what appear to be artefacts of an auto-generated union
across several load attempts: `course_during_hospitalisation` / `course_during_hospitalization`,
`page_no` / `page_number`, `medicine` / `medication_medicine` / `medication_name`,
`dose` / `medication_dose` / `discharge_medications_dose`, `report_date` / `reports_date`,
`age` / `age_text` / `age_years` / `basic_info_age`, plus a family of `report_details_*`
and `basic_info_*` raw-passthrough columns duplicating their canonical equivalents.

We implemented a clean canonical subset rather than reproducing all 70+ columns. The raw
payload is retained in full in `raw_json`, so nothing is lost and any passthrough column
can be reconstructed. Final column set (55 columns, `src/pipeline.py > CANONICAL_COLUMNS`),
grouped:

- **Lineage (13):** `document_id`, `record_type`, `file_source`, `trace_id`,
  `correlation_id`, `source_system`, `claim_no`, `nt_code`, `consumer_client_id`,
  `destination_identifier`, `case_id`, `ingested_at`, `row_seq`
- **Standardisation core (15, `lab_report` rows only):** `test_name_original`,
  `test_name_canonical`, `result_value`, `result_text`, `unit_canonical`, `unit_original`,
  `range_low`, `range_high`, `range_text`, `test_analytics`, `source_test_analytics`,
  `normalization_method`, `normalization_confidence`, `page_no`, `flags`
- **Patient/clinical context (20):** `patient_name`, `age_years`, `age_text`, `age_flags`,
  `gender`, `uhid`, `hospital_name`, `lab_or_hospital_name`, `doctor_name`, `bill_date`,
  `reports_date`, `admission_date`, `discharge_date`, `diagnosis`, `brief_history`,
  `general_examinations`, `recommendations`, `hospital_address`, `ward`,
  `post_discharge_advice`, `course_during_hospitalisation`
- **Medication (5, `discharge_summary` rows only):** `medicine`, `dose`, `frequency`,
  `medicine_type`, `other_med_inj_investigations`
- **Audit (1):** `raw_json` — the full classifier payload this row came from (FR-4.3)

Every row carries all 55 columns regardless of record type, nulled where not applicable —
this is what makes the long-format table uniform in the warehouse rather than two
differently-shaped tables glued together. `lab_report` rows populate the standardisation
core and leave medication columns null; `discharge_summary` rows do the reverse, emitting
one row per medication (or one header-only row if the encounter lists none).

---

## 4. Scope Exclusions

Consciously omitted, with what inclusion would require.

**S-1. Medicine name mapping (FR-2.6, optional) — omitted entirely.**
The medication data is the most OCR-damaged content in the samples: `"Spor 977"`,
`"Clo paris Abalamos"`, doses like `"bud- ob of"`, frequencies like `"RUR - 26/mus"`. A
brand→generic dictionary would resolve a negligible fraction and produce false mappings
on the rest, which in a medication context is actively harmful.

*To include:* a licensed drug reference (RxNorm, or a national formulary for the Indian
market), fuzzy matching tuned for pharmaceutical nomenclature, and — more fundamentally —
better OCR at the source. Recommended sequencing: improve extraction quality before
attempting mapping, since mapping accuracy is capped by input accuracy.

**S-2. Deployed cloud execution.**
The pipeline runs as a local Python process against real GCS and BigQuery. Pub/Sub
triggers, Cloud Run services, and Dataflow jobs are designed and documented but not
deployed.

*To include:* roughly a day of infrastructure work — Terraform for bucket, dataset,
Pub/Sub topic and subscription, service accounts, and Cloud Run deployment; plus
rewriting the transform as a Beam pipeline (mechanical, since the transforms are already
pure functions).

**S-3. Schema versioning (NFR-2.3).**
A `schema_version` field exists in config but historical reprocessing against a prior
version is not implemented.

*To include:* config profiles with effective-date ranges, version stamped on each output
row, and a resolver that selects the profile live at the record's ingestion timestamp.

**S-4. Production PII tokenisation (NFR-4.3).**
Truncated SHA-256 hashing, not Cloud DLP. Adequate for pseudonymisation but provides no
key rotation, no re-identification workflow, and no format-preserving encryption.

*To include:* Cloud DLP de-identification templates, a managed key in Cloud KMS, and a
documented re-identification path for authorised users.

**S-5. Live monitoring and alerting (NFR-5.1).**
Metrics are computed and displayed in the UI but not exported to Cloud Monitoring, and
no alert policies exist.

*To include:* custom metric export plus alert policies at the brief's stated thresholds
(error rate above 1%, processing lag beyond SLA).

**S-6. Authentication on the operational UI.**
The Streamlit app has no access control. Acceptable for a local prototype; unacceptable
for an interface exposing patient records.

*To include:* Identity-Aware Proxy in front of a Cloud Run deployment, with IAM groups
mapped to ops roles.

**S-7. Load and burst testing.**
Throughput claims (200k/day steady, 400k/day burst) are argued analytically in the
architecture narrative rather than demonstrated. The prototype has been exercised on five
files.

*To include:* synthetic file generation at volume and a Dataflow run measured against the
p95 latency target.

**S-8. Age-, sex-, and assay-stratified reference intervals.**
See D-10. Single adult interval set in use.

*To include:* per-laboratory interval tables keyed on analyte, method, age band, and sex,
sourced from each performing laboratory.

---

## 5. What We Would Want to Know Before Production

Open questions that materially affect the design and could not be resolved from the
supplied materials:

1. **Is there a true clinic identifier upstream?** Per-clinic metrics, per-clinic
   mappings, and clinic-level SLAs all depend on one. `source_system` is a coarse proxy.
2. **What is the source of the OCR damage, and can it be improved?** Roughly 6% of test
   names require fuzzy matching purely because of character-level truncation. Fixing
   extraction upstream would eliminate a whole class of downstream risk more cheaply than
   any matching improvement.
3. **What is the authoritative reference-range source?** Laboratory-supplied intervals,
   a national standard, or the payor's own clinical policy? This determines whether D-8
   is the correct call.
4. **What action follows each flag?** Whether `Outlier` blocks adjudication, delays it,
   or is merely reported changes how conservative the thresholds should be.
5. **What is the retention and residency requirement for `raw_json`?** Retaining full
   raw payloads for audit (FR-4.3) has cost and data-residency implications at 200k
   files/day.
6. **Is redaction applied at source consistently?** If unredacted PII can arrive under
   some conditions, tokenisation must be treated as a hard control rather than a fallback.
