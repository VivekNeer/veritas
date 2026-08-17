# Solution Architecture

Veritas Claims — Medical Data Standardisation Pipeline

---

## 1. Design Position

Three decisions shape everything else.

**Schema-on-read at the boundary, schema-on-write at the warehouse.** Incoming JSON is
never validated against a fixed contract — sources change and new ones onboard
constantly, so rejecting on structure would make ingestion the bottleneck. Instead the
ingestion layer parses only the shared envelope, and per-document-type field maps in
config translate payloads into the canonical schema. Once through the transform, the
warehouse contract is strict: BigQuery holds one well-typed canonical table, because
analysts and adjudication rules need guarantees.

**Micro-batch, not streaming.** The latency SLA is 15 minutes at p95, not sub-second. At
200k files/day the steady rate is ~2.3 files/second, ~4.6/second at the stated 2x burst.
That is comfortably inside a batch window, and batching amortises BigQuery load costs
across thousands of records instead of paying per-event streaming insert costs. Streaming
would add cost and operational complexity to beat a target that batch already meets.
If the SLA tightened to seconds, the same transform functions would run unchanged under
a streaming Beam runner — the design does not foreclose it.

**Flag, never silently fix.** The source data is OCR-derived and materially damaged.
Where a record is ambiguous, the pipeline records what it found, marks the record, and
routes it for human review. Inference would produce clean-looking output that quietly
misrepresents clinical values — the worst possible failure mode in claims adjudication,
because it is invisible. This principle is why the ops UI and review queue are integral
rather than decorative.

---

## 2. Layers

### Ingestion
Clinics deposit JSON into GCS, partitioned by source and date. Object-create
notifications publish to Pub/Sub; a Cloud Run service batches arrivals and invokes the
transform. Scheduled sweeps via Cloud Scheduler catch anything a notification missed.

Each file carries a shared envelope containing `traceId`, `correlationId`, `documentId`,
and a `metaDetails` key/value list, wrapping one or more logical records under
`data.responseDetails[]`. Each has a `classifier` selecting the parsing profile. One file
yields one to three records; the ingestion layer fans these out.

Deduplication runs at two levels: within a file (the samples contain a genuine repeated
payload) and across files via a deterministic composite key, which is also what makes
reloads idempotent. Both key sets are declared in config, per FR-1.2's requirement that
dedup logic be configurable.

*Prototype:* reads a real GCS bucket from a local process. Pub/Sub and Cloud Run are
designed, not deployed.

### Processing
Four independent standardisation stages, each a pure function of `(value, config)`:

- **Test name resolution** — exact lookup, then fuzzy match above a configurable
  threshold, then unresolved. The tier that fired and its confidence score are written to
  every row (`normalization_method`, `normalization_confidence`), so any mapping decision
  is auditable after the fact and the unresolved queue is triageable by score.
- **Numeric conversion** — extracts `result_value` while always preserving `result_text`.
  Handles embedded units, qualifier text, compound vitals, and detects ranges that have
  leaked into the result field.
- **Unit harmonisation** — alias normalisation, then per-analyte conversion factors.
  Detects units that contradict the resolved test and flags rather than corrects.
- **Demographic normalisation** — age parsing, gender canonicalisation, multi-format
  dates to ISO 8601, and detection of pre-redacted PII placeholders.

Purity is a deliberate choice: it makes the stages unit-testable in isolation and means
they lift into Beam DoFns without redesign if the deployment model changes.

Validation then classifies each result against configured reference ranges, distinguishing
clinically abnormal values from physiologically implausible ones — a haemoglobin of 999
is bad data, not a sick patient, and the two demand different responses.

### Storage
BigQuery, long-format: one row per test result. Chosen over the wide form described in
FR-2.2 because 72 canonical tests would require 360 columns, most null on any given
report, and real-world analyte counts run to thousands. `[See ASSUMPTIONS.md D-17.]`

Two tables: `standardised_records` for canonical output with the full raw payload
retained for audit, and `dead_letter` for records that failed processing, with reason.
Loads are MERGE on a deterministic row id, so re-running over the same inputs cannot
create duplicates.

Lineage is carried on every row — source file path, trace and correlation IDs, source
system, ingestion and processing timestamps — satisfying NFR-4.2 without a separate
lineage store at this scale.

### Configuration
Four YAML files, read at runtime:

| File | Governs |
|---|---|
| `document_type_mappings.yaml` | Envelope paths, per-classifier field maps, dedup keys, PII patterns, junk detection |
| `test_dictionary.yaml` | Canonical names, variants, matching thresholds and preprocessing |
| `reference_ranges.yaml` | Clinical intervals, outlier bounds, classification cascade |
| `unit_conversions.yaml` | Unit aliases, conversion factors, mismatch detection |

Adding a test, a variant spelling, a unit alias, a reference range, or a whole new source
profile is a config edit — no code change, no redeploy. This is NFR-2.1, the one
non-functional requirement the brief marks as required in implementation. A disabled
`_EXAMPLE_NEW_CLINIC` profile ships in the config as executable documentation of the
onboarding path.

