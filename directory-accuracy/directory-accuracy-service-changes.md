# Directory accuracy service — saved filter flow and rename: what changed, what is left

Tracker written 2026-10-21. Repository `CertifyOS/directory-accuracy-service`, local `pdm-dal-full/directory-accuracy-service`. Use this to pick the work up later without re-deriving anything.

## Why this happened

- Egress will resolve the practitioner selection itself from a saved filter id (egress design CP-41069, Confluence page 2282979391, In-Review).
  - The caller no longer selects practitioners or sends an NPI list; it sends a filter id.
  - Egress fetches the filter, freezes the matching list once per export, reports counts, and can reuse that list on a retry.
  - The egress design asks this module to adopt that flow (its ask A3).
- The service and repository are named the directory accuracy service: vendor export, vendor ingestion, and later attestation are modules in one repository, one deployment, one database.

## Decisions settled on 2026-10-21

- **Schedule stores a filter id only.** The schedule `PUT` takes `filterId` and stores it as given. This service never calls the filter store and never sees criteria.
  - The filter is created beforehand in api-layer's filter module (`POST /filters`, CP-44214) by an operator, once that module is in use.
  - The id is checked for form only (letters, digits, `_`, `-`, at most 255 characters).
  - A wrong or deleted id is found on the first export: egress fails it with `FILTER_NOT_USABLE`, the batch goes `FAILED`, alert E4.
- **Preview endpoint removed.** With no criteria on the schedule there is nothing to count. A filter that matches nobody ends `EMPTY` with alert E8.
- **List-used file on.** Every export request sends `listUsed: true`; the batch stores only `listUsedUri`; operators diff it against the file on a count mismatch (egress keeps it 7 days).
- **Completion event carries the selection data.** Egress ask 5 (CP-41070) is extended: the event adds `failureReason` (a reason code, null on success, also a message attribute) and the `selection` block (counts, `listUsedUri`, `reusedFrom`). The status response already carries both.
- **Step numbering kept in the phase 1 doc.** Step 2 became "Selection — egress resolves the saved filter".
- **`/v1` on every operator and review path** in both docs, matching the code. `/internal/…` push paths have no `/v1`.
- **Module names unchanged:** collections `vendor_export_*` and `vendor_review_*`, permissions `vendor-export:*` and `vendor-review:*`, paths `/v1/vendor-exports` and `/v1/vendor-review`, audit actors `system:vendor-export` and `system:vendor-ingestion`, metrics `vendor_export.*`, the `vendor-ingestion` file-ingestion consumer.
- **Companion docs stay local**; only the two design docs are published.

## Done

### Phase 1 design doc — CP-39602 vendor export (Confluence page 2252767300, now version 9)

- Title: "Design: Vendor export — scheduled export by saved filter, file build through the egress service, event-driven completion, and the export batch record".
- Purpose and scope: saved filter id on the schedule; selection by reference; batch row with egress counts and no per-NPI rows; "One service, three modules" block naming the repository `directory-accuracy-service`.
- Approach:
  - Step 0 takes `filterId`; preview bullet removed; note that a wrong id fails the first export.
  - Step 1 enqueues `RequestEgressJob` directly.
  - Step 2 rewritten as selection in egress.
  - Step 3: state check `SCHEDULED`; `selection` object with `listUsed: true`; retry sends `reuseSelectionFrom`, falls back to `filterId` with `reEvaluated = true`; handling for `429 SELECTION_QUOTA_EXCEEDED` and `403 SELECTION_SOURCE_DISABLED`.
  - Steps 4 and 5: event and status carry `failureReason` and `selection`; `NO_MATCHING_PRACTITIONERS` makes the batch `EMPTY`.
  - Step 6: reconcile file `totalRecords` against `selection.distinct`; `registered` renamed `selected`; list-used file for mismatches.
  - Steps 7 and 8: state lists updated; retry reuses the frozen list; supersede evaluates the filter afresh; acknowledgement is module 2's `inboundFiles[]`.
  - Three job types instead of four; the "select and request-egress are not one job" block removed.
