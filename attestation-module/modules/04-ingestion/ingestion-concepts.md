# Companion — Ingestion module (concepts + supplementary material)

Vocabulary first, deep-dive material second. Concepts entries carry no design decisions — the module doc (`ingestion.md`) owns the design.

## Messaging and processing concepts

### Pub/Sub

- GCP's messaging service: publishers write messages to a topic; subscribers receive them with **at-least-once** delivery (duplicates possible, ordering not guaranteed).
- Consumers must therefore be idempotent — here, the batch registry dedups by checksum.
- A message is redelivered until the subscriber acknowledges it — which is what makes a poison message dangerous without a dead-letter policy.

### Dead-letter queue (DLQ) and `maxDeliveryAttempts`

- A holding queue for messages that failed processing repeatedly, kept for human inspection instead of being retried forever or dropped.
- Pub/Sub supports one natively: set a dead-letter topic and `maxDeliveryAttempts` on the subscription; after that many failed deliveries the message moves to the DLQ topic and the subscription is unblocked.
- Without it, one poison message redelivers forever and can starve everything behind it.

### Drain endpoint

- An admin endpoint that pulls messages from a DLQ, lets an operator inspect them, and re-emits selected ones to the original topic after the underlying bug is fixed.
- Typical features: dry-run, filtering, per-message accept/discard — every action audited.
- The platform precedent is the webhook DLQ drain; its trap is that the whole DLQ feature ships disabled by default.

### Cloud Tasks

- GCP's managed work dispatcher: created tasks are pushed as HTTP calls to a handler; non-2xx responses are redelivered with growing backoff.
- Refuses a second task with the same **name** — a deterministic name makes double-enqueueing impossible.
- Has **no dead-letter queue**: a task that exhausts retries is silently deleted, so a final-attempt failure record must be written by the handler itself.

### Chunked work splitting

- Splitting a big job into one queue task per page of items (here ~1,000 rows), instead of one giant task or one task per item.
- Buys per-chunk retry isolation (one failing slice retries alone) and pacing, with a small task count.
- Parallelism comes from the queue dispatching many small workers — not from threads inside one process.

### Streaming parse (commons-csv, fastexcel)

- Reading a file record by record, holding only the current batch in memory — a 100 MB file costs the same memory as a 1 MB file.
- commons-csv is the platform's verified CSV streamer (first-record-as-header + trim); fastexcel streams XLSX sheets the same way.
- The opposite — loading the whole file — is the pattern that dies on the first oversized delivery.

### Idempotent inserts and natural keys

- An insert keyed by a natural unique key (`(source_batch_id, row_number)`) can be retried safely: the second attempt finds the row and no-ops.
- Crash-resume falls out for free: list which keys exist, continue from the gap — no checkpoint state to maintain.

### State machine and completion gate

- The batch's status column only moves along defined transitions (`REGISTERED → VERIFYING → PARSING → COMPLETED | REJECTED | PARKED`), each transition action-owned and audited.
- A **completion gate** is a transition guarded by a condition — here, `COMPLETED` requires the manifest row count to equal the sum of disposition counts. A batch that lost rows *cannot* complete.

### Reconciliation invariant

- A checkable equality between what was promised and what was accounted for: declared rows in = dispositioned rows out.
- Enforcing it at the state machine (not in a report) turns "nothing silently discarded" from a hope into a structural property.

## Domain and platform concepts

### Batch registry

- The ledger of every inbound delivery ever seen: registered on the manifest event, deduped by checksum, tracked through verification, parsing, and completion.
- Doc 3's hourly sweep compares GCS reality against it to find lost events — the registry is the shared truth between transport and ingestion.

### Adapter / schema mapping

- The one place a vendor's file format is known: a configuration row keyed `(source_id, schema_version)` declaring column translations, enum translations, value formats, and which columns identify vs carry payload.
- Onboarding a new vendor is a new mapping row — the pipeline code never changes and never names a vendor.

### Schema version

- A string both sides put on the wire (`candor-recs-v1`) naming the signed file contract.
- An unknown version rejects the whole batch — the gate that turns silent format drift (the CT-011 incident class) into a loud, immediate conversation.

### Disposition

