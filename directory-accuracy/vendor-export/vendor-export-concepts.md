# Companion — Vendor export module (concepts + supplementary material)

> Two parts. **Part 1 — Concepts:** plain-language explanations of every service, tool, and concept named in `vendor-export.md`, for a reader new to the stack. **Part 2 — Supplementary material:** the analysis behind the module doc — baseline evidence with receipts, approaches scored against criteria, traceability, inputs and outputs, anticipated questions, open questions with owners, ticket status. The module doc stands alone; this file is preparation and defense material. Local only, never published.
>
> Brought in line with the module doc on 2026-10-21: selection by saved filter id resolved in egress, event-driven completion with a deadline fallback, no per-NPI registry, no archive copy, the service named `directory-accuracy-service`.

## Part 1 — Concepts

### Directory accuracy service

- The one service that holds vendor export (this module), vendor ingestion (module 2), and later attestation: one repository (`CertifyOS/directory-accuracy-service`, renamed from `vendor-exchange-service` on 2026-10-21), one deployment, one MongoDB database (`directory_accuracy`).
- Two GCE instance groups built from one image: `directory-accuracy-api` (HTTP endpoints, behind a load balancer) and `directory-accuracy-worker` (JobRunr jobs, no inbound traffic).
- Module names stay module-specific: collections `vendor_export_*`, permissions `vendor-export:manage` and `vendor-export:read`, paths under `/v1/vendor-exports/…`.

### The egress service (`core-dal-egress-practitioner-async`)

- A Quarkus service plus Cloud Run Jobs that exports practitioner, facility, or group data for a tenant as a file.
- Flow: `POST /api/v1/egress/export` creates a per-job Spanner table; a seed job fetches each selected entity over DAL REST and stores its full JSON in the table's `data` column; an export job writes the file to GCS.
- Two file paths: NDJSON (one JSON per entity) or, when a template is given, a template-driven CSV. The launcher picks one; both are not produced for the same job.
- Today: completion is a status endpoint with a `phase` enum, an email to the initiating user, and a `complete=true` marker on the GCS object. No Pub/Sub event, no webhook; that is egress ask 5.
- Today: selection is an NPI or TIN list of at most 100. The saved filter selection (below) is egress ask 1, design CP-41069.
- The per-job table has a 7-day TTL; the GCS file stays.

### Saved filter (api-layer filter store)

- A practitioner filter saved once in api-layer's filter module and referred to by its id (`flt_…`). Built in CP-44214, switched off by default (`api-layer.saved-filter.enabled`).
- The filter is the data-access layer's own filter JSON: field → operator → value, fields joined with AND, for example `{ "data.delegationStatus": { "in": ["Direct", "Delegated"] } }`.
- Checked once, when saved: allowed fields and operators per entity type, value types, no OR groups, no `tenantId`, at most 20 fields, 200 values in one list, 8 KB. A broken rule is refused with `400` naming what is allowed.
- Tenant-less: the same criteria always get the same id, and the tenant travels with the export request.
- `POST /filters` needs a platform token; `GET /filters/{id}` accepts only egress's shared key. This service never reads a filter; it stores the id.

### Frozen list and list-used file

- When egress runs a saved-filter export, it fetches the filter, pages the data-access layer with it, and writes the matching practitioner ids once into its own table (`egress_selection_items`, kept 7 days). Every slice, retry, and count of that export reads only that copy: the "frozen list".
- A retry under a new correlation id can say `reuseSelectionFrom: <earlier correlation id>`, so every attempt of one batch exports the same practitioners.
- With `listUsed: true` egress also writes `list-used.csv` beside the export file: one `npi` per practitioner it used. This module stores its location on the batch row; operators read it when counts disagree.

### Egress template

- A row in the DAL's `egress_templates` table: entity type, output format, separator, `rowExpansionKeys`, a GCS URL to a mappings CSV.
- Created and listed through api-layer's `/api/v1/egress-templates` endpoints; the mappings CSV is uploaded to a tenant-scoped GCS path. This module provisions one per tenant from its own reviewed mappings CSV and pins the version-specific CSV URL on each batch.

### Mappings CSV

- One line per output column: `output_column`, `attribute_path`, `json_extraction_path`, `multi_value_handling`, `max_columns`, `column_order`, `entity_group`, `default_value`, plus optional `data_transformations`, `dedupe_by`, `fallback_json_extraction_path`.
- `json_extraction_path` is a dotted path into the entity JSON. `[]` on an expansion array means "the element for this row"; `[key=value]` filters an array; `[n]` is a literal index.
- `multi_value_handling`: blank = one value; `columns` = spread across `_1.._N`; `comma_separated` = join with `", "`.
- `default_value` fills a blank result. A path that never resolves (`_directory_accuracy.constant`) plus a default is how a constant column is made.
- `data_transformations` functions available today: `lower`, `upper`, `proper`, `trim`, `split`, `substitute`, `text`.

### Row expansion (`rowExpansionKeys`)

- Tells egress which arrays multiply rows. `locations` is an alias for the chain `groupMemberships` then `groupPractitionerLocations`.
- Expansion is hierarchical: locations are counted inside each group, so a practitioner in two groups with two and one locations yields three rows, not four.

### Practitioner relationships JSON

- The document egress stores per practitioner: the operational value fields at the top (`npi`, `firstName`, `languages[]`, …) plus `groupMemberships[]`, each with `tenantGroup.group.data` (group npi, name, tin) and `groupPractitionerLocations[]`.
- Each location row carries `data` (phone, fax, acceptingNewPatients, website), `groupLocation.locationId` (the core location id), `groupLocation.location.data` (name, handicapAccessible, office hours), and `locationEntityAddresses[]` with `data.addressType` and `address.data`.

### JobRunr

- A Java background-job library that stores jobs in our MongoDB: enqueue now, schedule for later, recurring by cron, retry with backoff, recover a job whose VM died, and a dashboard of every job per batch id.
- Runs on the worker group. Three job types here (`RequestEgressJob`, `DeadlineCheckJob`, `FinishJob`) and two recurring jobs (the daily tick and the hourly reconciler). Payloads carry the batch id only; the batch row holds all state.

