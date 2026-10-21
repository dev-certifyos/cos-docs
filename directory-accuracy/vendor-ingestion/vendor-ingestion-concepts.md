# Companion — Vendor ingestion module (concepts + supplementary material)

> Two parts. **Part 1 — Concepts:** plain-language explanations of every service, tool, and concept named in `vendor-ingestion.md`, for a reader new to the stack. **Part 2 — Supplementary material:** the analysis behind the module doc — baseline evidence with receipts, approaches scored against criteria, traceability, inputs and outputs, anticipated questions, open questions with owners, ticket impact. The module doc stands alone; this file is preparation and defense material.

## Part 1 — Concepts

### Directory accuracy service

- The one service that holds vendor export, vendor ingestion, and later attestation as modules: one repository, one deployment, one database.
- Deployed as two GCE instance groups, `directory-accuracy-api` (HTTP) and `directory-accuracy-worker` (JobRunr). Module 1's doc called them `vendor-export-api` and `vendor-export-worker`; same groups, renamed with the service.

### Pub/Sub topic

- A named channel in Google Cloud Pub/Sub; publishers send messages to it, subscribers read from it.
- A topic holds nothing by itself; a subscription keeps each message until a subscriber acknowledges it.
- Delivery is at least once: the same message can arrive more than once, so the reader must handle repeats.

### Cloud Storage notification configuration

- A setting on one bucket that tells Cloud Storage to publish a Pub/Sub message when an object in it changes.
- Can be limited by event type (for example `OBJECT_FINALIZE`) and by object name prefix (for example `to/`).
- Each bucket needs its own configuration; many buckets can point at the same topic.

### `OBJECT_FINALIZE`

- The event Cloud Storage raises when a new object is fully written, or an existing one is overwritten.
- Raised only after the write succeeds, so a half-uploaded file never triggers it.

### Cloud Storage service agent

- A Google-managed service account, one per project, that Cloud Storage acts as when it publishes notifications.
- Email form: `service-<project number>@gs-project-accounts.iam.gserviceaccount.com`.
- Needs `roles/pubsub.publisher` on the target topic, or notifications fail to publish.

### Push subscription

- A Pub/Sub subscription that sends each message as an HTTP POST to an endpoint we name, with an identity token the endpoint can verify.
- A `2xx` response acknowledges the message; anything else makes Pub/Sub retry with backoff.
- After a set number of failed deliveries the message is moved to a dead-letter topic (a holding topic for messages that could not be processed), where an operator can inspect and replay it.

### At-least-once delivery

- Pub/Sub guarantees a message is delivered, not that it is delivered once; the same message can arrive twice, and two subscribers can receive it at the same moment.
- A receiver must make its work safe to repeat; a unique database key is the usual way.

### Insert-if-absent claim

- Writing a row whose primary key is the thing being claimed; the database accepts the first writer and refuses every later one with a duplicate-key error.
- Atomic by construction, unlike "read, see nothing, then write", which two writers can both pass.

### JobRunr job

- A unit of background work stored in our MongoDB and run by the worker group; JobRunr retries it with backoff, keeps failed ones with their exception, and shows all of them in a dashboard.
- A job is enqueued by the handler and identified by the row it works on, so a retry reads the row's current state instead of trusting the original request.

### Archive before request

- Copying the exact uploaded object to a bucket we own before anyone parses it.
- The vendor can delete or overwrite in `to/`; the archive is the unchanging copy the audit trail and file-ingestion both read.

### The file-ingestion service (`file-ingestion-service`)

- A separate CertifyOS service (Quarkus, GCE api and worker groups, JobRunr, MongoDB) that takes a CSV or XLSX through a signed upload, parses it, stores the rows, and publishes a completion event per consumer.
- Flow for a caller today: register (`POST /v1/ingestion`, get an `ingestionId` and a signed upload URL), upload the bytes, wait for the event, read the rows (`GET /v1/ingestion-rows/{id}`), optionally mark consumed. This module asks for a `sourceUri` variant where file-ingestion copies the object itself (Rollout ask 9).
- `externalRef` is a caller-supplied id; a repeated register with the same ref returns the existing ingestion instead of making a second one.
- Mapped mode: a mapping (file column → target column plus rules such as `required`, `enum`, `date`) is checked against the header before any row is read; rows that fail a rule are stored as `INVALID` with the failing field named.
- Statuses: `REGISTERED → RECEIVED → VALIDATING → PARSING → COMPLETED | PARTIAL | INVALID`, then `CONSUMED`; a registration never uploaded becomes `EXPIRED` after 24 hours.
- Each consumer has its own completion topic, `file-ingestion-completed-<identifier>`, and a retention period for its rows (30 days by default).

