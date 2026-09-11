# Directory Accuracy — why vendor ingestion is not built on the roster pipeline

**Repos referenced:** `api-layer`, `core-data-access-layer`, `dal-mdm-survivorship-layer`, `frontend`, all under `pdm-dal-full/fe-api-coredal` (survivorship under `pdm-dal-full`). Line numbers are as of 2026-09-11 on the local checkout of `api-layer` `main` (`9597ac0`) and will drift; file and symbol names are the durable pointer. Vendor sample referenced: `MVP_09022026_Sample.xlsx` (September 2026, two sheets).

---

## 1. Purpose and scope

This document answers one question: can the existing roster pipeline in `api-layer` carry vendor accuracy recommendations (Candor first) into a review queue and then into the golden record, and should it?

It is written in the order the question is usually asked:

1. How it *could* be done, step by step, using two roster uploads per vendor delivery, with every code and configuration change named.
2. Why that path is a poor choice, with evidence.
3. What the roster runtime itself would bring along (scaling and reliability behaviour).
4. Where roster and vendor ingestion actually overlap in function, and where they do not.
5. Why a separate directory accuracy service is the right shape.

**Out of scope:** the vendor file contract itself, the review UI, and the export half of the directory accuracy loop (the vendor export module).

---

## 2. How it could be done: the two-upload method

### 2.1 The idea in one paragraph

The vendor drops a response file on SFTP. A small job picks it up, runs our own checks, and uploads it to `POST /roster` as a roster job of a new kind. Roster parses it, maps each vendor column to a new set of system fields, and writes the rows into a holding table instead of the practitioner tables. The job is hidden from the roster UI by a special status. A payer reviewer approves, rejects, or skips rows in the holding table through a new review surface. A second job collects the approved rows, builds a second roster file in the normal practitioner template shape, and uploads it again. This time the mapping targets the real practitioner system fields, so roster writes the approved values into `core_practitioners` and its relationship tables as a new slice, under a source name other than `roster:{tenantId}`.

The rest of this section lists what must change to make each of those sentences true.

### 2.2 Step 1: get the file into roster

Roster has exactly one way in: an authenticated human upload.

- `POST /roster` is `@Authenticated` with permission `CREATE_ROSTER` (`api-layer/src/main/java/com/certifyos/api_layer/roster/resource/RosterResource.java:70-71, 231`).
- The endpoint requires `tenant-id` header, `file`, `templateId`, `jobType` in `PRACTITIONER | FACILITY | GROUP`, and `sheetName` for any `.xlsx` (`RosterResource.java:233-295`).
- The creator is recorded as the caller's email (`RosterResource.java:278, 302`; `RosterService.java:689-690`). Every job needs a person.
- There is no service-to-service upload path. `createRoster` has one caller chain, the resource (`RosterResource.java:330` → `RosterService.java:221`). The only `/internal` roster route is a test harness gated off by default (`RosterResource.java:1294-1295`, `roster.transaction-test.enabled` default `false`).

**Changes needed:**

- A new internal upload endpoint or a machine identity that holds `CREATE_ROSTER`, plus a system "user" email to stand in as creator.
- The vendor sample is one workbook with two sheets, *Physicians Directory Accuracy* (57 columns) and *Physicians Additional Locations* (31 columns). Roster reads one sheet per job (`roster_subscribers/service/RosterValidator.java:750-759`), so every delivery becomes two roster jobs, or a pre-split into two files.

### 2.3 Step 2: teach roster the vendor columns (the system-field JSON)

Roster does not read a file against a table. It reads a file against a **template** (per tenant, which columns exist and which system field each maps to) and a **system-field JSON** (per job type, the catalogue of every field roster knows, each tagged with the entity it lands in). The entity name becomes the destination table.

**What the vendor sends versus what roster already knows.** The sample's main sheet follows a fixed pattern per attribute: the value we sent, then the vendor's verdict columns.

| Vendor column (sample) | Meaning | Closest existing system field | Existing entity |
| --- | --- | --- | --- |
| `npi` | our value, echoed | `practitionerNpi` | `CorePractitioner` |
| `npi_verification_status`, `_reason`, `_confidence`, `_evidence` | vendor verdict on NPI | none | — |
| `first_name` | our value, echoed | `firstName` | `CorePractitioner` |
| `first_name_verification_status`, `first_name_candor_value` | vendor verdict + suggested value | none | — |
| `last_name`, `_verification_status`, `_candor_value` | same pattern | `lastName` for the echo only | `CorePractitioner` |
| `specialties`, `_verification_status`, `_candor_value`, `_reason`, `_confidence`, `_evidence` | same pattern | specialty fields exist for the echo only | `TenantPractitionerSpecialty` and others |
| `address_line1`, `address_line2`, `city`, `state`, `zip` + five `_candor_value` twins | our address, vendor's corrected address | `groupPracticeLocationAddressLine1` etc. for the echo only | `CoreEntityAddress` |
| `location_verification_status`, `_relationship_candor_value`, `_reason`, `_confidence`, `_evidence` | verdict on the location itself | none | — |
| `phone`, `fax`, `accepting_new_patients`, `pcp_at_location` + verdict twins | same pattern | echo fields exist; verdict fields do not | mixed |
| `degrees_candor_value`, `practice_name*` | vendor-only attributes | none | — |