- Decisions: D1 (instance groups) renamed; D5 (selection) and D9 (batch record) rewritten; D12 (egress asks) reworded; new D16 (schedule stores a filter id only, api-layer checks the filter at save).
- Alternatives: new "Own selection through api-layer, NPIs in the request (the earlier design)"; tenant-configuration and Cloud Run alternatives updated.
- Contracts:
  - Step 0: preview row removed, `filterId` in the request and response, validation order kept with the tenant-id underscore rule.
  - Step 2: reference section "The saved filter".
  - Step 3: request sample with `selection`, retry sample, response table for `400`, `403`, `429`.
  - Step 4: event sample with `failureReason` and `selection`; handler table gains the `EMPTY` row.
  - Step 5: status sample with `selection`.
  - Step 6: `EXPORT_RECONCILED`; `/npis` endpoint removed.
  - Retry table: `SELECT` row removed, `EGRESS` goes back to `SCHEDULED`.
- Configuration, data model, security, performance, observability, audit, failure modes, rollout: all follow the above.
  - `vendor_export_npis` section deleted.
  - Audit events `NPIS_REGISTERED` and `EXPORT_SELECTION_COMPLETED` removed; `NPIS_RECONCILED` became `EXPORT_RECONCILED`.
  - `EXPORT_BATCH_ACKNOWLEDGED` fields match phase 2 (`uploadId`, `vendorBatchId`, `daysToResponse`).
  - Egress ask 1 is CP-41069; ask 5 adds `failureReason` and `selection`.
  - api-layer ask is "switch the saved-filter store on" (CP-44214).
- Three edits that existed only on the live page were folded into the local markdown before publishing: "through api-layer" on the template pin, "overwrite-race guarantee", the removed "Two places hold configuration" paragraph.
- The "flow in six steps" image was redrawn for the new flow (`vendor-export-flow-steps.png`) and sits under the Approach intro with the lifecycle artifact link.
- Published by `cos-docs/directory-accuracy/vendor-export/publish_confluence_page.py` (full body from markdown, keeps the H1, swaps the mermaid block for the image and link). Version 4 backup: `confluence-backup-v4.storage.html`.

### Phase 2 design doc — CP-40437 vendor ingestion (Confluence page 2302345225, now version 5)

- "One service, three modules" block names the repository `directory-accuracy-service`; the "module 1 open question Q10" wording removed.
- Identity lookup alternative: "reads module 1's registry" removed; "egress filters by practitioner id" became "egress resolves the saved filter and keeps its frozen list 7 days".
- Data model "Where": "export registry" became "export batch rows".
- Rejected values: the later exclusion feeds module 1's saved filter (two places).
- 22 `/vendor-review` paths now `/v1/vendor-review`.

### Lifecycle artifact

- https://claude.ai/artifact/3y3YLP3EutD9yYtPy7qNvf (same artifact as the old `claude.ai/code/artifact/18057e71…` link), version 2.
- Six steps: create the schedule (filter id), daily tick, request egress, egress selects practitioners, egress builds and places the file, confirm delivery.
- Board: `SelectJob`, `vendor_export_npis`, `NPIS_SELECTED` removed; `directory-accuracy-worker`; `/v1` paths; 26 numbered edges.

### Local companion and repository docs

- `cos-docs/directory-accuracy/vendor-export/vendor-export-concepts.md` brought in line with the design doc (concepts, baseline rows 28–31, approaches including the new selection comparison, traceability, inputs and outputs, rationale, open questions Q13 and Q14, ticket status).
- `cos-docs/directory-accuracy/README.md` lists the new files and the 2026-10-21 status.
- cos-docs commit `928d4b1` holds the doc work up to the concepts update; the concepts update itself is not committed yet.

### Repository rename (PR #25, merged 2026-10-21, merge commit `0b79bde`)

- Java package `com.certifyos.vendor_exchange` → `com.certifyos.directory_accuracy`; Gradle group and root project `directory-accuracy-service`.
- `DirectoryAccuracyConfig` (prefix `directory-accuracy.*`, `DIRECTORY_ACCURACY_ENABLED`), `DirectoryAccuracyMetrics`.
- MongoDB and JobRunr database `directory_accuracy`; OpenTelemetry service name and OpenAPI title `directory-accuracy-service`; secret `directory-accuracy-mongodb-uri`.
- Egress initiator `directory-accuracy-worker`; DAL requesting user `directory-accuracy-service`; problem URN `urn:certifyos:directory-accuracy:problem:`; egress template display name `directory-accuracy candor certify-export-v1`; mappings CSV constant path `_directory_accuracy.*`.
- Makefile and Terraform: image repository and image, instance groups, service accounts (`directory-accuracy-pubsub-push` is 30 characters, the GCP limit), load balancer, hostname `directory-accuracy.<env>.certifyos.com`, secrets, startup script, state prefix.
- Pub/Sub subscription and dead-letter topic aligned with the design: `vendor-export-egress-events`, `vendor-export-egress-events-dlq`, filter `initiator = "directory-accuracy-worker"`.
- Docs in the repository updated; finding 108 in `docs/decisions.md` records the rename.
- Evidence: 351 of 364 changed files identical after name mapping, the rest line wraps or the planned doc edits; CI green with 286 unit and 371 integration tests; no old name left outside history; no GCP resource with the old name exists.
- GitHub repository renamed; local folder moved; `origin` is `git@github.com:CertifyOS/directory-accuracy-service.git`; `main` synced; clean build compiles; all 23 local branches already merged.