### Server-side copy

- Cloud Storage copies an object from one bucket to another inside Google's network; the caller sends a command, not the bytes.
- File-ingestion uses it to pull our archived file into its own bucket at register time, so nothing streams through this module and there is no upload link to expire.

### Consumer (file-ingestion)

- A named caller of file-ingestion, registered once; the name travels as the `identifier` header, names the completion topic, and scopes every ingestion and row read.
- This module is the consumer `vendor-ingestion`.

### Conditional array push

- A MongoDB update that adds an element to an array only when no element with the same key exists, in one atomic operation.
- Used as the claim: the first handler to push wins; every later push matches nothing.

### Wide row and field recommendation

- The vendor's file has one wide row per practitioner and location: every field appears as a group of columns (our value, the vendor's status, the vendor's value, reason, confidence, evidence). Practitioner fields such as name and specialties repeat on every location row of that practitioner.
- A field recommendation is one of those field groups pulled out as its own document, when the vendor marked it `INVALID`: one practitioner, one location when the field belongs to a location, one field. The reviewer decides per document.

### Guarded update

- A database update whose filter includes the state (and version) the caller expects; if the row already moved, the filter matches nothing and the caller stops.
- Used for every state change on an inbound file entry and for every review decision, so two workers or two reviewers can never act on the same row twice.

### Keyset pagination

- Paging where the client sends "give me rows after this one" with the last row's sort key, instead of a page number.
- Stable under change: when rows leave the pending list as they are decided, the next page neither skips nor repeats rows.

### Normalised value

- The vendor's value rewritten by a fixed rule so that two spellings of the same thing compare equal: `(303) 679-0177` and `303-679-0177` both become `3036790177`.
- The rule set has a version number; rows remember which version made them, so a rule change never silently changes what an old row means.

### Rejected-value key without a batch id

- `vendor_rejected_values._id` is tenant, practitioner, location, field, and a hash of the normalised value. The export batch is left out on purpose, so a value rejected in October and sent again in December lands on the same row.
- The row counts rejections and keeps first and last time, who, why, and which batch, so "was this rejected before, and when" is one read from any later document's own fields.
- The value is hashed in the key only because it can be long or contain the `|` separator; the readable value stays on the row.

### Slice, operational value, survivorship

- Every source (roster, the tenant UI, Candor) writes its own copy of a practitioner, called a slice, keyed by source and crosswalk id.
- Survivorship reads all slices of one practitioner and picks, field by field, the value from the highest-ranked source for that tenant; the result is the operational value, the Golden record. The `contribution_map` on it says which source won each field.
- A whole-slice write (`UPSERT`) replaces the slice's data; a merge write (`UPSERT_DATA`) adds to it and never removes.

### Read-merge-write of our own slice

- Before each release write, the job reads the practitioner's current `candor:{tenantId}` slice from the DAL, overlays the newly approved fields, and writes the whole slice back in one go, so one write never erases what an earlier release put there.
- The DAL has no version number on a slice; its lock compares the row's `updated_at`, applies only to non-atomic batches, and gives up with a `500` after ten retries. The job therefore does not rely on it: one release job per practitioner runs at a time, and the batch is atomic.

### Crosswalk id, and why the slice carries identity fields

- A crosswalk id is the key a source uses for its own copy of an entity. The platform's recipes: practitioner = NPI; location = normalised name + a hash of the service address; address = a hash of the address lines.
- Matching does not join a slice to the Golden record by crosswalk id. For a practitioner it needs the same `data.npi` and names that sound the same; for a location it needs the same name and the same service address.
- That is why a `candor:` practitioner slice always carries `npi`, `firstName`, `lastName` (the tenant UI seeds a new slice with exactly those), and why a corrected location name or address cannot be released as our own slice today: the corrected value no longer equals the Golden one, and matching would create a new location. Those are edited in place through the same api-layer call the UI uses.

### Junction row, and why it is edited in place

- A junction row links two records: `GroupPractitionerLocation` links a practitioner-in-a-group to a group-location and holds practitioner-at-location values (`accepting_new_patients`, `pcp_at_location`); specialty links hold `specialties`. Junction rows have no source and no survivorship.
- The directory read returns one row per junction row it can reach from the practitioner's contributing slices. If this module built its own chain of junction rows under its slice, the same location would show twice, with no rule to choose between the two values.
- So junction fields are written into the existing row through the api-layer endpoint the tenant UI uses. The record of who changed it is our field document; the next roster file can overwrite the value, exactly as it overwrites a UI edit today.

