# Directory Accuracy — why vendor ingestion is not built on the roster pipeline

Talking-points version. Full write-up with every file and line pointer lives in `reference/roster-pipeline-reuse-analysis-detailed.md`. Repos: `api-layer`, `core-data-access-layer`, `dal-mdm-survivorship-layer`, `frontend`. Vendor sample: `MVP_09022026_Sample.xlsx` (September 2026, two sheets).

---

## 1. The question

- Can the existing roster pipeline carry Candor's accuracy verdicts into a review queue and then into the golden record? And should it?
- Short answer: it can, with two roster uploads per vendor delivery, but it costs about fifteen changes across four repos, buys us only the file parser, and inherits a worker that does not scale or recover well. Build it as its own service.

---

## 2. How reuse would work (the two-upload idea)

- Vendor drops a file on SFTP. A job uploads it to `POST /roster` as a new kind of job.
- Roster parses it and writes rows to a **holding table** instead of practitioner tables. Job is hidden from the roster UI by a special status.
- A reviewer approves, rejects, or skips rows in a new review screen.
- A second job turns approved rows into a normal practitioner roster file and uploads it again. This time roster writes into `core_practitioners` under a source like `candor:{tenantId}`.

---

## 3. What we would have to change to make that true

- **Getting the file in**
  - Roster has one entrance: a logged-in human with `CREATE_ROSTER` permission, a JWT, and an email recorded as creator. No service-to-service upload exists.
  - Roster reads one sheet per job. Vendor workbook has two sheets, so every delivery becomes two jobs or a pre-split.
  - Need: an internal upload path or machine identity, plus a system "user".

- **Teaching roster the vendor columns**
  - Roster only knows fields listed in `roster-system-fields.json` (422 fields today). Each field is tagged with the entity it writes to.
  - Vendor's verdict columns (`_verification_status`, `_candor_value`, `_confidence`, `_evidence`) have no fields at all. The echo columns (`first_name`, `npi`) do, but they are tagged to write straight into the practitioner record, which is exactly what the first upload must not do.
  - Need: **57 new system fields** tagged to a holding entity. Two ways to add them, both bad:
    - Option A, add them to the shared file: every roster admin, egress template, and the AI mapping service sees fields like "First Name Verification Status".
    - Option B, new file plus a fourth job type: 108 branch sites across 25 files must be reviewed (see below).
  - Tagging a field with a new entity does nothing by itself. Destinations are a hardcoded registry of 78 resources in `ResourceConfiguration.java`. An unknown entity is **skipped silently**, no error.
  - Roster writes only through the DAL `/batch` endpoint, so the holding table also needs a Liquibase changeset, a DAL entity, and a DAL batch resource.
  - Templates are per tenant, no global flag. One vendor schema becomes two templates per tenant, created and approved per tenant.

- **Hiding vendor jobs from customers**
  - Roster has no server-side status filter. List endpoints pass the client's filter through; the client decides what to show.
  - Every surface needs a "not vendor" branch: roster list, detail, records, download, delete, cancel, reupload, search-by-roster, customer webhook `roster_job.status.changed`, three status emails, BigQuery dashboard events, and post-import credentialing actions.
  - Miss one and a customer sees vendor jobs, or their webhook gets vendor events, or a `START` row opens a credentialing workflow.

- **Who else sees the system-field file**
  - Six user-facing endpoints return it, five front-end screens render it, and template creation sends every field name and description to an external AI mapping service, on by default in production.
  - Under option A, vendor fields leak into all of those. Under option B, each one must be excluded by hand.

- **Validations**
  - Two layers, neither switchable per template or per job: schema rules from field definitions, and a 5,569-line business validation service with about 40 hardcoded rules.
  - First upload needs a bypass but must still resolve NPI to a practitioner. Second upload runs the full rule set on reviewer-approved values, so a vendor-corrected address can fail the location-hash rule and block an approved change with no path back to the reviewer.

