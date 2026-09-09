# Design: SFTP exchange — outbound export + inbound delivery transport

> **RETIRED DRAFT — not a live design document.** This was doc 3 (SFTP exchange) of the attestation-module series (formerly `attestation-module/modules/03-sftp-exchange/`). The external-vendor program was decoupled from the attestation workflow on 2026-09-08 (source-of-truth v2 decision register entry D2-38, the decoupling decision), and the whole vendor program now has **one** standalone design doc: `platform/directory-accuracy/directory-accuracy.md` (to be written; its companion `directory-accuracy-concepts.md` exists). These drafts were moved here 2026-09-09 as source material for that doc — nothing in them is binding, and the internal "doc N" cross-references below refer to the retired attestation-series numbering.

## Purpose and scope

This module is the **transport doorway** between CertifyOS and external accuracy vendors (Candor first, any vendor later). It moves files in both directions, records who dropped what, and hands every delivery to the ingestion pipeline (doc 4) with full context. It deliberately contains **no parsing and no business logic**.

**Ownership:** the SFTP platform itself — servers, accounts, keys, buckets, event wiring — is an **existing shared CertifyOS service, built and operated by the platform/DevOps team** and already used by other teams (~14 partner accounts in production). This module does not build or run any SFTP infrastructure; it onboards its vendors onto that platform and builds two small workers of its own: the outbound export worker and the inbound receiver. ⚠ This narrows D2-30 ("we design, DevOps builds and operates") to "we integrate; the platform team operates" — proposed v2 amendment, listed in *Rollout*.

**This module owns:**

- The **vendor onboarding requirements** handed to the SFTP platform team: one account + one bucket per vendor, folder conventions, key policy asks.
- The **outbound export worker**: on the vendor cadence, generate the file of practitioners to verify, archive it, and write it into the vendor's bucket for SFTP download.
- The **inbound receiver**: the module's own subscription on the platform's upload events; it filters, deduplicates, archives each delivery immutably, and publishes the enriched event that wakes ingestion (doc 4).
- The **filename contract** in both directions — the batch identity travels in the name, not in a companion file.
- The **reconciliation sweep** that catches lost events and stuck deliveries.

**This module does NOT own:**

- The SFTP servers, partner accounts, keys, or bucket event wiring — the shared platform (Terraform in `certifyos-infra`, operated by the platform team).
- Parsing, validation, dispositions, batch registry, row-level dedup — doc 4 starts at "a delivery event arrived with an archived file."
- Which practitioners go into the export — doc 1's open tasks feed it; this module formats and ships.
- The client-facing export of attested data — module 8 (different audience, different content).

**The one-line thesis:** *the shared SFTP platform is the doorway; our versioned archive is the system of record; completeness is guaranteed by the consumer, not the transport.* The platform cannot prove a vendor's upload is logically complete — so this module archives every delivered version and the ingestion layer (doc 4) is idempotent at row grain: a partial file followed by the full file converges to exactly one ingestion of every row.

**Neighbors:** Cycle (doc 1) defines the open-task selection and the deterministic identity every export row carries; Ingestion (doc 4) consumes the delivery event, the archive layout, and the row-level idempotency contract; Backend (doc 5) hosts the export worker and the inbound receiver as module services (§6.1 microservices direction).

## Section triage

| Section                       | Included | Reason                                                                         |
| ----------------------------- | -------- | -------------------------------------------------------------------------------- |
| Approach                      | Yes      | The chosen design as numbered steps                                            |
| Alternatives considered       | Yes      | Two rejected manifest-based designs + one managed product + one decided-out transport |
| Components and files touched  | **No**   | Pre-implementation design doc — no file lists yet                              |
| Contracts and interfaces      | Yes      | Bucket layout, naming, event payloads — the O-1 input                          |
| Data model and migration      | Yes      | The archive layout, retention, and the export registry tables                  |
| Security, privacy, and access | Yes      | Platform accounts, our service identities, the trust boundary                  |
| Performance and scale         | Yes      | The volume and cadence numbers the cost estimates use                          |
| Observability                 | Yes      | A missed export or a silent vendor must page, not wait to be noticed           |
| Audit trail                   | Yes      | **Added section** — file custody is compliance evidence                        |
| Failure modes and rollback    | Yes      | Partial uploads, duplicate events, lost events, rollback                       |
| Test strategy                 | **No**   | Design phase; decisive failure cases are in Failure modes                      |
| Rollout                       | Yes      | Platform-team onboarding, contract dry-run, pilot vendor ordering              |

## Approach

Ride the existing shared SFTP platform end to end; add only what the attestation lane genuinely needs: an inbound receiver, an export worker, an immutable archive, and a hard idempotency contract with ingestion. Each step: what we do, why, and why not the alternative a reviewer would ask about.

**1. Vendors ride the existing shared SFTP platform — no new server, no new hosting.**

- **What:**
  - The platform team onboards each vendor exactly like its ~14 existing partners: one SFTP account, one dedicated GCS bucket (`certifyos-production-platform-sftp-<username>` in prod, `certifyos-development-sftp-<username>` in staging), key-only login. **Candor is already onboarded** (TS-111546, closed 2026-09-04): username `candor-health`, port 2222, `sftp.prod.certifyos.com` / `sftp.staging.certifyos.com`; prod bucket `certifyos-production-platform-sftp-candor-health` exists (created 2026-09-03, versioning on).
  - The vendor sees a plain SFTP folder; underneath, `gcsfuse` (a driver presenting the bucket as a folder) commits each uploaded file as a GCS object.

- **Why:** the platform already exists, is Terraform-managed, production-proven, and operated by a team whose job it is — building a second SFTP surface for one lane would duplicate infrastructure, keys, monitoring, and on-call.

- **Why not our own hardened OpenSSH host:** it buys a partial-file guarantee only if vendors also adopt a manifest convention — and that adoption cost is exactly why the manifest designs are rejected (*Alternatives considered*).

- **What if it fails:** the platform's MIG (managed instance group — auto-replaced VMs behind a load balancer) restarts failed servers; a full outage means vendors retry, nothing downstream is corrupted, and the platform team's alerting owns it.

