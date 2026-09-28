# Companion — Vendor export module (concepts + supplementary material)

> Two parts. **Part 1 — Concepts:** plain-language explanations of every service, tool, and concept named in `vendor-export.md`, for a reader new to the stack. **Part 2 — Supplementary material:** the analysis behind the module doc — baseline evidence with receipts, approaches scored against criteria, traceability, inputs and outputs, anticipated questions, open questions with owners, ticket impact. The module doc stands alone; this file is preparation and defense material.

## Part 1 — Concepts

### The egress service (`core-dal-egress-practitioner-async`)

- A Quarkus service plus Cloud Run Jobs that exports practitioner, facility, or group data for a tenant as a file.
- Flow: `POST /api/v1/egress/export` creates a per-job Spanner table; a seed job reads entity ids from the DAL (optionally filtered by NPI or TIN); a processor reads each entity over DAL REST and stores its full JSON in the table's `data` column; an export job writes the file to GCS.
- Two file paths: NDJSON (one JSON per entity) or, when a template id is given, a template-driven CSV. The launcher picks one; both are not produced for the same job.
- Status is a `GET` endpoint with a `phase` enum; completion also sends an email to the initiating user and sets a `complete=true` marker on the GCS object. No Pub/Sub, no webhook.
- The per-job table has a 7-day TTL; the GCS file stays.

### Egress template

- A row in the DAL's `egress_templates` table: entity type, output format, separator, `rowExpansionKeys`, a GCS URL to a mappings CSV, and an optional schedule.
- Created and listed through api-layer's `/api/v1/egress-templates` endpoints; the mappings CSV is uploaded to a tenant-scoped GCS path.

### Mappings CSV

- One line per output column: `output_column`, `attribute_path`, `json_extraction_path`, `multi_value_handling`, `max_columns`, `column_order`, `entity_group`, `default_value`, plus optional `data_transformations`, `dedupe_by`, `fallback_json_extraction_path`.
- `json_extraction_path` is a dotted path into the entity JSON. `[]` on an expansion array means "the element for this row"; `[key=value]` filters an array; `[n]` is a literal index.
- `multi_value_handling`: blank = one value; `columns` = spread across `_1.._N`; `comma_separated` = join with `", "`.
- `default_value` fills a blank result. A blank path with a default is how a constant column is made.
- `data_transformations` functions available today: `lower`, `upper`, `proper`, `trim`, `split`, `substitute`, `text`.

### Row expansion (`rowExpansionKeys`)

- Tells egress which arrays multiply rows. `locations` is an alias for the chain `groupMemberships` then `groupPractitionerLocations`.
- Expansion is hierarchical: locations are counted inside each group, so a practitioner in two groups with two and one locations yields three rows, not four.

### Practitioner relationships JSON

- The document egress stores per practitioner: the operational value fields at the top (`npi`, `firstName`, `languages[]`, …) plus `groupMemberships[]`, each with `tenantGroup.group.data` (group npi, name, tin) and `groupPractitionerLocations[]`.
- Each location row carries `data` (phone, fax, acceptingNewPatients, website), `groupLocation.locationId` (the core location id), `groupLocation.location.data` (name, handicapAccessible, office hours), and `locationEntityAddresses[]` with `data.addressType` and `address.data`.

### Cloud Scheduler, Cloud Tasks, Cloud Run, Cloud Run Jobs

- Cloud Scheduler: managed cron that calls a URL on a schedule; retries; carries no logic. Three jobs free, then $0.10 per job per month.
- Cloud Tasks: a managed work queue of HTTP calls with retries and backoff. A named task cannot be created twice — the duplicate shield. No dead-letter queue: an exhausted task is deleted, so a durable row plus a sweep must cover it.
- Cloud Run: managed containers behind HTTPS, scale to zero, request deadline up to 60 minutes.
- Cloud Run Job: the same image run to completion without a request deadline; how egress runs seed and export, and how module 2 runs the parse.

### GCE managed instance group (MIG)