Source: sheet headers read from the sample; existing fields from `api-layer/src/main/resources/schemas/roster-system-fields.json` (422 properties under `properties`; 182 tagged `CorePractitioner`, 56 `CoreEntityAddress`, 48 `GroupPractitionerLocation`, 28 `CoreLocation`, 32 distinct entities in total).

**Why the existing fields cannot be reused for the first upload.** `firstName` is tagged `metadata.entity = "CorePractitioner"`, `entityKey = "firstName"`. Mapping the vendor's `first_name` column to it would write the echo straight into the practitioner slice. The first upload must land in a holding table, so every column, echoes included, needs a field tagged with the holding entity. The verdict columns have no existing field at all.

**Two ways to add the fields.**

| Option | What changes | Consequence |
| --- | --- | --- |
| A. New attributes in the existing `roster-system-fields.json` | Add 57 properties such as `vendorFirstName`, `vendorFirstNameVerificationStatus`, `vendorFirstNameSuggestedValue`, each with `metadata.entity = "ExternalSourceStaging"` | Every consumer of the file sees them (section 2.5). Egress templates and the practitioner service load the same file. |
| B. A new file, for example `external-source-system-fields.json`, and a new job type | Add a fourth `RosterJobType`, a fourth path constant in three services, a fourth tenant-config key, and route every job-type branch | 108 job-type branch sites across 25 files (section 2.8). Per-tenant selection today picks one file per job type (`roster/service/TenantRosterSchemaService.java:24-27`), so a new type is the only way to avoid replacing the tenant's real practitioner schema. |

Whichever option, the count is the same: **57 new system fields** (the additional-locations sheet's 31 columns are a subset of the main sheet's 57 by name), each carrying `type`, `title`, `description`, `metadata.entity`, `metadata.entityKey`, and any `pattern` or `enum` we want roster to enforce.

**The entity name must exist in Java.** Tagging a field with `ExternalSourceStaging` does nothing by itself.

- Destinations are a hardcoded registry of 78 named resources built in `roster/service/ResourceConfiguration.java:261-263` (`initializeConfigs()`), each with a default operation, filter provider, crosswalk generator, and foreign-key wiring.
- An entity name that is not in the registry is skipped silently, with no error and no row-level failure: `roster/service/TransactionOrchestrator.java:2555-2558` returns when `getConfig(resourceName)` is null.
- Roster writes through the DAL `POST /batch` (`roster/service/RosterRecordHandler.java:1710, 2010-2016`), so the holding table also needs a DAL Liquibase changeset, a DAL entity, and a DAL batch resource in `core-data-access-layer`.

**Templates are per tenant.** `roster_templates.tenantId` is `NOT NULL` (`core-data-access-layer/.../db/changelog/roster-templates/001-create-roster-templates.yaml:16-19`), every template read is tenant-scoped, and there is no shared or global flag. One vendor schema therefore becomes two templates (two sheets) per tenant, created and approved per tenant through `POST /templates/...` and `PUT /templates/{id}/approve` (`roster/resource/TemplateResource.java:270-271, 1073-1074`).

### 2.4 Step 3: keep the vendor jobs out of the roster surfaces

The plan hides vendor jobs behind a special status. Roster has no such concept, and its list endpoints have no server-side status filter.

- `GET /roster/` builds only an `order` string and passes the caller's `filter` JSON through (`RosterResource.java:549-550, 599-673`). The front end supplies `"data.status": {in: [...]}`; the server never excludes anything.
- Row lists filter by a `statuses` query parameter the client chooses (`roster_record/resource/RosterRecordResource.java:468-477`, SQL `AND status IN UNNEST(@statuses)` at `roster_record/repository/RosterRecordRepository.java:52-54`).
- The nearest existing "held" state is `PRE_PROCESSING`, which only stops automatic dispatch (`roster/service/UserPermissions.java:37-52`). It is still listed.

**Every surface that would need a vendor branch:**

| Surface | Where | What happens without the branch |
| --- | --- | --- |
| Roster list, detail, records, download, delete, cancel, reupload | `RosterResource.java`, `RosterRecordResource.java` | Vendor jobs appear in the tenant's roster screen |
| Search practitioners by roster | `practitioner/dto/PractitionersGetFilter.java:270-275` | Vendor job ids become searchable provenance |
| Customer webhook `roster_job.status.changed` | `roster/service/RosterWebhookService.java:216-222`; enabled in staging and prod (`application.properties:243-245`) | Tenant's integration receives vendor job events, or shutdown waits on an export URL (`:203-205`) |
| Status emails to the uploader | `application.properties:450-453` (three SendGrid templates) | The system user's inbox receives every vendor job outcome |
| BigQuery dashboard events on `roster-logging` | `application.properties:749-757`; `common/logging/RosterEventType.java` | Roster throughput dashboards count vendor jobs |
| Post-import credentialing actions | `roster/service/RosterPostImportActionService.java:55-58` | A `START` row in a vendor file would open a credentialing workflow |