- **Details:** *Contracts → lifecycle step 0.*

**2. One bucket per vendor is the doorway; our versioned archive bucket is the system of record.**

- **What:**
  - The vendor bucket is a working buffer with per-tenant prefixes under the platform's two fixed root folders: `to/<tenant-id>/` (vendor uploads; partner has read/write/delete) and `from/<tenant-id>/` (our exports; partner is **read-only** — the platform never grants write/delete on `/from`). ⚠ Earlier drafts named these `inbound/`/`outbound/`; renamed 2026-09-08 to the platform's actual folder contract ("SFTP Onboarding for Clients", Confluence 1611366414).
  - Every delivery the receiver accepts is server-side-copied to our **versioned archive bucket** (7-year retention) before anything downstream sees it; every outbound file is archived before it is placed for the vendor.

- **Why:** the raw file is the reprocessing source and 7-year compliance evidence (N4, D2-20). The vendor bucket's lifecycle is platform-owned (a shared archival function already moves aged files); our evidence cannot depend on another team's hygiene rules.

- **What if it fails:** the copy is retried by Pub/Sub redelivery (step 3); the source object remains in the vendor bucket untouched until the copy succeeds.

**3. The inbound receiver — our own subscription on the platform's existing upload events.**

- **What:**
  - Each vendor bucket already publishes `OBJECT_FINALIZE` (GCS's "an object version was committed" notification) to the platform's `sftp-bucket-events` Pub/Sub topic; every subscription gets its own copy, so attaching ours disturbs nobody.
  - The receiver (a module service, §6.1) filters to our vendor buckets and `to/` prefixes, ignores temp-suffix names (`.part`, `.tmp`, `.filepart`), and deduplicates on `(bucket, object, generation)`.
  - It waits a **settle window** (default 10 minutes), re-checks the object, archives the newest generation, and publishes one enriched delivery event to ingestion.

- **Why our own consumer rather than pointing doc 4 at the raw topic:** the raw event is bucket-level noise — every partner's uploads, our own outbound writes, temp files, one event per generation. The receiver turns that into exactly one context-rich event per accepted delivery, and it is where the custody audit is written.

- **Why the settle window:** `gcsfuse` can commit an object mid-transfer (a client `fsync`, a dropped connection) — the first finalized generation may hold partial content, with the full version arriving as a later generation. Waiting, then taking the newest generation, absorbs the common retry-within-minutes case cheaply. It is a mitigation, not the guarantee — step 4 is the guarantee.

- **What if it fails:** the receiver nacks (rejects the delivery so Pub/Sub redelivers with backoff); after 5 failed attempts the message lands in the module's dead-letter queue (a holding queue for repeatedly failing messages) and pages — never an infinite loop, never a silent drop.

- **Payloads:** *Contracts → lifecycle steps 4 and 5.*

**4. Completeness is the consumer's guarantee: ingestion is idempotent at row grain.**

- **What:**
  - Nothing at the transport layer proves a vendor finished uploading — no manifest, no row count, no end-to-end checksum. `OBJECT_FINALIZE` proves only "these exact bytes were committed" (CRC32C-verified for the VM → GCS hop).
  - So the contract with doc 4 is: **every archived generation is offered for ingestion, and ingesting the same logical row twice is a no-op.** A 10-row partial followed by the 100-row full file yields the 10 rows once and the remaining 90 once.

- **Why:** this converts the platform's known partial-file weakness from a correctness problem into a latency detail — the data converges as soon as the complete file lands, with no vendor-side protocol changes and no human intervention.

- **Why not demand a completeness signal from the vendor:** that is the manifest design — rejected because it requires every SFTP counterpart to adopt an extra-file convention the platform's other clients do not follow (*Alternatives considered*).

- **What if it fails:** a partial file that is never followed by a full one is caught by reconciliation, not by parsing — the delivered row count is far below the export size (Observability) and the vendor-response window flags the batch for human follow-up.

- **Mechanics live in doc 4** (row fingerprint, dispositions); this module's obligation is to deliver every generation with its identity intact.

**5. Batch identity travels in the filename — the vendor echoes our export batch id in the name.**

- **What:** outbound files are named with our `exportBatchId`; the vendor's inbound files must carry the same id back as `exportBatchRef` in *their* filename. Doc 4 validates it against the export registry and attaches every row to its obligation through it.

- **Why the filename and not a manifest:** the echo is the one thing the design cannot live without (F2 — tying a delivery to the request that caused it), and a naming convention is the lightest possible ask of a vendor — every SFTP integration already follows one; no extra file, no upload-ordering rule.

- **What if the vendor cannot echo it:** the fallback is attribution by `(vendor, tenant, latest open export batch)` — workable but ambiguous across overlapping cycles; it is priced as a degraded mode in the O-1 negotiation, not designed for.

- **Schema:** *Contracts → file naming*.

**6. The outbound export writes GCS objects — no SFTP client code at all.**

- **What:** the export worker registers the batch, writes the file to our archive, then writes it into the vendor bucket's `from/<tenant>/` prefix as a plain GCS object. The vendor's SFTP session sees it appear, complete, via `gcsfuse`.

- **Why:** a GCS object is atomic — it exists whole or not at all — so the vendor can never list a half-written export; the `.part`-then-rename dance and the SFTP client library disappear from our side entirely.

- **What if it fails:** Cloud Tasks retries the worker; every step is idempotent (deterministic batch id, re-writable objects); retry exhaustion writes `EXPORT_FAILED` and pages.

- **Details:** *Contracts → lifecycle step 1.*

**7. An hourly reconciliation sweep — the loss backstop.**

- **What:** compare the vendor buckets' `to/` prefixes against doc 4's batch registry and flag two cases: an object past the settle window with no registered batch (lost event → re-offer it), and a registered batch stuck before completion beyond a grace period (alert).

- **Why:** F8 — a lost event or a stuck delivery must be found by machinery, never by a human noticing silence. The sweep re-emits events; it never parses files.

- **What if the sweep itself fails:** its own liveness alert; the sweep is stateless and re-runnable — the next run covers the gap.

**What the approach deliberately does NOT do:**

- No SFTP infrastructure of our own — servers, accounts, keys, and event wiring are the shared platform's.
- No manifest files, in either direction — batch identity rides the filename; completeness is the ingestion contract (step 4).
- No parsing, validation, or business logic — the receiver moves and records bytes; doc 4 owns meaning.
- No API or signed-URL upload path — transport = SFTP is decided (D2-03); an API doorway remains the documented future extension into the same archive.
- No second copy of retention logic — versioning + retention live on the archive bucket as configuration.

### Key decisions

| #   | Question                | Resolution                                                                                                 |
| --- | ----------------------- | ------------------------------------------------------------------------------------------------------------- |
| D1  | Hosting                 | The existing shared SFTP platform (`certifyos-infra`), one account + bucket per vendor — no new server; Candor account `candor-health` + prod bucket already provisioned 2026-09-04 (TS-111546); manifest-based and self-hosted variants rejected |
| D2  | System of record        | Our versioned archive bucket (7-year retention); the vendor bucket is a platform-owned buffer               |
| D3  | Partial-file handling   | Accepted at transport; settle window absorbs quick retries; **the guarantee is doc 4's row-grain idempotency** — ⚠ amends D2-04 (manifest-last), proposed v2 change |
| D4  | Pipeline trigger        | Platform's existing `OBJECT_FINALIZE` → `sftp-bucket-events`; the module's own filtered subscription; the receiver publishes one enriched event per accepted delivery |
| D5  | Loss backstop           | Hourly sweep against doc 4's registry — re-offers deliveries, alerts on stuck batches, never parses         |
| D6  | Origin vs integrity     | Origin = the vendor's dedicated account/bucket (one partner per bucket); integrity = per-hop (SSH transport + GCS CRC32C); **no end-to-end proof — documented accepted gap** |
| D7  | Retry bounds            | Receiver: nack → Pub/Sub redelivery → DLQ after 5 attempts + page; export: Cloud Tasks retries → `EXPORT_FAILED` + page |
| D8  | Detection latency       | Event-driven (seconds) + the settle window (default 10 min) before handoff — no polling hop remains         |
| D9  | Export registry         | `attestation_export_batches` + `attestation_export_items` in the module's own database (attestation-db, §6.1) written before anything is placed for the vendor — doc 4 validates `exportBatchRef` and attaches rows through them |
| D10 | Batch identity transport| `exportBatchId` in our outbound filename; the vendor echoes it as `exportBatchRef` in theirs — no manifest  |

## Alternatives considered

Each alternative in full: what it is, its genuine strengths, its costs, and the direct reason it lost to the chosen approach.

### Manifest-last convention on the existing platform

- **What it is:**
  - Keep the shared platform, but require each uploader to send the data file first and then a small companion JSON (the manifest: filename, row count, SHA-256 checksum, batch ids), uploaded **last**. The receiver reacts to manifests only.
  - Reading a half-uploaded file becomes structurally impossible: the manifest's existence proves the data file completed, and its checksum is end-to-end.

- **Pros:**
  - The strongest completeness and integrity guarantee available — proof at the protocol level, before a single row is parsed.
  - Row count + checksum catch truncation *and* corruption; ingestion's row-level dedup becomes an extra safety net instead of the primary guarantee.

- **Cons:**
  - **Every SFTP counterpart must adopt the convention** — produce a second file, compute a checksum, and honor a strict upload order. The platform's existing clients follow no such rule, and a vendor contract negotiation must now carry a protocol change, not just a naming ask.
  - Partial protection: a vendor that half-adopts (manifest first, or stale manifests) is worse than no manifest — the receiver trusts a signal that lies.
  - The row-grain idempotency in doc 4 is required regardless (duplicate events, re-sent files, replays are all non-negotiable #1 territory) — so the manifest buys a second guarantee for a case the consumer already has to survive.
  - Nothing else on the shared platform enforces or understands manifests; the convention lives only in our vendor contracts, invisible to the platform team's tooling.

- **Why rejected:** its entire value depends on vendor adoption of an extra-file protocol — a real negotiation and integration cost per vendor — to buy a guarantee the ingestion layer must provide anyway. The chosen approach asks vendors only for a filename echo.

- **Cost:** ≈ $0 infrastructure delta — the cost is contractual and integration effort per vendor, plus a per-vendor "did they honor it" verification burden.

### Self-hosted OpenSSH SFTP host + GCS mirror (the previous recommendation)

- **What it is:**
  - A small hardened Linux VM running OpenSSH's SFTP subsystem; one chrooted, key-only account per vendor; a 5-minute pickup job mirroring uploads to the versioned archive; manifest-last as the completeness signal.
  - Full control: folder layout, rotation policy, custody events written by our own pickup code.

- **Pros:**
  - OS-enforced per-vendor isolation (chroot) and origin attribution from the depositing account.
  - Paired with manifest-last, partial-file reads are structurally impossible.
  - Everything durable in GCS within minutes; the box is disposable.

- **Cons:**
  - Duplicates an SFTP platform CertifyOS already runs — second key ceremony, second monitoring stack, second on-call, second Terraform surface, for one lane's traffic.
  - Its partial-file guarantee still rests on the manifest convention — the same vendor-adoption cost as the alternative above.
  - A real server to patch and operate (small, but real), against a platform team already doing exactly that at larger scale.
  - The 5-minute polling hop exists only because raw SFTP has no events — the shared platform already solved that with bucket notifications.

- **Why rejected:** it rebuilds, at ~$20–45/month plus a second operational surface, capabilities the shared platform already provides — and its one genuine advantage (protocol-level completeness) is inseparable from the manifest adoption cost that disqualifies the manifest designs.

- **Cost:** VM e2-small + disk **$15–40/month** + GCS **< $2** + pickup compute **≈ $0–2** ≈ **$20–45/month**, plus the duplicated operational surface (un-dollared).

### AWS Transfer Family (the managed SFTP product)

- **What it is:**
  - AWS's fully managed SFTP endpoint writing straight to S3; per-user IAM isolation, no server to run; a sync job then copies S3 → GCS, because the pipeline, database, and audit all live on GCP.

- **Pros:**
  - Zero servers — the strongest managed-SFTP offering on any cloud; isolation and key management are product features.
  - If CertifyOS were AWS-native and had no SFTP platform, this would be a serious candidate.

- **Cons:**
  - The endpoint alone costs ~$0.30/hour ≈ $216/month idle — for capability the existing platform already provides at marginal cost ≈ $0.
  - A second cloud: IAM federation, an S3→GCS sync job, cross-cloud monitoring, a second bill and on-call.
  - Compliance evidence transits two clouds — an extra custody hop; detection events originate outside GCP.

- **Why rejected:** the existing platform makes the entire category moot — paying ~$216/month idle plus second-cloud operations to replace working, already-operated infrastructure.

- **Cost:** endpoint $0.30/h × 730 h ≈ $216 + data $0.04/GB ≈ $0 at our volume + S3 + sync job ≈ **$220–240/month, plus second-cloud operations**.

### (Decided out, recorded to prevent re-litigation) API / signed-URL upload instead of SFTP

- D2-03 already decided the transport: vendors speak SFTP, and Candor's delivery is SFTP-shaped.
- Direct API push or signed-URL upload (a pre-authorized one-time upload link) remains the documented **future extension**: a second doorway into the same archive, changing nothing downstream.

## Contracts and interfaces

**Who runs what:** the export worker and the inbound receiver are module services (doc 5, §6.1 microservices direction), triggered by Cloud Scheduler and the Pub/Sub subscription respectively; the SFTP platform (servers, accounts, buckets, notifications) is operated by the platform team. Per-vendor cadence and settings come from the `attestation-module-config` entry (doc 1's config pattern).

### Configuration and flags

Every tenant-level knob this module reads or adds, in one place (doc 1's carry-forward rule; doc 5 owns the consolidated schema). All keys live in the shared `attestation-module-config` entry. **No feature flags** — no Flagsmith or other flag system; the kill switches are config keys, and the outbound job can also be paused at the Cloud Scheduler level.

| Key | Lives in | Type | Default | Controls |
| --- | --- | --- | --- | --- |
| `exportCadence` | `attestation-module-config` | schedule, per vendor per tenant | monthly (Candor today) | How often the outbound export runs for a vendor |
| `vendorResponseSlaDays` | `attestation-module-config` | integer (days) | pending sign-off (Q3) | Days after an export before the vendor-response-overdue alert fires |
| `settleWindowMinutes` | `attestation-module-config` | integer (minutes), per vendor | `10` | How long the receiver waits after an upload event before archiving the newest generation |
| `exportPaused` | `attestation-module-config` | boolean, per tenant/vendor | `false` | Kill switch: stops new exports (see *Failure modes and rollback*) |
| `inboundPaused` | `attestation-module-config` | boolean, per tenant/vendor | `false` | Kill switch: the receiver acks and skips (no archive, no handoff); the sweep re-offers on unpause |

### The lifecycle, end to end — every exchange with its artifacts

> The full chain in execution order: vendor onboarding → outbound export → vendor fetch → vendor upload → receiver → pipeline event → sweep. Rationale per step: *Approach* (0 → step 1, 1 → step 6, 3–4 → steps 3 and 4, 5 → step 5, 6 → step 7).

**Step 0 — vendor onboarding (one-time per vendor, executed by the platform team). ✅ Done for Candor — TS-111546, closed 2026-09-04.**

- Standard request per the platform runbook ("SFTP Onboarding for Clients"): the vendor's public SSH key (vendor-generated, Ed25519 or rsa-sha2-256/512; private keys never transit) → DevOps creates the user and its bucket via Terraform. **For Candor this produced:** username `candor-health`, port 2222, hosts `sftp.prod.certifyos.com` and `sftp.staging.certifyos.com`, prod bucket `certifyos-production-platform-sftp-candor-health` (versioning on, labels `platform=pdm`; a test file sits at `from/` as of 2026-09-04). **Still to confirm with DevOps (Q1):** the bucket carries the standard `OBJECT_FINALIZE` notification to `sftp-bucket-events`, and the staging bucket exists.
- Key registration and rotation follow the **platform's** user-management process (Terraform variables + Secret Manager, reconciled onto the servers by a timer) — not a process we design.

Bucket layout (prefixes inside the vendor's bucket; the vendor's SFTP session is rooted at the bucket):

```
certifyos-production-platform-sftp-<username>/     (staging: certifyos-development-sftp-<username>)
    from/<tenant-id>/    ← we write; vendor READ-ONLY (platform rule: partners never get write/delete on /from)
    to/<tenant-id>/      ← vendor writes (read/write/delete); we read
```

- `/to` and `/from` are the platform's only two root folders and are not renameable per partner. Our own archive bucket keeps its `inbound/`/`outbound/` naming (*Data model*) — the two vocabularies are deliberately different so a path is never ambiguous about which bucket it lives in.

- Per-tenant prefixes keep multi-tenant vendors (Candor serves several of our tenants) cleanly separated for lifecycle and audit.
- **Audit written in this step:** `VENDOR_ONBOARDED` (vendor, bucket, requested-by; key fingerprint recorded by the platform's own trail — we record the request and its completion).

**Step 1 — the outbound export run.**

- Cloud Scheduler (per vendor cadence — monthly for Candor, fired at the **start** of each cadence period, so the vendor works in parallel with the attestation window rather than after it) → `POST /internal/attestation-exports/trigger` (202) → **one Cloud Task per tenant × vendor** with a deterministic name per cadence period (`export-candor-org-xyz-2026-09`) — a re-fired trigger cannot double-enqueue (the doc 1 pattern, reused).
- The export worker, in order, each step idempotent:
  1. **Register the batch:** insert one `attestation_export_batches` row under the deterministic batch id — a re-run of the period finds the row and resumes from its status; this row, not a bucket listing, is the already-ran check. It is also what doc 4 validates every inbound `exportBatchRef` against.
  2. Page the practitioners with `state = 'OPEN'` tasks (doc 1's scan index; overdue tasks are still `OPEN` — OVERDUE is derived, doc 1's D9). A task still open from the previous run is **deliberately exported again** — the obligation is still unanswered, so it stays on the vendor's worklist; dedup applies within one cadence period only (same batch id). A per-obligation export-once marker is a documented later option if Product wants it. **Identity comes from the task row; the data-to-verify fields come from a live OV read at export time** (through the platform's read APIs, §6.1) — task rows carry no OV data (doc 1's D10), so nothing stale can ship. Per page, write one **`attestation_export_items`** row per practitioner — the batch's membership record `(export_batch_id, certify_practitioner_id, due_period, task_id)`, idempotent on `(export_batch_id, certify_practitioner_id)`. Doc 4 attaches every inbound row to its obligation through these rows.
  3. Write the data file to the **archive bucket first** — our evidence copy exists before the vendor's; a failure here published nothing anywhere.
  4. Write the same bytes to `from/<tenant>/` in the vendor's bucket — one GCS object write; object creation is atomic, so the vendor's SFTP listing can never show a partial file. (This write fires an `OBJECT_FINALIZE` event too; the receiver's `to/` prefix filter ignores it.)
- Batch id: `<tenant>-<vendor>-<yyyy-MM>-<seq>` (ISO year-month, e.g. `org-xyz-candor-2026-09-001`). Re-running a period produces the **same** batch id; the worker finds the registered batch row, sees from its status how far the previous run got, and redoes only what is missing — never a second batch for one period.
- Retry exhaustion (Cloud Tasks' final attempt fails): write `EXPORT_FAILED` and **page** — a silently missed export means the vendor verifies nobody this cycle, a compliance event, not an ops detail. Cloud Tasks has no dead-letter queue (doc 1, *Failure modes*) — the final-attempt record is the dead letter; the export-liveness alert is the independent backstop.
- **Audit written in this step:** `EXPORT_RUN_STARTED`, `EXPORT_FILE_WRITTEN` (batch id, row count, sha256 of our own file, archive URI + generation), `EXPORT_PLACED` (vendor-bucket URI + generation), `EXPORT_RUN_COMPLETED`; `EXPORT_FAILED` on final failure. All stamped with the export batch id.

**Step 2 — the vendor fetches (passive).**

- The vendor lists `from/<tenant>/` over SFTP and downloads the file. `/from` is read-only for partners (platform rule), so the vendor **cannot** delete after fetch; ageing-out is the platform's archival function plus our lifecycle ask (Q1).
- Nothing on our side reacts to their fetch; the vendor-response-overdue alert (Observability) covers a vendor that never responds.

**Step 3 — the vendor uploads.**

- Into `to/<tenant-id>/`, named per the filename contract below — the name carries `exportBatchRef`, the echo of our batch id (F2): the tie between their delivery and our request.
- No manifest, no ordering rule, no client-side checksum — the vendor's only obligations are the folder, the name, and the agreed CSV schema (doc 4 / O-1).

**Step 4 — the receiver accepts the delivery (event-driven).**

The platform's bucket notification fires `OBJECT_FINALIZE` per committed object version; the module's subscription delivers it to the receiver. The step order IS the failure handling — every step is safe to repeat; the registry offer is the only commit:

1. **Filter:** our vendor buckets only, `to/` prefix only, temp-suffix names (`.part`, `.tmp`, `.filepart`) ignored; dedupe on `(bucket, object, generation)` — at-least-once delivery makes duplicates normal.
2. **Settle:** wait until `settleWindowMinutes` past the event's `eventTime`, then re-read the object's metadata. A newer generation exists → ack this event and stop (`GENERATION_SUPERSEDED` audit) — the newer generation's own event carries the work.
3. **Archive:** server-side copy the generation to `gs://<archive-bucket>/inbound/<vendor>/<tenant>/<filename>`; record both generations. The copy is idempotent — re-copying creates an identical new archive version.
4. **Hand off:** publish the enriched delivery event (step 5). Doc 4's registry insert is idempotent on `(bucket, object, generation)`, so a crash-and-redeliver here double-publishes harmlessly.

- **Bounded retries:** any step failing → nack → Pub/Sub redelivery with backoff; after **5 attempts** the message lands in `attestation-sftp-dlq` with a page and an operator drain endpoint (the doc 4 DLQ pattern). The source object is untouched either way.
- **Audit written in this step:** `INBOUND_DELIVERY_DETECTED` (bucket, object, generation, size, `crc32c` from the event — origin = the vendor's dedicated bucket, F6), `INBOUND_ARCHIVED` (archive URI + generation, source generation), `GENERATION_SUPERSEDED` when the settle check skips an older version.

**Step 5 — the pipeline event (one per accepted delivery, published by the receiver).**

- After archiving, the receiver publishes one message to the `accuracy-source-file-received` Pub/Sub topic.

Message:

```json
{
  "eventType": "accuracy-source.file.received",
  "sourceId": "candor",
  "tenantId": "org-xyz",
  "dataFileUri": "gs://acc-archive/inbound/candor/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260903.csv",
  "archiveGeneration": "1725348912345678",
  "sourceBucket": "certifyos-prod-sftp-candor",
  "sourceObject": "to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260903.csv",
  "sourceGeneration": "1725348899887766",
  "crc32c": "yZRlqg==",
  "sizeBytes": 41943040,
  "exportBatchRef": "org-xyz-candor-2026-09-001",
  "detectedAt": "2026-09-03T14:05:00Z"
}
```

- `sourceId` — the vendor's id in the sources registry (D2-11): one name across the event, the filename, and the audit envelope. Derived from the bucket (one vendor per bucket).
- `exportBatchRef` — parsed from the filename (step 3's contract); `null` if the name does not carry one — doc 4 then applies its unattributable-delivery handling, never a silent guess.
- `crc32c` — GCS's stored checksum for the committed bytes (covers the VM → GCS hop); doc 4's registry uses `(sourceBucket, sourceObject, sourceGeneration)` as the batch natural key and `crc32c` to recognize an identical re-send.
- Delivery is at-least-once; doc 4's registry dedups on the generation key, so a duplicate message is harmless (D2-05 shape).
- **Audit written in this step:** `DELIVERY_EVENT_EMITTED` (the Pub/Sub message id — the custody baton pass to doc 4).

**Step 6 — the reconciliation sweep (hourly).**

- List the vendor buckets' `to/` prefixes; compare against doc 4's batch registry; act on two cases:
  - (a) an object generation past the settle window with **no registered batch** → a lost event → **re-offer**: run the receiver's steps 2–4 for it.
  - (b) a registered batch **stuck before completion** beyond a grace period → alert (doc 4's processing stalled — its DLQ and metrics own the diagnosis; the sweep only refuses to let it stay silent).
- The sweep re-offers deliveries; it never parses files.
- **Audit written in this step:** `SWEEP_DISCREPANCY_FOUND` (which case, the object involved, action taken).

### File naming — the batch identity contract (the O-1 proposal)

**Outbound (we produce):** `<tenantId>_<exportBatchId>_<yyyyMMdd>.csv` — e.g. `org-xyz_org-xyz-candor-2026-09-001_20260901.csv`.

**Inbound (vendor produces):** `<tenantId>_<exportBatchRef>_<vendorBatchId>_<yyyyMMdd>.csv` — e.g. `org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260903.csv`.

- `exportBatchRef` — the vendor's echo of our `exportBatchId`, verbatim: the one load-bearing leg (F2). Doc 4 validates it by point read on `attestation_export_batches`.
- `vendorBatchId` — the vendor's own delivery id; recorded, never load-bearing.
- A name that does not parse → the delivery still archives and hands off with `exportBatchRef: null`; doc 4 quarantines it as unattributable (nothing silently discarded, non-negotiable #9).
- The exact spelling and separators are the O-1 negotiation input (Q2) — the *echo requirement* is the non-movable part.

### Integrity — what is and is not guaranteed

- **Per-hop integrity exists everywhere:** vendor → server is SSH (transport-level integrity checks built into the protocol); server → GCS is CRC32C-verified by the platform's `gcsfuse` write; our archive copy is a server-side GCS operation (checksummed internally).
- **End-to-end proof does not exist:** no producer-side checksum reaches us, so "the bytes match what the vendor's system generated" cannot be proven — and neither can logical completeness (the partial-upload case). Both are absorbed by design: row-grain idempotent ingestion (doc 4) + reconciliation alerts (Observability).
- This is the documented, accepted trade for zero vendor protocol burden (D6, D3). If a vendor offers per-file checksums voluntarily (e.g. a `.sha256` sidecar), doc 4 may verify opportunistically — never required, never load-bearing.

## Data model and migration

**Tables inventory.** This module **creates**: `attestation_export_batches`, `attestation_export_items` (both in the module's own database, attestation-db — §6.1; final DDL doc 6). It **interacts with**: `attestation_audit_events` (created by doc 1, attestation-db) and doc 4's batch registry (read by the sweep). Its other durable state is the object archive.

- **The archive bucket** (`gs://acc-archive/`, name final at provisioning; module-owned):
  - Layout: `outbound/<vendor>/<tenant>/…` and `inbound/<vendor>/<tenant>/…`, filenames as in *Contracts*.
  - **Versioning on** — a re-archived name becomes a new generation; nothing is overwritten, ever.
  - **Retention: 7 years** (N4, aligned with D2-20 — the raw file *is* evidence), lifecycle-tiered to cold storage classes as objects age.
  - Every archived object's generation **and** its source generation in the vendor bucket are recorded in `INBOUND_ARCHIVED` — the audit points at exact immutable versions on both sides.

- **The vendor bucket is a buffer, not a store:** platform-owned lifecycle (the org's shared archival function ages files out); our evidence never depends on it — everything accepted is in our archive within minutes. ⚠ Confirm the platform's lifecycle rules on our vendor buckets do not delete inside the settle window (Q1).

- **`attestation_export_batches`** — one row per export run: `export_batch_id` (the deterministic id), `tenant_id`, `source_id`, cadence period, row count, sha256 (of our own produced file), archive URI + generation, vendor-bucket URI + generation, status `REGISTERED → ARCHIVED → PLACED | FAILED`. Doc 4 validates every inbound `exportBatchRef` by a point read here; the export re-run check reads it too. Final DDL doc 6.

- **`attestation_export_items`** — one row per exported practitioner per batch: `export_batch_id`, `certify_practitioner_id`, `due_period`, `task_id`; unique `(export_batch_id, certify_practitioner_id)`. Doc 4 attaches each inbound row to its obligation through this table; it is also the base for per-practitioner export visibility ("sent to the vendor on X, no response yet") and a future export-once marker.

- Audit events ride the shared append-only store; migration = the two additive tables (module-owned migrations, doc 6 consolidates) + archive bucket, subscription, DLQ, and topic provisioning (Rollout).

## Security, privacy, and access

- **Isolation:** one bucket + one account per vendor is the platform's tenancy model — a vendor's SFTP session is rooted in its own bucket and cannot reach any other partner's; enforced by the platform, verified in its Terraform.
- **Authentication:** key-only SFTP logins, platform-managed (Terraform + Secret Manager registration, server-side reconciliation). Rotation policy and cadence are the platform team's; our onboarding request asks for their standard plus notification to us on rotation (Q5).
- **Origin vs integrity (F6):** the dedicated bucket proves *which vendor* (only that vendor's account writes there); per-hop checksums prove *uncorrupted in transit*; end-to-end authorship and completeness are not proven — the documented accepted gap (D6), compensated in doc 4 and Observability.
- **Data sensitivity:** export files carry provider demographic/practice data (names, addresses, phones, NPIs) — no member data. In transit: SSH (SFTP) and TLS (GCS). At rest: GCS default encryption in both buckets; the vendor-bucket copy is transient by lifecycle.
- **Least privilege our side:** the receiver's service account: read + list on the vendor buckets, write on the archive, publish on `accuracy-source-file-received`, subscribe on `sftp-bucket-events`. The export worker's: write on the vendor buckets' `from/` prefixes, write on the archive, read via the platform's OV read APIs. Nothing else.
- **Tenant isolation:** per-tenant prefixes; the filename's `tenantId` must match its prefix — a mismatch is rejected by doc 4's validation, never silently ingested into the wrong tenant.

## Performance and scale

| #   | Item              | Value / assumption                                                                                                      |
| --- | ----------------- | -------------------------------------------------------------------------------------------------------------------------- |
| N1  | Volume            | ~70k-row export monthly ≈ 20–50 MB CSV per vendor per tenant; inbound similar. Even 10 vendors × 10 tenants < 5 GB/month |
| N2  | Cadence tolerance | Monthly exchange → hours of platform downtime have zero business impact; vendors retry. 99% availability is ample        |
| N3  | Detection latency | Event within seconds of upload commit; handoff = event + settle window (default 10 min) — bounded and configurable       |
| N4  | Archive retention | Raw files 7 years, versioned, lifecycle-tiered (aligned with D2-20 — the raw file is evidence)                          |
| N5  | Event reliability | Duplicate/delayed events handled downstream (doc 4's queue + registry); **lost** events caught by the sweep ≤ 1 hour   |

**Cost (monthly, derived):**

- SFTP platform: **$0 marginal** — existing shared infrastructure; one more bucket + account is configuration.
- GCS archive: < 1 GB/month new data; 7-year tiered archive grows slowly → **< $2**.
- Pub/Sub (a few relevant messages/month; the subscription filter discards other partners' events before delivery) → **≈ $0**.
- Receiver + export compute (Cloud Run, minutes/month) → **≈ $0–5**.
- **Total ≈ $2–7/month.**

## Observability

Best-effort, outside transactions, never blocks a transfer.

**Metrics** (low-cardinality labels):

```
attestation.export.run.duration        timer    {vendor, tenant, outcome}
attestation.export.rows                counter  {vendor, tenant}
attestation.sftp.inbound.accepted      counter  {vendor, tenant}
attestation.sftp.handoff.lag           timer    {vendor}
attestation.sftp.generations.superseded counter {vendor}
attestation.sftp.dlq.depth             gauge    {}
attestation.sftp.sweep.discrepancies   counter  {vendor, case}
```

**Alerts:**

- **Export liveness:** no `EXPORT_RUN_COMPLETED` inside a cadence window (+2 days grace) → page — a missed export means the vendor verifies nobody this cycle.
- **Vendor response overdue:** no inbound delivery within N days of an export (per-vendor SLA, configurable — Q3) → notify the vendor-relationship owner.
- **Suspected-partial delivery:** an accepted delivery whose ingested row count (doc 4's reconciliation) is far below the export's row count → warn — the soft signal that replaces the manifest's hard row count; the vendor channel follows up.
- **Receiver DLQ non-empty** → page the module on-call (the receiver is our worker; the platform team owns only the servers).
- Any `SWEEP_DISCREPANCY_FOUND` → ticket automatically.
- Platform-side alerts (server health, disk, auth failures) are the platform team's — our onboarding request confirms coverage (Q1).

**Correlation:** export batch id → filenames → vendor-bucket generation → archive generation → the Pub/Sub message id → doc 4's registry — the custody chain is walkable from either end.

## Audit trail

*Added section — file custody is compliance evidence.*

**Audit table: `attestation_audit_events`** — the shared table doc 1 creates in attestation-db (final DDL doc 6); this module writes its custody events into it and owns no audit table of its own.

Rules: append-only; 7-year retention (D2-20); every event carries tenant + vendor + its batch/object correlation. This module's jobs touch systems that cannot share one transaction (two GCS buckets, Pub/Sub, Spanner) — so its events are **fact records written at their step's commit point** (the step ordering in *Contracts* is what makes a re-run harmless), and each event's row is idempotent on its natural key (object name + generation, or batch id + type).

- **`export_batch_id` and `object_name` are first-class, indexed columns** on these events (doc 6 DDL) — the custody chain is queryable, not buried in JSON.
- A batch id or an object name is a **lookup key**: `WHERE export_batch_id = @x` returns an export's whole story; the object name + generation pins the exact immutable file version, on both the source and archive side.

### The common envelope — every event carries these fields

```json
{
  "id": "ae-42d7…",
  "type": "INBOUND_ARCHIVED",
  "tenantId": "org-xyz",
  "sourceId": "candor",
  "exportBatchId": "org-xyz-candor-2026-09-001",
  "objectName": "inbound/candor/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260903.csv",
  "actor": "system:attestation-sftp-receiver",
  "occurredAt": "2026-09-03T14:15:12Z",
  "detail": { }
}
```

- Same append-only store as docs 1–2; this module's events correlate by `sourceId` + `exportBatchId` + `objectName` where task-lane events use task/submission ids.
- `exportBatchId` — null on events not tied to a batch (an onboarding, a delivery whose filename carries no parseable ref); inbound events carry the filename's `exportBatchRef` once parsed, else null.
- `actor` — `system:attestation-export`, `system:attestation-sftp-receiver`, `system:attestation-sweep`, or a user id for operator actions (a DLQ drain, an onboarding request).

### Per-event contracts — the `detail` fields and where each commits

| Event                       | Committed in                                                | `detail` fields                                                                    |
| --------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| `VENDOR_ONBOARDED`          | its own write, when the platform confirms the account       | `bucket`, `requestedBy`, `platformTicket`                                          |
| `EXPORT_RUN_STARTED`        | its own write, at run start                                 | `cadenceWindow`, `sourceId`, `tenantId`                                            |
| `EXPORT_FILE_WRITTEN`       | its own write, after the archive write                      | `rowCount`, `sha256`, `archiveUri`, `archiveGeneration`                            |
| `EXPORT_PLACED`             | its own write, after the vendor-bucket write                | `vendorBucketUri`, `vendorBucketGeneration`                                        |
| `EXPORT_RUN_COMPLETED`      | its own write, at run end                                   | `rowCount`, `durationMs`                                                           |
| `EXPORT_FAILED`             | its own write, on the final failing attempt                 | `period`, `attempt`, `error` — the export's dead-letter record                     |
| `INBOUND_DELIVERY_DETECTED` | its own write, when the receiver accepts the event          | `sourceBucket`, `sourceGeneration`, `sizeBytes`, `crc32c` — origin evidence        |
| `GENERATION_SUPERSEDED`     | its own write, when the settle check skips an older version | `sourceGeneration`, `newerGeneration`                                              |
| `INBOUND_ARCHIVED`          | its own write, after the archive copy                       | `archiveUri`, `archiveGeneration`, `sourceGeneration`, `handoffLagSeconds`         |
| `DELIVERY_EVENT_EMITTED`    | its own write, after the Pub/Sub publish                    | `pubsubMessageId`, `exportBatchRef` — the custody baton pass to doc 4              |
| `SWEEP_DISCREPANCY_FOUND`   | its own write, per finding                                  | `case` (`LOST_EVENT` \| `STUCK_BATCH`), `objectName`, `action`                     |

- "Committed in" is a contract: each event is written at the step position shown, and re-running the step must not double-write it (idempotent on the natural key above).
- An engineer implementing from this table should never have to invent an audit field. A field found missing during implementation is a gap in this doc — fix it here first.

Custody question answered end to end: *"show the complete chain for the file Candor sent on Sept 3"* — detected (which bucket, which generation, which checksum), archived (where, when, which archive generation), event emitted (which message id) — and from there doc 4's registry takes over.

## Failure modes and rollback

**Edge cases:**

| Case                                                    | Handling                                                                                                   |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Upload dies halfway; `gcsfuse` committed a partial object | `OBJECT_FINALIZE` fires for it; the settle window catches a quick retry (newer generation supersedes); an unretried partial is archived and ingested as a small valid batch — the full file later converges via doc 4's row-grain idempotency; the suspected-partial alert flags the gap |
| Vendor re-uploads the full file after a partial          | New generation → new event → new archived version → new registry batch; rows already ingested from the partial are no-ops in doc 4 — the remaining rows ingest normally |
| Same file re-sent identically                            | New generation, same `crc32c` → doc 4's registry recognizes the identical re-send and skips it            |
| Client uploads via temp-name-then-rename                 | The temp-suffix filter drops the `.part` event; the final name's event proceeds normally (`gcsfuse` rename = copy + delete, so both fire) |
| Filename carries no parseable `exportBatchRef`           | Archived and handed off with `exportBatchRef: null`; doc 4 quarantines as unattributable — never guessed, never dropped |
| Vendor writes into the wrong tenant prefix               | Filename `tenantId` must match the prefix; doc 4 rejects the mismatch with a clear error                   |
| Vendor tries to reach another partner's files            | Impossible below the protocol: one bucket per partner, platform-enforced                                   |
| SFTP platform outage                                     | Vendors retry; nothing downstream is corrupted; platform team's on-call owns recovery; a non-event at monthly cadence (N2) |
| Receiver down                                            | Pub/Sub retains and redelivers (message retention outlasts any realistic outage); on restart the backlog drains in order of redelivery |
| Receiver keeps failing on one message                    | 5 attempts → `attestation-sftp-dlq` + page + operator drain — never an infinite loop                       |
| Delivery event lost (notification never arrives)         | Sweep case (a) re-offers within an hour (F8/N5); duplicates are dedup'd by doc 4's registry                |
| Our outbound write fires an upload event                 | The receiver's `to/` prefix filter ignores `from/` objects — structurally, not by timing                  |
| Export write dies between archive and vendor bucket      | Cloud Tasks retries; the batch row's status shows the completed steps; the vendor-bucket write is idempotent (same bytes, new generation) |
| Export double-fires                                      | Deterministic task name + same batch id + registry-row check → no duplicate batch                          |
| Export retries exhausted                                 | `EXPORT_FAILED` + page — a missed export is a compliance event; the liveness alert is the independent backstop |
| Vendor key compromised                                   | Platform's rotation runbook; blast radius = that vendor's single bucket; every write is attributable to the bucket's account |
| Oversized file (runaway upload)                          | The receiver checks `sizeBytes` before archiving; > 10× expected → DLQ the event with a size-anomaly page before doc 4 parses |

**Rollback:**

- **Outbound:** pause the vendor's Cloud Scheduler job (or the `exportPaused` kill switch) — no new exports; already-placed batches stay with the vendor (files cannot be recalled; a correction is a new batch).
- **Inbound:** the `inboundPaused` kill switch — the receiver acks and skips; uploads simply accumulate in the vendor bucket; on unpause the sweep re-offers everything missed.
- Archived objects and audit events explicitly **stay** — they are custody evidence; the archive is append-only by construction.
- Nothing this module deploys touches the shared platform — unsubscribing and pausing the workers returns the world to today's state.

## Rollout

1. **Platform-team onboarding — done** (TS-111546, closed 2026-09-04): `candor-health` account and `certifyos-production-platform-sftp-candor-health` exist. **Remaining asks to DevOps:** confirm the `OBJECT_FINALIZE` → `sftp-bucket-events` notification on that bucket, the staging bucket, and lifecycle/alerting coverage (Q1). ⚠ Also present the D2-30 scope change (integrate, not build) and the D2-04 amendment (no manifest) for v2 sign-off before build starts.
2. Provision module resources: the archive bucket (versioning, 7-year retention, lifecycle tiers), the filtered subscription on `sftp-bucket-events` + `attestation-sftp-dlq`, the `accuracy-source-file-received` topic; additive schema migration for `attestation_export_batches` + `attestation_export_items` (module-owned migrations, doc 6 consolidates); wire the two service accounts (least-privilege grants per *Security*).
3. Align with the in-flight `sftp-service` (CP-38069): confirm no overlap or, if its transfer engine lands first, whether the outbound placement rides it (Q6).
4. Contract dry-run with Candor: exchange a synthetic file each direction; verify naming (the `exportBatchRef` echo), event flow, settle behavior, archive versions, and a deliberate partial-upload rehearsal (kill a transfer mid-way, re-send, watch doc 4 converge).
5. First real export for the pilot tenant on cadence; watch export liveness, handoff lag, the suspected-partial alert, and the vendor-response window through one full cycle.
6. Additional vendors repeat steps 1/4 — the design is per-vendor by construction; the kill switches are per tenant/vendor at every step.