### Error handling
Failures are isolated per record: one malformed file cannot block a batch. Parse
failures, unknown classifiers, and unrecoverable rows are written to the dead-letter
table with a reason and the original payload. Because the load is idempotent,
reprocessing after a config fix is simply a re-run over the same inputs.

Template placeholder rows — unfilled extraction templates containing no clinical data —
are the one category dropped rather than flagged, since admitting them would corrupt
every downstream aggregate.

### Operational UI
Streamlit over BigQuery: pipeline dashboard (files received, processed, failed, flagged),
record inspector showing raw JSON beside standardised output, flagged-record queue sorted
by normalisation confidence, and per-source quality metrics. Functional rather than
production-grade, per the brief's own framing.

---

## 3. Non-Functional Requirements

**Scale and performance.** 200k files/day is ~2.3 files/second; the 2x burst is ~4.6/s.
Neither is demanding — the design constraint is cost, not capability. Cloud Run scales to
zero between batches; Dataflow handles the transform with autoscaling workers; BigQuery
batch loads are free of streaming insert charges. The 15-minute p95 target is met with
batch windows well inside the SLA, leaving headroom for retries. Horizontal scaling is
inherent: records are independent, so worker count scales throughput linearly.

**Clinic onboarding.** A documented new source requires profiling its JSON, authoring a
mapping profile, dry-running against sample files, and promoting the config — within the
one-business-day target, with no deploy. Schema versioning over time is designed but not
implemented `[ASSUMPTIONS.md S-3]`.

**Reliability.** Per-record isolation, dead-letter capture, and deterministic row identity
give fault tolerance and idempotency. Availability derives from the managed services;
the local prototype makes no availability claim.

**Data quality and governance.** Measured coverage against the supplied samples is 94.2%
of clinical rows resolved to canonical names (262/278 across the five files: 255 exact, 3
fuzzy, 2 embedded-extraction, 2 derived-compound), against NFR-4.1's 98% target. The 16
unresolved rows are, on inspection, all panel-header strings carrying misattributed real
values — a genuine upstream defect, correctly retained and queued rather than guessed at.
The production loop that sustains coverage over time: unresolved names accumulate in the review
queue with their nearest candidate and score, ops updates the dictionary, affected
records are reprocessed. At larger variant volumes, Vertex AI text embeddings would
replace string similarity as the second tier — the output schema already records method
and confidence, so this is an additive change. PII arrives pre-redacted and is detected
as such; identifiers arriving unredacted are hashed, with Cloud DLP as the production
path.

**Observability.** Every log line carries the record's correlation ID, so a single
record's journey is traceable end to end. The payload supplies both `traceId` and
`correlationId`, which the pipeline propagates rather than generating its own. Pipeline
counters — throughput, error rate, unresolved rate, duplicate rate — are computed per run
and surfaced in the UI; exporting them to Cloud Monitoring with alert policies at the
brief's thresholds is the remaining step `[ASSUMPTIONS.md S-5]`.

---

## 4. Trade-offs Accepted

| Decision | Gained | Given up |
|---|---|---|
| Micro-batch over streaming | Lower cost, simpler ops, meets SLA | Sub-minute latency |
| Deterministic fuzzy matching over LLM resolution | Reproducible, testable, no API dependency | Weaker on genuinely novel variants |
| Long-format over wide output | Scales to thousands of analytes, sparse-friendly | Wide reporting needs a pivot view |
| Flag rather than infer corrections | Auditable, preserves evidence of upstream defects | More records need human review |
| BigQuery over PostgreSQL | Columnar analytics at scale, native GCP fit | Weaker for transactional workloads |
| Config-driven mapping | Zero-code onboarding | Config becomes a governed artefact needing review |
| Local prototype execution | Time budget spent on correctness and documentation | Deployment topology argued, not demonstrated |

---

## 5. Failure Modes Considered

| Scenario | Behaviour |
|---|---|
| Malformed JSON | Dead-letter with reason; batch continues |
| Unknown classifier | Dead-letter; profile added in config, then reprocess |
| Test name unresolvable | Row retained, `Invalid`, nearest candidate and score recorded, queued for review |
| Unit contradicts resolved test | Flagged `unit_test_mismatch`, not corrected |
| Duplicate submission | Suppressed at both intra-file and cross-file levels |
| Pipeline re-run on same inputs | MERGE on deterministic id; no duplicate rows |
| Source changes its schema silently | Unresolved and error rates spike per source in the quality tab; config profile updated |
| BigQuery load failure mid-batch | Batch retried; idempotency makes retry safe |
| Reference range missing for a test | `Invalid` with `no_reference_range_configured`; config-only fix |

---

*Diagram: `docs/architecture_diagram.svg` (solution architecture — sources through
ingestion, the four-stage transform, storage, and the ops UI, with config-driven and
error paths called out). The brief's stated preference for a Draw.io diagram exported into
a Google Slides deck with speaker notes is a manual step outside this repo's scope — the
SVG here is the submitted diagram; building the Slides version is still worth doing before
the panel review if time allows.*