- The single outcome assigned to every inbound row: facility-dropped, invalid, quarantined, excluded-terminated, excluded-not-in-export, no-op, duplicate/stale-superseded, previously-rejected, or actionable.
- Every row gets exactly one; all are counted and audited; only *actionable* produces reviewer work.

### Quarantine and poison items

- Quarantine is "decide later": a row parked because the system could not safely judge it right now (a lookup was down, or two rows conflict) — auto-retried or operator-replayed, never mislabeled, never lost.
- A poison item fails identically on every retry; bounded attempts + quarantine keep it from blocking its siblings.

### Crosswalk / canonical practitioner identity

- The platform's mapping from external identifiers (NPI, vendor ids) to the internal canonical practitioner (`certify_id`, stored on our rows as `certify_practitioner_id`).
- Resolution here is exact-match only: a fuzzy match that joins two different practitioners is a worst-class failure, so ambiguity quarantines instead of guessing.

### Normalization and versioned rules

- Making two spellings of the same value comparable: trim, collapse whitespace, casing; phones to digits-only; addresses to conservative uppercase text.
- The ruleset carries a version (`norm-v1`) stored with every normalized value — changing the rules triggers recomputation of stored values, never a silent mismatch between old and new spellings.

### Rejected-recommendations memory

- The lookup table (v2 §6.6, D2-07) recording every reviewer rejection of an external-source item, keyed `(tenant, practitioner, attribute, operation, normalized value)`.
- Ingestion checks each inbound row against it; a match is suppressed from the queue in MVP with the audit naming the prior rejection. External lane only — provider resubmissions always reach the queue.

### Staged change item

- The unit of reviewer work: one proposed field change (attribute, operation, old/new value, source tag) waiting in staging for a decision.
- Both lanes produce the same shape (doc 2 defines it); the source tag is what survivorship ranking reads after release.

### The roster pipeline (precedent)

- The platform's existing vendor-file ingestion: upload → GCS → explicit Pub/Sub publish → streamed validation → row-level staging in Spanner with indexed status.
- The single most relevant precedent — this module reuses its streaming and staging idioms and fixes its verified gap (no DLQ).

### REVALIDATE replay (precedent)

- The roster pipeline's operator recovery: re-run validation by reading the **staged rows from the database** in cursor batches — not by re-parsing the original file.
- The precedent for this module's quarantine replay; the GCS original stays the source for full-batch reprocessing only.

### `core_sources` registry

- The platform's existing sources table (globally unique `source_type`): every data source — portal attestation, each vendor — is one registry row (D2-11).
- Staged rows carry a `source_id` referencing it; adding a vendor is a row, never a schema change.

### Effective date

- The date a released change takes effect: the vendor's verification date when the row carries one, else a configured fallback (end of the current month) — D2-22.
- Stamped on the staged item at ingestion; enforced at release time by the backend.

### Evidence string

- Whatever the vendor offers as proof (a call date, a URL), carried as one plain copyable string (D2-27) — displayed to the reviewer, never interpreted.

### Confidence (inert)

- A score some vendors attach to recommendations. D2-07 removed all confidence-based logic (no data exists to trust an ordering), so the value is stored verbatim, visible and auditable, read by nothing.

### `attestation-module-config`

- The feature's per-tenant configuration entry (doc 1): this module adds `effectiveDateFallback`, `ingestionPaused`, and `quarantineRetrySchedule`.

---

# Supplementary material

Deep-dive material behind the module doc (`ingestion.md`): baseline evidence, the full approach analysis with weightage and cost derivations, traceability, context, design rationale, open questions, ticket impact. Vocabulary is in the *Concepts* half of this file, above.

## Baseline — verified current behavior

The platform already ingests vendor files end to end — the **roster pipeline** — and it is the single most relevant precedent. Every claim verified against the code; ⚠ marks corrections to earlier assumptions.