### Pub/Sub push subscription and dead-letter topic

- Egress publishes one message per finished export to a topic it owns. Our push subscription (`vendor-export-egress-events`) posts each message to `/internal/vendor-exports/egress-events` on the api group, filtered on `initiator = "directory-accuracy-worker"`.
- `2xx` acknowledges, anything else is retried; after five failed deliveries the message moves to the dead-letter topic `vendor-export-egress-events-dlq` (a holding topic for messages that could not be processed) and an alert fires.
- The message we ask for carries the phase, counts, the output path, a `failureReason` code, and the `selection` block (counts and the list-used file location).

### Deadline check

- One scheduled JobRunr job set when egress is asked, at 6 hours. If the completion event already arrived, it exits. If not, it makes one status call and acts on the answer; still running means one more check at 48 hours, then the batch fails for a human to decide.
- It replaces polling: the normal path never calls egress's status endpoint.

### Cloud Scheduler, Cloud Tasks, Cloud Run, Cloud Run Jobs

- Not used by this module; they appear in *Alternatives considered*.
- Cloud Scheduler: managed cron that calls a URL on a schedule; retries; carries no logic.
- Cloud Tasks: a managed work queue of HTTP calls with retries and backoff. A named task cannot be created twice. No dead-letter queue: an exhausted task is deleted.
- Cloud Run: managed containers behind HTTPS, scale to zero, request deadline up to 60 minutes.
- Cloud Run Job: the same image run to completion without a request deadline; how egress runs seed, selection, and export.

### GCE managed instance group (MIG)