### 2.5 Step 4: who else sees the system-field JSON

Adding fields to `roster-system-fields.json` is not an internal change. The file is served to users and to an external AI service.

**Endpoints that return its contents** (all behind a user JWT; permissions in brackets):

| Method and path | Resource | Returns |
| --- | --- | --- |
| `GET /templates/fields` [READ_TEMPLATE] | `roster/resource/TemplateResource.java:1241-1242, 1292` | the full field list via `FieldMatchingService.getAllFields` |
| `GET /roster-upload/system-fields` [UPDATE_ROSTER_UPLOAD] | `roster_upload/resource/RosterUploadResource.java:494-495, 591` | grouping metadata for every field |
| `POST /roster-upload/{templateId}/column-mapping` [UPDATE_ROSTER_UPLOAD] | `RosterUploadResource.java:79-80, 184` | response carries a `systemFields` array |
| `GET /roster/{id}/schema` [READ_ROSTER] | `RosterResource.java:802` | the generated JSON Schema for a job |
| `GET /templates/scratch/required-fields` [READ_TEMPLATE] | `TemplateResource.java:1312-1313` | required-field policy over the same catalogue |
| `GET /api/v1/egress-templates/fields` [READ_TEMPLATE] | `egress_template/resource/EgressTemplateResource.java:485-486, 517` | egress field catalogue sourced from the same file (`EgressFieldCatalogService.java:29-33, 41`) |

**Screens that render the list** (`frontend/apps/web`):

- Template builder, the main field-picking screen: `features/roster/components/roster-template-scratch-builder/roster-template-scratch-builder.tsx:492, 499`.
- Roster mapping form: `features/roster/components/roster-mapping/roster-mapping-form/roster-mapping-form-field/roster-mapping-form-field.tsx:42`.
- Template-from-file page: `features/roster/templates/roster-template-from-file-page.tsx:97`.
- Egress template builder and import pages: `features/egress-templates/components/egress-template-builder/egress-template-builder.tsx:96`, `features/egress-templates/templates/egress-template-from-import-page.tsx:63`.

**An external AI service receives the catalogue.** Template creation sends every system-field key and description, together with the customer's column samples, to the Schema Mapping service `/predict` endpoint (`schema_mapping/AiFieldSuggestionService.java:21, 116-143`; descriptions built from the system fields at `roster/service/TemplateService.java:895-898`). This is on by default including production (`application.properties:1579-1581`). Fifty-seven vendor-verdict fields would become candidate targets for every customer's column-mapping suggestions.

**Consequence.** Under option A, a payer's roster admin building a normal practitioner template would see fields such as "First Name Verification Status" and "Location Verification Confidence" in the picker, in egress, and in AI suggestions. Under option B, the new job type must be excluded from each of these endpoints and screens by hand, because they take a `type` and default to the practitioner catalogue.

### 2.6 Step 5: validations

Roster validates in two layers. Neither has a per-template switch.

- Schema layer: generated per job from template columns × system-field JSON (`roster/service/JsonSchemaGeneratorService.java:163-197`). Rules come from the field definitions (`pattern`, `enum`, `format`). Per-column controls are `Required` and skip only (`JsonSchemaGeneratorService.java:585-587, 649`).
- Business layer: `roster/service/BusinessValidationService.java`, 5,569 lines, about 40 hardcoded `validateXxx` methods (network exists, SSN uniqueness, NPI against NPPES, location hash, START preconditions). Switches are global (`:96-134`) or tenant-level (`roster/service/DataIngestionConstants.java:59, 72`), never per template or per job.

**Changes needed:**

- First upload: a job-type branch that bypasses practitioner business rules but still resolves the NPI to a practitioner, because the holding row is useless without that link.
- Second upload: the full practitioner rule set runs on vendor-approved values. Some rules are wanted (NPI format), some are harmful (a vendor-corrected address failing the location-hash rule and blocking a reviewer-approved change with no path back to the reviewer).

### 2.7 Step 6: the second upload and the golden-record write

After review, approved rows are shaped into a normal practitioner roster file and uploaded again. This is the part of the plan that fits roster least.