- A group of identical Compute Engine virtual machines created from one instance template; the group keeps a target number of VMs running, replaces unhealthy ones, and rolls out a new template VM by VM.
- Two groups is the usual split for a service with an HTTP side and a background side: an `api` MIG behind an HTTPS load balancer, and a `worker` MIG with no inbound traffic that runs the job server.
- A MIG never scales to zero; every VM in it is billed whether or not it is busy. Committed-use discounts (a one- or three-year commitment) cut the VM price by roughly a third or more.
- Inbound HTTPS needs an external load balancer and an identity check in front of it (IAP or the application's own token verification); Cloud Run provides both without configuration.
- Egress to the internet from a VM needs either an external IP per VM or a Cloud NAT gateway for the subnet.
- The file-ingestion service runs on this shape: an `api` MIG for the request side and a `worker` MIG for JobRunr.

### GCS server-side copy (rewrite)

- Copying an object between buckets without downloading it; one API call regardless of size; the destination object's hash is computed by GCS.
- Used here for both the archive copy and the SFTP placement.

### The shared SFTP platform

- DevOps-operated SFTP servers mounted on a GCS bucket per vendor (gcsfuse). Each vendor has an account; `from/<tenantId>/` is what CertifyOS drops (vendor read-only), `to/<tenantId>/` is what the vendor drops.
- Every upload becomes one GCS object; object-finalize events on the bucket feed module 2's inbound path.

### Tenant configuration (`tenant_configurations`)

- A platform table read through api-layer's `/tenant-configurations` endpoints: one entry per tenant per configuration type, with a JSON `configuration` body.
- Configuration types are strings like `credentialing-config`, `monitoring-config`; this module adds `directory-accuracy-config`.

### Structured filter (allowlisted rule grammar)

- A JSON object of conditions — `field`, `equals`, `notEquals`, `in`, `exists`, grouped by `all` (AND) and `any` (OR) — validated against a schema.
- It is data, not code: the service maps each condition to a typed query parameter. Nothing is concatenated into SQL. The platform already stores rules of this shape for crosswalk generation.

### Idempotency

- Running an operation twice produces the effect once. Here: deterministic ids (batch id, membership id, task names) plus database uniqueness, never application memory.

### Compare-and-set state transition

- An update that succeeds only if the row is still in the expected state (`WHERE state = 'X'`). Two workers racing for one batch: one wins, the other sees zero rows updated and exits.

### Reconciler

- A JobRunr recurring job on the worker group that runs once an hour and looks for batches whose next job was never enqueued.
- The gap it closes: every step writes the batch row and then enqueues the next job, two writes to two collections with no shared transaction. A crash between them leaves a row that says "ready for step X" with no job for step X. JobRunr cannot help, because there is no job to retry.
- What it does: one indexed query for batches in `SCHEDULED`, `NPIS_SELECTED`, or `EGRESS_COMPLETED` not updated for more than `reconcilerStaleMinutes` (30); for each, enqueue the job that state calls for (`SelectJob`, `RequestEgressJob`, `FinishJob`); fire alert E2.
- Why it is safe to be generous: every job checks the row state first, so a duplicate enqueue finds the state moved on and exits.
- What it leaves alone: `EGRESS_REQUESTED`, because waiting on egress for hours is normal and the deadline check owns that state; and every terminal state.
- One sentence: JobRunr guarantees a job that exists will run; the reconciler guarantees a batch row waiting for a job will get one.

### Point-in-time (stale) read

- Reading a database as of a fixed past timestamp so every row reflects one instant. Spanner supports it (`TimestampBound`) within its version retention window (default 1 hour, max 7 days). Not used anywhere in the platform; DAL REST has no as-of parameter.

### Export batch id and echo

- `<tenantId>-<vendor>-<yyyy-MM>-<seq>`, minted once per period. The vendor returns it in the inbound filename and on every row; that is how a returned finding attaches to the request that caused it.
- Two counters sit near the batch id and are easy to confuse:
  - `seq` is the last leg of the batch id (`…-2026-10-002`). It counts how many batches exist for the period. The vendor sees it. Only supersede increments it.
  - `attempt` is a field on the batch row and appears only in the egress correlation id (`…-2026-10-001-r2`). It counts how many times we tried to produce one batch. Only egress and we see it. Only retry increments it.
- Why they differ is explained under *Design rationale*.

### Membership

- One row per practitioner × practice location × group we sent, keyed by ids only. Module 2 checks every returned row's echoed ids against it.

### Kill switch

- A stored value that stops new work without a deployment. This service has two, at two scopes:
  - `enabled = false` on a schedule row (set by `disable`, with a reason) — the tick skips this tenant and vendor; the row is kept for history; a later `enable` turns it back on, with `catchUp` choosing whether the most recent missed period runs.
  - `enabled = false` in the deployment configuration — the whole service stops: the tick inserts nothing, jobs exit as no-ops, events are acknowledged and ignored.
- Why a temporary hold uses the same flag as turning a tenant off, and why on and off are their own calls, is explained under *Design rationale*.

---

# Supplementary material

## 1. Baseline — verified current behavior

Verified 2026-09-11 on local checkouts under `pdm-dal-full/`. Line numbers drift; symbol names are the durable pointer.

| # | Claim | Evidence |
| --- | --- | --- |
| 1 | Egress export request already accepts an NPI list, a template id, and a caller-supplied correlation id | `core-dal-egress-practitioner-async/src/main/java/com/certifyos/dal/egress/practitioner/model/ExportRequest.java:11-34` — `type`, `tenantId`, `correlationId`, `templateId`, `mappingsCsvUrl`, `outputFormat`, `npiFilter`, `tinFilter`, `userId` |
| 2 | Repeat `correlationId` returns the existing job (409) | `resource/EgressExportResource.java:383-384`; `openapi.yaml:235-239` |
| 3 | Status endpoint exposes `phase`, `gcs_uri`, `gcs_complete` | `EgressExportResource.java:527-528`; `openapi.yaml:253-320, 810-833` |
| 4 | Completion signals are email, audit event, GCS marker — no Pub/Sub publish | `notification/ExportNotificationService.java:131-182` (SendGrid); `dataflow/TemplateExportApp.java:226` (`TEMPLATE_EXPORT_COMPLETED`); grep for publisher or topic in `dal/egress` returns none |
| 5 | Egress output goes to its own bucket under `{type}/{tenant}/{correlation}/` | `application.properties:244` (`egress.gcs-bucket`); `openapi.yaml:311` |
| 6 | Egress reads each practitioner over DAL REST at process time | `service/PractitionerEgressService.java:48,88` (`getPractitionerOperationalValue`, `getPractitionerRelatedEntities`) |
| 7 | No point-in-time read anywhere | grep `TimestampBound|ofReadTimestamp|ofExactStaleness` across `pdm-dal-full` (excluding tests) — zero hits; all Spanner reads are `singleUse()` or plain `readOnlyTransaction()` |
| 8 | Per-job Spanner table with 7-day TTL holds one JSON per entity | `docs/tenant_egress_export_spanner→gcs_design.md` §4.1; `openapi.yaml:216-233` (`ttl_days: 7`) |
| 9 | Row expansion `locations` = `groupMemberships` → `groupPractitionerLocations`, hierarchical | `mapping/ExpansionKeyResolver.java:51-52`; `mapping/RowExpander.java:1-20` |
| 10 | Blank path plus `default_value` yields a constant column | `mapping/TemplateFieldExtractor.java:373-388` (blank path → null), `:1203-1208` (`valueOrDefault`) |
| 11 | `comma_separated` joins with a hard-coded `", "` | `TemplateFieldExtractor.java:705-721` (`StringJoiner(", ")`) |
| 12 | Egress injects `_certify_id` into each record before mapping — the hook for per-job values | `dataflow/TemplateMappingExportPipeline.java:499, 975` |
| 13 | Template path runs the template job in place of the NDJSON job | `service/DataflowJobLauncherService.java:602-604` |
| 14 | Template pipeline is tenant-gated; rate limiting is off by default | `application.properties:315-316`, `:267`; `service/ExportOrchestrationService.java:190-215` |
| 15 | Seed concurrency 20 batches | `application.properties:279` |
| 16 | Egress authenticates through IAP and checks the tenant header | `filter/IapAuthenticationFilter.java` (`X-Goog-IAP-JWT-Assertion`); `EgressExportResource.java:386-387` (`X-Forwarding-Tenant-Id`, `checkCallerTenant`) |
| 17 | Transformation functions available | `mapping/transformation/fn/` — `Lower`, `Proper`, `Split`, `Substitute`, `Text`, `Trim`, `Upper` |
| 18 | The core location id is `groupLocation.locationId` in the relationships JSON | `fe-api-coredal/core-data-access-layer/src/main/java/com/certifyos/dal/practitioner_operational_value/repository/CompositePractitionerOperationalValueRepository.java:887-891`; `dal/group_location/model/GroupLocation.java:18` |
| 19 | The existing tenant template maps `certify_location_id` to `locations[].locationId`, which resolves to nothing | `gs://pdm_dev_bucket/egress-templates/613fdbbc-…/c9a418b5-…/v1/mappings.csv` line 12; test export of one NPI with three locations: three rows, `certify_location_id` empty on all |
| 20 | Nested-array filters work in mappings | same mappings CSV, lines 170-188 (`groupEntityAddresses[data.addressType=mailing]`, `irs`, `billing`), values present in the test export |
| 21 | api-layer practitioner list endpoint takes named filters plus a JSON `filter` | `fe-api-coredal/api-layer/src/main/java/com/certifyos/api_layer/practitioner/resource/PractitionerResource.java:3540-3600` (`delegationStatus`, `practitionerType`, `practitionerRoles`, `licensedStates`, `statesToCredential`, `rosterIds`, `filter`, `order`) |
| 22 | The platform already stores allowlisted rule JSON in tenant configuration | `api-layer/.../common/generator/CrosswalkTemplateRule.java:1-60` (`field`, `equals`, `notEquals`, `in`, `exists`, `all`, `any`) |
| 23 | Tenant configuration entry shape | `api-layer/.../tenant_configuration/resource/TenantConfigurationResource.java:47`; `dto/TenantConfigurationItemDto.java:24,30` (`configurationTypeId`, `configuration`) |
| 24 | `acceptingNewPatients` and `telemedicineAvailable` are booleans in the API contract | `schemas/api-contracts/TenantPractitionerLocationResponse.schema.json:53-55`; `TenantPractitionerGroupResponse.schema.json:36-37` |
| 25 | Live data carries string values for `acceptingNewPatients` | test export value `Accepting New`; grep across api-layer and schemas: `Accepting New` (58), `Existing Patients Only` (45), `Accepting New Patients` (1) |
| 26 | Module 2 already names `vendor_export_memberships` and the echo check | Confluence "Design: Vendor ingestion service", Approach step 5 and decision D18; Data model table |
| 27 | Module 2's sweep runs every 5 minutes and is the guarantee for waiting work | same design, Approach step 7 and decision D20 |

**Consequence:** the file engine, the row grain, the NPI filter, the idempotent request, and the status poll all exist. What is missing is small: two egress template features, a corrected location-id path, and everything around the file — registry, placement, archive, audit.

## 2. Approaches analyzed in depth

### Criteria

| # | Criterion | Weight | Why it matters |
| --- | --- | --- | --- |
| C1 | Correct grain and identity — one row per practitioner × location, echo ids on every row | 5 | Module 2's attribution depends on it |
| C2 | Idempotent, restartable, no duplicate business effect | 5 | Non-negotiable requirement 1 |
| C3 | Durable record of what was sent, race-free | 4 | The comparison baseline for the whole program |
| C4 | Minimal new code and no duplicated platform engine | 3 | Maintenance and time to pilot |
| C5 | Ownership boundaries — vendor knowledge stays in this service | 3 | v2 D2-11; the egress team owns a generic engine |
| C6 | Operational load at monthly cadence | 2 | Alerts, retries, stuck work |
| C7 | Cost | 1 | Everything here is small |

### A. Egress builds the file; this service registers, polls, places, archives (chosen)

- **How:** *Approach* steps 1–7.
- **Scores:** C1 5 (proven by the test export once the location path is fixed) · C2 5 · C3 5 (archived CSV plus hash) · C4 5 · C5 4 (two small egress changes) · C6 4 · C7 5.
- **Failure modes:** egress outage delays a monthly file by hours; a stale job is caught by the sweep; a dropped NPI is reconciled and alerted.
- **Cost:** derived in the module doc's *Performance and scale*: under $1 per month of new infrastructure at the largest plausible volume.

### B. This service builds the file from api-layer reads

- **How:** page through the list endpoint, read relationships per practitioner, expand and map in code, stream the CSV to GCS.
- **Scores:** C1 4 (we control it, but write it from scratch) · C2 4 · C3 5 · C4 1 · C5 5 · C6 3 (70,000 HTTP reads in our task, deadline management) · C7 4.
- **Why it loses:** C4. Egress's mapping engine, expansion, and streaming already exist and are exercised by every tenant export. The control it buys is available through the template.
- **Failure modes:** a task deadline mid-file; partial files; memory pressure at the million-row scenario unless chunked and merged.
- **Cost:** development weeks; infrastructure equal to A.

### C. Egress writes into the vendor bucket directly

- **How:** per-tenant output bucket and object naming in egress configuration.
- **Scores:** C1 5 · C2 4 · C3 3 (no archive unless egress also learns our bucket) · C4 5 · C5 1 · C6 4 · C7 5.
- **Why it loses:** C5 and C3. Vendor naming and folder rules move into a generic engine; the only durable copy would be in the vendor's folder.
- **Cost:** saves one GCS rewrite per export.

### D. Completion by egress Pub/Sub event

- **How:** egress publishes on job completion; this service subscribes.
- **Scores:** C1 5 · C2 4 (the sweep is still required for lost messages) · C3 5 · C4 3 (egress publisher plus our subscriber) · C5 4 · C6 4 · C7 5.
- **Why it loses for now:** no consumer needs the latency; the sweep already exists. Recorded as the upgrade path.

### E. Pre-export value snapshot in this service

- **How:** store field values at selection time in a `vendor_export_rows` collection.
- **Scores:** C1 4 · C2 4 · C3 1 (two reads, hours apart — the snapshot diverges from the file for non-finding reasons) · C4 3 · C5 4 · C6 3 · C7 3 (PII duplication under 7-year retention).
- **Why it loses:** C3. The archived file is exact; the snapshot is not. Deferred without loss: the archive can be indexed later if module 2 needs per-field queries.

### F. Point-in-time read

- **How:** a Spanner read timestamp for the whole export.
- **Scores:** C1 5 · C2 5 · C3 5 · C4 1 (DAL REST has no as-of; egress would need direct Spanner reads for OV and relationships; version retention must be raised) · C5 3 · C6 3 · C7 4.
- **Why it loses:** C4 for no consumer need. Each row is self-consistent; rows minutes apart are acceptable for directory accuracy.

### G. Roster pipeline

- Disqualified on shape; see `roster-pipeline-reuse-analysis.md`. No outbound file builder; inbound-only runtime.

### H. A standalone export service

- **How:** a separate deployable and database for export only.
- **Scores:** C1 5 · C2 5 · C3 5 · C4 2 (a second service, second database, cross-service membership handoff) · C5 5 · C6 2 · C7 3.
- **Why it loses:** memberships, archive bucket, SFTP credentials, sweep, and audit collection are shared with module 2. Splitting them adds a network boundary inside one workflow. v2 D2-38 fixes one service for the program.

### I. Runtime — GCE managed instance groups (chosen) versus Cloud Run

The runtime is decided for the whole directory accuracy service, not for export alone: the folder instructions fix one service, one repository, and one database for both phases, so whatever runs the export tick and jobs also runs the ingestion jobs and the review API.

**The combined workload**

| Phase | Work | Shape |
| --- | --- | --- |
| Export | Tick, paged selection, one egress call, event handler, finish job | Small, bursty, minutes a month, no user traffic |
| Ingestion | Read the parsed rows from file-ingestion (up to a million per delivery), stage them, apply the rejected-recommendations lookup, release approved rows through Sync Latest | One long-running job per vendor delivery, tens of minutes to an hour, heavy Mongo writes |
| Review API | Approve, reject, skip; list and filter staged rows; status reads | Real user traffic from the PDM UI during review windows |

Everything long-running is a JobRunr job, not an HTTP request, in both phases. Both runtimes can run it; the comparison is about operations and cost.

**Comparison**

| Concern | Cloud Run: `api` service (request-serving) plus `worker` service (min 1, CPU always allocated, hosts JobRunr) | GCE: `api` MIG plus `worker` MIG (hosts JobRunr) |
| --- | --- | --- |
| Long-running jobs | A JobRunr job is not a request, so the 60-minute request timeout does not apply | Same; no platform ceiling to explain |
| Interruption of a running job | SIGTERM on deploy or scale-down; with min = max = 1 only deploys interrupt; jobs must be resumable | A rolling MIG update replaces the VM; same need for resumable jobs; update policy controls timing |
| Machine size for the ingestion job | Up to 8 vCPU and 32 GiB per instance | Any machine type |
| Pub/Sub delivery of the egress completion event | Push to a Cloud Run endpoint with IAM authentication built in | Pull subscription from the always-on worker, no inbound endpoint needed; or push through the load balancer |
| Inbound auth for operator and review endpoints | Cloud Run IAM invoker plus the application's JWT check | External HTTPS load balancer plus IAP or the application's JWT check |
| Deploy and rollback | Image push, revisions, seconds to roll back | Rolling MIG update, minutes to roll back |
| OS patching, agents, disk | None | Ours, reduced by container-optimised OS on the MIG |
| Scale to zero | `api` yes; `worker` no while it hosts JobRunr | Neither |
| Consistency with file-ingestion | Different runtime, Terraform, and runbook | Same topology, Terraform module, runbook, and on-call knowledge |

**Pros of GCE for this service**

- One operational model with file-ingestion, which the same team runs: one topology, one Terraform module, one deploy and rollback procedure, one set of alerts.
- Full control over the long-running worker: no SIGTERM on scale-down, no per-request timeout anywhere, MIG update policy decides when a VM is replaced.
- Any machine size, and committed-use discounts of roughly a third.
- A pull subscription on the worker removes the need to expose any HTTPS endpoint for Pub/Sub.
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

A phase-1-only footprint (1 × e2-small `api`, 1 × e2-small `worker`, load balancer, IPs) is about $50 a month.

| Cloud Run item | Sizing | Derivation | Monthly |
| --- | --- | --- | --- |
| `worker` service, always on | 2 vCPU, 4 GiB, min 1 | ($0.000018 × 2 + $0.000002 × 4) × 2,592,000 seconds | $114.05 |
| `api` service, request based | tens of thousands of requests | under free tier plus small overage | ≈ $5 |
| Load balancer, NAT, disks | none needed | | $0 |
| **Total** | | | **≈ $119** |

A phase-1-only Cloud Run worker at 1 vCPU and 1 GiB is about $52 a month.

**Reading the numbers:** at phase 2 size GCE is cheaper by roughly $15 a month on-demand and roughly $40 with a commitment; at phase 1 size the two are about equal. Cost is not the deciding factor.

**Why GCE is chosen:** one operational model for the two services the team runs, no platform ceiling on the long-running worker, cost parity or better at phase 2 size, and consistency with the platform direction recorded for file-ingestion ("avoid Cloud Run and use GCE"). Cloud Run is recorded in the module doc as the alternative with its honest advantages: no load balancer, no VM lifecycle, built-in ingress authentication.

**Scores:** GCE — C2 5 · C4 4 (load balancer and VM lifecycle are new work) · C5 5 · C6 4 (one runbook with file-ingestion) · C7 4. Cloud Run — C2 5 · C4 5 · C5 5 · C6 3 (a second runtime for the team) · C7 3.

## 3. Requirements traceability

| Requirement | Source | Covered by |
| --- | --- | --- |
| Query-selected population, own cadence, own service | v2 D2-38 | *Approach* steps 1–2; D1, D9 |
| Transport `from/<tenant>/`, vendor read-only | v2 D2-03 | *Approach* step 5; *Security* |
| Internal contract proposal before the vendor call; batch identity and naming | v2 D2-06; contract proposal §3 | D7, D8; *Contracts → step 5* |
| Vendor-agnostic naming | non-negotiable 6 | Collections and endpoints carry `vendor_export`, never a vendor name; vendor key only in configuration values and the batch id |
| Idempotency everywhere | non-negotiable 1 | Registry uniqueness, deterministic membership ids, named tasks, compare-and-set transitions |
| Full audit, same transaction, 7 years | non-negotiable 2; v2 D2-20 | *Audit trail* |
| Correlation end to end | non-negotiable 3 | `exportBatchId` → egress correlation → archive hash → module 2 `exportBatchRef` |
| Tenant isolation server-side | non-negotiable 4 | *Security*: tenant from configuration, stamped everywhere, egress tenant header |
| `certify_practitioner_id` convention | non-negotiable 5 | Membership document and every contract |
| Quarantine over guessing; nothing silently discarded | non-negotiables 8–9 | `PLACEMENT_CONFLICT` stops; `DROPPED_BY_EGRESS` counted and alerted |
| ISO 8601 and compact filename dates | non-negotiable 11 | Timestamps in every payload; `yyyyMMdd` in the filename |
| Selection query format and safety | v2 O-17 | D9; *Contracts → step 2*; allowlisted grammar, schema-validated |
| Registry before file; echo the vendor must return | instructions §7 | *Approach* step 2; contract columns `export_batch_id`, `certify_practitioner_id`, `certify_location_id` |
| Never rewriting a placed batch; failure semantics; reconciler | instructions §7 | D8; *Approach* steps 5–7; *Failure modes* |

## 4. Inputs and outputs of this module

**Consumes:**

- api-layer: practitioner list endpoint (selection), relationships response (locations), `tenant_configurations` of type `directory-accuracy-config`, egress-template endpoints (template creation, one-time).
- Egress service: `POST /api/v1/egress/export`, `GET /api/v1/egress/status/…`, the finished object in its bucket.
- Platform team: the vendor bucket and its `from/<tenantId>/` prefix; IAM grants.
- Module 2: `ingestion_batches.exportBatchRef` to set `acknowledgedAt`.

**Provides:**

- To the vendor: the CSV under the contract filename in `from/<tenantId>/`, optionally with a manifest.
- To module 2: `vendor_export_batches` (the batch the vendor's `exportBatchRef` must match) and `vendor_export_memberships` (the ids the echo check compares against).
- To the PDM reviewer UI: `GET /vendor-exports` and `GET /vendor-exports/{id}` for export status per tenant.
- To operators: `retry`, `supersede`, a manual tick.
- To the archive: one immutable, hashed copy of every delivered file.

## 5. Design rationale — anticipated questions

- **Why not just store what we read when we select? Then we know what we sent.** We do not read field values at selection; egress reads them later. Two reads, minutes to hours apart, cannot be guaranteed equal, so a stored snapshot would sometimes disagree with the file for reasons that are not findings. The file is what left; we archive it byte-exact with a hash.
- **If the archive is the record, how does module 2 compare values?** The vendor echoes `current_value_seen` per row, and the archived file holds every value by `certify_practitioner_id` and `certify_location_id`. Module 2 reads the live OV for "now". Nothing needs a third copy.
- **Why is the egress correlation id different from the export batch id?** Egress refuses to run the same correlation id twice — a retry after a failed egress job needs a new one. The vendor-facing batch id must stay the same for the period. So the egress id is the batch id plus an attempt suffix.
- **Why poll instead of an event?** Egress publishes no event today. Polling from a sweep that already exists costs nothing at monthly cadence and needs no cross-team change. The event is the upgrade path, not the MVP.
- **Why does the sweep matter if Cloud Tasks retries?** Cloud Tasks deletes an exhausted task with no dead-letter queue. The durable batch row plus the sweep is what makes "nothing waits forever unseen" true.
- **Why is `groupId` part of the membership key?** Egress expands locations inside groups, so one physical location reached through two groups produces two rows. The membership mirrors the file's grain exactly.
- **What if egress drops an NPI?** After the file exists we reconcile memberships against it and mark the missing ones `DROPPED_BY_EGRESS`. Module 2 then knows "never sent" from "sent, no answer".
- **Why a structured filter and not SQL?** Injection safety and translatability. The grammar is the same shape the platform already stores for crosswalk rules; every condition maps to a typed parameter of the list endpoint.
- **Why not egress's own template schedule?** It would run the export without writing memberships first, without our batch id, and without our registry. Egress stays the engine; the schedule is ours.
- **Why not a point-in-time snapshot like large data platforms do?** The platform has no as-of read path; egress reads over DAL REST. Each row is self-consistent. A monthly accuracy program tolerates rows minutes apart. The cost would be a DAL contract change for no consumer.
- **Is it a problem that the export module lives in a repository named for ingestion?** Naming only; the service is one deployable pair and one database for the program. A rename to `directory-accuracy-service` is proposed in §6.
- **How do large platforms handle "each tenant has its own selection rule, and it changes"?** Four patterns, all sharing one principle — the owner of the data owns the query semantics, and the consumer never re-implements evaluation:
  - Inline declarative criteria the data owner validates and runs (Kubernetes label selectors, LaunchDarkly and Flagsmith segment rules, Elasticsearch query DSL). This is the pilot design: the criteria object mirrors api-layer's `filter` parameter exactly.
  - A reference to a saved query the data owner stores (Salesforce list views and reports on scheduled jobs, Jira filters on subscriptions, Splunk saved searches, Stripe Sigma saved queries).
  - A reference to a named segment with count and member endpoints (Braze segments, Segment Audiences, Google Ads audiences, HubSpot active lists).
  - Materialised membership — a marker on the entity itself (Salesforce campaign membership, Kubernetes labels). For us this is a user-defined field on the practitioner, already selectable through `data.userDefinedFields.<key>`; a good fit for tenants whose directory population is a curated list rather than a rule.
- **What is the natural destination after the pilot?** A saved practitioner filter owned by api-layer: create and validate, return a count, evaluate as ids only with keyset paging; the PDM list screen gains "save this filter"; our schedule stores the filter id and the batch snapshots the criteria at run time. Because the pilot's inline criteria are byte-compatible with api-layer's filter, the migration is one field on the schedule row (`selectionRef` in place of `selection`) and one resolve call in the select job. This removes our schema, allowlist, and preview endpoint, since api-layer would own all three.
- **What is the one industry-standard piece we are missing?** A published field catalog — `GET /practitioners/filter-fields` returning filterable fields, types, and operators (Salesforce describe metadata, Elasticsearch mappings, GraphQL introspection are the same idea). Our schema would be built from it instead of hardcoded, and the PDM UI could build its filter controls from it too. Small api-layer ask; deferred, not requested for the pilot.
- **Why is there no separate pause for a temporary hold? An incident and a churned tenant both stop the export.** One flag covers both. The row is never deleted: `disable` sets `enabled = false` with a mandatory reason, and `enable` turns it back on. The difference between the two cases is carried by the reason and by the choice made on the way back, not by a second state.

  | | Tenant off the program | Temporary hold during an incident |
  | --- | --- | --- |
  | Who does it | Product or account management: contract ended, tenant churned, pilot over | Operations: vendor outage, a data problem under investigation, a template being fixed |
  | Call | `disable` with a reason such as "contract ended" | `disable` with a reason such as "roster load incident" |
  | Coming back | `enable` with `catchUp: false`: `nextDueAt` moves to the next future occurrence; no backfill | `enable` with `catchUp: true`: the most recent missed period runs at the next tick |
  | Audit | `SCHEDULE_DISABLED`, then `SCHEDULE_ENABLED` with `catchUp` | Same |

  - **Holding a tenant during an incident, concretely.** On 28 September a roster load for `org-xyz` goes wrong and half the addresses are stale; the fix takes a week. If the 1 October export runs, the vendor verifies data we already know is wrong. An operator calls `disable` with the reason "roster load incident". The 1 October tick skips `org-xyz`. Nothing left the building. While disabled, the operator can still fix the template or filter with `PUT`; that never turns the export back on.
  - **Coming back.** The fix lands on 9 October. The operator calls `enable` with `catchUp: true`. `nextDueAt` is set to 1 October, the most recent occurrence since the disable; the next tick creates the October batch on the 10th and moves `nextDueAt` to 1 November: the vendor gets October nine days late instead of never. Only one period runs, however long the hold lasted. With `catchUp: false`, October is skipped and `nextDueAt` jumps to 1 November.
  - **What this gives up.** The system cannot tell a hold that someone forgot to lift from a tenant that left on purpose, so there is no alert for a forgotten hold: a churned tenant is meant to be quiet, and an alert on "disabled and overdue" would fire for every one of them. The reason is visible on the schedule read and in the PDM UI, which is the check. At pilot scale (one vendor, a handful of tenants) this is accepted.
  - **If forgotten holds become a problem:** replace `enabled` with `status: ACTIVE | PAUSED | DISABLED`, add `pause` and `resume`, and alert when a `PAUSED` schedule's `nextDueAt` is more than 35 days in the past. The tick changes from `enabled = true` to `status = ACTIVE`; nothing else moves.
- **Why `PUT` to create a schedule, and `POST` actions to turn it on and off?**
  - **`PUT` for create and replace.** `POST` to a collection is for when the server picks the id. A schedule's id is the tenant and vendor, which the caller already knows, so the caller addresses it directly: `PUT /schedules/{tenantId}/{vendor}`. Omitting `version` means "create only" (`201`, or `409` if one exists), so a second create can never overwrite someone's schedule; sending `version` means "replace" (`200`, or `409` if stale). A call retried after a timeout is therefore safe: it can never duplicate or overwrite, and if the first write landed the retry gets `409` and the caller re-reads to see its own settings. `POST` to a collection would give the same `409` on retry, with a second endpoint for updates and nothing gained.
  - **`PUT` never changes `enabled`.** Changing settings and switching the export on are different decisions. If `PUT` re-enabled, fixing a template during an incident hold would restart the export before the incident was closed.
  - **`POST …/disable` and `POST …/enable`, not `DELETE`.** HTTP `DELETE` means the resource is gone and a later `GET` returns `404`; here the schedule stays readable and editable, and `disable` is also how a temporary hold is done. `DELETE` bodies have no defined meaning and some proxies and clients drop them, which breaks a mandatory reason. The reason and `catchUp` belong to the action, not the stored row, so they are action calls like `run-now` and `preview`.
- **Why does the batch move to `EGRESS_COMPLETED` when the event arrives, and not only when delivery finishes?** Because two different safety nets read the batch state and act on it without asking anyone, so the state must be true at every moment.
  - **Two moments, not one.** Egress finishes at 07:11 and publishes; our handler hears it at 07:11:05. Our finish job then reads the object's metadata, compares counts, and marks the batch delivered: 07:11:20 on a good day, 08:30 if the metadata read is retrying. Between those moments, "waiting for egress" is no longer true. `EGRESS_COMPLETED` is the honest note for the gap: egress is done, it is our turn, delivery not confirmed yet.
  - **Example, the deadline check.** The batch was requested at 06:01; the deadline check is due at 12:01. With the state written at 07:11, the check reads `EGRESS_COMPLETED`, knows someone else is on it, and exits. Without it, the check reads `EGRESS_REQUESTED`, calls egress, gets `COMPLETED`, and enqueues a second finish job: two jobs on one batch, one wasted egress call, and an `EXPORT_EVENT_MISSED` in the audit trail for a batch whose event actually arrived. The compare-and-set on `DELIVERED` keeps it correct, but not clean.
  - **Example, the reconciler.** The handler writes `EGRESS_COMPLETED` at 07:11:05 and the VM dies before it enqueues the finish job. At 08:00 the reconciler finds a batch in `EGRESS_COMPLETED` untouched for 49 minutes, knows that state wants a `FinishJob`, and enqueues one. Without the state, the row still says `EGRESS_REQUESTED`, which is allowed to sit for hours, so the reconciler leaves it alone and nothing happens until the 12:01 deadline check: five hours lost for a file that was ready at 07:11.
  - **In bullets for a review:** two things happen at different times; the state is a note both safety nets read; without the intermediate state the note lies during the gap; the deadline check gets fooled into a second run; the reconciler goes blind to a dropped enqueue; with the state both do the right thing.
- **What does the reconciler actually do?** It is a janitor that walks the floor once an hour looking for work that got dropped. Every step does two writes in a row, update the batch row then enqueue the next job, to two different collections, so they cannot be one transaction. If the VM dies between them the row says "ready for step X" and no job for step X exists; JobRunr has nothing to retry. Once an hour the reconciler runs one query for batches in `SCHEDULED`, `NPIS_SELECTED`, or `EGRESS_COMPLETED` untouched for 30 minutes, enqueues the job that state calls for, and fires alert E2. It is safe to be generous because every job checks the row state first and a duplicate exits. It never touches `EGRESS_REQUESTED`, where waiting for hours is normal and the deadline check is in charge, nor any terminal state.
  - **Example.** 06:00:12 the tick inserts a batch in `SCHEDULED` and the VM dies before enqueueing `SelectJob`. 07:00 the reconciler finds it, enqueues `SelectJob`, fires E2. 07:00:15 `SelectJob` checks the state is still `SCHEDULED` and starts paging api-layer. One hour late instead of lost.
- **Why are retry and supersede two endpoints? Both add one to a counter.** The counters live in different places, are seen by different parties, and must behave in opposite ways on the one thing the vendor sees.

  | | `attempt` (retry) | `seq` (supersede) |
  | --- | --- | --- |
  | Where | A field on the batch row; appears only in the egress correlation id, `org-xyz-candor-2026-10-001-r2` | The last leg of the batch id itself, `org-xyz-candor-2026-10-002` |
  | Who sees it | Only egress and us | The vendor, in the filename and on every row |
  | What it counts | How many times we tried to produce this one batch | How many batches exist for this period |
  | Changes the batch id | No | Yes; it is a new batch |

  - **Retry answers "the file never reached the vendor, try again under the same name".** The batch is in `FAILED`; nothing was placed; the vendor has seen nothing. Egress refuses a correlation id it has already seen, so we cannot resend `-r1`; we send `-r2`. That is the only reason `attempt` exists. The batch id stays `001`, because to the vendor this is still the first and only October batch. Retry re-enters at the step that failed: if selection succeeded and egress failed, the NPIs are already registered and only egress is asked again.
  - **Supersede answers "the vendor already has a file, and it was wrong".** The batch is in `DELIVERED`; the file is in the vendor folder; the vendor may have downloaded it, may be working from it, may echo `001` back next month. We cannot overwrite that file: two different files under one name and one id would make the echo ambiguous. So a new batch `002` is created and runs from selection onward, because what was wrong may include who was selected. The old batch becomes `SUPERSEDED` and its file stays.
  - **Why not one endpoint that guesses from the state.** After a failure the batch id must not change; after a delivery it must. An endpoint that chose by state would work but would hide the decision. An operator calling retry knows nothing reached the vendor; an operator calling supersede knows something did and is choosing to send a second file. Different decisions, different audit events, and the endpoint name should say which one was made.
  - **One line:** retry gives egress a new correlation id and keeps the vendor's batch id; supersede gives the vendor a new batch id because the old one is already in their hands.

## 6. Open questions and sign-offs

| # | Question | Owner | Blocks |
| --- | --- | --- | --- |
| Q1 | `Y`/`N` rendering for `acceptingNewPatients` (mixed `Accepting New`, `Existing Patients Only`, booleans) and `telemedicineAvailable` — egress `map` transformation, or the contract accepts raw values | Engineering with the egress team; Product for the value meaning | Template |
| Q2 | Source fields for `practitioner_phone` and `telehealth_url` | Product | Template |
| Q3 | `group_affiliation`: the group of this row's location, or every group `;`-joined | Product; contract call | Template |
| Q4 | Practitioner with zero practice locations: excluded by design here; confirm the vendor does not want practitioner-only rows | Product | Selection rules |
| Q5 | Location-level rules inside egress (only active locations) — today applied to memberships only, egress exports every location of a sent NPI; post-filter the file, or extend egress `npiFilter` with a location filter | Engineering with the egress team | File content |
| Q6 | Egress changes D11: injection of `_correlation_id` and `_generated_at`, configurable list joiner; whether the injected value should be the batch id we pass rather than the egress correlation id | Egress team | Template; *Rollout* step 1 |
| Q7 | Outbound manifest: does the vendor want one beside our file | Contract call (v2 O-1) | `writeOutboundManifest` default |
| Q8 | Response window in days for `EXPORT_RESPONSE_OVERDUE` | Contract call (v2 O-1) | Alert E7 threshold |
| Q9 | Egress throughput per 10,000 practitioners on the template path — measure in staging | Engineering | `egressStaleAfterHours` default |
| Q10 | Repository and audit collection naming: `vendor-ingestion-service` → `directory-accuracy-service`; `vendor_ingestion_events` → `vendor_exchange_events` | Engineering; module 2 amendment | None — naming |
| Q11 | Per-deployable service accounts (service constraint) versus module 2's single `vendor-ingestion` identity | Engineering; module 2 amendment | *Security* |
| Q12 | Whether the selection filter's allowlisted field set needs fields the list endpoint does not expose yet (would land in api-layer or DAL) | Engineering | Selection |

## 7. Ticket impact

| Ticket | Change |
| --- | --- |
| CP-39603 (spike) | This document is the export half of the spike deliverable; re-baseline estimates below |
| CP-39604 (scaffolding) | Add: `vendor_export_batches` collection and indexes, `directory-accuracy-config` type and JSON schema, Cloud Scheduler `vendor-export-tick`, Cloud Tasks queue `vendor-export`, IAM grants (egress bucket read, vendor bucket `from/` create, archive create, egress endpoint access) |
| CP-39605 (selection, export, inbound receipt) | Rewrite scope to this doc: selection translation and validation, membership registration, egress request and poll, placement and archive, reconciliation, retry and supersede, read endpoints. Inbound receipt stays with module 2's ticket. Estimate to re-baseline after Q6 and Q9 |
| CP-39606 (ingestion) | Consume `vendor_export_memberships` as defined here; set `acknowledgedAt` and write `EXPORT_BATCH_ACKNOWLEDGED` |
| CP-39608 (observability and audit) | Add alerts E1–E8 and E10, metrics above, export events into the shared audit collection |
| New: egress team ticket | D11 — per-job value injection and configurable list joiner in the template pipeline; optional `map` transformation (Q1) |
| New: template ticket | Create the pilot tenant's vendor template with verified paths; fix the location-id path; one-practitioner verification run |
| Re-homed | The export job that never had a ticket (jira draft note) is CP-39605 as rewritten |
