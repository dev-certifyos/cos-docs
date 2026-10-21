# CP-39605 — Vendor exchange service: export lane implementation plan

Fills the bodies the scaffolding (CP-39604, merged as PR #1) left empty: schedule endpoints, the tick's batch creation, the four jobs, the completion event branch, reads, retry and supersede. Source of truth for every contract below: Confluence "CP-39602 - Design: Directory accuracy vendor export", sections Approach, Contracts and interfaces, Data model, Audit trail, Failure modes. Repository: `CertifyOS/vendor-exchange-service`, working rules in its `AGENTS.md`, findings in `docs/decisions.md` (this subtask continues the numbering from 69).

## 1. What this subtask delivers

- A batch runs end to end in code: tick or run-now creates it, `SelectJob` registers its NPIs from api-layer, `RequestEgressJob` pins the template and asks egress for the file with a destination, the completion event or the deadline check moves it on, `FinishJob` verifies the object and marks it `DELIVERED`. Every transition is compare-and-set with its audit event in the same transaction, exactly as the design's per-event table says.
- The operator surface from the design's Contracts step 0, 7 and 8: schedule `PUT`, `GET`, `disable`, `enable`, `run-now`, `preview`; batch `GET` by id, list by tenant and period, NPI list; `retry` and `supersede`. All under `/v1/vendor-exports`, problem+json errors, `@RequiresPermission` on each, committed OpenAPI updated.
- The vendor mappings CSV (`certify-export-v1`, 30 columns) authored in this repository and provisioned per tenant through api-layer at schedule creation.
- Proof in CI: unit tests per rule, integration tests per job against WireMock stand-ins for api-layer and egress and a fake vendor bucket, and one whole-lane test from tick to `DELIVERED`, including the retry and supersede paths.
- The ticket's "done when" (a file verified present in the Candor SFTP bucket) needs the egress changes and the deployment from section 2. Everything in this plan is built and proven in CI before those land; the live verification is the first thing run when they do.

## 2. Nothing external blocks implementation

| Dependency | Interim (works today, in CI) | Final | Swap-in point |
| --- | --- | --- | --- |
| Egress ask 1: NPI list above 100 | WireMock accepts any list; a unit test pins the request shape | Egress raises `filterMaxListSize` | None in code |
| Egress ask 2, 3: `_export_batch_id`, `_generated_at` record fields and `;` joiner | Mappings CSV rows for those columns are present and marked; template upload is validated by api-layer's field catalog, so the two injected fields may be refused until egress exposes them | Egress injects the fields; the CSV rows stay as written | Re-run provisioning (new template version) |
| Egress ask 5: completion topic | Push handler tested by direct POST with a mocked verifier; `DeadlineCheckJob` is the fallback and is tested as the only path too | Topic exists; Terraform/Pulumi variable creates the subscription | One deployment variable |
| Egress ask 6: `destination` on the request, create-only copy, `correlationId` in metadata, no manifest | Request carries `destination`; `FinishJob` reads object metadata keys `complete`, `totalRecords`, `totalRows`, `correlationId` through a `VendorBucket` interface faked in tests | Egress copies to the destination | None in code |
| Egress ask 4, 7: email suppression, template-pipeline tenant gate, rate-limit exemption | Not visible to code | Egress configuration | None |
| Auth0 machine client (A1) | `ApiLayerTokenService` reports not configured; jobs that call api-layer fail fast with `ApiLayerNotConfiguredException` and JobRunr retries; readiness names it | Secret version plus client id | One secret version, one variable |
| IAP grants on DAL and api-layer (A2) | WireMock in CI; live calls answer 401 or 503 until granted | DevOps grants | None in code |
| Vendor bucket read (A4) | `VendorBucket` fake in CI; live `FinishJob` fails with `FILE_NOT_FOUND` semantics kept separate from a permission error (a 403 is a job failure that retries, not a missing file) | Platform team grants `storage.objects.get` on `from/` | None in code |
| Deployment (Pulumi, replaces held Terraform) | Nothing runs live | Pulumi component reproduces `terraform/internal` | Outside this repo |
| DAL permission rows (A8) | Tenant membership only | `permissions_enforce = true` | One variable |

## 3. Fixed inputs from the design that bind this subtask

- **Endpoints** (Contracts step 0, 7, 8), all under `/v1/vendor-exports`: `PUT /schedules/{tenantId}/{vendor}` (create 201 without `version`, replace 200 with `version`, 409 on conflict or stale), `GET /schedules?tenantId=`, `GET /schedules/{tenantId}/{vendor}`, `POST …/disable` `{reason}`, `POST …/enable` `{reason, catchUp}`, `POST …/run-now` → `201 {exportBatchId, jobId, nextDueAt}`, `POST …/preview` → `200 {totalCount}`; `GET ?tenantId=&period=`, `GET /{exportBatchId}`, `GET /{exportBatchId}/npis` (paged); `POST /{exportBatchId}/retry` `{reason}` → `202 {exportBatchId, attempt, jobId}` from `FAILED`; `POST /{exportBatchId}/supersede` `{reason}` → `201 {exportBatchId, jobId}` from `DELIVERED`. Manage permission on writes, read on reads.
- **Schedule `PUT` body**: `version?`, `cadence {type: monthly|cron, dayOfMonth 1–28 | expression}`, `timezone` (IANA), `selection` (flat object, field → one operator clause, AND), optional `egressTemplateId` override (pilot) verified to belong to the tenant. Validation order: token and permission → tenant exists → schema → template provisioned or verified → write. Tenant ids with underscores refused. `PUT` never changes `enabled`.
- **Selection schema** (Contracts step 2): fields `data.delegationStatus` (eq, in), `data.practitionerRoles` (in), `data.practitionerType` (in), `data.licensedStates` (in), `data.statesToCredential` (in), `data.lineOfBusiness` (in), `credentialingStatus` (eq, in), `data.credentialingDueDate` (gte, lte, ISO dates), `data.userDefinedFields.<key>` (eq, in), `rosterIds` (in, max 20). Passed to api-layer as `filter` JSON `{ "<field>": { "<op>": value } }`, page size 100, offset paging, newest first. Only `id` and `npi` kept.
- **`nextDueAt` rules**: computed from cadence in the schedule's timezone, never in the past; cadence or timezone change recomputes, filter or template change does not; `enable` with `catchUp` picks the most recent missed occurrence between `disabledAt` and now, else the next future one.
- **Tick** (Contracts step 1): per due schedule one transaction: insert batch, advance `nextDueAt`, set `lastBatchId` and `lastRunAt`, write `EXPORT_BATCH_SCHEDULED` (`trigger = TICK | MANUAL | SUPERSEDE`); period is the month of the due `nextDueAt` in the tenant's timezone; `seq` is the next for that period; the unique index refusal counts as `skippedAlreadyExists`. Enqueue `SelectJob` after the transaction commits.
- **SelectJob** (step 2): page through api-layer, `upsertAll` per page, `NPIS_REGISTERED` per page (`page, count, firstNpi, lastNpi`), save `selection.page` on the batch after each page, resume from `page + 1` on retry; last page → `NPIS_SELECTED` with `EXPORT_SELECTION_COMPLETED` (`criteria, pages, practitionersSelected, durationMs`) and enqueue `RequestEgressJob`; zero → `EMPTY` with `EXPORT_BATCH_EMPTY`.
- **RequestEgressJob** (step 3): on `attempt > 1` cancel the prior `egress.correlationId` first (202 proceed; 409 → check the destination object: complete object → `EGRESS_COMPLETED` and enqueue `FinishJob`, stop); pin the template once (`templateVersion, mappingsCsvUrl, separator, outputFormat, rowExpansionKeys` from `GET /api/v1/egress-templates/{id}`), written to the batch before the call; destination `bucket = vendorBucket`, `objectName = from/<tenantId>/<tenantId>_<exportBatchId>_<yyyyMMdd>.csv` with the date in the schedule's timezone, stored on the batch as `egress.destination` and `file.name/path`; `POST /api/v1/egress/export` with `type practitioner, tenantId, correlationId <batchId>-r<attempt>, templateId, mappingsCsvUrl, separator, outputFormat, rowExpansionKeys (string), npiFilter, userId vendor-exchange-worker, destination`; 202 or 409 → store `egress.jobReference`, `requestedAt`, move to `EGRESS_REQUESTED` with `EXPORT_EGRESS_REQUESTED` (`egressCorrelationId, templateId, npiCount, egressResponse, deadlineAt`); then schedule `DeadlineCheckJob(check 1)` at now + `egressDeadlineHours` and store `egress.deadlineJobId`.
- **Event handler** (step 4 table): 401 before the body; unparseable or unknown `schemaVersion` → ack, `EXPORT_EVENT_REJECTED` (`UNPARSEABLE | UNKNOWN_SCHEMA`); unknown correlation → ack, log; tenant mismatch → ack, `EXPORT_EVENT_REJECTED (TENANT_MISMATCH)`; state past `EGRESS_REQUESTED` → ack, no effect; `COMPLETED` → `EGRESS_COMPLETED` (`completionSource = EVENT`, `completedAt`, `outputUri`, counts) with `EXPORT_EVENT_RECEIVED` (idempotent on `messageId`) and `EXPORT_EGRESS_COMPLETED` in one transaction, then enqueue `FinishJob`; `FAILED` → `FAILED` (`failedStep = EGRESS`, `cause = EGRESS_FAILED`, `lastError`) with `EXPORT_EVENT_RECEIVED` and `EXPORT_BATCH_FAILED`, delete the deadline job; exception before the state write → 500.
- **DeadlineCheckJob** (step 5): exit if past `EGRESS_REQUESTED`; else `GET /status/practitioner/{tenant}/{correlationId}`; `COMPLETED` with `gcs_complete = true` → `EGRESS_COMPLETED` (`completionSource = DEADLINE`) with `EXPORT_EGRESS_COMPLETED` and `EXPORT_EVENT_MISSED`, enqueue `FinishJob`; `FAILED` → `FAILED` with `EXPORT_BATCH_FAILED` and `EXPORT_EVENT_MISSED`; still running at check 1 → `EXPORT_EGRESS_STALE`, schedule check 2 at request + `egressAbandonHours`; still running at check 2 → best-effort cancel (`EXPORT_PRIOR_ATTEMPT_CANCELLED | _CANCEL_REJECTED`), then `FAILED` cause `EGRESS_DID_NOT_FINISH`.
- **FinishJob** (step 6): expects `EGRESS_COMPLETED`; read object metadata at `egress.destination`; missing → `FAILED` cause `FILE_NOT_FOUND`, `failedStep = EGRESS`; record `egress.fileProducedBy` from metadata `correlationId`; reconcile `totalRecords` against `countForBatch` → `reconciliation {registered, inFile, match, reconciledAt}` and `NPIS_RECONCILED`; move to `DELIVERED` with `file {name, path, rowCount, bytes, schemaVersion}`, `deliveredAt`, `EXPORT_BATCH_DELIVERED`; delete the pending deadline job.
- **Retry**: from `FAILED` only; `attempt + 1`; `SELECT` → `SCHEDULED` and `SelectJob`; `EGRESS` → `NPIS_SELECTED` and `RequestEgressJob`; `EXPORT_RETRY_REQUESTED` (`fromState, failedStep, newAttempt, reason`) with the operator as actor. **Supersede**: from `DELIVERED` only; new batch `seq + 1` in `SCHEDULED` (`trigger = SUPERSEDE`), old → `SUPERSEDED` with `supersededBy`, schedule `lastBatchId` updated, `EXPORT_BATCH_SUPERSEDED` and `EXPORT_BATCH_SCHEDULED`, enqueue the new `SelectJob`.
- **Reconciler**: for each stale non-terminal batch enqueue the job its state names (`SCHEDULED → select`, `NPIS_SELECTED → request-egress`, `EGRESS_COMPLETED → finish`); `EGRESS_REQUESTED` is owned by the deadline job and skipped.
- **Metrics** to record where they belong: `batches.created`, `selection.pages`, `selection.practitioners`, `egress.wait.seconds`, `egress.failed{reason}`, `completion.source`, `event.rejected{reason}`, `reconcile.mismatch`. All exist in `VendorExchangeMetrics`.
- **Mappings CSV** `certify-export-v1`: the 30 columns of the design's outbound file contract table with the verified egress paths; `entityType practitioner`, `outputFormat csv`, `rowExpansionKeys ["locations"]`. Open cells in the design (`practitioner_phone`, `telehealth_url` sources; `Y/N` rendering; group join) are carried as documented placeholders and tracked as a vendor-contract item, not resolved here.

## 4. Target layout (additions to the scaffolding)

```
export/schedule/   ScheduleResource, ScheduleService, SchedulePut/Enable/Disable requests, ScheduleView,
                   SelectionSchema (field/operator/value rules), SelectionCriteria.toFilterJson,
                   Cadence.next(from, zone) and catch-up, TemplateProvisioner (api-layer upload or reuse),
                   resources: vendor/candor/certify-export-v1.mappings.csv
export/batch/      BatchResource (get, list, npis, retry, supersede), BatchService, BatchView, NpiView,
                   DestinationNames (filename and object name), BatchLifecycle (the transition helpers
                   each job and handler share: scheduled → … with their audit events in one place)
export/jobs/       TickJob (creates batches), SelectJob, RequestEgressJob, DeadlineCheckJob, FinishJob bodies,
                   ReconcilerJob (re-enqueue by state)
export/events/     EgressEventResource branch → EgressEventHandler (the table above)
clients/           VendorBucket (interface: head object with metadata), GcsVendorBucket (google-cloud-storage)
```

No change in `config/`, `persistence/`, `auth/`, `http/`, `metrics/`, `audit/`, `terraform/`. A change there is a finding.

## 5. Steps

Each step: branch `feature/CP-39605-step-N-<slug>` from `main`, one PR, CI green, finding with gate evidence in `docs/decisions.md`, OpenAPI regenerated when an endpoint changes, hard stop for review before the next.

### Step 1 — Schedule endpoints and template provisioning (9 hours)

- `SelectionSchema` validating the field and operator table; `SelectionCriteria.toFilterJson()`; `Cadence.next(Instant from, ZoneId zone)` for monthly and cron (cron-utils or JobRunr's own cron parser), never in the past; catch-up computation.
- `TemplateProvisioner`: find the tenant's template by name (`GET /api/v1/egress-templates` list through `ApiLayerClient`, one new interface method) or create it with the mappings CSV from resources; verify an override id belongs to the tenant; return `{templateId, version}`.
- `ScheduleResource` and `ScheduleService`: `PUT` (create and replace with version check, `SCHEDULE_CREATED` / `SCHEDULE_UPDATED` with field diff), `GET` single and list, `disable`, `enable` with catch-up, `preview` (api-layer page size 1, `totalCount`). `run-now` arrives in Step 2 with batch creation.
- **Gate:** `ScheduleResourceIT` with WireMock api-layer: create 201 and the template upload parts; create again 409; replace with stale version 409 and with the right version 200 recomputing `nextDueAt` only for cadence or timezone; disable then enable with `catchUp` sets the most recent missed occurrence; invalid field or operator 400 with problem code; underscore tenant 400; preview returns api-layer's count. `CadenceTest` pins month-end, timezone offsets and DST days; `SelectionSchemaTest` pins the table.

### Step 2 — Tick creates batches, run-now, reconciler mapping (5 hours)

- `TickJob.run`: per due schedule one `Transactions.run` with insert, `ScheduleRepository.advance`, `EXPORT_BATCH_SCHEDULED`; duplicate key → `skippedAlreadyExists`; after commit `JobEnqueuer.select`; `metrics.batchCreated`. `BatchLifecycle.schedule(...)` is shared with run-now and supersede.
- `POST …/run-now` with `trigger = MANUAL` and `SCHEDULE_RUN_NOW`.
- `ReconcilerJob`: enqueue by state as in section 3; one `EXPORT_*` free log line and the E2 metric path.
- **Gate:** `TickJobIT`: two due schedules → two batches, `nextDueAt` advanced, events present, jobs enqueued once; the same tick twice creates nothing more; a period that already has a batch counts as skipped. `ReconcilerJobIT`: a stale `SCHEDULED` row gets a `select` job, an `EGRESS_REQUESTED` row gets nothing.

### Step 3 — SelectJob (5 hours)

- Paging loop against `ApiLayerClient.practitionerFindMany`, resume from `selection.page`, `upsertAll` per page, `NPIS_REGISTERED` per page, `updateInState` for the page cursor, final transition with counts, `EMPTY` branch, metrics.
- **Gate:** `SelectJobIT` with WireMock pages: three pages register every NPI once; a failure on page 2 then a retry fetches only pages 2 and 3 (verified by WireMock counts) and registers nothing twice; zero results → `EMPTY` with its event; api-layer 503 propagates so JobRunr retries.

### Step 4 — RequestEgressJob (6 hours)

- Template pin, prior-attempt cancel with the 409 destination check, `DestinationNames`, `EgressExportRequest` build, 202 and 409 handling, `EGRESS_REQUESTED` transition with its event, deadline scheduling and `deadlineJobId`, `egress.failed` metric on refusal.
- **Gate:** `RequestEgressJobIT` with WireMock egress: the request body matches the design's JSON field for field (asserted with `equalToJson` ignoring order); attempt 2 cancels `-r1` before requesting `-r2`; a 409 on export is treated as success; the pin is written before the export call (WireMock scenario: export fails after the pin, retry does not re-read the template); the deadline job exists with the right `scheduledAt`.

### Step 5 — Completion: event handler, DeadlineCheckJob, FinishJob (9 hours)

- `EgressEventHandler` implementing the handler table, `writeIdempotent` on `messageId`, `event.rejected` and `completion.source` metrics, deadline job deletion.
- `DeadlineCheckJob` both checks, status parsing, stale and abandon paths, cancel on check 2.
- `VendorBucket` interface plus `GcsVendorBucket` (`Storage.get(BlobId)` metadata only, never a download); `FinishJob` verification, reconciliation, `DELIVERED`, `reconcile.mismatch` and `egress.wait.seconds` metrics.
- **Gate:** `EgressEventHandlerIT` covers every row of the handler table including the duplicate message id (second delivery writes nothing) and the tenant mismatch; `DeadlineCheckJobIT` covers the four status outcomes across the two checks; `FinishJobIT` with a fake bucket covers present and matching, present and mismatching (`match = false`, metric, still `DELIVERED`), missing (`FAILED FILE_NOT_FOUND`), and the producing attempt recorded from metadata.

### Step 6 — Reads, retry, supersede (5 hours)

- `BatchResource`: get, list by tenant and period, NPI list with keyset paging (`listForBatch`); views hide `deadlineJobId` and other job ids.
- `retry` and `supersede` per section 3, actor from `UserContext`, both enqueue after commit.
- **Gate:** `BatchResourceIT`: retry from `FAILED/SELECT` yields `SCHEDULED` and a select job with `attempt 2`; retry from `FAILED/EGRESS` yields `NPIS_SELECTED` and a request-egress job; retry from any other state 409; supersede from `DELIVERED` creates `seq 2`, marks the old `SUPERSEDED`, updates `lastBatchId`; from any other state 409; reads are tenant-scoped (another tenant's batch is 404).

### Step 7 — Whole-lane proof (4 hours)

- `ExportLaneIT` on the worker profile with WireMock api-layer and egress and the fake bucket: `run-now` → `SelectJob` → `RequestEgressJob` → push event → `FinishJob` → `DELIVERED`, asserting the audit trail in order and the batch document against the design's sample. A second run with the event withheld proves the deadline path to `DELIVERED` with `completionSource = DEADLINE`. A third proves `FAILED` → retry → `DELIVERED` under `-r2` with the cancel call observed.
- **Gate:** the three scenarios green in CI; `docs/decisions.md` records the full event sequence of each as evidence.

### Step 8 — Documentation, OpenAPI, Jira (3 hours)

- README and runbook: operator procedures for enable, preview, run-now, retry, supersede, and reading a batch; `HANDOFF.md` state; `openapi/openapi.yaml` regenerated and reviewed; Jira CP-39605 Implementation Details and points, drafted in chat first.
- **Gate:** a second engineer can follow the runbook to enable a tenant and read its first batch against the integration-test stack.

## 6. Estimate

| Step | Hours |
| --- | --- |
| 1 Schedule endpoints and template provisioning | 9 |
| 2 Tick, run-now, reconciler | 5 |
| 3 SelectJob | 5 |
| 4 RequestEgressJob | 6 |
| 5 Completion (handler, deadline, finish) | 9 |
| 6 Reads, retry, supersede | 5 |
| 7 Whole-lane proof | 4 |
| 8 Docs, OpenAPI, Jira | 3 |
| **Total** | **46** |

Re-baseline for CP-39605: 46 (from the provisional 5; 1 SP = 1 hour).

## 7. Decisions taken before Step 1 (2026-10-05)

- **Mappings CSV content: option 1.** v1 ships with the twenty-six verified columns and the four open ones (`practitioner_phone`, `telehealth_url` sources; `Y/N` rendering of `accepting_new_patients` and `telehealth_available`; `group_affiliation` join) present with a documented placeholder, so provisioning works and the lane is proven end to end. The Candor contract call (open item O-1) produces a v2 upload touching only those rows; every batch pins its version, so nothing in flight moves. Decided by Dev.
- **Template lookup.** api-layer's `GET /api/v1/egress-templates` takes `tenant-id` and filters `search`, `status`, `entityType`, `createdBy`, `scheduleEnabled`, `page`, `size`, answering `{templates[], total, page, size}`. `TemplateProvisioner` searches by the template name this service assigns (`vendor-exchange candor certify-export-v1`), `entityType = practitioner`, `status = active`; one match is reused, none creates, more than one is a 409 for the operator to resolve. One new method on `ApiLayerClient`.
- **Cron parser.** JobRunr's `org.jobrunr.scheduling.cron.CronExpression(String)` with `next(Instant createdAt, Instant current, ZoneId zone)` is on the classpath already; `Cadence.next` uses it for `type = cron` and a `YearMonth`-based computation for `type = monthly`. No new dependency.

## 8. Definition of done for CP-39605

- Every gate in section 5 has evidence in `docs/decisions.md`; CI green on `main`.
- The three whole-lane scenarios pass in CI.
- `openapi/openapi.yaml` lists every endpoint in section 3 with problem+json responses.
- No change was needed in the foundation packages or the deployment definition; if one was, it is a numbered finding with the reason.
- Live verification against egress and the Candor bucket is listed in the runbook as the first action after the egress asks and the Pulumi deploy land, with the exact commands.