- **The second upload fights the write model** (this is the worst fit)
  - Source name `roster:{tenantId}` is concatenated in eight places and read back in five lookups. Writing as `candor:` means threading a source through everything.
  - Default operation is `UPSERT_DATA`, a deep merge where arrays only grow. A vendor recommendation to **remove** a location cannot be expressed. Our agreed release model rebuilds the slice from a ledger and replaces it, the opposite default.
  - The batch write path is `@Deprecated(forRemoval = true)`; its replacement is built but off in production.
  - Survivorship treats a source as tenant-specific only if its name starts with `tenant` or `roster`. A `candor:` slice may never reach the operational value.
  - Practitioner writes carry side effects: group, location, network, address relationships; NPPES check; credentialing start and stop. None wanted for a corrected phone number.
  - Grain mismatch: roster is one wide row per practitioner; vendor rows are one per practitioner per address. Approved rows must be pivoted back.

- **A fourth job type touches 108 branch sites**
  - One real `switch` the compiler would catch.
  - About 25 three-way `if/else` chains, silent fall-through.
  - About 80 binary `== FACILITY ? a : b` ternaries; a fourth type silently becomes practitioner. 44 of those are in the business validation service alone.
  - Two places default to `PRACTITIONER` silently.

- **Change inventory, in one breath**
  - 14 modifications inside roster and DAL and survivorship to make reuse possible, plus item 15, the work we write regardless: export leg, response correlation, dispositions, review APIs, rejection memory, Sync Latest, audit.

---

## 4. Why that is a poor choice

Take each change from section 3 and ask what it does to roster.

- **We are changing roster, not using it**
  - None of the changes in section 3 call roster. All of them edit roster.
  - Roster was built for one job: customer uploads their practitioner file. Job types, entity list, source name, validations, status handling are all hardcoded for that job.
  - Making it generic is a rewrite of its core. That is a project on its own, not a side task of directory accuracy.

- **Fake user for a machine upload**
  - Roster needs a logged-in person with an email as creator. We would create a system "user" to stand in.
  - Every vendor job then shows a fake person as its owner. Status emails go to a fake inbox. Audit trail says a person did it when a cron did.

- **57 vendor fields in the practitioner field list**
  - Fields like "First Name Verification Status" and "Location Confidence" sit next to `firstName` and `npi`.
  - Every roster admin sees them in the template picker. Egress templates see them. The AI mapping service receives them and may suggest them for a customer's column.
  - Fields describing a vendor's opinion do not belong in a list that describes a practitioner.

- **New entity in a hardcoded registry**
  - Roster's list of destinations is a Java class with 78 entries. Adding a holding table means a new entry plus a DAL table, DAL entity, and DAL batch resource.
  - An entity not in the list is skipped silently. No error, no failed row. Easy to get wrong, hard to notice.

- **Two templates per tenant for a file no tenant wrote**
  - Templates exist because each customer names columns their own way. The vendor file has one shape for everyone.
  - We would create and approve two templates per tenant per vendor, by hand, to describe the same file each time. Ceremony with no value.
  - One adapter class does this job.

- **Add "if not vendor" to every roster screen and event**
  - Roster list, detail, records, download, delete, cancel, reupload, search, webhook, three emails, dashboard events, credentialing actions. Each needs a branch.
  - Miss one and a customer sees vendor jobs, or their webhook fires, or a `START` row opens credentialing.
  - Every roster endpoint written later inherits the same duty. The first one that forgets leaks.
  - In a separate service none of this code exists, so nothing needs excluding.

- **Turn validations off, then on again**
  - First upload must skip the practitioner rules. Second upload must run them. Roster has no per-job switch, so both need a job-type branch through a 5,569-line rule service.
  - Second upload runs rules on values a reviewer already approved. A vendor-corrected address can fail the location-hash rule and get blocked, with no way back to the reviewer.

- **Thread a source name through 13 places**
  - `roster:{tenantId}` is concatenated in eight places and read back in five. Every one must learn `candor:{tenantId}`.
  - Miss one and vendor data writes into roster's own slice, or lookups read the wrong slice.