## Pending code changes — move the export module to the saved filter flow

Nothing below is built. The code still implements the earlier own-selection design. Paths are under `src/main/java/com/certifyos/directory_accuracy/` unless stated.

### Blocked by

- **CP-41069 (egress saved-filter selection):** In-Review. The request shape, status block, failure reasons, `reuseSelectionFrom`, and list-used file must exist in egress before this ships.
- **CP-41070 (egress completion event):** In Progress. Must also carry `failureReason` and the `selection` block (not yet asked; see *Pending outside the code*).
- **CP-44214 (api-layer saved-filter store):** built, switched off by default (`api-layer.saved-filter.enabled`). Must be on in each environment, with egress holding its shared read key.
- A ticket for this work does not exist yet.

### Schedule: criteria out, `filterId` in

- `export/schedule/Schedule.java`: replace `SelectionCriteria selection` with `String filterId`; update the factory, document mapping, and every reader.
- `export/schedule/ScheduleRequests.java`:
  - `PutRequest`: replace `Map<String, ClauseRequest> selection` with `String filterId`.
  - Validate `filterId`: present, `[A-Za-z0-9_-]{1,255}`. Answer `400` with a clear code (for example `FILTER_ID_INVALID`).
  - Delete `ClauseRequest`, `PreviewRequest`, and `toSelection`.
- `export/schedule/ScheduleService.java`:
  - Store `filterId` on create and replace.
  - A `filterId` change does not move `nextDueAt`.
  - Validation order: token and permission → tenant exists (no underscore in the tenant id) → `filterId` form → template provisioned or verified → write.
- `export/schedule/ScheduleResource.java`: delete `POST /v1/vendor-exports/schedules/{tenantId}/{vendor}/preview`; schedule views return `filterId`.
- Delete `export/schedule/SelectionCriteria.java`, `SelectionSchema.java`, `SelectionPreview.java`.
- `export/schedule/ScheduleRepository.java`: persist and read `filterId`; drop `selection`.
- Audit: `SCHEDULE_CREATED` detail `selection` → `filterId` (`audit/AuditDetailContract.java` line with `"selection"`). `SCHEDULE_UPDATED` diffs then show `filterId` before and after.

### Batch row and states

- `export/batch/BatchState.java`: remove `NPIS_SELECTED`. Main path `SCHEDULED → EGRESS_REQUESTED → EGRESS_COMPLETED → DELIVERED`; side states `EMPTY`, `FAILED`, `SUPERSEDED`.
- `export/batch/ExportBatch.java`:
  - `Selection` record becomes `filterId`, `requested`, `distinct`, `unmatched`, `parts`, `reusedFrom`, `listUsedUri`, `reEvaluated` (only when a retry could not reuse), `completedAt`. Drop `criteria` and `page`.
  - `FailedStep`: remove `SELECT`; keep `EGRESS`.
  - Remove the unused `acknowledgedAt` and `inboundBatchId` fields; acknowledgement lives in module 2's `inboundFiles[]`.
  - `scheduled(...)` factory takes `filterId` instead of `SelectionCriteria`.
- `export/batch/Reconciliation.java`: rename `registered` to `selected` (field, factory, document key, Javadoc).
- `export/batch/BatchLifecycle.java`:
  - Delete `registerPage` and `completeSelection`.
  - `markEmpty` is now called from the event handler and the deadline check, not from selection; its audit detail becomes `filterId`, `failureReason`, `completionSource`.
  - `egressRequested` takes `filterId` and `reuseSelectionFrom` instead of an NPI count.
  - `schedule(...)` copies the schedule's `filterId` onto the new batch.
  - `egressCompleted` (and the deadline path) records the `selection` block.