- **Source name is hardcoded.** `"roster:" + tenantId` is concatenated in eight places (`RosterRecordHandler.java:961, 1361`; `BusinessValidationService.java:662, 2271, 2844, 2913, 3086, 5404`) and read back in lookups (`ResourceConfiguration.java:279, 308, 333, 356, 603`). Writing under `candor:{tenantId}` means threading a source through the transaction context and every lookup that assumes roster's own slice.
- **Operation cannot express removal.** `CorePractitioner` defaults to `UPSERT_DATA` (`ResourceConfiguration.java:270`), a deep merge in which arrays only grow. A vendor recommendation to remove a practice location has no representation. The agreed release model for the vendor source rebuilds the whole slice from a ledger of released values and writes it with `UPSERT` each time, which is the opposite default.
- **The write path is scheduled for removal.** `executeBatch` is `@Deprecated(forRemoval = true)` in favour of a Pub/Sub path that is built but off in production (`RosterRecordHandler.java:1696`; `application.properties:786-788`, `roster.processing.use-pubsub` `%prod` `false`).
- **Survivorship keys on the source prefix.** The MDM survivorship layer treats a source as tenant-specific only when its name starts with `tenant` or `roster` (`dal-mdm-survivorship-layer/.../common/repository/AbstractOVRepository.java:813`, used at `:396-404`; same check in four entity services). Whether a `candor:` slice ever reaches the operational value must be verified before any release design, roster-based or not.
- **Side effects come along.** The practitioner write path also creates group, location, network, and address relationships from the same row (`ResourceConfiguration.java:1020, 1051, 1993`), runs the NPPES NPI check (`npiApiCheck: true` on `practitionerNpi`), and can open or cancel credentialing workflows on `START`/`STOP` rows. None of these are wanted for a vendor correction to one phone number.
- **Grain mismatch.** Roster is one row per practitioner with repeated column groups for multiple locations. Approved vendor rows are one per practitioner per address. The second-upload builder must pivot them back into roster's wide row shape, and the in-file uniqueness rule (`idx_roster_rows_provider_claim`, one `START`/`STOP` claim per provider per roster) must be checked against multi-row practitioners.

### 2.8 Step 7: the fourth job type touches 108 branch sites

If option B is chosen (new file, new job type), the enum `RosterJobType { PRACTITIONER, FACILITY, GROUP }` gains a value and every branch on it must be reviewed.

| Kind of site | Count | Examples | Risk |
| --- | --- | --- | --- |
| Real `switch` that would flag a missing case | 1 | `TransactionOrchestrator.java:512-519` | Low, compiler helps |
| Three-way `if/else` chains | ~25 | `FieldMatchingService.java:152-154`, `NpiValidationService.java:397-569` (9 sites), `RosterSchemaService.java:491-493` | Medium, silent fall-through |
| Binary `== FACILITY ? a : b` | ~80 | `BusinessValidationService.java` alone has 44 (`:583` … `:4254`); `RosterRecordHandler.java:221, 235, 246, 370, 411, 638, 747, 826, 1261, 1313` | High, a fourth type is silently treated as practitioner |
| Silent defaults to `PRACTITIONER` | 2 | `RosterResource.java:943`; `TransactionContext.java:346` | High |
| Non-code seams | 6 | three path constants, three tenant-config keys (`TenantRosterSchemaService.java:25-27`), egress catalogue (`EgressFieldCatalogService.java:41-42`), AI mapping types (`AiFieldSuggestionService.java:43, 46`), front-end `type` props | Medium |

Total: 108 branch sites in 25 production files, plus schema files and front-end type unions.

### 2.9 Change inventory

| # | Change | Repo | Why |
| --- | --- | --- | --- |
| 1 | Internal upload path or machine identity with `CREATE_ROSTER`; system user email | api-layer | roster has human-only ingress |
| 2 | 57 new system fields tagged with the holding entity, in the existing file (option A) or a new file plus job type (option B) | api-layer | vendor columns have no fields; echoes cannot reuse practitioner fields |
| 3 | New `ResourceConfiguration` registry entry with crosswalk and filter logic | api-layer | unknown entity is dropped silently |
| 4 | Holding table changeset, DAL entity, DAL batch resource | core-data-access-layer | roster writes only through DAL `/batch` |
| 5 | Two templates per tenant per vendor, created and approved per tenant | ops, per tenant | templates are tenant-scoped, one sheet each |
| 6 | Vendor branch in list, detail, records, download, delete, cancel, reupload, search-by-roster | api-layer | no server-side hiding exists |
| 7 | Vendor branch in webhook, emails, BigQuery events, post-import actions | api-layer | all fire on job status |
| 8 | Exclusion from six field-catalogue endpoints, five screens, and the AI mapping payload | api-layer, frontend | catalogue is user-visible and leaves the platform |
| 9 | Validation bypass for the first upload; rule review for the second | api-layer | rules are hardcoded per field |
| 10 | Source name parameter through transaction context and eight lookups | api-layer | `roster:{tenantId}` hardcoded |
| 11 | Operation override `UPSERT_DATA` → `UPSERT`, slice rebuild from a ledger, removal semantics | api-layer + new ledger | vendor REMOVE has no representation |
| 12 | Survivorship prefix rule extended or source renamed | dal-mdm-survivorship-layer | `candor:` may never reach the operational value |
| 13 | Review of 108 job-type branch sites (option B) | api-layer | binary ternaries mis-route a fourth type |
| 14 | Pivot builder: approved per-address rows → roster wide row | new code | grain mismatch |
| 15 | Export leg, response correlation, dispositions, review APIs, rejection memory, Sync Latest, audit | new code | roster has none of these |