- **Change the write operation**
  - Roster merges and only adds. Vendor needs rebuild-and-replace so a location can be removed.
  - "Remove this location" is the most useful thing a vendor can tell us. Roster cannot say it.
  - The write path itself is marked deprecated for removal.

- **Side effects we do not want**
  - A practitioner write creates group, location, network, address relationships, runs NPPES, can start or stop credentialing.
  - A vendor saying "phone number is wrong" should change one phone number. Every side effect here is a bug.

- **Reshape rows twice**
  - Vendor rows are one per practitioner per address. Roster rows are one wide row per practitioner. Approved rows must be pivoted back into roster's shape, then roster unpivots them again.

- **A fourth job type across 108 branch sites**
  - About 80 are `== FACILITY ? a : b`. A fourth type silently falls into the practitioner branch.
  - Two places default to `PRACTITIONER` when no type is given.
  - Nothing tells us which of the 108 we missed until it misbehaves in production.

- **Run on roster's worker**
  - Two fixed slots, no autoscale, no dead-letter queue, lease used as a lock (section 5).
  - One bad vendor file blocks a customer roster. One huge customer roster blocks a hundred vendor files.

- **Vendor files parsed with full `api-layer` permissions**
  - Vendor files are outside input. Parsing them inside `api-layer` gives that code everything `api-layer` can do.
  - A separate worker gets a service account that sees the vendor bucket, its own DB, and one release call.

- **Every vendor change redeploys the customer pipeline**
  - Vendor changed its columns three times in three months. Each change edits the shared field file and redeploys `api-layer`.
  - A regression from a vendor column rename reaches every tenant's roster screen.

- **What we get for all this**
  - Parse, map, stage, job status. About a fifth of the work.
  - Export, correlation, review, rejection memory, ledger, release, audit are new either way.
  - The reusable part is two libraries and three parser classes. Copying them costs an afternoon.
  - Undoing it later means pulling a job type out of 108 sites, a shared schema, webhooks, and per-tenant templates while customers use it.

---

## 5. What the roster runtime brings along

- **Two files at a time, fixed.**
  - Each worker instance pulls one validation message and one ingestion message at a time. Production runs two instances. So: two rosters validating, two ingesting, at any moment. Staging: one.

- **It does not autoscale.**
  - Cloud Run scales on HTTP concurrency or CPU. The roster worker gets no HTTP traffic and a single-threaded pull rarely pins CPU. `MAX_INSTANCES=20` is never approached.
  - Cost is inverted: two warm 6 GiB instances all month for a bursty workload, with no burst headroom.

- **Pub/Sub lease used as a lock.**
  - Ack deadline is a liveness signal, default cap 60 minutes. Roster raises it to 3 hours (validation) and 10 hours (ingestion), extending every 600 seconds.
  - Every extension is a network call that must succeed. A 10-hour file needs about 60 consecutive successes. One missed heartbeat (network blip, GC pause, CPU-starved instance) and Pub/Sub thinks the worker died and redelivers. The worker thread keeps running; nothing tells it the lease is gone.
  - Longer files are the ones most likely to be redelivered, and the most expensive to redo.

- **After a lapsed lease: concurrent double processing.**
  - Redelivery usually lands on the other instance, so both production slots work the same file and everyone else waits.
  - Validator's re-entry guard does not include `VALIDATION_IN_PROGRESS`, so the second run passes, wipes the rows the first run is still writing, and starts over. Status writes have no compare-and-set.
  - The first run's eventual `ack()` is ignored. Its hours of work count for nothing.
  - Ingestion is worse: its guard only checks `COMPLETED` and `FAILED`, so redelivery replays hours of live practitioner writes while the first run is still writing them.
  - No defence exists for any of this: no in-progress guard, no lease, no runtime budget.

- **No dead-letter queue.**
  - Any exception except "roster not found" nacks. Pub/Sub redelivers immediately. A file that fails deterministically occupies one of two slots on every retry for seven days.
  - The roster README says dead-letter topics exist. The code disagrees.