- Delete `export/batch/ExportNpi.java` and `export/batch/ExportNpiRepository.java`.
- `persistence/Collections.java`: remove `npis()` and `vendor_export_npis` from the owned-collection list (the count in the Javadoc changes).
- `config/MongoIndexes.java`: remove the two `npis_*` indexes.
- `export/batch/BatchOperations.java`:
  - Retry from `FAILED` always goes back to `SCHEDULED` and enqueues `RequestEgressJob`.
  - Supersede enqueues `RequestEgressJob` for the new `seq`.
  - Remove `SelectJob` imports.

### Jobs

- Delete `export/jobs/SelectJob.java`, `SelectJobRequest.java`, and `JobEnqueuer.select(...)`.
- `export/batch/BatchLifecycle.java` `schedule(...)` (around line 141, used by the tick and run-now) enqueues `RequestEgressJob` instead of `enqueuer.select(...)`; same in `BatchOperations` retry (around line 158) and supersede (around line 259).
- `export/jobs/RequestEgressJob.java`:
  - Expect state `SCHEDULED` (was `NPIS_SELECTED`).
  - Remove `ExportNpiRepository` and the NPI read.
  - Build the `selection` object.
    - First attempt: `{ "source": "SAVED_FILTER", "filterId": <batch.filterId>, "listUsed": true }`.
    - Retry (`attempt > 1`): `{ "reuseSelectionFrom": <previous correlationId>, "listUsed": true }`.
    - Egress `400` on reuse (unfinished, expired, missing): send the `filterId` form again and set `selection.reEvaluated = true`.
  - `429 SELECTION_QUOTA_EXCEEDED`: throw so JobRunr retries; honour `Retry-After` if the JobRunr retry policy allows, else document that backoff covers it.
  - `403 SELECTION_SOURCE_DISABLED` and any other `400`: mark the batch `FAILED` at `EGRESS`, alert E4.
  - Keep cancel-before-retry, template pin, `409` handling, destination, deadline scheduling as they are.
- `export/jobs/DeadlineCheckJob.java`:
  - Read `selection` from the status response.
  - `COMPLETED` with `gcs_complete`: record `selection` with `EGRESS_COMPLETED`.
  - `FAILED` with `selection.failureReason = NO_MATCHING_PRACTITIONERS`: mark `EMPTY`.
  - Any other `FAILED`: unchanged.
- `export/jobs/FinishJob.java`:
  - Remove `ExportNpiRepository`.
  - Compare object metadata `totalRecords` with `batch.selection().distinct()`.
  - Write `reconciliation.selected`; audit `EXPORT_RECONCILED`.
  - Decide what happens when `selection` is missing (an event without the block and no deadline check yet): one status call, or fail with a clear cause. Pick one and record it in `docs/decisions.md`.
- `export/jobs/ReconcilerJob.java`: `SCHEDULED` → `RequestEgressJob`; `EGRESS_COMPLETED` → `FinishJob`; remove the `NPIS_SELECTED` case and the `SelectJob` reference; update the Javadoc.
- `export/jobs/BatchFinalFailureFilter.java`: remove the `SelectJob` → `SELECT` mapping; `RequestEgressJob`, `DeadlineCheckJob`, `FinishJob` map to `EGRESS`.
- `export/jobs/BatchJobSupport.java` and `JobIds`: drop anything select-specific.

### Clients

- `clients/EgressExportRequest.java`:
  - Remove `npiFilter`.
  - Add `Selection selection` with `source`, `filterId`, `expectedCount` (not sent), `listUsed`, `reuseSelectionFrom`.
  - Field names and nullability exactly as CP-41069's final contract; recheck against the merged egress code, not the design page.
- `clients/EgressStatusResponse.java`: add `selection` (`state`, `requested`, `distinct`, `matched`, `unmatched`, `parts`, `listUsedUri`, `reusedFrom`, `attempts`, `failureReason`).
- `clients/ApiLayerClient.java`: delete `practitionerFindMany`; delete `clients/PagedPractitioners.java` and `PractitionerRef` if nothing else uses them. Keep the egress-template methods.
- `config/DirectoryAccuracyConfig.java` and `application.properties` hold no selection-only keys (checked 2026-10-21); the api-layer client config stays for templates.

### Completion event