- A group of identical Compute Engine virtual machines created from one instance template; the group keeps a target number of VMs running, replaces unhealthy ones, and rolls out a new template VM by VM.
- Two groups is the usual split for a service with an HTTP side and a background side: an `api` MIG behind an HTTPS load balancer, and a `worker` MIG with no inbound traffic that runs the job server.
- A MIG never scales to zero; every VM in it is billed whether or not it is busy. Committed-use discounts (a one- or three-year commitment) cut the VM price by roughly a third or more.
- Inbound HTTPS needs an external load balancer and an identity check in front of it (IAP or the application's own token verification).
- Egress to the internet from a VM needs either an external IP per VM or a Cloud NAT gateway for the subnet.
- The file-ingestion service runs on this shape.

### GCS server-side copy (rewrite)

- Copying an object between buckets without downloading it; one API call regardless of size.
- Egress uses it to copy the finished file to the destination we name on the request, create-only (`ifGenerationMatch: 0`), so a late copy from an older attempt cannot replace a file that has landed. This module never copies or archives the file.

### The shared SFTP platform

- DevOps-operated SFTP servers mounted on a GCS bucket per vendor (gcsfuse). Each vendor has an account; `from/<tenantId>/` is what CertifyOS drops (vendor read-only), `to/<tenantId>/` is what the vendor drops.
- Every upload becomes one GCS object; object-finalize events on the bucket feed module 2's inbound path.

### Tenant configuration (`tenant_configurations`)

- A platform table read through api-layer's `/tenant-configurations` endpoints: one entry per tenant per configuration type, with a JSON `configuration` body.
- Not used by this module: the schedule lives in our own collection. Why is in the module doc's *Alternatives considered*.

### Idempotency

- Running an operation twice produces the effect once. Here: deterministic ids (batch id, egress correlation id, JobRunr job ids) plus database uniqueness and guarded state transitions, never application memory.

### Compare-and-set state transition

- An update that succeeds only if the row is still in the expected state. Two workers racing for one batch: one wins, the other sees zero rows updated and exits.

### Reconciler

- A JobRunr recurring job on the worker group that runs once an hour and looks for batches whose next job was never enqueued.
- The gap it closes: every step writes the batch row and then enqueues the next job, two writes to two collections with no shared transaction. A crash between them leaves a row that says "ready for step X" with no job for step X. JobRunr cannot help, because there is no job to retry.
- What it does: one indexed query for batches in `SCHEDULED` or `EGRESS_COMPLETED` not updated for more than `reconcilerStaleMinutes` (30); for each, enqueue the job that state calls for (`RequestEgressJob`, `FinishJob`); fire alert E2.
- Why it is safe to be generous: every job checks the row state first, so a duplicate enqueue finds the state moved on and exits.
- What it leaves alone: `EGRESS_REQUESTED`, because waiting on egress for hours is normal and the deadline check owns that state; and every terminal state.
- One sentence: JobRunr guarantees a job that exists will run; the reconciler guarantees a batch row waiting for a job will get one.

### Point-in-time (stale) read

- Reading a database as of a fixed past timestamp so every row reflects one instant. Spanner supports it (`TimestampBound`) within its version retention window. The egress design asks the data-access layer for a cursor list with an optional `asOf` read, which would make the frozen list one instant; until then it is "as of the reading window".

### Export batch id and echo

- `<tenantId>-<vendor>-<yyyy-MM>-<seq>`, minted once per period. The vendor returns it in the inbound filename and on every row; that is how a returned finding attaches to the request that caused it.
- Two counters sit near the batch id and are easy to confuse:
  - `seq` is the last leg of the batch id (`…-2026-10-002`). It counts how many batches exist for the period. The vendor sees it. Only supersede increments it.
  - `attempt` is a field on the batch row and appears only in the egress correlation id (`…-2026-10-001-r2`). It counts how many times we tried to produce one batch. Only egress and we see it. Only retry increments it.
- Why they differ is explained under *Design rationale*.

### Selection counts and reconciliation

- Egress reports how many practitioners it selected (`selection.distinct`), how many of those have no NPI (`unmatched`, exported but absent from the list-used file), and the list-used file location. This module copies those onto the batch row.
- After the file lands, the finish job compares the file's `totalRecords` (object metadata) with `selection.distinct`. Equal: delivered. Different: delivered with `reconciliation.match = false` and an alert; an operator diffs the file against the list-used file.
- No per-NPI rows exist in this module. The vendor echoes `certify_practitioner_id` and `certify_location_id` on every row, which is what module 2 keys on.

### Kill switch

- A stored value that stops new work without a deployment. This service has two, at two scopes:
  - `enabled = false` on a schedule row (set by `disable`, with a reason) — the tick skips this tenant and vendor; the row is kept for history; a later `enable` turns it back on, with `catchUp` choosing whether the most recent missed period runs.
  - `enabled = false` in the deployment configuration (`DIRECTORY_ACCURACY_ENABLED`) — the whole service stops: the tick inserts nothing, jobs exit as no-ops, events are acknowledged and ignored.
- Why a temporary hold uses the same flag as turning a tenant off, and why on and off are their own calls, is explained under *Design rationale*.

---

# Supplementary material

## 1. Baseline — verified current behavior

Rows 1–25 verified 2026-09-11 on local checkouts under `pdm-dal-full/`; rows 28–31 added 2026-10-21. Line numbers drift; symbol names are the durable pointer.

| # | Claim | Evidence |
| --- | --- | --- |
| 1 | Egress export request already accepts an NPI list, a template id, and a caller-supplied correlation id | `core-dal-egress-practitioner-async/src/main/java/com/certifyos/dal/egress/practitioner/model/ExportRequest.java:11-34` — `type`, `tenantId`, `correlationId`, `templateId`, `mappingsCsvUrl`, `outputFormat`, `npiFilter`, `tinFilter`, `userId` |
| 2 | Repeat `correlationId` returns the existing job (409) | `resource/EgressExportResource.java:383-384`; `openapi.yaml:235-239` |
| 3 | Status endpoint exposes `phase`, `gcs_uri`, `gcs_complete` | `EgressExportResource.java:527-528`; `openapi.yaml:253-320, 810-833` |
| 4 | Completion signals are email, audit event, GCS marker — no Pub/Sub publish | `notification/ExportNotificationService.java:131-182` (SendGrid); `dataflow/TemplateExportApp.java:226` (`TEMPLATE_EXPORT_COMPLETED`); grep for publisher or topic in `dal/egress` returns none |
| 5 | Egress output goes to its own bucket under `{type}/{tenant}/{correlation}/` | `application.properties:244` (`egress.gcs-bucket`); `openapi.yaml:311` |
| 6 | Egress reads each practitioner over DAL REST at process time | `service/PractitionerEgressService.java:48,88` (`getPractitionerOperationalValue`, `getPractitionerRelatedEntities`) |
| 7 | No point-in-time read anywhere | grep `TimestampBound|ofReadTimestamp|ofExactStaleness` across `pdm-dal-full` (excluding tests) — zero hits |
| 8 | Per-job Spanner table with 7-day TTL holds one JSON per entity | `docs/tenant_egress_export_spanner→gcs_design.md` §4.1; `openapi.yaml:216-233` (`ttl_days: 7`) |
| 9 | Row expansion `locations` = `groupMemberships` → `groupPractitionerLocations`, hierarchical | `mapping/ExpansionKeyResolver.java:51-52`; `mapping/RowExpander.java:1-20` |
| 10 | Blank path plus `default_value` yields a constant column | `mapping/TemplateFieldExtractor.java:373-388` (blank path → null), `:1203-1208` (`valueOrDefault`) |
| 11 | `comma_separated` joins with a hard-coded `", "` | `TemplateFieldExtractor.java:705-721` (`StringJoiner(", ")`) |
| 12 | Egress injects `_certify_id` into each record before mapping — the hook for per-job values | `dataflow/TemplateMappingExportPipeline.java:499, 975` |
| 13 | Template path runs the template job in place of the NDJSON job | `service/DataflowJobLauncherService.java:602-604` |
| 14 | Template pipeline is tenant-gated; rate limiting is off by default | `application.properties:315-316`, `:267`; `service/ExportOrchestrationService.java:190-215` |
| 15 | Seed concurrency 20 batches | `application.properties:279` |
| 16 | Egress authenticates through IAP and checks the tenant header | `filter/IapAuthenticationFilter.java` (`X-Goog-IAP-JWT-Assertion`); `EgressExportResource.java:386-387` (`X-Forwarding-Tenant-Id`, `checkCallerTenant`). The CP-41069 design says the sign-in check is off today; tracked outside this module |
| 17 | Transformation functions available | `mapping/transformation/fn/` — `Lower`, `Proper`, `Split`, `Substitute`, `Text`, `Trim`, `Upper` |
| 18 | The core location id is `groupLocation.locationId` in the relationships JSON | `fe-api-coredal/core-data-access-layer/src/main/java/com/certifyos/dal/practitioner_operational_value/repository/CompositePractitionerOperationalValueRepository.java:887-891`; `dal/group_location/model/GroupLocation.java:18` |
| 19 | The existing tenant template maps `certify_location_id` to `locations[].locationId`, which resolves to nothing | `gs://pdm_dev_bucket/egress-templates/613fdbbc-…/c9a418b5-…/v1/mappings.csv` line 12; test export of one NPI with three locations: three rows, `certify_location_id` empty on all |
| 20 | Nested-array filters work in mappings | same mappings CSV, lines 170-188 (`groupEntityAddresses[data.addressType=mailing]`, `irs`, `billing`), values present in the test export |
| 21 | api-layer practitioner list endpoint takes named filters plus a JSON `filter` | `fe-api-coredal/api-layer/src/main/java/com/certifyos/api_layer/practitioner/resource/PractitionerResource.java:3540-3600` |
| 22 | The platform already stores allowlisted rule JSON in tenant configuration | `api-layer/.../common/generator/CrosswalkTemplateRule.java:1-60` |
| 23 | Tenant configuration entry shape | `api-layer/.../tenant_configuration/resource/TenantConfigurationResource.java:47`; `dto/TenantConfigurationItemDto.java:24,30` |
| 24 | `acceptingNewPatients` and `telemedicineAvailable` are booleans in the API contract | `schemas/api-contracts/TenantPractitionerLocationResponse.schema.json:53-55`; `TenantPractitionerGroupResponse.schema.json:36-37` |
| 25 | Live data carries string values for `acceptingNewPatients` | test export value `Accepting New`; grep across api-layer and schemas: `Accepting New` (58), `Existing Patients Only` (45), `Accepting New Patients` (1) |
| 28 | api-layer hosts the saved-filter store | `api-layer/.../saved_filter/resource/SavedFilterResource.java` (`POST /filters` authenticated, `GET /filters/{id}` with `X-Saved-Filter-Api-Key`), `config/SavedFilterSettings.java` (`enabled` default false); commit `5826766a95` (CP-44214) |
| 29 | Saved filters are checked, normalised, and de-duplicated at save | `saved_filter/rules/FilterRules.java`, `canonical/FilterNormalizer.java`, `canonical/FilterHasher.java`; the same filter saved twice returns the existing id with `200` |
| 30 | Egress's saved-filter selection is designed, not yet built | Confluence "CP-41069 - Design : Full-tenant NPI selection in one export request" (page 2282979391, In-Review): `selection` object, frozen list, slices of 10,000, `reuseSelectionFrom`, list-used file, named failure reasons |
| 31 | The DAL practitioner list drops unknown operators silently | CP-41069 design, *The filter language* and ask A2 (b); the reason the filter is checked at save, not at use |

**Consequence:** the file engine, the row grain, and the idempotent request exist today. What is missing is on the egress side: selection by saved filter, the completion event, the destination copy, two template features. This module is the schedule, the request, the completion handling, and the batch record around them.

## 2. Approaches analyzed in depth

### Criteria

| # | Criterion | Weight | Why it matters |
| --- | --- | --- | --- |
| C1 | Correct grain and identity — one row per practitioner × location, echo ids on every row | 5 | Module 2's attribution depends on it |
| C2 | Idempotent, restartable, no duplicate business effect | 5 | Non-negotiable requirement 1 |
| C3 | Durable, honest record of what was sent | 4 | The comparison baseline for the whole program |
| C4 | Minimal new code and no duplicated platform engine | 3 | Maintenance and time to pilot |
| C5 | Ownership boundaries — vendor knowledge stays in this service, query semantics stay with the data owner | 3 | v2 D2-11 (vendor knowledge in this service); the egress team owns a generic engine |
| C6 | Operational load at monthly cadence | 2 | Alerts, retries, stuck work |
| C7 | Cost | 1 | Everything here is small |

### A. Egress resolves the filter, builds and places the file; this service schedules, requests, and confirms (chosen)

- **How:** module doc *Approach* steps 0–8.
- **Scores:** C1 5 (proven by the test export once the location path is fixed) · C2 5 · C3 4 (the placed file plus egress's counts and list-used file; no copy of our own) · C4 5 · C5 5 (filter semantics in api-layer and egress, vendor contract here) · C6 4 · C7 5.
- **Failure modes:** egress outage delays a monthly file by hours; a lost event is caught by the deadline check; a count mismatch is reconciled and alerted.
- **Cost:** derived in the module doc's *Performance and scale*.

### B. This service builds the file from api-layer reads

- **How:** page through the list endpoint, read relationships per practitioner, expand and map in code, stream the CSV to GCS.
- **Scores:** C1 4 (we control it, but write it from scratch) · C2 4 · C3 5 · C4 1 · C5 5 · C6 3 (70,000 HTTP reads in our job) · C7 4.
- **Why it loses:** C4. Egress's mapping engine, expansion, and streaming already exist and are exercised by every tenant export.

### C. Egress places the file by tenant configuration

- **How:** egress's existing connector (CP-37875) finds the target in the tenant's configuration, names the file itself, and writes a manifest beside it.
- **Scores:** C1 5 · C2 4 · C3 4 · C4 5 · C5 1 · C6 4 · C7 5.
- **Why it loses:** C5. Vendor naming and folder rules move into a generic engine and a store it does not own. The chosen design reuses the same copy code with the destination named on the request (egress ask 6).

### D. Completion by polling the status endpoint

- **How:** a recurring job calls egress's status endpoint until the export finishes.
- **Scores:** C1 5 · C2 5 · C3 5 · C4 5 (exists today) · C5 4 · C6 3 (calls with no natural end) · C7 5.
- **Why it loses:** latency or call volume, whichever way it is tuned. The chosen design is the egress event (ask 5) with one deadline check as the fallback; the status call happens only when the event did not arrive.

### E. Pre-export value snapshot in this service

- **How:** store field values at selection time in a `vendor_export_rows` collection.
- **Scores:** C1 4 · C2 4 · C3 1 (two reads, hours apart — the snapshot diverges from the file for non-finding reasons) · C4 3 · C5 4 · C6 3 · C7 3 (PII duplication under 7-year retention).
- **Why it loses:** C3. The placed file is exact; the snapshot is not.

### F. Point-in-time read

- **How:** a Spanner read timestamp for the whole export.
- **Scores:** C1 5 · C2 5 · C3 5 · C4 1 (DAL REST has no as-of; egress would need direct Spanner reads; version retention must be raised) · C5 3 · C6 3 · C7 4.
- **Why it loses:** C4 for no consumer need. Each row is self-consistent; rows minutes apart are acceptable. The egress design's `asOf` cursor list would give the frozen list one instant if the data-access layer builds it.

### G. Roster pipeline

- Disqualified on shape; see `roster-pipeline-reuse-analysis.md`. No outbound file builder; inbound-only runtime.

### H. A standalone export service

- **How:** a separate deployable and database for export only.
- **Scores:** C1 5 · C2 5 · C3 5 · C4 2 (a second service and database, a cross-service handoff to ingestion) · C5 5 · C6 2 · C7 3.
- **Why it loses:** the export batch row, the vendor bucket, the push endpoints, and the audit collection are shared with module 2. Splitting them adds a network boundary inside one workflow. v2 D2-38 (one service for the program) fixes one service: the directory accuracy service.

### I. Runtime — GCE managed instance groups (chosen) versus Cloud Run

The runtime is decided for the whole directory accuracy service, not for export alone: one service, one repository, and one database for both phases, so whatever runs the export jobs also runs the ingestion jobs and the review API.

**The combined workload**

| Phase | Work | Shape |
| --- | --- | --- |
| Export | Tick, one egress call, event handler, deadline check, finish job | Small, bursty, minutes a month, no user traffic |
| Ingestion | Read the parsed rows from file-ingestion (up to a million per delivery), stage them, release approved fields | One long-running job per vendor delivery, tens of minutes to an hour, heavy Mongo writes |
| Review API | Approve, reject, skip; list and filter staged fields; status reads | Real user traffic from the PDM UI during review windows |

Everything long-running is a JobRunr job, not an HTTP request, in both phases. Both runtimes can run it; the comparison is about operations and cost.

**Comparison**

| Concern | Cloud Run: `api` service (request-serving) plus `worker` service (min 1, CPU always allocated, hosts JobRunr) | GCE: `api` MIG plus `worker` MIG (hosts JobRunr) |
| --- | --- | --- |
| Long-running jobs | A JobRunr job is not a request, so the 60-minute request timeout does not apply | Same; no platform ceiling to explain |
| Interruption of a running job | SIGTERM on deploy or scale-down; with min = max = 1 only deploys interrupt; jobs must be resumable | A rolling MIG update replaces the VM; same need for resumable jobs; update policy controls timing |
| Machine size for the ingestion job | Up to 8 vCPU and 32 GiB per instance | Any machine type |
| Pub/Sub delivery of the egress completion event | Push to a Cloud Run endpoint with IAM authentication built in | Push through the load balancer to the api group, token checked by the handler (chosen) |
| Inbound auth for operator and review endpoints | Cloud Run IAM invoker plus the application's JWT check | External HTTPS load balancer plus the application's JWT check |
| Deploy and rollback | Image push, revisions, seconds to roll back | Rolling MIG update, minutes to roll back |
| OS patching, agents, disk | None | Ours, reduced by container-optimised OS on the MIG |
| Scale to zero | `api` yes; `worker` no while it hosts JobRunr | Neither |
| Consistency with file-ingestion | Different runtime and runbook | Same topology, runbook, and on-call knowledge |

**Pros of GCE for this service**

- One operational model with file-ingestion, which the same team runs.
- Full control over the long-running worker: no SIGTERM on scale-down, no per-request timeout anywhere, MIG update policy decides when a VM is replaced.
- Any machine size, and committed-use discounts of roughly a third.
- Predictable cost: fixed VMs, no per-second billing surprises during a review-window spike.

**Cons of GCE for this service**

- An external HTTPS load balancer and an identity layer in front of the API, configured and maintained by us.
- VM lifecycle is ours: image or startup script, patching, disks, health checks, autoscaler settings.
- Egress networking is a decision: external IPs per VM, or Cloud NAT.
- Two always-on planes from day one; the `api` MIG is billed whether or not anyone calls it.
- Slower deploys and rollbacks than Cloud Run revisions.

**Cost, GCP list prices, us-central1, on-demand, sized for both phases**

| GCE item | Sizing | Derivation | Monthly |
| --- | --- | --- | --- |
| `api` MIG | 2 × e2-small (2 shared vCPU, 2 GB) | 2 × $0.01675 per hour × 730 hours | $24.46 |
| `worker` MIG | 1 × e2-standard-2 (2 vCPU, 8 GB) | $0.06701 per hour × 730 hours | $48.92 |
| Persistent disks | 3 × 20 GB balanced | 60 GB × $0.10 per GB | $6.00 |
| External HTTPS load balancer | 1 forwarding rule | $0.025 per hour × 730 hours | $18.25 |
| Egress, external IPs | 3 IPs | 3 × $0.004 per hour × 730 hours | $8.76 |
| Egress, Cloud NAT instead | 1 gateway | $0.044 per hour × 730 hours plus data | $32.12 |
| **Total** | | external IPs / NAT | **≈ $106 / ≈ $130** |
| With a one-year committed-use discount on the VMs (≈ 37 percent) | | | **≈ $79 / ≈ $103** |

| Cloud Run item | Sizing | Derivation | Monthly |
| --- | --- | --- | --- |
| `worker` service, always on | 2 vCPU, 4 GiB, min 1 | ($0.000018 × 2 + $0.000002 × 4) × 2,592,000 seconds | $114.05 |
| `api` service, request based | tens of thousands of requests | under free tier plus small overage | ≈ $5 |
| Load balancer, NAT, disks | none needed | | $0 |
| **Total** | | | **≈ $119** |

**Reading the numbers:** at phase 2 size GCE is cheaper by roughly $15 a month on-demand and roughly $40 with a commitment. Cost is not the deciding factor.

**Why GCE is chosen:** one operational model for the two services the team runs, no platform ceiling on the long-running worker, cost parity or better at phase 2 size, and consistency with the platform direction recorded for file-ingestion ("avoid Cloud Run and use GCE").

### J. How the practitioners are selected

| | J1. Own paged selection, NPIs in the request (the earlier design) | J2. Saved filter id resolved by egress (chosen) | J3. Raise egress's 100-NPI limit, or upload an NPI CSV |
| --- | --- | --- | --- |
| How | `SelectJob` pages api-layer with criteria stored on the schedule, records every NPI in `vendor_export_npis`, sends the list as `npiFilter` | The schedule stores a saved filter id; egress fetches the filter, freezes the list, reports counts | We still select; egress takes a large list in the body or as a file |
| Who checks the filter | Our own schema, a copy of the allowed list that drifts | api-layer, once, at save | Our own schema |
| Evaluations of one population | Two: ours, then egress's NPI query, which counts records and can disagree | One | Two |
| Work in this module | Up to 700 paged calls per large tenant, a per-NPI collection, resumable paging | One field on the schedule, one object in the request | As J1, plus a large request or upload |
| Egress change | The 100 cap lifted anyway | `selection` object (CP-41069) | Cap raised or upload path |
| Scores | C1 5 · C2 5 · C3 4 · C4 2 · C5 2 · C6 3 | C1 5 · C2 5 · C3 4 · C4 5 · C5 5 · C6 4 | C1 5 · C2 4 · C3 4 · C4 2 · C5 2 · C6 3 |

- **Why J2 wins:** one evaluation of one filter, checked where it is written, and the selection runs next to the data it reads. It is also what the egress team designed for this caller (CP-41069, ask A3 to us).
- **What J2 costs:** this module cannot read the filter, so a wrong or deleted id shows up only when the export runs (`FILTER_NOT_USABLE`); and the exact list lives 7 days in egress, so the list-used file is copied as a location, not kept.

## 3. Requirements traceability

| Requirement | Source | Covered by |
| --- | --- | --- |
| Query-selected population, own cadence, own service | v2 D2-38 (one service, own cadence) | *Approach* steps 0–2; decisions D1 (two instance groups), D5 (saved filter id), D14 (schedule in our database) |
| Transport `from/<tenant>/`, vendor read-only | v2 D2-03 (SFTP folders) | *Approach* step 3 (destination copy); *Security* |
| Internal contract proposal before the vendor call; batch identity and naming | v2 D2-06; contract proposal §3 | Decision D10 (batch id); *Contracts → step 3* (filename) |
| Vendor-agnostic naming | non-negotiable 6 | Collections and endpoints carry `vendor_export`, never a vendor name; vendor key only in configuration values and the batch id |
| Idempotency everywhere | non-negotiable 1 | Deterministic batch and correlation ids, egress `409` on repeats, create-only destination copy, compare-and-set transitions |
| Full audit, same transaction, 7 years | non-negotiable 2; v2 D2-20 (retention) | *Audit trail* |
| Correlation end to end | non-negotiable 3 | `exportBatchId` → egress `correlationId` → object metadata → vendor echo → module 2 `inboundFiles[]` |
| Tenant isolation server-side | non-negotiable 4 | *Security*: tenant from the schedule row, stamped on the batch, egress tenant header; saved filters carry no tenant |
| `certify_practitioner_id` convention | non-negotiable 5 | Contract columns `certify_practitioner_id`, `certify_location_id` |
| Quarantine over guessing; nothing silently discarded | non-negotiables 8–9 | `EMPTY` and `FAILED` with named egress reasons; count mismatch delivered but alerted |
| ISO 8601 and compact filename dates | non-negotiable 11 | Timestamps in every payload; `yyyyMMdd` in the filename |
| Selection query format and safety | v2 O-17 (selection format) | Decisions D5 and D16; *Contracts → step 2*: saved filter checked by api-layer at save |
| Echo the vendor must return | instructions §7 | Contract columns `export_batch_id`, `certify_practitioner_id`, `certify_location_id` |
| Never rewriting a placed batch; failure semantics; reconciler | instructions §7 | Decisions D11 (create-only copy) and D13 (final-failure filter, reconciler); *Approach* steps 5–8; *Failure modes* |

## 4. Inputs and outputs of this module

**Consumes:**

- api-layer: egress-template endpoints (provisioning at enablement, the template pin on each batch's first attempt).
- api-layer filter module: nothing at runtime. Operators save a practitioner filter there and put its id on the schedule; egress reads it.
- Egress service: `POST /api/v1/egress/export` with `selection`, `destination`, and the pinned template values; the completion event on its topic; `GET /api/v1/egress/status/…` at the deadline only; `POST …/cancel` before a retry.
- Platform team: the vendor bucket and its `from/<tenantId>/` prefix; IAM grants.

**Provides:**

- To the vendor: the CSV under the contract filename in `from/<tenantId>/`, written create-only by egress.
- To module 2: `vendor_export_batches`, the batch the vendor's echoed batch id must match; module 2 appends `inboundFiles[]` and writes `EXPORT_BATCH_ACKNOWLEDGED` on the first claim.
- To the PDM reviewer UI: `GET /v1/vendor-exports` and `GET /v1/vendor-exports/{id}` for export status per tenant, including egress's selection counts.
- To operators: `retry`, `supersede`, `run-now`, a manual tick, schedule `enable` and `disable`.

## 5. Design rationale — anticipated questions

- **Why not just store what we read when we select? Then we know what we sent.** We do not read field values at all; egress reads them when it builds the file. Two reads, minutes to hours apart, cannot be guaranteed equal, so a stored snapshot would sometimes disagree with the file for reasons that are not findings. The file is what left.
- **Then how does module 2 compare values?** The vendor echoes the batch id and our ids on every row, with its finding per field. Module 2 archives the inbound file and reads the live operational value for "now". Nothing needs a third copy.
- **Why is the egress correlation id different from the export batch id?** Egress refuses to run the same correlation id twice — a retry after a failed egress job needs a new one. The vendor-facing batch id must stay the same for the period. So the egress id is the batch id plus an attempt suffix.
- **Why an event plus a deadline check, and not polling?** A poll has no natural end and costs calls on every batch. The event (egress ask 5) arrives in seconds; one deadline check at 6 hours, and a second at 48, catch the case where the event never comes. The normal path never calls the status endpoint.
- **Why does the event need `failureReason` and the `selection` block?** Without a reason code, "the filter matched nobody" cannot be told from a crash, and the batch cannot become `EMPTY` instead of `FAILED`. Without the selection counts, the finish job has nothing to compare the file with. The status endpoint already returns both; the event carrying them keeps the "never poll" property.
- **What if egress drops a practitioner?** The finish job compares the file's record count with egress's selected count. A difference is delivered with `match = false` and alerted; the operator diffs the file against the list-used file.
- **Why a saved filter id and not criteria on the schedule?** The data-access layer drops an unknown field or operator silently and then matches the whole tenant, so a filter must be checked before it is ever used. api-layer checks it once, at save, and egress evaluates it once, next to the data. Our own criteria would mean a second copy of the allowed list that drifts, and a second evaluation that can disagree with egress's.
- **This service cannot read the filter. Is that a problem?** Only for early feedback: a wrong or deleted id fails the first export as `FILTER_NOT_USABLE`, and a filter that matches nobody ends `EMPTY`. The preview endpoint went away with the criteria; whoever saves the filter checks its count there.
- **Why not egress's own template schedule?** It would run the export without our batch id, our destination, and our batch record. Egress stays the engine; the schedule is ours.
- **Why not a point-in-time snapshot like large data platforms do?** The platform has no as-of read path; egress reads over DAL REST. Each row is self-consistent; a monthly accuracy program tolerates rows minutes apart.
- **Is the repository name settled?** Yes. It was `vendor-exchange-service`; it is `directory-accuracy-service` since 2026-10-21 (PR #25), with the instance groups, service accounts, and database renamed to match. Module names (`vendor_export_*`, `vendor-export:*`, `/v1/vendor-exports`) did not change.
- **How do large platforms handle "each tenant has its own selection rule, and it changes"?** Four patterns, all sharing one principle — the owner of the data owns the query semantics, and the consumer never re-implements evaluation:
  - Inline declarative criteria the data owner validates and runs (Kubernetes label selectors, LaunchDarkly and Flagsmith segment rules, Elasticsearch query DSL). This was the earlier pilot design.
  - A reference to a saved query the data owner stores (Salesforce list views and reports on scheduled jobs, Jira filters on subscriptions, Splunk saved searches). **This is the design now:** the schedule holds a saved filter id.
  - A reference to a named segment with count and member endpoints (Braze segments, Segment Audiences, HubSpot active lists).
  - Materialised membership — a marker on the entity itself (Salesforce campaign membership, Kubernetes labels). For us a user-defined field on the practitioner, which a saved filter can select through `data.userDefinedFields.<key>`; a good fit for tenants whose directory population is a curated list rather than a rule.
- **What is still missing from the industry-standard picture?** A published field catalog — `GET /practitioners/filterable-fields` returning filterable fields, types, and operators — so the filter module and the PDM UI build their controls from one list. Asked of the data-access layer in the egress design (ask A2 (d)); not needed for the pilot.
- **Why is there no separate pause for a temporary hold? An incident and a churned tenant both stop the export.** One flag covers both. The row is never deleted: `disable` sets `enabled = false` with a mandatory reason, and `enable` turns it back on. The difference between the two cases is carried by the reason and by the choice made on the way back, not by a second state.

  | | Tenant off the program | Temporary hold during an incident |
  | --- | --- | --- |
  | Who does it | Product or account management: contract ended, tenant churned, pilot over | Operations: vendor outage, a data problem under investigation, a template being fixed |
  | Call | `disable` with a reason such as "contract ended" | `disable` with a reason such as "roster load incident" |
  | Coming back | `enable` with `catchUp: false`: `nextDueAt` moves to the next future occurrence; no backfill | `enable` with `catchUp: true`: the most recent missed period runs at the next tick |
  | Audit | `SCHEDULE_DISABLED`, then `SCHEDULE_ENABLED` with `catchUp` | Same |

  - **Holding a tenant during an incident, concretely.** On 28 September a roster load for `org-xyz` goes wrong and half the addresses are stale; the fix takes a week. If the 1 October export runs, the vendor verifies data we already know is wrong. An operator calls `disable` with the reason "roster load incident". The 1 October tick skips `org-xyz`. Nothing left the building. While disabled, the operator can still change the schedule with `PUT` (for example point it at a different filter id); that never turns the export back on.
  - **Coming back.** The fix lands on 9 October. The operator calls `enable` with `catchUp: true`. `nextDueAt` is set to 1 October, the most recent occurrence since the disable; the next tick creates the October batch on the 10th and moves `nextDueAt` to 1 November: the vendor gets October nine days late instead of never. Only one period runs, however long the hold lasted. With `catchUp: false`, October is skipped and `nextDueAt` jumps to 1 November.
  - **What this gives up.** The system cannot tell a hold that someone forgot to lift from a tenant that left on purpose, so there is no alert for a forgotten hold. The reason is visible on the schedule read and in the PDM UI, which is the check. At pilot scale this is accepted.
  - **If forgotten holds become a problem:** replace `enabled` with `status: ACTIVE | PAUSED | DISABLED`, add `pause` and `resume`, and alert when a `PAUSED` schedule's `nextDueAt` is more than 35 days in the past.
- **Why `PUT` to create a schedule, and `POST` actions to turn it on and off?**
  - **`PUT` for create and replace.** A schedule's id is the tenant and vendor, which the caller already knows, so the caller addresses it directly: `PUT /v1/vendor-exports/schedules/{tenantId}/{vendor}`. Omitting `version` means "create only" (`201`, or `409` if one exists), so a second create can never overwrite someone's schedule; sending `version` means "replace" (`200`, or `409` if stale). A call retried after a timeout is therefore safe.
  - **`PUT` never changes `enabled`.** Changing settings and switching the export on are different decisions. If `PUT` re-enabled, fixing a template during an incident hold would restart the export before the incident was closed.
  - **`POST …/disable` and `POST …/enable`, not `DELETE`.** `DELETE` means the resource is gone and a later `GET` returns `404`; here the schedule stays readable and editable. `DELETE` bodies have no defined meaning and some proxies drop them, which breaks a mandatory reason. The reason and `catchUp` belong to the action, so they are action calls like `run-now`.
- **Why does the batch move to `EGRESS_COMPLETED` when the event arrives, and not only when delivery finishes?** Because two safety nets read the batch state and act on it without asking anyone, so the state must be true at every moment.
  - **Two moments, not one.** Egress finishes at 07:11 and publishes; our handler hears it at 07:11:05. Our finish job then reads the object's metadata, compares counts, and marks the batch delivered: 07:11:20 on a good day, later if the metadata read is retrying. `EGRESS_COMPLETED` is the honest note for the gap: egress is done, it is our turn, delivery not confirmed yet.
  - **Example, the deadline check.** The batch was requested at 06:01; the deadline check is due at 12:01. With the state written at 07:11, the check reads `EGRESS_COMPLETED` and exits. Without it, the check calls egress, gets `COMPLETED`, and enqueues a second finish job, with an `EXPORT_EVENT_MISSED` in the audit trail for a batch whose event actually arrived.
  - **Example, the reconciler.** The handler writes `EGRESS_COMPLETED` at 07:11:05 and the VM dies before it enqueues the finish job. At 08:00 the reconciler finds the batch untouched for 49 minutes and enqueues `FinishJob`. Without the state, the row still says `EGRESS_REQUESTED`, which may sit for hours, so nothing happens until the 12:01 deadline check.
- **What does the reconciler actually do?** It is a janitor that walks the floor once an hour looking for work that got dropped. Once an hour it runs one query for batches in `SCHEDULED` or `EGRESS_COMPLETED` untouched for 30 minutes, enqueues the job that state calls for, and fires alert E2. It never touches `EGRESS_REQUESTED`, where waiting for hours is normal and the deadline check is in charge, nor any terminal state.
  - **Example.** 06:00:12 the tick inserts a batch in `SCHEDULED` and the VM dies before enqueueing `RequestEgressJob`. 07:00 the reconciler finds it, enqueues `RequestEgressJob`, fires E2. 07:00:15 the job checks the state is still `SCHEDULED`, pins the template, and asks egress. One hour late instead of lost.
- **Why are retry and supersede two endpoints? Both add one to a counter.** The counters live in different places, are seen by different parties, and must behave in opposite ways on the one thing the vendor sees.

  | | `attempt` (retry) | `seq` (supersede) |
  | --- | --- | --- |
  | Where | A field on the batch row; appears only in the egress correlation id, `org-xyz-candor-2026-10-001-r2` | The last leg of the batch id itself, `org-xyz-candor-2026-10-002` |
  | Who sees it | Only egress and us | The vendor, in the filename and on every row |
  | What it counts | How many times we tried to produce this one batch | How many batches exist for this period |
  | Changes the batch id | No | Yes; it is a new batch |

  - **Retry answers "the file never reached the vendor, try again under the same name".** The batch is in `FAILED`; nothing was placed; the vendor has seen nothing. Egress refuses a correlation id it has already seen, so we send `-r2`. The batch id stays `001`. The retry cancels the previous attempt first and asks egress to reuse that attempt's frozen list (`reuseSelectionFrom`), so the practitioner set does not change; if that list is gone (over 7 days or never finished), the filter is evaluated again and the batch records it.
  - **Supersede answers "the vendor already has a file, and it was wrong".** The batch is in `DELIVERED`; the vendor may already be working from the file and may echo `001` back. We cannot overwrite it. So a new batch `002` is created and egress evaluates the filter afresh, because what was wrong may include who was selected. The old batch becomes `SUPERSEDED` and its file stays.
  - **One line:** retry gives egress a new correlation id and keeps the vendor's batch id; supersede gives the vendor a new batch id because the old one is already in their hands.

## 6. Open questions and sign-offs

| # | Question | Owner | Blocks |
| --- | --- | --- | --- |
| Q1 | `Y`/`N` rendering for `acceptingNewPatients` (mixed `Accepting New`, `Existing Patients Only`, booleans) and `telemedicineAvailable` — egress `map` transformation, or the contract accepts raw values | Engineering with the egress team; Product for the value meaning | Template |
| Q2 | Source fields for `practitioner_phone` and `telehealth_url` | Product | Template |
| Q3 | `group_affiliation`: the group of this row's location, or every group `;`-joined | Product; contract call | Template |
| Q4 | Practitioner with zero practice locations: excluded by design here; confirm the vendor does not want practitioner-only rows | Product | Selection rules |
| Q5 | Location-level rules (only active locations, location state): egress exports every location of every selected practitioner. Needs location fields in the filter language (egress design ask A2) | Engineering with the data-access layer and egress teams | File content |
| Q6 | Egress template features: per-run correlation id and generation time as record fields, a configurable list joiner (egress asks 2 and 3) | Egress team | Template |
| Q7 | Outbound manifest: does the vendor want one beside our file | Contract call (v2 O-1, the vendor contract call) | Destination contents |
| Q8 | Response window in days for an overdue vendor response | Contract call (v2 O-1) | Alert threshold |
| Q9 | Egress end-to-end time for a 70,000-practitioner selection export — the egress design estimates 1.7–2.8 hours; measure in staging | Engineering | `egressDeadlineHours` default |
| Q10 | **Answered 2026-10-21:** the repository is `directory-accuracy-service`; module names stay | — | — |
| Q11 | Per-deployable service accounts versus one identity for the ingestion module | Engineering | *Security* |
| Q12 | **Answered 2026-10-21:** the allowed filter fields are api-layer's save-time rules; widening them is the data-access layer's ask A2 (c) and (e) in the egress design | — | — |
| Q13 | Egress adds `failureReason` and the `selection` block to the completion event (extends ask 5, CP-41070); not yet raised with the egress team | Egress team | Telling `EMPTY` from `FAILED`; reconciliation without a status call |
| Q14 | Whether NPIs skipped by a reviewer sit out later cadences; if so, the exclusion has to live in the saved filter or in egress, since this module no longer selects | Product | Selection rules |

## 7. Ticket status

| Ticket | State |
| --- | --- |
| CP-39602 (phase 1 export) | Closed with its subtasks CP-39603, CP-39604, CP-39605, CP-40438. The code implements the earlier own-selection design |
| CP-40437 (phase 2 ingestion) | Merged 2026-10-21 (PRs #15–#22) |
| New ticket needed | Move the code to the saved filter flow: drop `SelectJob`, `vendor_export_npis`, and the preview endpoint; schedule `filterId`; request `selection` with `listUsed`; `reuseSelectionFrom` on retry; record egress counts from the event; `EMPTY` on `NO_MATCHING_PRACTITIONERS`; reconcile against egress's count. Blocked by CP-41069 and Q13 |
| CP-41069 (egress saved-filter selection) | In-Review |
| CP-41070 (egress completion event, email suppression) | In Progress; Q13 to be added |
| CP-44214 (api-layer saved-filter store) | Built, switched off by default |
| Repository rename | Done 2026-10-21 (PR #25, GitHub repository renamed); new names go to DevOps for the Pulumi stack |