### Operational value generated event

- Survivorship publishes `mdm.slice.ov_generated` after it writes an operational value, naming which source's slice triggered the run.
- It is the right moment to read the result back. The DAL's `mdm.slice.changed` fires earlier, when the slice is written, before cleansing, matching, and survivorship have run.

### Object generation

- A number Cloud Storage assigns to each write of an object; an overwrite gets a new generation.
- Bucket, object name, and generation together identify one specific upload.

### `field_recommendations` — the review table, as it sits in the database

- One document per field the vendor marked `INVALID`: one practitioner, one location when the field belongs to a location, one field, one batch. Status is kept per field, so two fields of the same practitioner can be in different states.
- Fields the vendor marked `VALID` are not documents; they are counted on the inbound-file entry, and the archived file is the record. The `NOT_REQUIRED` rows in the sample below are shown for completeness only.
- `location` is `null` for practitioner-level fields (`npi`, `first_name`, `last_name`, `specialties`) and filled for location-level fields (`location`, `practice_name`, `phone`, `fax`, `accepting_new_patients`, `pcp_at_location`).
- `_id` = `tenant | source | export batch | practitioner | location or - | field`. A re-stage of the same file lands on the same document.

**Keys, in plain words:**

| Key | Meaning |
| --- | --- |
| `tenantId`, `sourceType`, `source` | Which tenant; `vendor` today, `attestation` later; `candor` today |
| `exportBatchId` | Which export this answers; equals `vendor_export_batches._id` |
| `practitioner` | `{ certifyPractitionerId, npi }` — who the statement is about, nothing else |
| `location` | `{ certifyLocationId }` or `null` |
| `field` | Which field; `location` being `null` or not tells the level |
| `sentValue`, `vendorValue` | What we sent; what the vendor says it should be (`null` when the vendor says the thing does not exist) |
| `vendorStatus`, `vendorReason`, `vendorConfidence`, `vendorEvidence` | The vendor's verdict as received: `VALID` or `INVALID`, reason ids, confidence tier, evidence array |
| `reviewStatus` | `PENDING`, `SKIPPED`, `APPROVED`, `REJECTED`, `RELEASE_REQUESTED`, `RELEASING`, `RELEASED`, `RELEASE_FAILED` |
| `reviewHistory` | Every decision in order: `{ action, by, at, comment }`; the last entry is the latest decision; comment is mandatory on reject |
| `version` | Increments on every write; the decision endpoint must send the version it read |
| `goldenRecordUpdate` | After release: `{ releaseId, outcome WON / OUTRANKED / NOT_APPLIED, winningSource, releasedAt }` |
| `sourceFile` | `{ uploadId, sheet, row }` — which upload and which row produced it; `uploadId` matches an `inboundFiles[]` entry |
| `stagedAt`, `updatedAt` | Timestamps |

**Sample rows — two practitioners, one batch.** Columns identical on every row are left out: `tenantId = org-xyz`, `sourceType = vendor`, `source = candor`, `exportBatchId = org-xyz-candor-2026-10-001`. Every `_id` starts with `org-xyz|candor|org-xyz-candor-2026-10-001|`, cut for width. `vendorEvidence` and `sourceFile` omitted for width.