Items 1 to 14 are the price of reuse. Item 15 is the work that has to be written regardless.

---

## 3. Why this is not a good choice

- **Reuse buys the wrong capability.** Roster's core asset is a per-tenant template with a mapping UI, so that each customer can send their own file shape. The vendor file has one fixed shape for every tenant. One adapter class replaces two templates per tenant per vendor, and the mapping UI is never used by anyone for it.
- **Reuse covers about a fifth of the work.** Parse, map, stage, and job status come from roster. Export, correlation to what we sent, dispositions, review actions, rejection memory, ledger and release, and the audit trail are new either way (inventory item 15).
- **Every reuse point is a modification, not a call.** Items 1 to 14 are edits at hardcoded seams inside a 48k-line pipeline (`roster`, `roster_record`, `roster_subscribers`, `roster_upload` packages) that live customers use daily and that customer webhooks depend on. A regression ships to every tenant's roster screen.
- **Hiding by negative filter is fragile by construction.** Each new roster endpoint or report added later must remember the vendor exclusion. The first one that forgets shows vendor jobs to a customer.
- **The second upload fights the ratified release model.** Hardcoded source, merge-only operation, deprecated write path, relationship side effects, and the survivorship prefix rule all point the wrong way (section 2.7).
- **Schema churn lands in a shared file.** The vendor changed its column set three times in three months (July dictionary, MMO delivery, September dictionary). Under option A every revision edits the file that egress and the practitioner service load; under option B every revision still edits the packaged schema and redeploys `api-layer`.
- **Ownership.** Roster is another team's live feature. Every vendor change becomes a cross-team review inside their release train, on their regression risk.

---

## 4. What the roster runtime would bring along

Reusing roster means running vendor ingestion on roster's worker. This section describes that worker as deployed, with evidence, and what happens when many files arrive at once. Full treatment: `cos-docs/platform/roster-processing/roster-processing-architecture.md`.

### 4.1 Fixed concurrency of two files per stage

- Each worker instance leases one validation message and one ingestion message at a time: `gcp.pubsub.validation.max-concurrent-messages=1`, `parallel-pull-count=1`, same for ingestion (`api-layer/src/main/resources/application.properties:740-743`; applied at `roster/messaging/RosterValidationConsumer.java:149-156`, `RosterIngestionConsumer.java:146-153`). No profile override exists.
- Production runs `MIN_INSTANCES=2`, `MAX_INSTANCES=20`, `CPU=2`, `MEM=6Gi`, `--no-cpu-throttling` (`.github/workflows/deploy-production.yml:292, 333-336`). Staging is one instance (`deploy-roster-worker-staging.yml:117-120`).
- Effective throughput is `MIN_INSTANCES × 1` = **two rosters validating and two ingesting at any moment in production, one each in staging.**

### 4.2 The worker does not autoscale

- Cloud Run adds instances on incoming HTTP request concurrency, or on CPU when CPU is always allocated. It has no view of a pull subscription's backlog.
- The roster worker receives no HTTP traffic. Traffic is shifted to it on deploy (`deploy-production.yml:550`), but no client in `frontend` or `api-layer` calls it; it exists so its startup hook can open the subscribers (`RosterValidationConsumer.java:84, 126`).
- A single-threaded pull processing one file rarely holds CPU high enough to trigger CPU-based scale-out, and each new instance adds exactly one more slot. `MAX_INSTANCES=20` is a ceiling normal operation never approaches.
- The cost profile is inverted: two warm 6 GiB instances with CPU always on, all month, protecting a bursty workload, with no burst headroom gained.

### 4.3 Lease extension used as a lock

- Pub/Sub's acknowledgement deadline is a liveness signal: "the subscriber is still alive". The client library default cap on extending it is 60 minutes.
- Roster raises the cap to **3 hours for validation and 10 hours for ingestion**, extending in 600-second steps (`application.properties:745-749`; `RosterValidationConsumer.java:157-158`; `RosterIngestionConsumer.java:154-155`). The comment at `:744` still says 3 hours for both.
- Google's guidance for work measured in minutes or hours is to persist a job record, acknowledge quickly, and run the work in a job runner (Cloud Run Jobs, Batch, Dataflow, Workflows). Holding the broker's lease for the length of a business process turns a liveness signal into a distributed lock it was not designed to be.

**The heartbeat itself is a failure point.** The extension is not free; it is a background thread in the worker calling Pub/Sub's `modifyAckDeadline` on a timer, and every call has to succeed.