- **Ingestion writes one row at a time over HTTP.**
  - Async row path exists but is gated behind a "prove stable in production" TODO.

- **Cloud Run Job mode is not a fix as deployed.**
  - Only live on internal and feature environments. No cap on parallel executions, no queue, a failed dispatch is lost.

- **Worked example: 100 tenants deliver on the same day, 30 minutes each to validate.**
  - Validation: 100 files ÷ 2 slots × 30 minutes ≈ **25 hours**. Ingestion after review: another ≈ 25 hours of serial DAL calls.
  - Customer rosters that day sit in the same two slots. No priority, no fairness.
  - Any file over 3 hours hits double processing. Any parse failure hits the poison loop.
  - The DAL is a shared bottleneck in either design; the difference is roster gives no knob for a global concurrency limit. The directory accuracy design puts that limit on a Cloud Tasks queue as configuration and scales the worker to zero between cadence days.

---

## 6. Where roster and vendor ingestion actually overlap

- **Different in almost every dimension**
  - Direction: roster is one-way inbound; vendor is a round trip (we export, they grade, we ingest).
  - Author: each tenant in their own shape, versus one vendor in one shape.
  - Trigger: a person with a JWT, versus an object landing in a bucket.
  - Cadence: ad hoc one tenant at a time, versus all tenants in one monthly window.
  - Row grain: one per practitioner, versus one per practitioner per address.
  - Row meaning: values to store, versus verdicts about values (status, reason, confidence, evidence, suggested value).
  - Human step: optional fix-and-approve, versus mandatory per-row decision that feeds a suppression memory.
  - Correlation: none, versus every row must attach to the export row it grades.
  - Write semantics: whole-entity merge, versus per-attribute release from a ledger.
  - Side effects: many, versus none.
  - Memory across runs: none, versus rejected recommendations suppressed on re-send.

- **The one real overlap: field names.** Same names because every MDM source shares the operational-value vocabulary. NPPES, CAQH, the portal, and roster all share them too, and nobody proposes one pipeline for all of them.
- **The reusable part of roster is its file parser**: two libraries (Apache Commons CSV, fastexcel) and three thin wrapper classes with no roster coupling.

---

## 7. Why a separate service is the right shape

- **Resource class.** Ingestion idles most of the month, then parses files near a million rows. Review is small, always-on, millisecond reads. Scale-to-zero worker plus small API pays for the burst only when it happens.
- **Credentials boundary.** Vendor files are untrusted external input. In roster they parse inside `api-layer` with its full permissions. A separate worker gets a service account scoped to the vendor bucket.
- **Release cadence.** The vendor adapter is our highest-churn code. Alone, it redeploys itself. Inside roster, it redeploys every customer's pipeline.
- **The transactional test.** A split is wrong when two sides share a transaction or a chatty sync call. Here there are none: no shared table, no shared transaction. Release is one HTTP hop to the DAL in every design.
- **Structural safety over negative filters.** In its own service the vendor job cannot show in a roster list or fire a roster webhook, because that code is not there.
- **Failure isolation.** Malformed file lands in a dead-letter queue with the file untouched in the bucket. No customer roster is delayed.
- **Reversibility.** Folding a small clean service back in later is mechanical. Extracting a fourth job type out of roster means unpicking 108 branch sites, a shared schema file, webhooks, and per-tenant templates.
- **Reuse still happens, as copy not runtime.** Declare Commons CSV and fastexcel, copy the three parser wrappers, copy the good patterns (per-row-status staging, streaming batch reads, pointer messages with file left in GCS, trace-id propagation). A shared library for three small files would recreate the coupling we are removing.
- **Conceded cost.** One more repo, pipeline, service account, dashboard, on-call entry. Smart-outreach already set the pattern on the same rails, so most of it is a template.

---

## 8. Recommendation

- Build vendor ingestion inside the standalone directory accuracy service: one bounded context, two deployables (`-api`, `-worker`), one database, parsers copied from roster, nothing shared at runtime.
- Record "reuse the roster pipeline via two uploads" as a rejected alternative, with the detailed document as its evidence.