| # | Claim | Evidence |
| --- | --- | --- |
| 1 | Roster trigger is multipart HTTP upload → GCS → **explicit Pub/Sub publish** — not a GCS object notification. Doc 3's event is likewise an explicit publish (by the pickup job); both lanes end at Pub/Sub | `RosterService.java:667` (upload), `:649` (dispatch); topics `roster-validation` / `roster-ingestion`, `application.properties:708-712` |
| 2 | Row-level staging exists: `roster_records` in Spanner — `id` PK, `data` JSON, **generated columns** (computed-from-JSON) `record_key` and `status` under NULL_FILTERED indexes | `roster_records/003:14,30` |
| 3 | Streamed parsing is proven: XLSX via fastexcel (`sheet.openStream()`), CSV via **commons-csv** (first-record-as-header + trim), feeding a batching consumer that flushes fixed-size batches — bounded memory at any file size | `RosterValidator.java:687`, `:739`, `:764` |
| 4 | ⚠ The CSV stack is **commons-csv 1.10 + fastexcel** — not opencsv/univocity as earlier notes guessed | `build.gradle:174,199` |
| 5 | Quarantine precedent is a **status, not a table**: failed rows sit as `VALIDATION_FAILED` on `roster_records`; operator replay (REVALIDATE) re-reads the staged rows from Spanner in 500-row cursor batches — not the original file; failed rows are exportable as CSV | `RosterRecordStatus.java:3`; `RosterValidator.java:349-355` (comment at `:314`); `RosterResource.java:1074` |
| 6 | ⚠ **Roster has no dead-letter queue at all** — failures `nack()` and Pub/Sub redelivers forever. The platform's only DLQ precedent is the webhook one: publish to `webhook-delivery-dlq` on the final attempt + an admin drain endpoint — and it ships **disabled by default** (`webhook.dlq.enabled=false`) | `RosterValidationConsumer.java:236`; `WebhookDlqPublisherService.java:133`, `WebhookDlqDrainService.java:90`, `WebhookDlqResource` |
| 7 | Source registry exists: `core_sources` (PK `source_id`; **`source_type` globally unique**), registered via REST or seed data; plus `source_ingestions` (source_id, acquired_date, file_url) as a delivery-log precedent | `core-source/002`; `CoreSourceResource.java:109` |
| 8 | ⚠ **Normalization reality:** the MDM cleansing layer's only reusable logic is `DataCleansingUtil` (trim, whitespace collapse, name/business-suffix casing) — and it is a Quarkus **service, not a library** (no maven-publish; reuse means copying the class). **No phone normalization exists anywhere in the platform.** Address cleansing is an **HTTP call to a Smarty-backed service** with a Spanner cache — not local rules | `DataCleansingUtil.java:121,37-47`; `AddressCleanserService.java:144,128` |
| 9 | ⚠ **`tenant_configurations` cannot hold per-source schema mappings:** uniqueness is one row per `(tenant_id, configuration_type_id)` — and vendor schema mappings are not per-tenant anyway (Candor's file format does not change per tenant) | changeset 002 |

**Consequence:** the chosen approach is assembly of verified idioms (streamed commons-csv, one Cloud Task per chunk, row staging with indexed status, REVALIDATE-style replay, explicit Pub/Sub publishes), with the two verified gaps fixed rather than inherited: a real, **enabled** DLQ (rows 6), and adapter mappings in their own table instead of `tenant_configurations` (row 9). The normalization utility is genuinely new code (row 8).

## Approaches analyzed in depth

**Decision criteria (stated before the approaches):**

1. **No silent loss, ever** — the reconciliation invariant must be structurally checkable, not aspirational. Highest weight.
2. **Idempotency at every hop** — event, batch, row, staged item.
3. **Source-agnosticism** — adding vendor #2 must be config, not code.
4. **Transactional audit fit** — dispositions and staged items commit with their audit rows (the audit brief's hard rule).
5. **Operational burden / team familiarity** — prefer the platform's proven streaming + chunked-worker idioms.
6. **Cost** — reported per approach; at these volumes compute is small everywhere, so cost is secondary (storage gets real in doc 6).
7. **Future limits** — weekly cadence, 1M-row batches, ten vendors.

**Volume inputs for every estimate:** ~3k observed rows per batch (design headroom 100k); monthly per vendor per tenant; a handful of batches a month at MVP.

Weightage scale: **5 = fully satisfies · 3 = workable with caveats · 1 = fails or requires hand-building.**

### A. The chosen approach — Pub/Sub with an enabled DLQ + registry + chunked streaming workers, inside the backend

- **How it works:** see the module doc's *Approach* (steps 1–6).

- **Pros:**
  - Every building block is a verified platform idiom — assembly, not invention (baseline rows 1–5, 7).
  - The reconciliation invariant is enforced as a **state-machine gate**, not a report someone reads.
  - Idempotent at all four hops (event/batch/row/item) via uniqueness — the platform's one true pattern.
  - Fixes the two verified precedent gaps: a real, **enabled** DLQ, and audited replay.
  - Concurrency from many small chunk workers, not in-process threading — the runtime-language question dissolves.

- **Cons:**
  - Lives inside the shared api-layer deployable (doc 5's placement): a bad unrelated deploy pauses ingestion — tolerable at monthly cadence, and batches resume where they stopped.
  - 100k `ROW_DISPOSITIONED` audit rows per 100k-row batch is deliberate volume (N5) — the cost lands in doc 6's storage math, flagged there.

- **Weightage:**

| Criterion                  | Score  | Note                                                          |
| -------------------------- | ------ | ---------------------------------------------------------------- |
| 1 No silent loss           | 5      | Completion gate — database-enforced                            |
| 2 Idempotency hops         | 5      | Uniqueness at all four hops                                    |
| 3 Source-agnosticism       | 5      | Mapping row + registry row, zero code                          |
| 4 Transactional audit      | 5      | Native — shared data layer, shared transactions                |
| 5 Ops burden / familiarity | 4      | +0 surfaces, proven idioms; shared-deployable coupling         |
| 6 Cost                     | 5      | ≈ $5–10/mo                                                     |
| 7 Future limits            | 4      | 1M-row weekly = chunk tuning + worker pool, not redesign       |
| **Total**                  | **33** |                                                                |

- **Failure modes:** duplicate event → registry no-op · consumer crash-loop on one event → DLQ after 5 attempts, subscription unblocked · chunk crash → lone retry, idempotent rows · verification mismatch → parked, alerted, nothing staged · counts don't reconcile → batch never completes, alert fires.

- **Cost ≈ $5–10/month, derived:** Pub/Sub + DLQ (a few messages) ≈ $0 · Cloud Tasks (a few hundred chunk tasks; first 1M ops free) ≈ $0 · parse compute on the existing service (minutes per batch) < $5 · crosswalk-lookup Spanner reads < $2. Storage counted in doc 6.

### B. Managed data processing (Dataflow/Beam, or BigQuery-load + SQL dispositions)

- **How it works:** load the mirrored file into BigQuery, or run an Apache Beam pipeline on Dataflow; express dispositions as set-based SQL/transforms; write actionable rows back to the staging store.

- **Pros:**
  - Set-based dispositions (no-op detection, duplicate joins) are elegant in SQL; scales to millions of rows without thought.
  - Fully managed compute; Dataflow retries and checkpointing built in.

- **Cons:**
  - Dispositions computed outside Spanner cannot commit atomically with staged items and audit rows — criterion 4 fails structurally.
  - Rejection-memory and crosswalk lookups are row-by-row keyed reads against live Spanner state — the worst fit for batch SQL engines; quarantine-on-outage does not map to a set operation.
  - A new runtime and skillset for a 3k–100k-row monthly file.

- **Weightage:**

| Criterion                  | Score  | Note                                                     |
| -------------------------- | ------ | ----------------------------------------------------------- |
| 1 No silent loss           | 3      | Report-level reconciliation only                          |
| 2 Idempotency hops         | 3      | Batch-level natural; row/item hops hand-built             |
| 3 Source-agnosticism       | 3      | Config + per-vendor SQL/transform variants                |
| 4 Transactional audit      | 1      | **Broken** — cross-system commit                          |
| 5 Ops burden / familiarity | 2      | New runtime/skillset                                      |
| 6 Cost                     | 3      | $10–30/mo, plus the new-runtime tax                       |
| 7 Future limits            | 5      | Effortless at any volume                                  |
| **Total**                  | **20** |                                                           |

- **Why it loses:** disqualified on criteria 1/2/4 — the module's defining properties; its one strength (scale) answers a problem the volumes do not pose.

- **Failure modes / what would break it:** any requirement to decide a row against live state mid-pipeline (rejection memory, quarantine) forces per-row callbacks that erase the set-based advantage.

- **Cost:** Dataflow job-hours ≈ **$10–30/month** at our cadence; BigQuery pennies.

### C. Standalone ingestion service in Go or Python

- **How it works:** a separate Cloud Run service (goroutines or asyncio + a fast CSV library) consumes the events, parses, and calls the backend's APIs to stage rows.

- **Pros:**
  - Nicer CSV ergonomics; isolated load and deploys.

- **Cons:**
  - The concurrency argument evaporates: parallelism comes from Cloud Tasks dispatching many small chunk workers, and a Java worker streaming 1,000 rows is nowhere near any language's limits (the roster pipeline streams far bigger files in Java — baseline row 3).
  - Staging over HTTP either breaks same-transaction audit or forces new internal batch-write endpoints.
  - A new deployable: pipeline, dashboards, secrets, on-call — for a monthly batch job; and the team is Java/Quarkus.

- **Weightage:**

| Criterion                  | Score  | Note                                                    |
| -------------------------- | ------ | ---------------------------------------------------------- |
| 1 No silent loss           | 3      | Possible, but enforced across an API seam                |
| 2 Idempotency hops         | 3      | All four, but via HTTP round-trips                       |
| 3 Source-agnosticism       | 5      | Same mapping design                                      |
| 4 Transactional audit      | 2      | Broken, or new internal endpoints to fake it             |
| 5 Ops burden / familiarity | 2      | +1 deployable, non-house language                        |
| 6 Cost                     | 4      | $5–15/mo + un-dollared overhead                          |
| 7 Future limits            | 4      | Fine                                                     |
| **Total**                  | **23** |                                                          |

- **Why it loses:** it pays a permanent deployable-plus-broken-transaction tax to buy a concurrency win the chunked workers already provide (criteria 4 and 5).

- **Failure modes / what would break it:** partial failures across the HTTP seam (rows staged, audit lost) — exactly the class the same-transaction rule exists to make impossible.

- **Cost:** scale-to-zero Cloud Run ≈ **$5–15/month** + operational overhead.

### D. AWS (S3 events → SQS → Lambda parsers → DynamoDB registry)

- **How it works:** the same pipeline shape on AWS managed pieces; included because it is a genuine infrastructure choice — it repeats doc 1/doc 3's cross-cloud pattern.

- **Pros:**
  - First-class managed pieces; Lambda parse scale is effortless.

- **Cons:**
  - The data, staging tables, audit store, and crosswalk lookups live on GCP — every row round-trips clouds; same-transaction audit impossible.
  - A second cloud's IAM, monitoring, bill, and on-call for a monthly file.

- **Weightage:**

| Criterion                  | Score  | Note                                          |
| -------------------------- | ------ | ------------------------------------------------ |
| 1 No silent loss           | 3      | Possible, rebuilt cross-cloud                  |
| 2 Idempotency hops         | 3      | All four, rebuilt                              |
| 3 Source-agnosticism       | 5      | Same mapping design                            |
| 4 Transactional audit      | 1      | **Broken** — cross-cloud                       |
| 5 Ops burden / familiarity | 1      | A whole second cloud's operations              |
| 6 Cost                     | 2      | $20–50/mo + egress + second on-call            |
| 7 Future limits            | 5      | Scales effortlessly                            |
| **Total**                  | **20** |                                                |

- **Why it loses:** criteria 4 and 5 — the same verdict as doc 1's and doc 3's AWS variants, for the same reasons.

- **Failure modes / what would break it:** cross-cloud partial failures and egress costs on every row.

- **Cost:** ≈ **$20–50/month** + cross-cloud egress + the un-dollared second on-call.

### Comparison

| Criterion                  | A Backend workers + DLQ | B Dataflow/BigQuery | C Standalone Go/Py | D AWS |
| -------------------------- | ----------------------- | ------------------- | ------------------ | ----- |
| 1 No silent loss           | 5                       | 3                   | 3                  | 3     |
| 2 Idempotency hops         | 5                       | 3                   | 3                  | 3     |
| 3 Source-agnosticism       | 5                       | 3                   | 5                  | 5     |
| 4 Transactional audit      | 5                       | 1                   | 2                  | 1     |
| 5 Ops burden / familiarity | 4                       | 2                   | 2                  | 1     |
| 6 Cost                     | 5                       | 3                   | 4                  | 2     |
| 7 Future limits            | 4                       | 5                   | 4                  | 5     |
| **Total (unweighted)**     | **33**                  | 20                  | 23                 | 20    |
| **Cost (monthly)**         | **≈ $5–10**             | ≈ $10–30            | ≈ $5–15 + overhead | ≈ $20–50 + 2nd cloud |

**Recommendation rationale:** A is the only approach where "nothing silently discarded" is a database-enforced state transition and where dispositions, staged items, and audit rows commit together. B and D fail the transactional-audit non-negotiable outright; C survives it only by adding surface that reintroduces partial-failure classes. A's every idiom is verified platform practice, with the two verified precedent gaps explicitly fixed.

## Requirements traceability

| # | Requirement | Trace |
| --- | --- | --- |
| F1 | Trigger on **manifest events only**; duplicated/delayed/out-of-order events cause no duplicate work | v2 §6.5, D2-05 |
| F2 | Batch dedup: `(tenant, source, checksum)` unique — a re-delivered file is recognized, acknowledged, skipped | v2 §6.5 |
| F3 | Verify manifest sha256 + row count against the mirrored file; mismatch parks the batch, nothing staged | D2-04 |
| F4 | Resolve the adapter by `(source, schemaVersion)`; **unknown version → whole batch rejected** with a clear error | v2 §6.5 |
| F5 | Per-row: exactly one disposition, all counted, all audited; **manifest row count must equal the sum of disposition counts** before the batch may complete | v2 §6.5, §8 |
| F6 | Rejected-recommendations lookup: an exact match on the normalized key marks the row **previously-rejected and suppresses it from review** (audit kept; capability to surface later stays) | D2-07 |
| F7 | Identity resolution to the canonical practitioner is server-side, exact-match, and quarantines on lookup outage — **never mislabels**; misjoining two practitioners is a worst-class failure | v2 §6.8 |
| F8 | Facility rows: dropped with an audited, counted disposition | D2-15 |
| F9 | Effective date: evidence/verification date when present, else end-of-month; configurable | D2-22 |
| F10 | Parser crash mid-batch resumes from the last staged row; rows idempotent on `(batch_id, row_number)` | v2 §8 |
| F11 | Quarantined rows are operator-replayable (audited), never lost, never auto-mislabeled | v2 §8 |
| F12 | Source-agnostic: onboarding vendor #2 = register a source + add an adapter mapping + SFTP credentials. No pipeline/code changes | D2-11, v2 §6.5 |

Non-functional numbers: module doc, *Performance and scale*.

## Context from previous module docs

| Inherited from | What |
| --- | --- |
| Doc 1 — Cycle (✅) | The deterministic identity `(tenant_id, practitioner_id, due_period)` (physical column `certify_practitioner_id`); the page-chunked Cloud Tasks worker pattern with deterministic task names and the final-attempt failure record (no Cloud Tasks DLQ); the `attestation-module-config` tenant entry and its key carry-forward rule; the shared audit envelope and its additive-extension rule. |
| Doc 2 — Portal lane (✅) | The staged-change-item shape — one review model for both lanes: `attribute`, `operation` (`ADD \| UPDATE \| REMOVE`), `entry_key`, `old_value`, `new_value`, `source`; the `STAGED_ITEM_CREATED` event and its detail contract. Rejection memory applies to **this lane only**; portal resubmissions always reach the queue. This module proposes an additive amendment to the item shape (module doc, *Contracts step 6*). |
| Doc 3 — SFTP Exchange (✅) | The `accuracy-source.file.received` event payload (published by the pickup, one per mirrored manifest); the manifest schema (`vendorBatchId`, `exportBatchRef`, `sha256`, `rowCount`, `producedAt`, `schemaVersion`); the GCS archive layout + generations; the batch id format `<tenant>-<vendor>-<yyyy-MM>-<seq>`; the hourly sweep that re-emits lost events against this module's registry; the export registry (`attestation_export_batches` — the `exportBatchRef` validation source) and the per-practitioner membership rows (`attestation_export_items` — obligation attribution). |

Exports to later docs:

| Export | Used by |
| --- | --- |
| `source_batches`, `source_batch_rows`, `source_schema_mappings` contracts + the staged-item amendment | Database (6) — DDL |
| The disposition vocabulary | UI (7) — queue filters; Operations |
| The quarantine/replay and DLQ-drain surface | Backend (5) — hosting; Operations |
| The `rejected_recommendations` read-side key + normalization versioning | Backend (5) — the write side on rejection; Database (6) |
| The Candor data-schema proposal | The O-1 call (with doc 3's manifest contract) |

## Design rationale — anticipated questions

**"Why does this module get a DLQ when the cycle module deliberately skipped one?"**

- Different loss models. The cycle's scan re-derives all pending work from `SCHEDULED` rows — a lost trigger costs a day, nothing more; there is no per-message state to protect.
- Here each message is one external delivery, and the failure to fear is not loss (the sweep re-emits) but **poison**: a message that crashes the consumer identically every attempt would redelivery-loop and block every delivery behind it — the exact verified gap in the roster pipeline.
- The DLQ is Pub/Sub configuration plus one drain endpoint — cheap insurance on the one module whose input is external and untrusted.

**"Why quarantine both rows on a same-batch conflict instead of picking one?"**

- v1's rule — highest confidence tier wins — became unimplementable when D2-07 removed confidence from all logic (no data exists to derive or trust an ordering).
- Any remaining tiebreaker (file order, last-writer) is arbitrary, and an arbitrary winner that releases to the Golden record is a silent wrong answer with a 7-year audit trail saying we chose it.
- Quarantine costs one human decision per conflict — rare in practice — and never mislabels. The safe failure direction.

**"Why not call the Smarty address service to normalize addresses in the rejection lookup?"**

- The lookup runs inside the disposition cascade for every row; an external HTTP dependency there makes every batch hostage to a third-party outage — and an outage would force mass quarantine for a *matching convenience*.
- Conservative text-only normalization fails only toward resurfacing an occasionally-rejected recommendation — reviewer noise, never data loss. The trade is deliberate.
- Full address standardization stays available upstream (the cleansing layer) where it belongs — on the release path, not the matching path.

**"Why replay quarantined rows from the database instead of re-reading the file?"**

- The staged row already holds the parsed, translated values — re-reading the file re-runs parsing and adapter translation for rows that already passed both.
- It is the platform's proven recovery idiom (roster REVALIDATE, cursor-batched from Spanner) — and it keeps replay working even if the archive object is cold-tiered.
- The GCS original remains authoritative for full-batch reprocessing (a bad mapping fixed → re-run the whole batch as a new pass).

**"Why store a vendor confidence value at all if nothing reads it?"**

- Dropping a delivered field is information loss with no upside; storing it inert keeps the reviewer able to see it and the audit able to reproduce exactly what the vendor sent.
- The boundary D2-07 draws is on *logic*: nothing branches on it, nothing ranks by it. If real confidence data ever justifies escalation logic, the history is already there.

**"Why attach vendor rows to their obligation through export items instead of asking Candor to echo our task id per row?"**

- The binding must be deterministic on our side: the September delivery answers September's obligation even when it lands after October's cycle opened, and vendor-side per-row echo discipline is exactly what drifted in the real MMO delivery (CT-011).
- `exportBatchRef` (batch-level echo, already contractual) plus our own `attestation_export_items` gives the mapping with zero new vendor obligations; a per-row echo can still be proposed in the O-1 call as a cross-check — never load-bearing.
- A row for a practitioner not in the referenced export has no obligation to attach — `EXCLUDED_NOT_IN_EXPORT`, counted and audited; surfacing those later is a configuration change (the D2-07 pattern).

**"Why do the schema mappings get their own table instead of `tenant_configurations`?"**

- Verified: `tenant_configurations` is unique per `(tenant_id, configuration_type_id)` — one row per tenant per type, and a mapping would have to be duplicated per tenant.
- The data has no tenant axis: Candor's file format is identical for every tenant. Keying it `(source_id, schema_version)` in its own table puts uniqueness where the reality is.
- Per-tenant knobs (effective-date fallback, pause, retry schedule) stay in `attestation-module-config` — the two axes never mix.

**"Why one `ROW_DISPOSITIONED` audit event per row — isn't 100k rows of audit per batch excessive?"**

- The disposition *is* the compliance answer to "what happened to this row?" — sampling or summarizing would make some rows unanswerable, which "nothing silently discarded" forbids.
- Volume is bounded and priced: batches are monthly and the rows are small; the storage math lands in doc 6 where all retention costs are consolidated.

**"Why does ingestion live inside the backend instead of its own service?"**

- The same-transaction audit rule: dispositions, staged items, and audit rows must commit together, which requires the shared data layer — an external service would stage over HTTP and lose that.
- Doc 2 already analyzed and rejected the standalone-service shape for the backend itself; the same arithmetic (rebuilt rails, second on-call, no needed isolation at monthly cadence) applies here with smaller numbers.

## Open questions and sign-offs

| # | Question | Owner |
| --- | --- | --- |
| Q1 | **The signed Candor schema** (module doc *Contracts → the Candor data-schema proposal* + doc 3's manifest contract = our full O-1 proposal) — the technical call; the Candor mapping row (DA-15) is hard-blocked until signed | Product + Engineering + Candor technical team |
| Q2 | **Conflict-rule sign-off:** quarantine-both for same-attribute conflicts (replaces v1's confidence-tier rule, which D2-07 made unimplementable) — note in v2 if confirmed | Module review |
| Q3 | Normalization ruleset `norm-v1` scope — especially how conservative address matching should be (proposed: text-only, no Smarty dependency in the lookup path) | Engineering (DA-03) + MDM consult |
| Q4 | Store vendor confidence verbatim as inert data (proposed: yes — visible, auditable, unused) | Module review |
| Q5 | Who authors adapter mapping content when vendor #2 arrives (proposed: the Roster/Integration team owns mapping-table content; this module owns the framework) | Team leads |
| Q6 | **Staged-item amendment sign-off:** the ⚠ additive columns + nullable `submission_id` proposed to doc 2's `attestation_staged_items` contract (module doc *Contracts step 6*; `task_id` stays required — vendor rows fill it via obligation attribution) — doc 2 amended only after review, then doc 6 consolidates | Module review + doc 6 |

## Ticket impact

| Ticket | Impact |
| --- | --- |
| DA-15 (adapter framework) | **Two corrections:** (1) mapping storage = `source_schema_mappings` in the module database keyed `(source, schema_version)` — **not** `tenant_configurations` (verified one-row-per-tenant-per-type uniqueness + wrong axis); (2) the "canonical ordered confidence tiers" requirement is **deleted** (D2-07) — replaced by the quarantine-both conflict rule. The signed schema (Q1) still hard-blocks. |
| DA-16 (disposition engine) | Updated cascade: add `FACILITY_DROPPED`, `PREVIOUSLY_REJECTED` (suppressed, D2-07), `CONFLICTING_IN_BATCH` quarantine; the reconciliation invariant as the completion gate is an explicit acceptance criterion. |
| DA-03 (identifier/normalization library) | **Scope correction:** normalization is genuinely new code — no phone normalizer exists platform-wide; name/whitespace rules copied from `DataCleansingUtil` (a service, not a library); versioned rules `norm-v1`; the fingerprint-hash portion remains superseded by the lookup table. |
| DA-17 (quarantine/replay ops) | Confirmed; shape it on the roster REVALIDATE precedent (replay from staged rows, cursor-batched) + audited operator actions + the `CLOSED_BY_OPERATOR` terminal state. |
| New — Pub/Sub DLQ + drain | Dead-letter policy on the ingestion subscription (`maxDeliveryAttempts = 5` → `accuracy-source-dlq`) + drain admin endpoint (webhook-drain shape, **enabled in every environment**). |
| New — `rejected_recommendations` write/read pair | Table contract to doc 6; the write-on-rejection lands in doc 5's scope; the read side belongs to DA-16's cascade step 8 — make the dependency explicit on both tickets. |