- Pub/Sub accepts at most 600 seconds per extension, and this code asks for exactly that (`min-ack-extension-period-seconds=600`). A file that takes 5 hours needs about 30 consecutive successful extension calls; a 10-hour ingestion needs about 60.
- Each call is a network round trip from a Cloud Run instance to Google's API. A transient network fault, an API timeout, a paused or CPU-starved instance, or a garbage-collection pause long enough to delay the timer past the deadline means one extension is missed.
- One missed extension is enough. Pub/Sub does not know the worker is busy; it knows only that the lease lapsed, so it treats the worker as crashed and redelivers. The worker thread that was mid-file keeps running; nothing tells it the lease is gone.
- The library has no retry-until-success guarantee on extensions and surfaces no signal to the handler. From the code's point of view the file is still being processed. From Pub/Sub's point of view it has been abandoned.
- For a job of 15 to 60 minutes, this is a small, bounded exposure and the pattern is acceptable. Stretched to hours, the number of heartbeats that must all land grows with the file, so the probability of at least one miss grows with the file. The longest files, which are the ones that hurt most to reprocess, are the ones most likely to be redelivered.

### 4.4 What happens after a lapsed lease: concurrent double processing

Two roads lead here: the 3-hour or 10-hour cap is reached, or a single heartbeat is missed before that. The result is the same.

- The lease expires mid-file and Pub/Sub redelivers the message to whichever instance next pulls. With two instances and one slot each, that is often the other instance, so both production slots are now working on the same file and every other tenant's roster waits.
- The validator's re-entry guard checks only `{VALIDATED, VALIDATION_FAILED, COMPLETED, FAILED}` (`roster_subscribers/service/RosterValidator.java:204-216`). `VALIDATION_IN_PROGRESS` is a real persisted status but is not in the guard set, so the second delivery passes.
- The second run calls `initializeSpannerRosterData(rosterId)` (`RosterValidator.java:224`), wiping the rows the first run is still writing, and starts from row one. Two validators now write the same job. Status writes are unconditional (`roster/repository/RosterRepository.updateRosterStatusById`), with no compare-and-set, lease, or heartbeat.
- The first run does not stop. When it eventually finishes and calls `ack()`, Pub/Sub ignores the call because that lease already expired (`RosterValidationConsumer.java:216` via `acknowledgeMessage`). The first run's hours of work are counted by nobody; the second run's result is the one Pub/Sub tracks, and it may itself miss a heartbeat and start a third.
- Ingestion has the same hole with worse effect: `RosterProcessor` guards only `{COMPLETED, FAILED}` (`roster_subscribers/service/RosterProcessor.java:141-152`), so redelivery re-executes DAL transactions against live practitioner records. Each row is a synchronous `POST /batch` (section 4.6), so a lapsed lease at hour four means four hours of practitioner writes replayed while the first run is still writing them.
- There is no defence in the code for either road: no in-progress guard, no compare-and-set on status, no heartbeat of the worker's own into the roster row that a second worker could check, no runtime budget that fails a file cleanly before the cap (`roster-processing-architecture.md` §9.3–§9.4).

### 4.5 No dead-letter queue; poison messages loop

- No roster subscription has a dead-letter topic. A repo-wide search finds dead-letter configuration only for webhook and anchor-date subscriptions (`application.properties:260, 1019-1021, 1045-1047`).
- Any exception other than "roster not found" calls `nack()` (`RosterValidationConsumer.java:221-235`; `RosterIngestionConsumer.java:188, 226`). Pub/Sub redelivers immediately. A file that fails deterministically occupies one of the two slots on every redelivery until the seven-day retention expires.
- The roster README claims dead-letter topics exist (`roster/README.md:434`). The code does not agree.

### 4.6 Ingestion writes one row at a time over HTTP

- In production, `roster.processing.use-pubsub` is `false` (`application.properties:786-788`). Each validated row becomes a synchronous DAL `POST /batch` call with three retries (`RosterRecordHandler.java:1697-1698, 1710`).
- The asynchronous row path exists (`roster-ingestion-rows-request` topic, `RosterRowResultConsumer`) but is gated behind a "prove stable in production" TODO (`application.properties:786`).

### 4.7 Cloud Run Job mode is not a fix as deployed

- A second dispatch mode starts one Cloud Run Job execution per roster (`roster/messaging/CloudRunIngestionDispatcher.java:25-26, 83-92`). It is live only on the internal environment and feature branches (`deploy-roster-worker-internal.yml:116-117`; `terraform/feature/cloudrun.tf:60-61`); production, staging, and demo run `pubsub`.
- It has no cap on parallel executions and no queue. N arrivals start N executions at once, with no shared limit on DAL calls.
- `runJobAsync` has no durable queue behind it. A failed dispatch call is lost unless someone republishes.
- Task count, parallelism, and timeout for the two jobs are console-managed, not in the repo (`deploy-production.yml:388-392, 408-412` update only the image).

### 4.8 Worked example: one hundred vendor files on cadence day

Assume 100 tenants on a monthly vendor cadence, all delivering on the same day, each response validated in about 30 minutes (a 20k-row file with per-row NPI, location, and address lookups) and ingested in about the same.

| Stage | Capacity | Queue behaviour | Wall clock |
| --- | --- | --- | --- |
| First upload, validation | 2 slots | 98 messages wait unleased in `roster-validation-sub`; drained first-in first-out | 100 files ÷ 2 slots × 30 min ≈ **25 hours** |
| Review | human | not a roster stage | days |
| Second upload, ingestion | 2 slots | same drain, one synchronous DAL call per row | another ≈ **25 hours** of serial DAL traffic |