| _id (tail) | practitioner | location | field | sentValue | vendorValue | vendorStatus | vendorReason | vendorConfidence | reviewStatus | reviewHistory | version | goldenRecordUpdate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cert-000123\|-\|first_name` | `{cert-000123, 1003002890}` | `null` | first_name | Daniel | Danny | INVALID | [direct_outreach] | HIGH | APPROVED | [{APPROVE, reviewer-7, 2026-10-20T14:02:11Z}] | 2 | null |
| `cert-000123\|-\|last_name` | `{cert-000123, 1003002890}` | `null` | last_name | Hamman | Hammon | INVALID | [health_system_verified] | MEDIUM | REJECTED | [{REJECT, reviewer-7, 2026-10-20T14:03:30Z, NPPES confirms Hamman}] | 2 | null |
| `cert-000123\|-\|specialties` | `{cert-000123, 1003002890}` | `null` | specialties | ["Orthopedic - Hip And Knee"] | ["Orthopedic - Hip And Knee"] | VALID | [] | HIGH | NOT_REQUIRED | [] | 1 | null |
| `cert-000123\|loc-7781\|location` | `{cert-000123, 1003002890}` | `{loc-7781}` | location | 5351 S Roslyn St, Greenwood Village, CO 80111 | null | INVALID | [no_evidence_health_system] | HIGH | SKIPPED | [{SKIP, reviewer-3, 2026-10-18T09:10:00Z}] | 2 | null |
| `cert-000123\|loc-7781\|phone` | `{cert-000123, 1003002890}` | `{loc-7781}` | phone | (303) 679-2070 | (303) 679-2070 | VALID | [direct_outreach] | VERY HIGH | NOT_REQUIRED | [] | 1 | null |
| `cert-000123\|loc-7781\|practice_name` | `{cert-000123, 1003002890}` | `{loc-7781}` | practice_name | AdventHealth | Greenwood Village | INVALID | [direct_outreach] | VERY HIGH | RELEASED | [{APPROVE, reviewer-7, 2026-10-20T14:02:50Z}, {RELEASE, reviewer-7, 2026-10-20T14:05:40Z}] | 3 | `{rel_01HX…, WON, candor:org-xyz, 2026-10-20T14:06:12Z}` |
| `cert-000987\|-\|first_name` | `{cert-000987, 1114567893}` | `null` | first_name | Priya | Priya | VALID | [] | HIGH | NOT_REQUIRED | [] | 1 | null |
| `cert-000987\|-\|specialties` | `{cert-000987, 1114567893}` | `null` | specialties | ["Cardiology"] | ["Cardiovascular Disease"] | INVALID | [health_system_verified] | HIGH | PENDING | [] | 1 | null |
| `cert-000987\|loc-5520\|phone` | `{cert-000987, 1114567893}` | `{loc-5520}` | phone | (212) 555-0100 | (212) 555-0177 | INVALID | [direct_outreach] | VERY HIGH | APPROVED | [{APPROVE, reviewer-7, 2026-10-20T15:01:00Z}] | 2 | null |
| `cert-000987\|loc-5520\|accepting_new_patients` | `{cert-000987, 1114567893}` | `{loc-5520}` | accepting_new_patients | true | false | INVALID | [direct_outreach] | VERY HIGH | REJECTED | [{REJECT, reviewer-3, 2026-10-20T15:04:20Z, Office says still accepting}] | 2 | null |
| `cert-000987\|loc-5520\|fax` | `{cert-000987, 1114567893}` | `{loc-5520}` | fax | (212) 555-0101 | (212) 555-0101 | VALID | [direct_outreach] | VERY HIGH | NOT_REQUIRED | [] | 1 | null |
| `cert-000987\|loc-6031\|phone` | `{cert-000987, 1114567893}` | `{loc-6031}` | phone | (718) 555-0400 | (718) 555-0411 | INVALID | [direct_outreach] | HIGH | PENDING | [] | 1 | null |

**Reading the rows:**

- Practitioner 1003002890 has six documents: three with `location = null`, three for `loc-7781`; its fields sit in five different states at once.
- Practitioner 1114567893 has two locations, so `phone` appears twice with different `location` values and different states.
- The `loc-6031` phone is `PENDING` even though the same number was rejected last cycle: nothing is hidden in MVP; the earlier rejection sits in `vendor_rejected_values`.
- `location` as a field (`cert-000123|loc-7781|location`) is the vendor saying the location itself is not valid; `vendorValue` is `null`.

**Indexes:**

| Index | Serves |
| --- | --- |
| `{ _id: 1 }` | staging upserts; decision by id |
| `{ tenantId: 1, reviewStatus: 1, field: 1, stagedAt: -1, _id: 1 }` | review list with filters and keyset paging |
| `{ tenantId: 1, exportBatchId: 1, reviewStatus: 1, field: 1 }` | per-batch counts |
| `{ tenantId: 1, "practitioner.certifyPractitionerId": 1, exportBatchId: 1 }` | one practitioner's full set |
| `{ tenantId: 1, "practitioner.npi": 1 }` | search by NPI |

# Supplementary material

## 1. Baseline — verified current behavior

## 2. Approaches analyzed in depth

## 3. Requirements traceability

## 4. Inputs and outputs of this module

## 5. Design rationale — anticipated questions

## 6. Open questions and sign-offs

| # | Question | Owner | Blocks |
| --- | --- | --- | --- |
| Q1 | Real vendor bucket names per environment, and whether one bucket per tenant or one per vendor | DevOps | Step 1 setup |
| Q2 | Which project hosts `central-vendor-ingestion-events` when vendor buckets live in a different project than this service | DevOps; Engineering | Step 1 setup |
| Q3 | Completeness signal: data file only, manifest-last, or sidecar (v2 O-1 §2.4) | Contract call | `completenessSignal` default; whether step 2 parks data-file events |
| Q4 | Does the SFTP platform commit a file once on close, or can one upload produce several `OBJECT_FINALIZE` events (flush mid-transfer) | DevOps (SFTP platform owner) | Whether the partial-then-complete case is real or theoretical |
| Q5 | Files answering a `SUPERSEDED` export batch: ingest and flag, or hold for an operator | Product | Step 2 matching rule |
| Q6 | A second upload for a batch already processed (correction under a new `vendorBatchId`, or a complete file after a partial one): never processed, or processed later; and if processed, whether it replaces or adds to the first upload's unreviewed rows | Product | First-upload rule (D4); review queue shape |
| Q7 | Archive bucket: one per environment shared by all vendors, or one per vendor | DevOps; Engineering | Rollout ask 4 |
| Q8 | **Answered 2026-10-16:** file-ingestion stores rows in its own MongoDB for the consumer's retention and exposes them through `GET /v1/ingestion-rows/{id}`; it has no extension point. This module's row rules run in `StageRowsJob` over that stream (D10) | — | — |
| Q9 | **Answered 2026-10-16:** `filename` is a name, not a path; the file is uploaded to a signed URL file-ingestion returns; register answers at once; status is `GET /v1/ingestion/{id}` (D7, D8) | — | — |
| Q10 | **Answered 2026-10-19:** the vendor must echo `certify_practitioner_id` and `certify_location_id` on every row; staging keys documents by them and does no lookup; a row without them is `MISSING_IDS` (Rollout ask 5) | — | — |
| Q11 | **Answered 2026-10-16:** the vendor returns only the fields we sent; no vendor-only fields and no discovered locations in the split rule (D15) | — | — |
| Q12 | **Answered 2026-10-16:** skip is status tracking only; a skipped finding can be approved or rejected later and has no effect on the next export (D12) | — | — |
| Q13 | Rejected-value normalisation version 1 is written in Contracts step 7; confirm the abbreviation table for addresses with Product before build | Engineering; Product | `vendor_rejected_values` key |
| Q18 | Lifetime of `field_recommendations`: 30-day TTL after a batch is fully decided, or cleared when the next export's results arrive for the tenant | Product; Engineering | Data model retention; D13 |
| Q19 | When to switch on readers of `vendor_rejected_values`: the `hideRejected` list filter, and module 1's export exclusion | Product | Step 7 later uses |
| Q15 | Own-slice route for corrected `practice_name` and address later (Rollout ask 15); MVP edits the existing location slice in place as the tenant UI does | Matching team; DAL team | Step 8 (D22) |
| Q16 | Removals (`INVALID` on `location` or `npi` with no replacement): nothing written in MVP; Terminations route per Rollout ask 14 | Product; Terminations owners | Step 8 |
| Q17 | Read-back source of truth: the DAL practitioner read with `contribution_map`, or survivorship's `GET /events/ov/core-practitioner/{crosswalkId}/{sourceType}` | Engineering | Step 8 read back |
| Q20 | Sourced model for `accepting_new_patients`, `pcp_at_location`, `specialties` later (Rollout ask 16); MVP edits the existing junction row in place through the UI's endpoints | DAL team; Product | Step 8 (D22) |
| Q22 | Does a survivorship run triggered by one `candor:` slice write carry only that source in `triggeringSourceTypes`, so the subscription filter `hasPrefix(attributes.triggeringSourceTypes, "candor:")` holds (Rollout ask 18) | Survivorship team | Step 8 read back |
| Q23 | Source naming for the next vendor: `<source>:{tenantId}` per vendor is decided (D21); whether `core_sources.tags` should carry `vendor` to group them | DAL team | Rollout ask 11 |
| Q21 | Location slice contents: every Golden contact entry plus the corrected phone and fax, or only the corrected entries (depends on how survivorship merges `contacts[]`, Rollout ask 17) | Survivorship team | Step 8 merge |
| Q14 | File-ingestion `retentionDays` for consumer `vendor-ingestion`: 30 default is ample for a staging job that runs within minutes; confirm the alert threshold for `PARSED` not `STAGED` | Engineering | Rollout ask 6; Observability |

## 7. Ticket impact