- `export/events/EgressEvent.java`: add `String failureReason` and a `Selection selection` record matching ask 5 as egress builds it.
- `export/events/EgressEventHandler.java`:
  - `COMPLETED`: record `selection` with `EGRESS_COMPLETED`.
  - `FAILED` with `failureReason = NO_MATCHING_PRACTITIONERS`: move to `EMPTY` with `EXPORT_EVENT_RECEIVED` and `EXPORT_BATCH_EMPTY` in one transaction, delete the deadline job, alert E8.
  - Other `FAILED`: unchanged.
- `EXPORT_EVENT_RECEIVED` detail adds `failureReason` and `selectionDistinct`.
- Message attribute `failureReason` is informational; the subscription filter stays on `initiator`.

### Read API and views

- `export/api/VendorExportsResource.java`: delete `GET /v1/vendor-exports/{exportBatchId}/npis`; update the retry Javadoc (no `NPIS_SELECTED`).
- `export/api/BatchViews.java`: delete `NpiView`; the batch view's `selection` shows `filterId`, counts, `reusedFrom`, `reEvaluated`, `listUsedUri`.
- `export/ExportStateGauges.java`: no `NPIS_SELECTED` gauge value.

### Audit

- `audit/AuditEventType.java`: remove `NPIS_REGISTERED` and `EXPORT_SELECTION_COMPLETED`; rename `NPIS_RECONCILED` to `EXPORT_RECONCILED`.
- `audit/AuditDetailContract.java`:
  - `SCHEDULE_CREATED`: `selection` → `filterId`.
  - `EXPORT_BATCH_EMPTY`: `criteria` → `filterId`, `failureReason`, `completionSource`.
  - `EXPORT_EGRESS_REQUESTED`: `npiCount` → `filterId`, `reuseSelectionFrom`.
  - `EXPORT_EVENT_RECEIVED`: add `failureReason`, `selectionDistinct`.
  - `EXPORT_RECONCILED`: `selected`, `inFile`, `match`.
- No stored audit rows exist (nothing deployed), so no back-fill.

### Metrics

- `metrics/DirectoryAccuracyMetrics.java`: delete `SELECTION_PAGES` and `selectionPage`; `selectionPractitioners` is set from egress's `distinct` when the batch reaches `EGRESS_COMPLETED`.
- Runbook metric list and the observability ticket (CP-44604) follow.

### Tests to update or delete

- Delete: `export/jobs/SelectJobIT`, `export/schedule/SelectionCriteriaTest`, `SelectionSchemaTest`, `export/batch/ExportNpiRepositoryIT`, preview cases in `ScheduleResourceIT`, `/npis` cases in `BatchResourceIT`.
- Update: `ExportLaneIT` (whole lane with `selection` request, event with counts, reconciliation), `RequestEgressJobIT` (request body, retry with reuse, reuse refused, `403`, `429`), `DeadlineCheckJobIT` (`EMPTY` from status), `EgressEventHandlerIT` (`EMPTY` from event, counts recorded), `FinishJobIT` (selected count), `ReconcilerJobIT`, `TickJobIT`, `JobRunrGateIT`, `JobLogContextIT`, `EgressFixtures`, `EgressClientIT`, `EgressContractsTest`, `ApiLayerClientIT`, `ScheduleRequestsTest`, `ScheduleTest`, `ScheduleRepositoryIT`, `ExportBatchTest`, `ExportBatchRepositoryIT`, `BatchStateTest`, `BatchJobSupportTest`, `AuditDetailContractTest`, `AuditLogLineIT`, `AuditRepositoryIT`, `AuditReadIT`, `DirectoryAccuracyMetricsTest`, `MetricsEndpointIT`.
- Ingestion tests that build export batches (`InboundFixtures`, `DesignRows`, `ArrivalHandlerIT`, `VendorIngestionOpsResourceIT`, `VendorReviewResourceIT`, `IngestionFinalFailureFilterTest`, `RequestIngestionJobTest`): change their fixtures from `SelectionCriteria` / `NPIS_SELECTED` to `filterId` and the new states.
- `ArchitectureTest` still passes; nothing in `export` may import `clients.PagedPractitioners`.

### Repository docs and contract

