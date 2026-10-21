# CP-40438 — Vendor exchange service: observability and audit, export leg

Completes the export leg's operability on top of the lane CP-39605 built: the audit trail as a checked contract and a readable API, the metrics proven end to end, retention and append-only asserted by tests, and the runbook entries an on-call engineer follows. Source of truth: Confluence "CP-39602 - Design: Directory accuracy vendor export", sections Observability (metrics, alerts E1 to E11, correlation) and Audit trail (envelope, per-event contracts, retention). Repository: `CertifyOS/vendor-exchange-service`, working rules in its `AGENTS.md`, findings in `docs/decisions.md` (this subtask continues the numbering from 91). Branches stack on CP-39605's PR #9 until the stack merges. Everything here is Java, tests and documentation in this repository.

## 1. What this subtask delivers

- Every audit event the lane writes carries the detail fields the design's per-event table names, enforced where the event is built, so a missing field fails the test that exercises the writer rather than being noticed in production. Two fields the ticket asks for beyond the design are added: the schedule version the selection came from, and the delivered file's checksum from the object metadata (no download).
- One structured log line per audit write, carrying the fields the design's alerts E1 to E8 evaluate on (`type`, `cause`, `completionSource`, `match`, `reason`, `check`), so the deployment's alerting can count them from Cloud Logging without a code change.
- The audit trail is readable through the API: a batch's events in the order they happened and a schedule's events, tenant-scoped like every other read.
- Proof that the fourteen design metrics are recorded by the lane: the whole-lane test scrapes `/q/metrics` after its scenarios and finds every name with the right labels and consistent values.
- Retention and append-only as facts a test asserts: no TTL index on any service collection, no code path that updates or deletes an audit event.
- Runbook: one row per alert E1 to E11 with the condition, the response the design prescribes and the procedure it points to; section 6 points at the audit read endpoints.

## 2. Nothing external blocks implementation

Every step runs in CI against the Testcontainers MongoDB and the WireMock stand-ins the lane already uses. The audit log line and the metrics endpoint are consumed by the deployment (Cloud Logging, a future scraper); nothing in this subtask depends on them being consumed.

## 3. Fixed inputs from the design that bind this subtask

