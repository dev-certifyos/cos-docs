# Design: External source ingestion — vendor file to dispositioned, staged change items

## Purpose and scope

This module turns *"a vendor's manifest landed in GCS"* into *"every row of that delivery has exactly one audited outcome, and the actionable ones are staged change items."* It is deliberately vendor-agnostic: Candor is adapter #1, never a special case.

**This module owns:**

- Consuming the **manifest event** (doc 3's `accuracy-source.file.received` message), with dead-letter protection.
- The **batch registry** — recognizing every delivery ever seen, skipping replays.
- **Batch verification** — checksum, row count, schema version; unknown schema rejects the whole batch, nothing partially staged.
- The **adapter framework** — per-source, config-driven translation of vendor columns and enums into the internal change contract. No vendor name in any code path.
- **Streamed parsing** with crash-resume.
- The **disposition cascade** — every row gets exactly one outcome; counts must reconcile against the manifest.
- The **rejected-recommendations lookup** (D2-07) — suppressing already-rejected suggestions with a full audit trail.
- **Identity resolution** of each row to the canonical practitioner **and to its obligation** (through doc 3's export membership rows), and **quarantine + audited operator replay** for everything that cannot be safely decided.
- The **Candor data-schema proposal** — the content half of the O-1 negotiation (doc 3's manifest contract is the transport half).

**This module does NOT own:**

- Transport, folders, mirroring, the reconciliation sweep — doc 3 ends where this begins.
- The staging tables' final DDL — doc 6 (this doc defines the contract).
- Review of staged items — doc 7. Release/sync — doc 5.

**The one-line thesis:** *nothing is ever silently discarded* — a batch can only complete when the manifest's row count equals the sum of all disposition counts, enforced as a state-machine gate, not a report.

**Neighbors:** SFTP Exchange (doc 3) hands over the manifest event and the GCS archive; Cycle (doc 1) defines the deterministic identity and the chunked worker pattern; Portal lane (doc 2) defines the staged-change-item shape both lanes share; Backend (doc 5) hosts these workers; Database (doc 6) stores registry, rows, and items; UI (doc 7) shows the actionable output.

## Section triage

| Section                       | Included | Reason                                                                        |
| ----------------------------- | -------- | ------------------------------------------------------------------------------- |
| Approach                      | Yes      | The chosen design as numbered steps                                           |
| Alternatives considered       | Yes      | Three rejected designs (managed data processing, standalone service, AWS)     |
| Components and files touched  | **No**   | Pre-implementation design doc — no file lists yet                             |
| Contracts and interfaces      | Yes      | The batch lifecycle, the internal change contract, the O-1 data-schema proposal |
| Data model and migration      | Yes      | Registry, row store, mappings, and the staged-item extension this module writes |
| Security, privacy, and access | Yes      | Tenant checks on external input, least privilege, audited operator replay     |
| Performance and scale         | Yes      | The volume numbers the cost estimates use                                     |
| Observability                 | Yes      | A stuck or silently shrinking batch must page, not wait to be noticed         |
| Audit trail                   | Yes      | **Added section** — the per-row disposition record is compliance evidence     |
| Failure modes and rollback    | Yes      | Poison events, conflicts, outages, resume, rollback                           |
| Test strategy                 | **No**   | Design phase; decisive failure cases are in Failure modes                     |
| Rollout                       | Yes      | Schema-signing blocker, DLQ provisioning, pilot vendor ordering               |

## Approach

Event-driven ingestion inside the Attestation Module Backend: Pub/Sub with an enabled dead-letter queue, a uniqueness-guarded batch registry, page-chunked streaming parse workers on Cloud Tasks, and a disposition cascade gated by the reconciliation invariant — all writing through the shared data layer. Each step: what we do, why, and why not the alternative a reviewer would ask about.

**1. Consume the manifest event on a dead-letter-protected subscription.**

- **What:**
  - Doc 3's manifest event arrives on subscription `accuracy-source-file-received-sub`, configured with Pub/Sub's **native dead-letter policy**: after `maxDeliveryAttempts = 5` failed deliveries, the message moves to the `accuracy-source-dlq` topic instead of redelivering forever.
  - A drain endpoint (the platform's webhook-drain shape: pull, inspect, re-emit, dry-run) re-emits from the DLQ after a fix — **enabled in every environment from day one**.

- **Why a DLQ here when the cycle module skipped one:**
  - Doc 3's sweep can regenerate a *lost* event, but a **poison event** — one that crashes the consumer identically every attempt — would otherwise redelivery-loop and block the subscription.
  - This is the module where input is external and untrusted; cheap insurance against exactly the gap the platform's roster pipeline has today (companion baseline).

- **What if it fails:** consumer down → messages wait in the subscription; poison event → dead-lettered after 5 attempts, subscription unblocked, DLQ-depth alert pages; a drained fix re-emits with no duplicate effect (step 2 dedups).

- **Payloads:** *Contracts → lifecycle step 0*.

**2. Register the batch — the registry is the dedup gate.**

- **What:**
  - The consumer inserts one `source_batches` row under the unique key `(tenant_id, source_id, checksum)`; a duplicate event or re-delivered file no-ops here — acknowledged, audited, skipped.
  - The row carries the manifest facts (vendor batch id, `exportBatchRef`, declared counts, schema version) and a status: `REGISTERED → VERIFYING → PARSING → COMPLETED | REJECTED | PARKED`.

- **Why checksum, not batch id:** a **corrected re-issue** for the same period has a different checksum → a new batch (unactioned rows of the superseded batch become `STALE_SUPERSEDED`); a true re-send has the same checksum → recognized and skipped. The content, not the label, decides.

- **What if it fails:** the insert races itself → the unique key makes one insert win, the other reads the existing row and no-ops; a crash after insert → the redelivered event finds the row and resumes from its status.

- **Payloads:** *Contracts → lifecycle step 1*.

**3. Verify before parsing — checksum, row count, schema version.**

- **What:**
  - Stream-hash the mirrored GCS data file (chunked SHA-256, constant memory), compare to the manifest's `sha256`; compare row counts. Mismatch → batch `PARKED` + alert — a vendor conversation, not code.
  - Resolve the adapter mapping by `(source_id, schema_version)`. Unknown version → the **whole batch `REJECTED`** with a clear error; nothing partially staged.

- **Why whole-batch rejection for unknown schemas:** a schema the mapping does not know means every column's meaning is unverified — partially staging "the columns that still parse" is exactly the silent drift the signed contract (O-1) exists to prevent.

- **What if it fails:** verification is stateless and re-runnable; a crash mid-verify leaves status `VERIFYING`, and the stuck-batch alert plus the redelivered event resume it.

- **Payloads:** *Contracts → lifecycle steps 2–3*.

**4. Parse in chunks — one Cloud Task per page of rows, streaming.**

- **What:**
  - The verified batch is split into **one Cloud Task per ~1,000 rows** (doc 1's chunk pattern; deterministic task names per `(batch, chunk)` — a re-fired split cannot double-enqueue).
  - Each worker streams its slice with commons-csv (the platform's verified roster idiom: first-record-as-header + trim), never loading the whole file; XLSX via fastexcel if a vendor ever ships spreadsheets.
  - Rows insert idempotently on `(source_batch_id, row_number)` — a retried chunk re-inserting is a no-op.

- **Why chunks:** per-chunk retry isolation — one failing slice retries alone while its siblings finish; and resume after a crash is mechanical (list which row numbers exist, continue from the gap).

- **Why not in-process threading for concurrency:** parallelism comes from the queue dispatching many small chunk workers — so the "which language has better concurrency" question dissolves; any runtime just streams its chunk (*Alternatives considered*).

- **What if it fails:** a chunk crash → Cloud Tasks retries it alone, idempotent rows absorb the replay; retries exhausted → the batch never reaches its counts, the completion gate holds it visible, and the stuck-batch alert fires — delay possible, silent loss impossible.

- **Payloads:** *Contracts → lifecycle step 4*.

**5. Disposition every row — exactly one exit per row.**

- **What:** each parsed row walks the ordered cascade (*Contracts → lifecycle step 5*): facility-dropped → invalid → identity resolution (quarantine on lookup outage) → obligation attribution (via doc 3's export items) → excluded-terminated → no-op → duplicate/stale-superseded → previously-rejected → actionable. Every exit is counted and audited (`ROW_DISPOSITIONED`, one per row, always).

- **Why an ordered cascade:** order is the correctness rule — e.g. identity must resolve before the no-op comparison can read the current Golden-record value, and the rejected-recommendations lookup only makes sense on rows that survived everything above it.

- **Why every row binds to an obligation:** a delivery answers the export that requested it, never the current cycle. Example: the September export covers the obligation due 2026-09-30; the practitioner attests in October and a new obligation opens; Candor's September delivery arrives in November — its rows must attach to the **September** obligation, not October's. The batch's `exportBatchRef` plus doc 3's `attestation_export_items` make that binding deterministic on our side, with no dependence on the vendor echoing per-row fields.

- **Why quarantine, never guessing:** a lookup dependency being down says nothing about whether the row is good — quarantined rows are auto-retried on recovery or operator-replayed, never mislabeled `INVALID` (v2 §8; misjoining or mislabeling is the worst-class failure, F7).

- **Why the conflict rule changed from v1:** two rows proposing **different values for the same practitioner + attribute** in one batch were arbitrated by confidence tiers in v1 — D2-07 removed confidence from all logic, so that rule is unimplementable. New rule: **quarantine both** (`CONFLICTING_IN_BATCH`) — a human decides; processing order never silently picks a winner (Q2).

- **What if it fails:** the cascade is per-row — one bad row exits as `INVALID` or `QUARANTINED` and its siblings continue; a mid-cascade crash re-runs idempotently (the row's disposition is written once, keyed by the row).

**6. Stage the actionable rows and close the batch on the reconciliation invariant.**

- **What:**
  - Each `ACTIONABLE` row becomes one staged change item — the same row shape doc 2's portal submissions produce, source-tagged with the vendor's `source_id` (D2-11), carrying the internal change contract plus the effective date (D2-22).
  - The batch flips to `COMPLETED` **only** when `manifest_row_count == no_op + duplicate_stale_superseded + invalid + excluded_terminated + excluded_not_in_export + quarantined + previously_rejected + facility_dropped + actionable`. Otherwise it stays visibly unfinished and alerts.

- **Why a state-machine gate, not a report:** "nothing silently discarded" (v2 §8) must be structurally checkable — a batch that lost rows *cannot* complete, rather than completing and hoping someone reads the reconciliation report.

- **What if it fails:** counts that never reconcile leave the batch in `PARSING` with the mismatch alert paging; quarantined rows do not block completion — they are counted (their later resolution is its own audited transition).

- **Payloads:** *Contracts → lifecycle step 6*.

**What the approach deliberately does NOT do:**

- No writes toward the Golden record — output is staged change items behind the review boundary (D2-01 lives downstream).
- No vendor name in any table, topic, class, or API name — vendor knowledge lives only in registry and mapping rows (D2-11).
- No confidence-based logic of any kind — a vendor confidence field is stored verbatim as inert data, visible and auditable, read by nothing (D2-07).
- No fuzzy identity matching — crosswalk resolution is exact-match only; anything else quarantines (F7).
- No parsing in doc 3's territory — this module starts at "a manifest event arrived" and reads only the GCS archive.

### Key decisions

| #   | Question                       | Resolution                                                                                                       |
| --- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| D1  | Event protection               | Pub/Sub native DLQ, `maxDeliveryAttempts = 5` → `accuracy-source-dlq` + drain endpoint, **enabled from day one** |
| D2  | Batch dedup key                | Unique `(tenant_id, source_id, checksum)` — content decides; a corrected re-issue is a new batch                 |
| D3  | Adapter mapping home           | `source_schema_mappings` table keyed `(source_id, schema_version)` — **not** `tenant_configurations` (wrong axis: mappings are tenant-independent; verified one-row-per-type uniqueness) |
| D4  | Same-batch conflicts           | **Quarantine both** (`CONFLICTING_IN_BATCH`) — v1's confidence-tier rule is unimplementable post-D2-07 (Q2)      |
| D5  | Vendor confidence field        | Stored verbatim as inert data — no logic reads it (Q4)                                                           |
| D6  | Batch completion               | State-machine gate: manifest row count must equal the sum of disposition counts                                  |
| D7  | Normalization                  | New versioned utility (`norm-v1`) — no platform library exists (verified); no Smarty call in the lookup path (Q3) |
| D8  | Quarantine replay source       | The staged rows in the database, not the original file (the roster REVALIDATE precedent); full-batch reprocessing alone re-reads GCS |
| D9  | Operation enum                 | `ADD \| UPDATE \| REMOVE` — identical to doc 2's staged-item contract; Candor's `KEEP` maps to the no-op path, never to an operation |
| D10 | Runtime placement              | Worker code inside the Attestation Module Backend package (doc 5), writing through the data layer — dispositions, staged items, and audit rows share transactions |
| D11 | Obligation attribution         | Every inbound row binds to the obligation of the export it answers: `(exportBatchRef, practitioner)` point read on doc 3's `attestation_export_items` fills `task_id`; no match → `EXCLUDED_NOT_IN_EXPORT`. A late delivery never attaches to a newer cycle |

## Alternatives considered

Each alternative in full: what it is, its genuine strengths, its costs, and the direct reason it lost to the chosen approach.

### Managed data processing (Dataflow/Beam, or BigQuery-load + SQL dispositions)

- **What it is:**
  - Load the mirrored file into BigQuery (or run an Apache Beam pipeline on Dataflow — GCP's managed data-processing runtime).
  - Express the dispositions as set-based SQL or pipeline transforms; write the actionable rows back to the staging store.

- **Pros:**
  - Set-based dispositions (no-op detection, duplicate joins) are elegant in SQL; scales to millions of rows without thought.
  - Fully managed compute; Dataflow retries and checkpointing built in.

- **Cons:**
  - **Transactional audit breaks:** dispositions computed in BigQuery/Dataflow cannot commit atomically with the Spanner staged items and audit rows — the same-transaction rule is a non-negotiable.
  - The rejected-recommendations lookup and crosswalk identity resolution are **row-by-row keyed reads against live Spanner state** — exactly what batch SQL engines do worst; quarantine-on-outage semantics do not map to a set operation.
  - A new runtime and skillset (Beam), or a data-warehouse hop, for a 3k–100k-row monthly file.

- **Why rejected:** it fails the transactional-audit and quarantine requirements structurally, and its one strength — effortless scale — solves a volume problem the module does not have.

- **Cost:** Dataflow job-hours ≈ **$10–30/month** at our cadence; BigQuery pennies — irrelevant, the approach is disqualified above.

### A standalone ingestion service in Go or Python

- **What it is:**
  - A separate Cloud Run service (Go's goroutines or Python's asyncio plus a fast CSV library) consumes the events, parses, and calls the backend's APIs to stage rows.
  - Motivated by "better concurrency and CSV ergonomics" than Java.

- **Pros:**
  - Go/Python CSV ergonomics are genuinely nicer; a dedicated service isolates ingestion load and deploys.

- **Cons:**
  - The concurrency argument evaporates under the chosen shape: **parallelism comes from Cloud Tasks dispatching many small chunk workers**, not in-process threads — a Java worker streaming 1,000 rows is nowhere near any language's limits (the roster pipeline already streams far bigger files in Java, verified).
  - Staging through HTTP APIs instead of the shared data layer either **breaks same-transaction audit** or forces the backend to expose internal batch-write endpoints — new surface, new failure modes.
  - A new deployable = a new pipeline, dashboard set, secrets, and on-call surface — for a monthly batch job.
  - The team is Java/Quarkus; every verified parsing idiom is Java.

- **Why rejected:** it chases a concurrency win the chunked workers already provide, and pays a permanent deployable-plus-broken-transaction tax for it.

- **Cost:** scale-to-zero Cloud Run ≈ **$5–15/month** plus the deployable's un-dollared operational overhead — no capability gained.

### AWS (S3 events → SQS → Lambda parsers → DynamoDB registry)

- **What it is:**
  - The same pipeline shape on AWS managed pieces: S3 object events feed SQS, Lambda workers parse, a DynamoDB table is the registry.
  - Included because it is a genuine infrastructure choice; it repeats the doc 1/doc 3 cross-cloud pattern.

- **Pros:**
  - First-class managed pieces; Lambda parse scale is effortless.

- **Cons:**
  - The data, staging tables, audit store, and crosswalk lookups live on GCP — every row round-trips clouds.
  - Same-transaction audit is impossible across clouds; a second cloud's IAM, monitoring, bill, and on-call for a monthly file.

- **Why rejected:** it breaks the same-transaction audit rule and adds a whole cloud of operational surface — the same verdict as doc 1's and doc 3's AWS variants, for the same reasons.

- **Cost:** ≈ **$20–50/month** + cross-cloud egress + the un-dollared second on-call. Dominated.

## Contracts and interfaces

**Who runs what:** the consumer, verifier, and parse workers live in the Attestation Module Backend (doc 5), writing through the data layer; the DLQ drain and quarantine replay are internal admin endpoints on the same backend (shared-secret header auth, the doc 1 pattern). Per-tenant knobs come from the `attestation-module-config` entry.

### Configuration and flags

Every tenant-level knob this module reads or adds, in one place (doc 1's carry-forward rule; doc 5 owns the consolidated schema). All keys live in the shared `attestation-module-config` entry. **No feature flags** — no Flagsmith or other flag system; the kill switch is a config key.

| Key | Lives in | Type | Default | Controls |
| --- | --- | --- | --- | --- |
| `effectiveDateFallback` | `attestation-module-config` | rule (enum) | end of the current month (D2-22) | The effective-date rule applied when a row carries no verification date |
| `ingestionPaused` | `attestation-module-config` | boolean, per tenant/source | `false` | Kill switch: batches still register, then wait in `REGISTERED` (see *Failure modes and rollback*) |
| `quarantineRetrySchedule` | `attestation-module-config` | schedule | none stated — set per tenant | How often dependency-outage quarantines auto-retry |

### The lifecycle, end to end — every hop with its artifacts

> The full chain in execution order: manifest event → registration → verification → adapter resolution → chunked parse → disposition cascade → staged items → completion gate. Field names are the contract for docs 5–6. Rationale per step: *Approach* (0 → step 1, 1 → step 2, 2–3 → step 3, 4 → step 4, 5 → step 5, 6 → step 6).

**Step 0 — the wake-up event (doc 3's contract, consumed here).**

```json
{
  "eventType": "accuracy-source.file.received",
  "sourceId": "candor",
  "tenantId": "org-xyz",
  "manifestUri": "gs://acc-archive/inbound/candor/org-xyz/org-xyz_CB-2026-09-001_20260903.manifest.json",
  "dataFileUri": "gs://acc-archive/inbound/candor/org-xyz/org-xyz_CB-2026-09-001_20260903.csv",
  "gcsGeneration": "1725348912345678",
  "depositedBy": "sftp-candor",
  "detectedAt": "2026-09-03T14:05:00Z"
}
```

- Delivery is at-least-once (duplicates expected); the registry dedups. The subscription's dead-letter policy is D1.
- The consumer reads the manifest from `manifestUri` — the event is a pointer, never the data.
- **Audit written in this step:** `MANIFEST_RECEIVED` (vendor batch id, `exportBatchRef`, declared sha/counts, producedAt, the Pub/Sub message id).

**Step 1 — batch registration (the dedup gate).**

The registry row (`source_batches`, contract for doc 6):

```json
{
  "id": "sb-77aa…",
  "tenantId": "org-xyz",
  "sourceId": "candor",
  "vendorBatchId": "CB-2026-09-001",
  "exportBatchRef": "org-xyz-candor-2026-09-001",
  "checksum": "9f3ac2…",
  "schemaVersion": "candor-recs-v1",
  "declaredRowCount": 2987,
  "status": "REGISTERED",
  "dispositionCounts": null,
  "registeredAt": "2026-09-03T14:05:07Z"
}
```

- `UNIQUE (tenant_id, source_id, checksum)` — the idempotency backbone of the whole lane: a re-delivered file or duplicate event no-ops here.
- `exportBatchRef` ties the delivery to the outbound export that caused it (doc 3's echo rule).
- **Audit written in this step:** `BATCH_REGISTERED`, or `BATCH_SKIPPED_ALREADY_SEEN` (the dedup decision is itself evidence).

**Step 2 — verification (status `VERIFYING`).**

- Stream the GCS data file through SHA-256 (chunked, constant memory) and compare to the manifest's `sha256`; count rows and compare to `declaredRowCount`.
- Mismatch → status `PARKED` + alert; nothing parsed, nothing staged — the resolution is a vendor conversation.
- The manifest's `tenantId` must match the folder tenant and the batch's tenant — a mismatch rejects with an explicit error, never cross-tenant ingestion.
- The manifest's `exportBatchRef` must match a registered `attestation_export_batches` row for this tenant/source — a point read on doc 3's export registry, written before anything was uploaded. An unknown reference parks the batch (doc 3's echo rule, closing the loop).
- **Audit written in this step:** `CHECKSUM_VERIFIED` / `CHECKSUM_MISMATCH`, `ROWCOUNT_VERIFIED` / `ROWCOUNT_MISMATCH`.

**Step 3 — adapter resolution.**

- Look up `source_schema_mappings` by `(source_id, schema_version)`. Unknown → batch `REJECTED`, whole and loudly (the drift-at-the-gate defense); known → status `PARSING`.
- A mapping declares: column → attribute translations, enum translations (Candor's `UPDATE`/`ADD`/`REMOVE` → our operations; `KEEP` → the no-op path; `verification_status` values passed through as data), value-format rules, and which columns are identity vs payload.
- **The internal change contract** — what every adapter produces per row, whatever the vendor's format:
  - canonical practitioner identity (resolution in step 5),
  - attribute path, plus `entry_key` for list entries — the same routing-id field doc 2's deltas carry,
  - for list entries, observed and proposed values are the **full entry object** (doc 2's D15 rule), so the release path can derive the merge engine's grouping key (`ARRAY_FIELD_GROUPING_KEYS`, e.g. `[address1, city, state, zipcode]`) from the values — the derivation is doc 5's,
  - operation `ADD | UPDATE | REMOVE` (doc 2's enum, identical),
  - observed current value · proposed value,
  - evidence (one plain string: a date or a URL, D2-27) · verified-at date · source row reference.
- A vendor confidence column, if present, is carried verbatim as inert data (D5).
- **Audit written in this step:** `SCHEMA_VERSION_RESOLVED` (which mapping) / `SCHEMA_VERSION_REJECTED` (the whole-batch rejection evidence).

**Step 4 — chunked parse (one Cloud Task per ~1,000 rows).**

Task name: `ingest-sb-77aa-0003` — deterministic per `(batch, chunk)`; the queue refuses a duplicate name.

Body:

```json
{
  "sourceBatchId": "sb-77aa…",
  "tenantId": "org-xyz",
  "sourceId": "candor",
  "chunkNumber": 3,
  "rowRange": { "from": 3001, "to": 4000 }
}
```

- The worker streams its slice via commons-csv and inserts one row-store row per line, idempotent on `(source_batch_id, row_number)` — a retried chunk is a no-op; resume after a crash = list existing row numbers, continue from the gap.
- **Audit written in this step:** none per parse insert — the row's audit is its disposition (step 5); a chunk that exhausts retries writes `INGEST_CHUNK_FAILED` on its final attempt (the doc 1 pattern — Cloud Tasks has no dead-letter queue; named distinctly from doc 1's cycle-chunk event, whose detail differs).

**Step 5 — the disposition cascade (order matters; every row exits at exactly one step).**

1. **`FACILITY_DROPPED`** — facility-typed rows: counted, audited, never staged (D2-15).
2. **`INVALID`** — malformed/unmappable rows; reason recorded, never shown to reviewers.
3. **Identity resolution** — NPI (+ the address components for location rows) → canonical practitioner via crosswalk, **exact match only** (no fuzzy matching — a misjoin is a worst-class failure). Lookup dependency down → **`QUARANTINED`** ("decide later": auto-retried, operator-replayable — never mislabeled).
4. **Obligation attribution** — point read on doc 3's `attestation_export_items` by `(export batch ← exportBatchRef, practitioner)`: found → the row inherits that obligation's `task_id` (and through it the full deterministic identity — a late delivery binds to the cycle that requested it, never a newer one); not found → **`EXCLUDED_NOT_IN_EXPORT`** (the vendor sent a row we never asked it to verify this cycle — counted, audited, never staged; surfacing these later is a configuration change, the D2-07 pattern). Same-database read — no outage quarantine needed.
5. **`EXCLUDED_TERMINATED`** — the practitioner is terminated; reason recorded.
6. **`NO_OP`** — the proposed value equals the current Golden-record value (compared post-normalization), or the vendor's recommendation is `KEEP`. Filtered from review, counted.
7. **`DUPLICATE` / `STALE_SUPERSEDED`** — an equivalent item is already pending, or a newer batch supersedes this row; kept for audit.
8. **`PREVIOUSLY_REJECTED`** — the rejected-recommendations lookup matched (below): suppressed from the queue in MVP, full audit row naming the matching `rejected_recommendation_id` (D2-07).
9. **`ACTIONABLE`** — becomes a staged change item (step 6).

**The conflict rule (D4):** two rows in one batch proposing different values for the same practitioner + attribute → **both `QUARANTINED`**, reason `CONFLICTING_IN_BATCH` — a human decides; processing order never silently picks a winner. Same-value duplicates are just `DUPLICATE`.

**The rejected-recommendations lookup (read side; doc 5 owns the write side, on every external-lane rejection):**

- Per chunk, batch-load the rejected rows for the chunk's practitioners (keyed reads), then exact-match on `(tenant_id, certify_practitioner_id, attribute, operation, normalized_value)` — the v2 §6.6 key, index-served.
- **Normalization is new, versioned code** (`norm-v1` — no platform library exists, verified): trim + whitespace collapse + casing (rules copied from the cleansing layer's `DataCleansingUtil` — a service, not a library); phones → digits-only with a country-code default (no phone normalizer exists anywhere in the platform); addresses → conservative text normalization only (uppercase, collapse whitespace, strip punctuation).
- The Smarty-backed address service is deliberately **not** called in this path — an external HTTP dependency inside a matching rule would make lookups outage-prone; conservative matching means at worst an occasional resurfaced rejection, the safe failure direction (Q3).
- Every stored row and every comparison carries the **rules version**; a future `norm-v2` triggers recomputation of stored values — never silent mismatch.
- The memory applies to **this lane only** (D2-07): a provider's own re-submission of a previously rejected edit always reaches the queue.

**The effective date (D2-22):** the row's verified-at date when present, else the configured fallback (end of the current month). Stamped on the staged item; enforcement at release time is doc 5's job.

**Audit written in this step:** **`ROW_DISPOSITIONED` — one per row, always**; plus `ROW_QUARANTINED` with its reason for cascade exits 3 and the conflict rule.

**Step 6 — staged items and the completion gate.**

One staged change item per `ACTIONABLE` row — the shape doc 2 defined, extended for this lane:

```json
{
  "id": "si-31bb…",
  "tenantId": "org-xyz",
  "certifyPractitionerId": "cert-000123",
  "submissionId": null,
  "taskId": "task-7f3a…",
  "sourceBatchId": "sb-77aa…",
  "sourceRowNumber": 3007,
  "attribute": "telephoneNumbers",
  "operation": "UPDATE",
  "entryKey": "office-1",
  "oldValue": { "number": "555-0100" },
  "newValue": { "number": "555-0199" },
  "evidence": "https://vendor-evidence.example/rec/8812",
  "verifiedAt": "2026-08-28",
  "effectiveDate": "2026-09-30",
  "source": "candor"
}
```

- ⚠ **Proposed amendment to doc 2's `attestation_staged_items` contract** (per the cross-document compatibility rule): additive nullable columns `source_batch_id`, `source_row_number`, `evidence`, `verified_at`, `effective_date`; `submission_id` becomes nullable — vendor-lane rows have no submission. `task_id` stays required: vendor rows fill it from the export item (cascade step 4), so the deterministic identity is reachable from every staged row through its task — both lanes alike (v2 §6.1 holds). Column names, `attribute`/`operation`/`entry_key`/`old_value`/`new_value`/`source` semantics, and the operation enum are doc 2's, unchanged. Doc 6 consolidates the DDL.
- The reviewer's side-by-side view still joins the two lanes by the canonical practitioner identity (v2 §6.8); a vendor row's `task_id` names the obligation whose export it answers — possibly already `SUBMITTED` by the time the delivery lands — it is provenance, never the queue's join condition.
- `source` stores the bare registry reference (`candor`); at release the sync worker maps it to the tenant-scoped slice source id (`candor:{tenantId}` — one source per lane, v2 D2-37) — that mapping is doc 5's.
- The batch flips to `COMPLETED` only when the reconciliation invariant holds (D6); its per-disposition counters are written on the registry row.
- **Audit written in this step:** `STAGED_ITEM_CREATED` per item (the same event and detail contract doc 2 defined, committed with the item); `BATCH_COMPLETED` with the reconciled counts — written only when the invariant holds.

### Operator surface (internal, audited)

- **DLQ drain:** `POST /internal/attestation-ingestion/dlq/drain` — pull, inspect, re-emit (with a dry-run flag). Every drain is an audited operator action.
- **Quarantine replay:** `POST /internal/attestation-ingestion/quarantine/replay` — re-runs the cascade for named quarantined rows, **reading the staged rows from the database, not the file** (D8; the GCS original remains the source for full-batch reprocessing only). Terminal closure exists (`CLOSED_BY_OPERATOR` with a reason) so quarantine is never an infinite loop.

### The Candor data-schema proposal (the O-1 content half)

Grounded in Candor's own July 2026 data dictionary (S13) and the drift observed in the real MMO delivery (S12/CT-011 — the technical call's job is to eliminate that drift by signing one version). Bring together with doc 3's manifest contract (*doc 3 → Contracts, the manifest contract*) as the full O-1 proposal (Q1).

**Inbound row (vendor → us), proposed columns:**

| Column | Meaning | Notes |
| --- | --- | --- |
| `npi` + address identity (`address1, city, state, zipcode`) | Which practitioner + which location row | Matches the merge engine's list grouping key |
| `attribute` | Which field the recommendation concerns | From the signed attribute vocabulary |
| `recommendation` | `KEEP / UPDATE / ADD / REMOVE` | S13's own semantics; `KEEP` maps to the no-op path |
| `current_value_seen` | What the vendor believes we publish | Drift detector |
| `recommended_value` | The proposed value | Required when not `KEEP` |
| `verification_status` | `VALID / INVALID / UNKNOWN / INCONCLUSIVE` | S13 enum, passed through as data |
| `verification_reason` | S13 reason enum (e.g. `direct_outreach`, `deceased`) | Data + reviewer context |
| `evidence` | One string: a date or a URL | D2-27 (rendered as copyable text) |
| `verified_at` | When the vendor verified | Feeds the effective date (D2-22) |
| `confidence` *(optional)* | Whatever the vendor sends | **Stored inert** — no logic reads it (D2-07) |

**Outbound row (us → vendor):** the deterministic identity + NPI + the mandated verify-field values (doc 2's field list) + our export batch id. Schema version strings in both directions; unknown version = whole-batch reject.

## Data model and migration

- **Where everything lives:** the same app-data Spanner database as the other module tables (doc 1's D8) — new tables via DAL Liquibase changesets; final DDL is doc 6's, these shapes are this doc's contract.

- **`source_batches`** — the batch registry (shape in *Contracts → step 1*): `UNIQUE (tenant_id, source_id, checksum)`; manifest facts; per-disposition counters; status `REGISTERED → VERIFYING → PARSING → COMPLETED | REJECTED | PARKED`; timestamps per status. Precedent acknowledged: the platform's `source_ingestions` table is the same idea at file granularity; this adds verification state and counters.

- **`source_batch_rows`** — the row store: one row per data-file line, `UNIQUE (source_batch_id, row_number)`, the raw parsed values, the translated change contract, the resolved `certify_practitioner_id` and `task_id` (after cascade steps 3–4), disposition + reason + retry counter, `normalization_version`. Quarantined rows live here (a status, not a separate table — the roster precedent).

- **`source_schema_mappings`** — adapter mappings keyed `(source_id, schema_version)`, tenant-independent (D3): column translations, enum translations, value-format rules, identity-vs-payload column roles.

- **`rejected_recommendations`** — read here, written by doc 5 on every external-lane rejection. The v2 §6.6 contract restated: normalized, indexed columns `(tenant_id, certify_practitioner_id, attribute, operation, normalized_value)` under a composite index, append-only, per-tenant indexed, carrying `normalization_version` and metadata (source, rejected-by/-at, reason, batch/item references).

- **`attestation_staged_items`** — owned by doc 2; this module writes vendor-lane rows under the ⚠ amendment in *Contracts → step 6*.

- **Sources:** one `core_sources`-registry row per vendor (D2-11) — adding a vendor is a registry row + a mapping row + SFTP credentials (doc 3), never a schema change.

- **Migration:** additive only — three new tables + indexes; no shared-table changes, no backfill (batches accrue from go-live).

## Security, privacy, and access

- **Tenant isolation on external input, three checks:** the manifest's `tenantId` must match its GCS folder, the registry row, and every parsed row's resolution — a mismatch rejects with an explicit error, never cross-tenant ingestion (v2 §8).
- **Data sensitivity:** vendor files carry provider demographic/practice data (names, addresses, phones, NPIs) — no member data. Read from the GCS archive over TLS; staged into Spanner behind the data layer.
- **Least privilege:** the ingestion workers' service account reads exactly the archive bucket (read-only) and writes only through the data layer; the DLQ drain and replay endpoints are internal-only, shared-secret authenticated (the doc 1 pattern).
- **Untrusted input discipline:** vendor files are parsed with bounded-memory streaming (no decompression bombs into memory), validated against the signed schema before any row is interpreted, and never executed or templated.
- **Operator actions are audited, never direct DB edits:** every drain, replay, and closure records who, what, and outcome (v2 §8 — operator recovery is always an audited replay).

## Performance and scale

| #   | Item             | Value / assumption                                                                                       |
| --- | ---------------- | ----------------------------------------------------------------------------------------------------------- |
| N1  | Batch size       | Observed: ~3k rows (the real MMO delta report, S12). **Design headroom: 100k rows** per batch            |
| N2  | Cadence          | Monthly per vendor per tenant (configurable) — a handful of batches a month at MVP                       |
| N3  | Parse budget     | A 100k-row batch fully dispositioned in **< 30 minutes**; memory bounded (streaming, never whole-file)   |
| N4  | Event protection | A poison event must not block the subscription — dead-lettered after 5 attempts                          |
| N5  | Audit volume     | One `ROW_DISPOSITIONED` per row, always — a 100k-row batch is 100k audit rows; 7-year retention (D2-20)  |
| N6  | Tenant isolation | Server-side everywhere; the manifest's `tenantId` must match folder, batch, and rows                     |
| N7  | Growth headroom  | 1M-row weekly batches = bigger chunks + a dedicated worker pool — tuning, not redesign; ten vendors = ten mapping rows, zero code |

**Cost (monthly, derived at N1/N2):** Pub/Sub + DLQ (a few messages) ≈ **$0** · Cloud Tasks (a few hundred chunk tasks; first 1M ops free) ≈ **$0** · parse compute on the existing backend runtime (minutes per batch) ≈ **< $5** · crosswalk-lookup Spanner reads ≈ **< $2** → **Total ≈ $5–10/month** (staging/audit storage counted in doc 6).

## Observability

Best-effort, outside transactions; never blocks a batch.

**Metrics** (low-cardinality labels: tenant and source yes, practitioner never):

```
attestation.ingest.batch.received           counter  {tenant, source}
attestation.ingest.batch.skipped_duplicate  counter  {tenant, source}
attestation.ingest.batch.rejected           counter  {tenant, source, reason}
attestation.ingest.batch.duration           timer    {tenant, source}
attestation.ingest.row.dispositioned        counter  {tenant, source, disposition}
attestation.ingest.quarantine.backlog       gauge    {tenant, source}
attestation.ingest.dlq.depth                gauge    {}
```

**Alerts:**

- **DLQ depth > 0 → page** — a poison event is blocking a vendor's delivery.
- Batch in `VERIFYING`/`PARSING` > 4 hours → stuck-batch page.
- Reconciliation mismatch at completion attempt → page — the invariant caught something.
- `SCHEMA_VERSION_REJECTED` → immediate notify — the vendor shipped an unsigned change (the CT-011 scenario, caught at the gate).
- Quarantine backlog growing week-over-week → ops ticket.
- Zero batches from a vendor beyond the SLA window → doc 3's vendor-response-overdue alert (shared).

**Correlation:** Pub/Sub message id → `sourceBatchId` → row numbers → staged item ids → the deterministic identity — one batch id returns the delivery's whole story.

## Audit trail

*Added section — the per-row disposition record is compliance evidence.*

**Audit table: `attestation_audit_events`** — the shared table doc 1 creates (final DDL doc 6); this module writes its batch and row events into it and owns no audit table of its own.

Rules: append-only; committed in the same transaction as the state change it records; 7-year retention (D2-20); every event carries tenant + source + its batch/row/item correlation.

- **`source_batch_id`, `row_number`, and `staged_item_id` are first-class, indexed columns** on these events (doc 6 DDL) — never buried in a JSON blob.
- A batch id is a **lookup key**: `WHERE source_batch_id = @x` returns everything a delivery caused, row by row.

### The common envelope — every event carries these fields

```json
{
  "id": "ae-90ef…",
  "type": "ROW_DISPOSITIONED",
  "tenantId": "org-xyz",
  "sourceId": "candor",
  "sourceBatchId": "sb-77aa…",
  "rowNumber": 3007,
  "certifyPractitionerId": "cert-000123",
  "stagedItemId": null,
  "actor": "system:attestation-ingestion",
  "occurredAt": "2026-09-03T14:22:41Z",
  "detail": { }
}
```

- **Envelope carried forward from docs 1–3, plus additive correlation fields:** `sourceId`, `sourceBatchId`, `rowNumber`, `stagedItemId` (all nullable — no inherited field changes meaning; doc 6 owns the consolidated shape).
- `certifyPractitionerId` fills once identity resolution succeeds; `taskId` and `duePeriod` (doc 1's base fields) fill once obligation attribution succeeds (cascade step 4). All null on batch-level events and on rows that never resolved.
- `actor` — `system:attestation-ingestion` for pipeline events; a user id for operator actions (a drain, a replay, a closure).

### Per-event contracts — the `detail` fields and where each commits

| Event | Committed in | `detail` fields |
| --- | --- | --- |
| `MANIFEST_RECEIVED` | its own write, on event consumption | `vendorBatchId`, `exportBatchRef`, `declaredSha256`, `declaredRowCount`, `producedAt`, `pubsubMessageId` |
| `BATCH_REGISTERED` | the registry-insert transaction | `checksum`, `schemaVersion`, `declaredRowCount` |
| `BATCH_SKIPPED_ALREADY_SEEN` | its own write (nothing else changed) | `existingSourceBatchId`, `existingStatus` |
| `CHECKSUM_VERIFIED` / `CHECKSUM_MISMATCH` | the status-transition transaction | `computedSha256`, `declaredSha256` |
| `ROWCOUNT_VERIFIED` / `ROWCOUNT_MISMATCH` | the status-transition transaction | `countedRows`, `declaredRowCount` |
| `SCHEMA_VERSION_RESOLVED` / `SCHEMA_VERSION_REJECTED` | the status-transition transaction | `schemaVersion`, `mappingId` (resolved) / `error` (rejected) |
| `INGEST_CHUNK_FAILED` | its own write, on the final failing attempt | `chunkNumber`, `rowRange`, `attempt`, `error` |
| **`ROW_DISPOSITIONED` — one per row, always** | the row's disposition transaction | `disposition`, `reason`, `normalizationVersion`, `rejectedRecommendationId` (previously-rejected rows only) |
| `ROW_QUARANTINED` / `QUARANTINE_RETRIED` / `QUARANTINE_RESOLVED` / `QUARANTINE_CLOSED_BY_OPERATOR` | the row's transition transaction | `reason` (`DEPENDENCY_OUTAGE` \| `CONFLICTING_IN_BATCH`), `dependency`, `attempt`, `resolution` |
| `STAGED_ITEM_CREATED` | the row's disposition transaction (doc 2's event, same detail contract) | `stagedItemId`, `attribute`, `operation`, `entryKey`, `oldValue`, `newValue`, `source` |
| `BATCH_COMPLETED` | the completion transaction — written only when the invariant holds | per-disposition counts, `declaredRowCount`, `durationMs` |

- "Committed in" is a contract, not a note: events listed with a transaction are written **inside** it — if the change rolls back, so does its event.
- An engineer implementing from this table should never have to invent an audit field. A field found missing during implementation is a gap in this doc — fix it here first.

Questions this answers directly: *"why did the reviewer never see this Candor row?"* → its `ROW_DISPOSITIONED(PREVIOUSLY_REJECTED)` names the exact prior rejection it matched · *"did every row of the Sept delivery get an outcome?"* → `BATCH_COMPLETED`'s reconciled counts · *"who replayed this quarantined row and what happened?"* → the `QUARANTINE_*` chain.

## Failure modes and rollback

**Edge cases:**

| Case | Handling |
| --- | --- |
| Same file delivered/mirrored twice | Registry unique `(tenant, source, checksum)` → skipped, audited |
| Corrected file re-issued for the same period | New checksum → new batch; superseded unactioned rows from the old batch → `STALE_SUPERSEDED` |
| Parser crash mid-batch | Chunk retries alone; rows idempotent on `(source_batch_id, row_number)`; resume from the gap |
| Unknown/changed schema version | Whole batch `REJECTED`, nothing partially staged, vendor notified — the CT-011 gate |
| Two rows, same practitioner + attribute, different values | **Both quarantined** (`CONFLICTING_IN_BATCH`) — no confidence tiers exist to arbitrate (D2-07); order never picks the winner |
| Recommendation for a terminated practitioner | `EXCLUDED_TERMINATED`, reason recorded, never queued |
| Crosswalk/lookup outage mid-parse | Rows `QUARANTINED`, auto-retried on recovery — never mislabeled `INVALID` |
| Facility rows in the delivery | `FACILITY_DROPPED`, counted per batch (D2-15) |
| Identical recommendation re-sent after a rejection | `PREVIOUSLY_REJECTED` — suppressed, audit row names the prior rejection (D2-07) |
| Normalization rules change after go-live | Versioned rules (`norm-v1`); a bump triggers recomputation of stored lookup values — never silent mismatch |
| 100k-row batch | Streaming + one Cloud Task per chunk: bounded memory, minutes of compute (N3) |
| Manifest counts ≠ file contents | `ROWCOUNT_MISMATCH` → batch `PARKED` + alert — nothing staged |
| Manifest references a missing/corrupt data file | Checksum step fails → `PARKED`; doc 3's sweep grace-period alert covers the vendor side |
| Manifest `tenantId` ≠ folder tenant | Explicit rejection — never cross-tenant ingestion |
| Delivery arrives after a newer obligation opened | Obligation attribution binds each row to the export batch's obligation (`exportBatchRef` → export items) — never to the current cycle |
| Vendor row for a practitioner not in the referenced export | `EXCLUDED_NOT_IN_EXPORT` — counted, audited, never staged; surfacing later is a config change |
| Poison event (consumer crashes on it every time) | Native Pub/Sub DLQ after 5 attempts; drain endpoint re-emits post-fix; subscription never blocked |
| Chunk exhausts all retries | `INGEST_CHUNK_FAILED`; the batch cannot complete (the gate holds it) and the stuck-batch alert pages — delay possible, loss impossible |

**Rollback:**

- Per-tenant/per-source kill switch (`ingestionPaused` in `attestation-module-config`): incoming events still **register** their batch (so nothing is lost and the DLQ policy is never fought), then stop — the batch waits in `REGISTERED`; re-enabling resumes registered batches from the registry, and the stuck-batch alert covers a forgotten pause.
- Registered batches, dispositioned rows, staged items, and audit events explicitly **stay** — they are recorded business facts; review (docs 5/7) decides what proceeds.
- The upstream brake also exists: doc 3's pickup can be paused per vendor, and nothing here fires without a mirrored manifest event.
- Schema is additive-only (new tables) — schema rollback never required.

## Rollout

1. Additive schema migration: `source_batches`, `source_batch_rows`, `source_schema_mappings` + indexes, and the ⚠ staged-item amendment — DAL changesets, doc 6 consolidates.
2. Provision the `accuracy-source-dlq` topic, the subscription's dead-letter policy (`maxDeliveryAttempts = 5`), and the drain endpoint — verified enabled in every environment.
3. **Hard blocker:** the signed Candor schema (Q1 — the O-1 call, taking this doc's data-schema proposal and doc 3's manifest contract). The Candor mapping row is seeded only after signing; the adapter framework can build against the internal change contract meanwhile.
4. Dry-run with a synthetic batch end to end (paired with doc 3's contract dry-run): event → registry → verification → parse → cascade → staged items → `BATCH_COMPLETED` with reconciled counts; verify the DLQ path with a deliberately poisoned event.
5. Pilot tenant's first real Candor delivery: watch the completion gate, DLQ depth, quarantine backlog, and disposition mix through one full cycle.
6. Additional vendors: one `core_sources` row + one mapping row + doc 3's onboarding — the kill switch is per tenant/source at every step.