- Customer rosters uploaded that day sit in the same two slots. There is no priority, no size-based routing, and no per-tenant fairness (`roster-processing-architecture.md` §8.3). A payer's real roster waits behind vendor files, or vendor files wait behind a customer's huge roster.
- Any single file over 3 hours enters the double-processing case in 4.4. Any file that fails on parse enters the poison loop in 4.5 and takes one slot with it.
- The DAL is a shared bottleneck in either design. The difference is that roster gives no place to put a global concurrency limit: two slots is the only knob, and it is a floor of cost as much as a ceiling of throughput. The directory accuracy design puts that limit on a Cloud Tasks queue (`max_concurrent_dispatches`), which is configuration, and scales its worker to zero between cadence days.

---

## 5. Where roster and vendor ingestion overlap, and where they do not

| Dimension | Roster | Vendor accuracy ingestion |
| --- | --- | --- |
| Direction | One-way inbound: customer sends us their data | Round trip: we export a selected population, the vendor grades it, we ingest the grades |
| Who authors the file | Each tenant, in their own shape | One vendor, one fixed shape for all tenants |
| Schema variability | Per tenant, hence templates and a mapping UI | Per vendor version, hence one adapter per vendor |
| Trigger | A person clicks upload with a JWT | An object appears in a vendor bucket; no person |
| Cadence | Ad hoc, one tenant at a time | All tenants in a window, monthly |
| Row grain | One row per practitioner, repeated column groups for multiplicity | One row per practitioner per address |
| Row meaning | Values to store | Verdicts about values: status, reason, confidence tier, evidence array, suggested value |
| Human step | Optional: fix rows that failed validation, then approve the whole job | Mandatory: approve, reject, or skip every row; rejections feed a suppression memory |
| Correlation to prior output | None; roster never sends anything out | Every row must attach to the export row it grades (`gpl_id`, staleness check against exported values) |
| Write semantics | Whole-entity merge, `UPSERT_DATA`, under `roster:{tenantId}` | Per-attribute release; full slice rebuilt from a ledger and replaced with `UPSERT` under `candor:{tenantId}` |
| Side effects | Group, location, network relationships; NPPES check; credentialing start and stop; terminations | None: a corrected phone number is a corrected phone number |
| Downstream consumers | Customer webhooks, status emails, dashboards | Reviewer queue only |
| Memory across runs | None | Rejected recommendations suppressed on re-send; export registry compared on arrival |
| Shared vocabulary | Practitioner field names | The same field names, because every MDM source shares the operational-value vocabulary |

The last row is the whole overlap. Shared field names are not shared workflow; NPPES, CAQH, the tenant UI, the portal, and roster all carry the same fields and nobody proposes running them through one pipeline. The reusable part of roster is its file parsing, and that is two library dependencies (Apache Commons CSV, fastexcel) and three thin wrapper classes with no roster coupling (`utils/file_storage/`: `FileParserInterface`, `CsvFileParserService`, `XlsxFileParserService`).

---

## 6. Why a separate service is the right shape

- **Resource class.** Ingestion is idle most of the month, then parses files that can approach a million rows. Review is small, always-on, millisecond reads. Roster's answer is two 6 GiB instances warm all month with no burst capacity. A scale-to-zero worker plus a small API pays for the burst only when it happens.
- **Credentials boundary.** Vendor files are untrusted external input with vendor-bucket credentials attached. In roster they would be parsed inside `api-layer` with `api-layer`'s full permissions. A separate worker gets a service account scoped to the vendor bucket, its own database, and the release call.
- **Release cadence.** The vendor adapter is the highest-churn code in the program (three schema revisions in three months). Inside roster, every adapter fix redeploys the pipeline every customer uses. Inside any other always-on backend, it redeploys an unrelated user-facing surface. Alone, it redeploys itself.
- **The transactional test.** A service split is wrong when the two sides share a transaction or a chatty synchronous call. Between vendor ingestion and roster there are none: no shared table, no shared transaction, no synchronous call on any latency path. The inputs are files and events; the release write is one HTTP hop to the DAL in every design.
- **Structural safety over negative filters.** In its own service the vendor job cannot appear in a roster list, fire a roster webhook, or open a credentialing workflow, because none of that code is present. Nothing has to remember to exclude it.
- **Failure isolation.** A malformed vendor file in roster loops on one of two shared slots and can double-process past the lease cap. In its own service it lands in a dead-letter queue with the file untouched in the bucket, chunked work retries by deterministic task name, and no customer roster is delayed.
- **Reversibility.** Folding a clean small service back into `api-layer` later is mechanical. Extracting a fourth job type out of roster after it ships means unpicking 108 branch sites, a shared schema file, webhook mappings, and per-tenant templates.
- **Reuse still happens, as copy not runtime.** The new service declares Commons CSV and fastexcel itself, copies the three parser wrappers, and copies the patterns worth having: per-row-status staging, streaming batch reads, pointer messages with the file left in GCS, trace-id propagation across the queue hop. A shared library for three small files would recreate the deploy coupling this decision removes.
- **Conceded cost.** One more repo, pipeline, service account, dashboard, and on-call entry. The smart-outreach service already established that pattern on the same rails, so most of it is a template, not an invention.