- **Metrics**: the fourteen names, labels `tenant`, `vendor`, `state`, `reason`, `source`, `job`; never a practitioner identifier. All exist in `VendorExchangeMetrics` and are recorded by the lane (CP-39605 findings 73 to 86).
- **Alerts** (what the runbook maps and the log line serves): E1 no `EXPORT_TICK_COMPLETED` for 26 h (page); E2 batch untouched in `SCHEDULED`, `NPIS_SELECTED` or `EGRESS_COMPLETED` for `reconcilerStaleMinutes` (warn, page on repeat for one batch); E3 `EXPORT_EGRESS_STALE` (warn); E4 `EXPORT_BATCH_FAILED` any cause (page); E5 dead-letter message or `EXPORT_EVENT_REJECTED` twice in an hour (page); E6 `reconciliation.match = false` (warn); E7 `completionSource = DEADLINE` (warn); E8 `EMPTY` batch (warn); E10 JobRunr heartbeat absent on every worker VM for 5 min (page); E11 api group below target or load balancer health failing for 5 min (page).
- **Audit envelope**: `id`, `type`, `tenantId`, `vendor`, `exportBatchId` (every batch event; null on schedule events), `attempt`, `actor` (`system:vendor-export`, `system:egress-event`, or a user id), `jobId` (the writing job; null for HTTP handlers), `occurredAt`, `detail`. Append-only; committed with the transition where one exists, otherwise a standalone fact idempotent on its natural key; 7-year retention; `certifyPractitionerId` reserved for the ingestion module.
- **Per-event detail fields**: the design's table of 23 types, which `AuditEventType` already enumerates. Two names in the lane differ from the table and are aligned here: `EXPORT_PRIOR_ATTEMPT_CANCELLED` / `_CANCEL_REJECTED` carry `cancelledCorrelationId` and `outcome` (the lane wrote `priorCorrelationId`, `egressAnswer`).
- **Correlation chain**: `exportBatchId` to egress `correlationId` to the vendor object path to the event `messageId`; every log line, job payload and audit event carries `exportBatchId` (the MDC key from the scaffolding).
- **Ticket additions** (not in the design's table; design-doc edit on ask A9): `scheduleVersion` on `EXPORT_BATCH_SCHEDULED` (the selection-query version the batch copied); `md5` on `EXPORT_BATCH_DELIVERED` and `file.md5` on the batch, read from the object's metadata through `VendorBucket` (GCS returns it with the object's metadata, no download).

## 4. Target layout (additions)

```
audit/            AuditDetailContract (required detail keys per type, checked in AuditEvent.Builder.build())
                  AuditRepository.write* logs one line per event: AUDIT type=... tenantId=... vendor=... exportBatchId=... attempt=... actor=... plus the alert fields
export/api/       VendorExportsResource: GET /v1/vendor-exports/{exportBatchId}/events
export/schedule/  ScheduleResource: GET /v1/vendor-exports/schedules/{tenantId}/{vendor}/events
                  AuditViews (the envelope as stored; detail as a map)
clients/          VendorBucket.ObjectInfo gains md5 (GcsVendorBucket reads Blob.getMd5())
export/batch/     FileDetails gains md5; BatchCompletion.delivered writes it
docs/runbook.md   section 10, alert responses E1 to E11
```

No change in `config/`, `persistence/`, `auth/`, `http/`, `metrics/` or `terraform/`. A change there is a finding.

## 5. Steps

Each step: branch from the previous step's branch, one PR, CI green, a numbered finding in `docs/decisions.md`, a hard stop for review.

### Step 1 — Audit contract, the two added fields, the audit log line (3 hours)

- `AuditDetailContract`: the design's table as a map from `AuditEventType` to required detail keys; `AuditEvent.Builder.build()` throws when one is missing, naming the type and the key. Optional keys (`jobAttempts` on `EXPORT_BATCH_FAILED` from the retries filter, `inserted` and `skippedNoNpi` on `NPIS_REGISTERED`, the extra keys the lane added) are allowed, never required.
- Align the two cancel events to `cancelledCorrelationId` and `outcome`; add `scheduleVersion` to `EXPORT_BATCH_SCHEDULED` (tick, run-now, supersede); add `md5` to `VendorBucket.ObjectInfo`, `FileDetails` and `EXPORT_BATCH_DELIVERED`.
- One log line per audit write at INFO on the `audit` logger: `AUDIT type=<type> tenantId=<t> vendor=<v> exportBatchId=<id> attempt=<n> actor=<a>` plus `cause`, `completionSource`, `match`, `reason` and `check` when the event carries them. Never a practitioner identifier. The reconciler's `EXPORT_RECONCILER_REENQUEUED` line and this line are pinned by tests so their wording cannot drift.
- **Gate:** `AuditDetailContractTest` (every type in the table has an entry; a missing required key is refused; an optional key is not); the whole-lane test and every lane IT still green, which proves every writer satisfies the contract; `FinishJobIT` shows `file.md5` and the delivered event's `md5` from the mocked metadata; `AuditLogLineIT` captures the line with its fields and no ten-digit token.

### Step 2 — Audit read endpoints (2 hours)

- `GET /v1/vendor-exports/{exportBatchId}/events`: the batch's events in insertion order (`_id` ascending), at most 500, tenant-scoped through `BatchOperations.require` (another tenant's batch is 404). `GET /v1/vendor-exports/schedules/{tenantId}/{vendor}/events`: the schedule-scoped events (those with no `exportBatchId`), newest first, at most 200. Both `vendor-export:read`, `@Operation`, problem+json, OpenAPI regenerated.
- **Gate:** `AuditReadIT`: the lane's first scenario trail comes back in order through the endpoint; another tenant's batch is 404; the schedule list holds `SCHEDULE_CREATED` and `SCHEDULE_RUN_NOW` and no batch event; oasdiff accepts the two paths.

### Step 3 — Metrics and retention proof (1 hour)

- `ExportLaneIT` scrapes `/q/metrics` after its scenarios and asserts the fourteen names with the design's labels and values consistent with what ran (3 batches created, 9 practitioners selected, one `completion.source{DEADLINE}`, one `egress.failed{EGRESS_FAILED}`, no mismatch).
- `MongoIndexesIT`: no index on any of the four service collections has `expireAfterSeconds`. `ArchitectureTest`: `AuditRepository` depends on no `updateOne`, `replaceOne`, `deleteOne` or `deleteMany` (append-only by construction).
- **Gate:** both green in CI.

### Step 4 — Runbook, docs, Jira (1 hour)

- Runbook section 10: a table E1 to E11 with condition, severity, the response from the design, the signal to evaluate it from (the `AUDIT` log line's fields, the reconciler line, the JobRunr heartbeat, the group and load balancer health) and the section-7 or section-8 entry to follow (re-run is `retry`, re-deliver is `supersede`, stuck batch is section 8's reconciler row, missed event is E7's row and the push subscription). Section 6 points at the audit read endpoints. `HANDOFF.md` state. Jira CP-40438 Implementation Details and points, drafted in chat first.
- **Gate:** a reader can go from an alert name to the signal and the command that resolves it without leaving the runbook.

## 6. Estimate

| Step | Hours |
| --- | --- |
| 1 Audit contract, added fields, log line | 3 |
| 2 Audit read endpoints | 2 |
| 3 Metrics and retention proof | 1 |
| 4 Runbook, docs, Jira | 1 |
| **Total** | **7** |

Re-baseline for CP-40438: 7 (from 10; 1 SP = 1 hour). The ticket's item 1 (metrics at every lane step) was delivered inside CP-39605; its hour here is the proof, not the recording.

## 7. Decisions taken before Step 1 (2026-10-06)

- **Alert and dashboard definitions belong to the deployment, not this repository.** Decided by Dev: the platform is moving from Terraform to Pulumi, and the Pulumi component owns every GCP resource for this service; this subtask gives it the signals (one structured `AUDIT` log line per event, the pinned reconciler and heartbeat lines, the metrics endpoint) and the runbook's table of what each alert evaluates on.
- **The audit detail contract is enforced in the builder, not only in a test.** A test over a table cannot see a writer that forgets a key; the builder can, and every writer runs in an integration test, so a missing key fails CI at the writer. The cost is one map lookup per event.
- **Checksum is the object's MD5 as GCS reports it**, read with the metadata the finish job already fetches; no second request and no download. It is recorded, not compared: nothing on our side has the file's bytes to compare against.
- **Audit reads expose the envelope as stored, `jobId` included.** The design names `jobId` as the link from an audit row to the JobRunr dashboard entry; batch views hide job ids because they are not batch facts, audit rows show them because that is their purpose.

## 8. Definition of done for CP-40438

- Every gate in section 5 has evidence in `docs/decisions.md`; CI green on `main` once the stack merges.
- Every event type in the design's table is required by `AuditDetailContract` and every lane IT passes under it.
- The audit read endpoints are in `openapi/openapi.yaml` with problem+json responses.
- The fourteen metrics are asserted after the whole-lane run; no TTL index; the audit repository is append-only by test.
- The runbook maps every alert to its signal and its response.