- `openapi/openapi.yaml`: regenerate with `make openapi` on the branch (CI's `verifyOpenApi` compares byte for byte). Preview and `/npis` disappear; the schedule `PUT` body and batch view change. The `openapi` CI job runs oasdiff against `main` and will flag removed endpoints as breaking; acceptable because nothing is deployed, but say so in the PR.
- `README.md`: lane line (no `SelectJob`), operator endpoint list (no preview, no NPIs), package map.
- `docs/runbook.md`:
  - Enable-a-tenant example uses `filterId`.
  - Remove the preview and `/npis` commands.
  - Main-path states without `NPIS_SELECTED`; retry text without `SELECT`.
  - Mismatch row points at `selection.listUsedUri`.
  - E2, E6, E8 wording; metrics list; pilot checklist step 3 becomes "confirm the filter's count in the filter module".
- `AGENTS.md`, `HANDOFF.md`: any mention of selection, NPIs, preview.
- `docs/decisions.md`: a new finding for the switch (what was removed, the request shape as egress built it, the missing-`selection` decision from `FinishJob`, the oasdiff note).
- Terraform: no change (subscription and filter already match).

### Verification before the PR

- Compile all three source sets; Spotless, Checkstyle, SpotBugs locally. Tests run in CI.
- Search for leftovers: `SelectJob`, `SelectionCriteria`, `SelectionSchema`, `SelectionPreview`, `ExportNpi`, `vendor_export_npis`, `npiFilter`, `NPIS_SELECTED`, `NPIS_REGISTERED`, `NPIS_RECONCILED`, `EXPORT_SELECTION_COMPLETED`, `practitionerFindMany`, `preview`, `registered` (in reconciliation), `FailedStep.SELECT`, `acknowledgedAt`, `inboundBatchId`. Only `docs/decisions.md` history may keep them.
- CI green: unit, integration, `verifyOpenApi`, secrets, Snyk.
- Compare against the phase 1 design doc section by section (Approach steps 0–8, Contracts steps 0–8, Data model, Audit) so the code and the published page say the same thing.
- After egress ships CP-41069 and the event change: one staging run end to end with a real saved filter (store on, egress switch on, our tenant on the template pipeline). Check the request body, the event payload, `EMPTY` with a filter that matches nobody, a retry that reuses the list, and the reconciliation.

## Pending outside the code

- **Rotate the Atlassian API token** pasted in chat on 2026-10-21.
- **Push cos-docs** (`928d4b1` and the uncommitted concepts update).
- **Raise ask 5's extension with the egress team** on CP-41070: `failureReason` (reason code, null on `COMPLETED`, also as a message attribute) and the `selection` block (`requested`, `distinct`, `matched`, `unmatched`, `parts`, `listUsedUri`, `reusedFrom`) on every completion event. Why: tells an empty selection from a failure, gives reconciliation its expected count without a status call.
- **Confirm with the egress team** (CP-41069 review): `listUsed` for a `reuseSelectionFrom` request writes a list-used file too; the exact `400` codes for a refused reuse; whether `429` carries `Retry-After` in seconds.
- **Create the ticket** for the code change above (Type of Effort, Pod PDM, points in hours).
- **Send DevOps the new names** for the Pulumi stack: finding 108 in `docs/decisions.md` lists them (instance groups, service accounts, secrets, hostname, registry, Pub/Sub subscription and dead-letter topic, database name).
- **api-layer:** switch the saved-filter store on per environment and give egress its read key (CP-44214); operators create filters once the filter module is in use.
- **Cleanup, optional:**
  - Delete the GitHub branch `chore/rename-directory-accuracy-service`.
  - Delete the empty Terraform state at `gs://certifyos-tf-state-development/vendor-exchange-internal/state/` (0 resources, left by a `terraform init` on 2026-10-05).
  - Remove the unused attachments on the phase 1 Confluence page (`image-20260924-113650.png`, `vendor-export-lifecycle.png`).
- **The other developer:** the local rename steps were shared on 2026-10-21 (folder, remote, clean build, `.env`, local Mongo database, IDE, rebasing branches).

## Open questions

- Egress sign-in: the CP-41069 design says egress checks no caller identity today, while the phase 1 doc says IAP. Tracked outside this module.
- Location-level filtering (only active locations, location state) needs location fields in the filter language (egress design ask A2 to the data-access layer).
- Whether NPIs skipped by a reviewer sit out later cadences: the exclusion would have to live in the saved filter or in egress (concepts doc Q14).
- Egress end-to-end time for a 70,000-practitioner selection export: estimated 1.7–2.8 hours; measure in staging against the 6-hour first deadline check.