**Recommendation.** Build vendor ingestion as part of the standalone directory accuracy service: one bounded context, two deployables (`-api`, `-worker`), one database, parsers copied from roster, nothing shared at runtime. Record "reuse the roster pipeline via two uploads" as a rejected alternative with this document as its evidence.

---

## 7. Evidence index

**`api-layer/src/main/java/com/certifyos/api_layer`**

- `roster/resource/RosterResource.java:70-71, 231-295, 302, 330, 549-673, 802, 943, 1294-1295` — upload auth and params, list passthrough, schema endpoint, silent job-type default, internal test route.
- `roster/resource/TemplateResource.java:108, 270-271, 1073-1074, 1241-1242, 1292, 1312-1313` — template creation, approval, field catalogue endpoints.
- `roster_upload/resource/RosterUploadResource.java:79-80, 184, 494-495, 591` — column mapping and system-fields endpoints.
- `egress_template/resource/EgressTemplateResource.java:485-486, 517`; `egress_template/service/EgressFieldCatalogService.java:29-33, 41` — egress catalogue from the same file.
- `roster/service/JsonSchemaGeneratorService.java:47-49, 163-197, 585-587, 616-620, 649` — schema paths, generation, skip and required, entity retarget.
- `roster/service/FieldMatchingService.java:51-53, 152-154`; `roster/service/ActionTypeValidationService.java:29-31`; `roster/service/TenantRosterSchemaService.java:24-27` — path constants and per-tenant selection.
- `roster/service/ResourceConfiguration.java:261-263, 270, 279, 308, 333, 356, 603, 1020, 1051, 1993` — registry, default operation, roster-source filters, relationship multiplication.
- `roster/service/TransactionOrchestrator.java:512-519, 2555-2558` — the one real switch; silent drop on unknown entity.
- `roster/service/RosterRecordHandler.java:961, 1361, 1696-1710, 2010-2016` — hardcoded source, deprecated batch, DAL call.
- `roster/service/BusinessValidationService.java:96-134, 662, 2271, 2844, 2913, 3086, 5404` and 44 `FACILITY` ternaries — rule switches, source concatenation.
- `roster/service/RosterWebhookService.java:203-222`; `roster/service/RosterPostImportActionService.java:55-58`; `roster/service/UserPermissions.java:37-52`.
- `roster/service/TemplateService.java:823, 895-898`; `schema_mapping/AiFieldSuggestionService.java:21, 43, 46, 116-143` — AI mapping payload.
- `roster/messaging/RosterValidationConsumer.java:84, 126, 149-158, 221-235`; `roster/messaging/RosterIngestionConsumer.java:146-155, 188, 226`; `roster/messaging/CloudRunIngestionDispatcher.java:25-26, 83-92`.
- `roster_subscribers/service/RosterValidator.java:204-224, 750-759`; `roster_subscribers/service/RosterProcessor.java:141-152`.
- `practitioner/dto/PractitionersGetFilter.java:270-275`; `common/logging/RosterEventType.java`.
- `roster/model/RosterJobType.java`; `roster/model/RosterJobStatus.java:3-16`; `roster_record/model/RosterRecordStatus.java:3-31`.

**`api-layer/src/main/resources`**

- `schemas/roster-system-fields.json` — 422 properties; `firstName`, `practitionerNpi`, `groupPracticeLocationAddressLine1` entries.
- `application.properties:243-245, 260, 450-453, 740-749, 786-788, 1019-1021, 1045-1047, 1579-1581`.

**`api-layer` deploy**

- `.github/workflows/deploy-production.yml:168, 292, 321, 333-336, 388-412, 550`; `deploy-roster-worker-staging.yml:117-120`; `deploy-roster-worker-internal.yml:116-117`; `terraform/feature/cloudrun.tf:60-61`.

**Other repos**

- `core-data-access-layer/src/main/resources/db/changelog/roster-templates/001-create-roster-templates.yaml:16-19, 40-44`.
- `dal-mdm-survivorship-layer/src/main/java/com/certifyos/survivorship/common/repository/AbstractOVRepository.java:396-404, 813`.
- `frontend/apps/web/features/roster/components/roster-template-scratch-builder/roster-template-scratch-builder.tsx:492, 499`; `features/egress-templates/components/egress-template-builder/egress-template-builder.tsx:96`.

**Companion analysis**

- `cos-docs/platform/roster-processing/roster-processing-architecture.md` §8–§10 — scaling model, lease extension, reliability table.
- `cos-docs/directory-accuracy/reference/candor-sept-2026-sample-analysis.md` — sample sheet structure and issues C1–C8.
