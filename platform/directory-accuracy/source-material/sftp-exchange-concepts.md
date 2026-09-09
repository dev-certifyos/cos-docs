# Companion — SFTP exchange module (concepts + supplementary material)

> **RETIRED DRAFT — not a live design document.** This was the companion to doc 3 (SFTP exchange) of the attestation-module series (formerly `attestation-module/modules/03-sftp-exchange/`). The external-vendor program was decoupled from the attestation workflow on 2026-09-08 (source-of-truth v2 decision register entry D2-38, the decoupling decision), and the whole vendor program now has **one** standalone design doc: `platform/directory-accuracy/directory-accuracy.md` (to be written; its companion `directory-accuracy-concepts.md` exists). These drafts were moved here 2026-09-09 as source material for that doc — nothing in them is binding, and the internal "doc N" cross-references below refer to the retired attestation-series numbering.

Vocabulary first, deep-dive material second. Concepts entries carry no design decisions — the module doc (`sftp-exchange.md`) owns the design.

## File transfer and server concepts

### SFTP

- The SSH File Transfer Protocol: file upload/download over an SSH connection — encrypted in transit, authenticated by SSH credentials.
- The de-facto standard for business-to-business file exchange in healthcare; every vendor's tooling speaks it.
- Two properties matter here: it has **no event mechanism** (nothing announces "upload complete"), and an upload is **not atomic** (the file appears byte by byte).

### SSH key authentication

- Login by cryptographic keypair instead of a password: the client holds the private key, the server holds the matching public key.
- The private key never travels; a server compromise leaks no reusable credential.
- Here: key-only logins, one keypair per vendor, vendor-generated, registered through the platform's user-management process.

### The shared CertifyOS SFTP platform

- An existing, Terraform-managed service (`certifyos-infra`): partner → load balancer → a managed instance group of Debian VMs running OpenSSH in SFTP-only mode.
- Underneath each account, `gcsfuse` mounts a dedicated GCS bucket — an uploaded file becomes a GCS object when the client closes it.
- One account + one bucket per partner (~14 in production); usernames and keys live in Terraform variables + Secret Manager, reconciled onto the servers by a timer.
- Operated by the platform/DevOps team; other CertifyOS teams already ride it.

### Managed instance group (MIG)

- GCP's self-healing VM pool: a template defines the machine; the group keeps N healthy copies running behind a load balancer, replacing failed ones automatically.
- Here: the SFTP servers are a MIG — a dead VM is a platform non-event, not an incident for us.

### gcsfuse

- A driver that presents a GCS bucket as if it were a local folder, so ordinary programs (here: the SFTP server) read/write objects as files.
- Its POSIX semantics are incomplete — because object stores are not disks — and the property that matters here: it **commits an object on file close and on `fsync`**, so an interrupted transfer can leave a valid, complete-looking GCS object holding partial content.

### Chroot jail / `ForceCommand internal-sftp`

- OpenSSH mechanisms that lock an account into one directory subtree and into the SFTP subsystem only (no shell, no command execution).
- Here: background for the rejected self-hosted alternative; the shared platform achieves the same isolation with one bucket per partner.

### AWS Transfer Family

- AWS's fully managed SFTP endpoint: no server to run, per-user isolation, uploads land directly in S3.
- Priced per endpoint-hour (~$0.30/h ≈ $216/month) plus data; the reference example of "managed transfer straight into object storage."

## GCS concepts

### GCS (Google Cloud Storage)

- GCP's object store: files become immutable objects in buckets, addressed by name, replicated and durable by default.
- Not a filesystem — objects are written whole; there is no partial state and no in-place edit.

### Object versioning and generations

- Every committed object version gets a **generation** number; with versioning on, writing a name again creates a new generation and keeps the old one addressable forever.
- Here: "the file Candor sent on Sept 3" pins to an exact generation — on both the vendor bucket and our archive.

### `OBJECT_FINALIZE` and bucket notifications

- GCS's built-in eventing: when an object version is committed, the GCS service itself publishes a Pub/Sub message — no application code involved.
- The message carries attributes (`eventType`, `bucketId`, `objectId`, `objectGeneration`, `eventTime`) plus a JSON body with the object's metadata (`size`, `crc32c`, `md5Hash`, timestamps) — metadata only, never file contents.
- Guarantee: the committed *object* is atomically complete as stored. **No guarantee** the uploader finished their logical transfer — a partial upload that gcsfuse committed still fires FINALIZE.

### Pub/Sub subscription filters

- A subscription can carry a filter expression over message **attributes** (equality, `hasPrefix`); non-matching messages are dropped server-side and never billed to the subscriber.
- Here: the module's subscription filters `sftp-bucket-events` down to its own vendor buckets; finer rules (prefix `to/`, temp suffixes) run in receiver code.

### Retention policy and lifecycle tiering

- A bucket retention policy blocks deletion of objects younger than the configured age — immutability as configuration, not discipline.
- Lifecycle rules move aging objects to colder (cheaper) storage classes automatically; a 7-year archive costs pennies.

### Atomic object creation

- A GCS object either fully exists or does not exist — a partially uploaded object never becomes visible to readers.
- This is why the outbound write needs no temp-name dance: the vendor's SFTP listing (via gcsfuse) shows the export complete or not at all.

### CRC32C / MD5 (transfer checksums) vs end-to-end

- GCS computes and stores CRC32C (and usually MD5) for every object and verifies its own transfer hop — corruption between the writer and GCS is caught by the platform.
- An end-to-end checksum would have to be computed on the *producer's* machine and verified after arrival — no such signal exists in this design (no manifest); per-hop integrity (SSH transport checks + CRC32C) is what remains.

## Messaging and reliability concepts

### Pub/Sub

- GCP's messaging service: publishers write messages to a topic; subscribers receive them with **at-least-once** delivery (duplicates possible, ordering not guaranteed); each subscription gets its own full copy of the stream.
- Consumers must therefore be idempotent — here, the receiver dedupes on `(bucket, object, generation)` and doc 4's registry dedupes the same key.

### Dead-letter queue (DLQ)

- A holding queue for messages that failed processing repeatedly, kept for human inspection instead of being retried forever or dropped.
- Pub/Sub supports one natively (max delivery attempts → dead-letter topic); **Cloud Tasks does not** — an exhausted Cloud Task is deleted, so a final-attempt failure record must be written by the handler itself.

### Cloud Scheduler

- GCP's managed cron: fires an HTTP call on a schedule, with run history, retries, and alerting on failed executions.
- External to our services — it fires even when a service is down, and the failure is a visible, recorded event.

### Cloud Tasks

- GCP's managed work dispatcher: created tasks are pushed as HTTP calls to a handler; non-2xx responses are redelivered with growing backoff.
- Refuses a second task with the same **name** — a deterministic name makes double-enqueueing impossible.
- No dead-letter queue; a task that exhausts retries is silently deleted.

### The settle window

- A deliberate delay between "upload event received" and "delivery accepted": wait a fixed interval past the event time, then re-check the object.
- Purpose: an interrupted-then-retried upload produces two generations minutes apart; waiting lets the receiver skip the truncated one and take the newest.
- A mitigation only — a partial upload that is never retried settles as-is; the guarantee against it lives elsewhere (row-grain idempotency).

### Row-grain idempotency

- The property that ingesting the same logical row twice produces the effect once — enforced by a deterministic row identity and database uniqueness, never application memory (non-negotiable #1).
- Here: the mechanism that converts "the transport cannot prove completeness" into a latency detail — a partial file then the full file converges to exactly one ingestion per row. Mechanics (row fingerprint, dispositions) are doc 4's.

### Idempotent steps and the commit point

- An idempotent step produces the same result run once or five times — safe to repeat after a crash.
- A job spanning systems that cannot share a transaction is built as idempotent steps with **one final commit step**; a crash before the commit means the next run redoes everything harmlessly.
- Here: the receiver's handoff (registry offer) is the commit; the export's vendor-bucket write is its last externally visible step, resumable via the batch row's status.

### Reconciliation sweep

- A periodic job comparing two systems that should agree (here: the vendor buckets' inbound prefixes vs the batch registry) and acting on differences.
- The safety net under event delivery: events make things fast; the sweep makes them guaranteed.

### The manifest pattern (rejected here, defined for the alternatives)

- **The data file** is the payload; **the manifest** is a tiny companion JSON *describing* it: which file, how many rows, its checksum, which batch it answers — the packing slip to the data file's box.
- **Manifest-last:** the uploader sends the data file first, the manifest only after that upload fully completes; the receiver reacts to manifests only — making a half-uploaded read structurally impossible and giving end-to-end row count + checksum.
- Its cost, and why it is rejected here: every uploader must adopt the convention — a second file, a checksum computation, a strict ordering rule — a per-vendor protocol negotiation the shared platform's clients do not follow.

### SHA-256 (and streaming it)

- A cryptographic hash: feed it every byte of a file, get a fixed 64-character fingerprint; any single-byte change produces a different fingerprint.
- Here: used only on our **own** outbound files (we control both computation and record); no inbound SHA-256 exists without a manifest.

## Domain and platform concepts

### Accuracy vendor / Candor

- An external service that verifies provider directory data (calls offices, checks sources) and returns recommendations.
- Candor is vendor #1; the design is vendor-agnostic by rule (D2-11: vendor-specific knowledge lives only in adapters/configuration).

### Export batch id / `vendorBatchId` / `exportBatchRef`

- `exportBatchId`: our identifier for one export run (`<tenant>-<vendor>-<yyyy-MM>-<seq>`, e.g. `org-xyz-candor-2026-09-001`), stable across retries of the same period.
- `vendorBatchId`: the vendor's own identifier for their delivery — recorded, never load-bearing.
- `exportBatchRef`: the vendor's **echo of our exportBatchId in their filename** — the tie between their delivery and the request that caused it.

### Batch registry (doc 4)

- The ingestion module's ledger of every inbound batch: registered on the delivery event, keyed by `(bucket, object, generation)`, deduped by CRC32C for identical re-sends, tracked through parsing and dispositions.
- This module's sweep compares bucket reality against it to find lost events.

### Custody chain

- The evidentiary record of a file's journey: which vendor bucket and generation it arrived as, when it was archived, which archive generation, which event handed it downstream.
- Each hop is an audit event with the object name + generation — the chain is queryable end to end, 7 years.

### attestation-db (§6.1 microservices direction)

- The Attestation Module's own database (D2-32): module tables — including this module's `attestation_export_batches` / `attestation_export_items` and the shared `attestation_audit_events` — live there, owned by module services, never in the platform's app-data schema.

### `attestation-module-config`

- The feature's per-tenant configuration entry (doc 1): cadences, offsets, kill switches. This module reads per-vendor export cadence, the settle window, and pause flags from it.

### Tenant / tenant isolation

- A tenant is one customer organization; vendor buckets have per-tenant prefixes, and the filename's `tenantId` must match its prefix — a mismatch is rejected, never silently ingested.

---

# Supplementary material

Deep-dive material behind the module doc (`sftp-exchange.md`): baseline evidence, the full approach analysis with weightage and cost derivations, traceability, context, design rationale, open questions, ticket impact. Vocabulary is in the *Concepts* half of this file, above.

## Baseline — verified current behavior

1. The original search of checked-out repositories (`api-layer`, `core-data-access-layer`, `frontend`, the MDM layers) found zero SFTP code — the infrastructure lives elsewhere. An org-wide GitHub search plus live GCP API inspection (2026-09-01) found it; the next section is the verified picture, and the design now builds on it.

2. The platform precedents this module reuses from doc 1's verified baseline: Cloud Scheduler → trigger → Cloud Tasks (deterministic task names), the 202-trigger pattern, and the fact that Cloud Tasks has no dead-letter queue.

## Existing CertifyOS SFTP platform — verified 2026-09-01

What actually exists today, verified from three independent sources: an org-wide GitHub code search, the live Pub/Sub / Cloud Run / GCS APIs, and Cloud Monitoring/Logging. This grounds the chosen approach in what is actually running.

### The hosted SFTP servers (inbound, partner-facing)

- **Where:** `certifyos-infra` repo, Terraform module `modules/sftp/`, instantiated in `gcp/certifyos-development/terraform/sftp.tf` (staging, `staging.certifyos.com`) and `gcp/certifyos-production-platform/terraform/sftp.tf` (`prod.certifyos.com`). Base VM image built by Packer (`packer/sftp-server.pkr.hcl` → `sftp-base` / `sftp-base-patched`).
- **Architecture:** partner → TCP load balancer (port 2222) → managed instance group of Debian 12 VMs running OpenSSH in SFTP-only mode → **gcsfuse** mounts a per-user GCS bucket prefix at `/data` inside each user's chroot.
- **Tenancy:** one bucket per partner (`certifyos-production-platform-sftp-<username>` in prod, `certifyos-development-sftp-<username>` in staging); ~14 partner accounts live in both environments (Humana, Molina, Optum, United, Magellan, MedStar, MedMutual, Evry, Zing, BSW, CalMHSA, Presbyterian + internal test users). Humana additionally gets a fully isolated module instance (own VPC, own startup script).
- **Folder contract (platform runbook "SFTP Onboarding for Clients", Confluence 1611366414):** exactly two root folders per partner — `/to` (partner read/write/delete; partner uploads) and `/from` (partner **read-only**, never write/delete, "against compliance"; CertifyOS drops files here). Verified live 2026-09-08 on the Med Mutual bucket: `from/REPORTING/<date>/…` written by us, `to/Rosters/…` uploaded by the partner — the bidirectional flow the attestation lane needs already runs in production.
- **Candor (verified 2026-09-08):** user `candor-health` created 2026-09-03 via TS-111546 (requested by Customer Success, key shared via LastPass; DevOps closed 2026-09-04). Prod bucket `certifyos-production-platform-sftp-candor-health` — versioning on, uniform bucket-level access, labels `platform=pdm`, `sftp_user=candor-health`; one test file at `from/Test File for Candor SFTP.docx`. Pub/Sub notification on this bucket not yet confirmed (Q1).
- **User management:** usernames + SSH public keys live in the Terraform module variables and in Secret Manager; a systemd timer on each VM (`sftp-sync-users`) reconciles system accounts, keys, and gcsfuse mounts against the secret. Key-only auth.

### The event flow — who publishes, who consumes

The full lifecycle of one partner upload:

1. **Upload.** The partner's SFTP client streams the file to the VM; gcsfuse stages the bytes and commits the object to the partner's GCS bucket when the client closes the file (or calls `fsync` — see the caveat below).
2. **Publish — GCS itself is the publisher.** Each partner bucket carries a `google_storage_notification` config: *on `OBJECT_FINALIZE`, publish to the topic*. When the object commits, the GCS service (its own service agent, no application code) makes the Pub/Sub publish call to the topic `sftp-bucket-events` (`sftp-humana-bucket-events` for the isolated Humana module — that topic currently has **zero subscribers**).
3. **The message** is an envelope with two compartments: **attributes** — string key-value labels on the outside (`eventType=OBJECT_FINALIZE`, `bucketId`, `objectId`, `objectGeneration`, `eventTime`) that a consumer or subscription filter can inspect without parsing the body — and **data**, the full JSON object resource (`name`, `bucket`, `generation`, `size`, `crc32c`, `md5Hash`, timestamps). Metadata only, never file contents; a consumer that needs the bytes fetches them from GCS separately.
4. **Distribution.** Every subscription attached to the topic gets its own copy. Verified live subscribers:
   - **Prod:** one Eventarc **push** subscription → HTTP POST to the Cloud Run service `sftp-test` (Python 3.13) → posts a Slack notification per event. Active (log-verified same day). ⚠ `sftp-test` was deployed from local source (`cloud-run-source-deploy` registry); its source is in no GitHub repo.
   - **Dev:** the same Eventarc → `sftp-test` pattern, plus one **pull** subscription (`api-layer-gcs-roster-automation-luis-test`) with no code-searchable consumer — an experiment, expires after 31 days idle.
5. **Ack / retry.** Push delivery: a 2xx response acks the message; anything else is redelivered later (at-least-once, so duplicates are normal and consumers must dedupe on `objectId` + `generation`).

**Net:** the only production consumer today is a Slack notifier. No automated file processing hangs off these events — roster ingestion and similar flows run as scheduled Airflow DAGs that poll, not event-driven consumers. Attaching the module's own subscription disturbs nothing.

### Partial-file behavior of the platform

`OBJECT_FINALIZE` guarantees the *object* is atomically complete as stored (no torn bytes, checksums describe exactly what was committed). It does **not** guarantee the partner finished their logical transfer, and the platform does nothing to close that gap:

- gcsfuse commits on `close()` **and on `fsync()`** — a client that fsyncs mid-transfer (common on resume) produces a valid, complete GCS object holding partial content, and FINALIZE fires for it.
- An interrupted-then-retried upload fires FINALIZE once per generation: first for the truncated version, again for the full one.
- A client using temp-name-then-rename fires FINALIZE for both the `.part` name and the final name (gcsfuse rename = copy + delete).
- No sentinel/manifest convention, no generation dedup, no suffix filtering exists anywhere in the current chain — the Slack notifier fires per raw event.

This is the gap the chosen design absorbs deliberately: the settle window + temp-suffix filter + generation dedup handle the mechanical noise; **row-grain idempotency in doc 4 is the correctness guarantee**; the suspected-partial reconciliation alert is the human backstop.

### Outbound SFTP code elsewhere in the org

- `etl-dags` `imports/caqh_roster/sftp.py` — paramiko client pulling dated CAQH ReturnRoster files, with staleness tracking.
- `cdf-dags` `third_party_downloader_dag.py` — Airflow DAG pulling from third-party SFTP sources.
- `certifyos-pulumi` `functions/sftp-archive/main.py` — Cloud Function archiving files older than 30 days across `*sftp*` buckets. ⚠ Touches our vendor buckets too — its behavior inside the settle window is a Q1 confirmation item.
- `roster-processing-cf` — MedStar change-file delivery.

### In-flight: `sftp-service` (epic CP-38069)

A new NestJS transfer service (`CertifyOS/sftp-service` repo): tenant-scoped file transfer between CertifyOS GCS buckets and partner SFTP servers, on demand via a `/v1/*` HTTP API (Cloud Run service) or scheduled (Cloud Run job). Design spec mirrored at `docs/superpowers/specs/2026-08-18-sftp-service-design.md` in that repo. As of this writing only the skeleton is merged (health endpoint + config); the transfer engine is pending. Overlap check is Q6 — our outbound placement could ride it if its engine lands first.

## Approaches analyzed in depth

**Decision criteria (stated before the approaches):**

1. **Origin trust and isolation** — per-vendor identity, no cross-vendor visibility; the security story must be explainable in one sentence.
2. **Correctness under partial and duplicate delivery** — a half-uploaded, re-sent, or duplicated file must never corrupt data or double-ingest a row; the guarantee may live at the transport or at the consumer, but it must be structural.
3. **Durability of the archive** — the raw file is the reprocessing source and 7-year evidence; it must survive any single component dying.
4. **Operational burden** — new infrastructure, new on-call, new conventions to police; fewer are better, and D2-32's own-service direction does not license duplicating platform infrastructure.
5. **Vendor adoption cost** — what the vendor (and every future vendor) must implement beyond plain SFTP: nothing > a naming rule > a protocol convention.
6. **Cost** — with derivation; at these volumes transfer/storage is pennies, so cost differences come from servers and licenses, not data.
7. **Future-limitation risk** — more vendors, more tenants, bigger files, tighter cadences.

**Volume inputs for every estimate:** ~20–50 MB CSV per vendor per tenant per month; < 5 GB/month total transfer even at 10 vendors × 10 tenants; a handful of relevant Pub/Sub events per month.

Weightage scale: **5 = fully satisfies · 3 = workable with caveats · 1 = fails or requires hand-building.**

### A. The chosen approach — existing shared SFTP platform + module receiver + row-grain idempotent ingestion

- **How it works:** see the module doc's *Approach* (steps 1–7).

- **Pros:**
  - Zero new SFTP infrastructure — servers, accounts, keys, and event wiring already exist, are Terraform-managed, and are another team's operated product.
  - Vendor asks nothing beyond plain SFTP + a filename rule — the lightest possible onboarding, repeatable per vendor.
  - Event-driven from the first hop (no polling); detection in seconds, handoff bounded by the settle window.
  - The completeness guarantee (row-grain idempotency) is machinery doc 4 must have anyway for duplicates and replays — no second mechanism to build or police.
  - Cheapest option by an order of magnitude.

- **Cons:**
  - Logical completeness is never proven at the transport — a partial file can be ingested as a small valid batch until the full file arrives; an unretried partial is caught only by reconciliation alerts, not by protocol.
  - No end-to-end checksum — integrity is per-hop only (SSH + CRC32C); vendor-side corruption before upload is undetectable at this layer.
  - A dependency on another team's platform: onboarding lead time, lifecycle rules, and alert coverage are theirs (Q1).

- **Weightage:**

| Criterion                 | Score | Note                                                        |
| ------------------------- | ----- | ------------------------------------------------------------- |
| 1 Origin trust            | 5     | One bucket + account per vendor, platform-enforced           |
| 2 Partial/duplicate correctness | 4 | Structural via consumer idempotency; convergence is eventual, not at-arrival |
| 3 Archive durability      | 5     | Own versioned archive within minutes; both generations recorded |
| 4 Operational burden      | 5     | No infra; two small workers + a subscription                 |
| 5 Vendor adoption cost    | 5     | Plain SFTP + a filename rule                                 |
| 6 Cost                    | 5     | ≈ $2–7/mo                                                    |
| 7 Future limits           | 4     | Scales with the platform; very tight cadences would want a smaller settle window |
| **Total**                 | **33** |                                                             |

- **Failure modes:** partial upload never retried → small batch ingested, reconciliation alert flags the row-count gap · receiver down → Pub/Sub retains and redelivers · event lost → hourly sweep re-offers · platform outage → vendors retry, platform on-call owns it.

- **Cost ≈ $2–7/month, derived:** platform marginal **$0** (existing shared infra; one bucket + account is configuration) · GCS archive < 1 GB/month new data, tiered **< $2** · Pub/Sub (filtered to a few events/month) **≈ $0** · Cloud Run compute (receiver + export, minutes/month) **≈ $0–5**.

### B. Manifest-last convention on the existing platform

- **How it works:** same platform, but each uploader sends data file → then a manifest JSON (name, row count, SHA-256, batch ids) **last**; the receiver reacts to manifests only, verifying count and checksum before handoff.

- **Pros:**
  - Protocol-level completeness proof — a half-uploaded file can never trigger processing; truncation *and* corruption caught before parsing.
  - End-to-end SHA-256 — covers every leg from the producer's machine.

- **Cons:**
  - Every vendor must adopt an extra-file protocol: produce a manifest, compute a checksum, honor upload ordering — a real per-vendor negotiation and integration cost; the platform's other clients follow no such convention.
  - Half-adoption is worse than none: a manifest uploaded first, or describing a stale file, is a trusted signal that lies.
  - Doc 4's row-grain idempotency is required regardless (duplicates, re-sends, replays — non-negotiable #1), so the manifest is a second guarantee for an already-covered case.
  - Convention policing (did each vendor honor it?) becomes a permanent verification burden invisible to the platform's tooling.

- **Weightage:**

| Criterion                 | Score | Note                                                       |
| ------------------------- | ----- | ------------------------------------------------------------ |
| 1 Origin trust            | 5     | Same platform tenancy                                       |
| 2 Partial/duplicate correctness | 4 | 5 if fully adopted; adoption risk makes the signal itself a failure mode |
| 3 Archive durability      | 5     | Same archive design                                         |
| 4 Operational burden      | 4     | No new infra, but a convention to police per vendor         |
| 5 Vendor adoption cost    | 1     | An extra-file protocol per vendor — the deciding failure    |
| 6 Cost                    | 5     | ≈ $2–7/mo, same as A                                        |
| 7 Future limits           | 4     | Each new vendor repeats the negotiation                     |
| **Total**                 | **28** |                                                            |

- **Why it loses:** criterion 5 — its entire value is bought with a per-vendor protocol change, to duplicate a guarantee the consumer must provide anyway. A vendor that volunteers checksums is welcomed opportunistically (module doc, *Integrity*); a design that *depends* on it is not.

- **Failure modes / what would break it:** a vendor that cannot or will not produce manifests (the design has no answer but a per-vendor exception — which is approach A); a lying manifest.

- **Cost:** ≈ $2–7/month infrastructure (same as A) + un-dollared per-vendor negotiation and verification effort.

### C. Self-hosted OpenSSH SFTP host + GCS mirror + manifest-last (the previous recommendation)

- **How it works:** a small hardened VM runs OpenSSH; one chrooted key-only account per vendor; a 5-minute pickup job mirrors uploads to the versioned archive and publishes the pipeline event; manifest-last is the completeness signal; an hourly sweep backstops.

- **Pros:**
  - OS-enforced isolation and origin attribution from the depositing account; full control of layout, rotation, monitoring.
  - With manifest-last, partial-file reads are structurally impossible at arrival.
  - The box is disposable — everything durable in GCS within minutes.

- **Cons:**
  - Duplicates the existing platform: second key ceremony, second monitoring, second on-call, second Terraform surface — for one lane's monthly traffic.
  - Its completeness guarantee is the manifest convention — inheriting approach B's vendor-adoption failure in full.
  - A real VM to patch; a polling hop (raw SFTP has no events) the platform already eliminated with bucket notifications.

- **Weightage:**

| Criterion                 | Score | Note                                                      |
| ------------------------- | ----- | ------------------------------------------------------------ |
| 1 Origin trust            | 5     | chroot + per-vendor key — OS-enforced                      |
| 2 Partial/duplicate correctness | 5 | Manifest-last + size-stability + `.part`-rename           |
| 3 Archive durability      | 5     | Mirror ≤ 5 min; box disposable                             |
| 4 Operational burden      | 2     | A second SFTP surface beside an existing platform          |
| 5 Vendor adoption cost    | 1     | Manifest protocol required — same as B                     |
| 6 Cost                    | 3     | ≈ $20–45/mo                                                |
| 7 Future limits           | 3     | Multi-GB files want streaming tuning; scale means growing a parallel platform |
| **Total**                 | **24** |                                                           |

- **Why it loses:** criteria 4 and 5 — it rebuilds an operated platform to host a convention vendors must be persuaded to follow; every point it wins on criterion 2 is available more cheaply as consumer-side idempotency.

- **Failure modes / what would break it:** everything in B's adoption story, plus VM operations and pickup-job liveness as new failure classes.

- **Cost ≈ $20–45/month, derived:** VM e2-small + disk **$15–40** · GCS **< $2** · pickup compute (8,640 listings/month × ms each) **≈ $0–2** · plus the duplicated operational surface (un-dollared).

### D. AWS Transfer Family

- **How it works:** AWS's managed SFTP endpoint writes straight to S3; per-user IAM isolation; a sync job then copies S3 → GCS, where the pipeline, database, and audit live.

- **Pros:**
  - Zero servers — the strongest managed SFTP on any cloud; isolation and key management are product features.
  - If CertifyOS had no SFTP platform and were AWS-native, a serious candidate.

- **Cons:**
  - ~$0.30/hour ≈ $216/month idle — to replace working infrastructure whose marginal cost is $0.
  - A second cloud: IAM federation, S3→GCS sync, cross-cloud monitoring, second bill, second on-call.
  - Compliance evidence transits two clouds — an extra custody hop; detection events originate outside GCP.
  - Without a manifest it has approach A's completeness gap anyway (S3 events are per-object, same semantics).

- **Weightage:**

| Criterion                 | Score | Note                                                   |
| ------------------------- | ----- | ---------------------------------------------------------- |
| 1 Origin trust            | 5     | Per-user IAM — excellent                                 |
| 2 Partial/duplicate correctness | 4 | Same consumer-side guarantee as A                       |
| 3 Archive durability      | 3     | Durable in S3, then a sync hop to reach the real archive |
| 4 Operational burden      | 1     | No server, but a whole second cloud's operations         |
| 5 Vendor adoption cost    | 5     | Plain SFTP                                               |
| 6 Cost                    | 1     | ≈ $220–240/mo, ~100× A                                   |
| 7 Future limits           | 5     | Scales effortlessly                                      |
| **Total**                 | **24** |                                                          |

- **Why it loses:** criteria 4 and 6 — the existing platform makes the whole category moot; this buys a second cloud's operations and ~$216/month idle for no guarantee A lacks.

- **Failure modes / what would break it:** everything in A, plus S3→GCS sync lag/failures — a new class with nothing bought.

- **Cost ≈ $220–240/month, derived:** endpoint $0.30/h × 730 h ≈ $216 · data $0.04/GB ≈ $0 at our volume · S3 storage < $1 · sync-job compute < $5 · plus un-dollared second-cloud operations.

### Comparison

| Criterion                     | A Shared platform + receiver | B Manifest on platform | C Self-hosted + mirror | D AWS Transfer Family |
| ----------------------------- | ---------------------------- | ---------------------- | ---------------------- | --------------------- |
| 1 Origin trust                | 5                            | 5                      | 5                      | 5                     |
| 2 Partial/duplicate correctness | 4                          | 4                      | 5                      | 4                     |
| 3 Archive durability          | 5                            | 5                      | 5                      | 3                     |
| 4 Operational burden          | 5                            | 4                      | 2                      | 1                     |
| 5 Vendor adoption cost        | 5                            | 1                      | 1                      | 5                     |
| 6 Cost                        | 5                            | 5                      | 3                      | 1                     |
| 7 Future limits               | 4                            | 4                      | 3                      | 5                     |
| **Total (unweighted)**        | **33**                       | 28                     | 24                     | 24                    |
| **Cost (monthly)**            | **≈ $2–7**                   | ≈ $2–7 + negotiation   | ≈ $20–45 + 2nd surface | ≈ $220–240 + 2nd-cloud ops |

**Recommendation rationale:** A leads or ties every criterion except at-arrival completeness, where it concedes one point that B and C buy only with the vendor-adoption cost that disqualifies them (criterion 5 = 1). The concession is priced: convergence is eventual (bounded by the vendor's own retry plus reconciliation alerts), and the machinery that makes it safe — row-grain idempotency — is a non-negotiable the ingestion layer owes regardless of transport.

## Requirements traceability

| #   | Requirement                                                                                                                          | Trace                    |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| F1  | Outbound on the vendor cadence, placed for SFTP download; archived before placement                                                    | v2 §6.3 — ⚠ manifest leg amended (see flags below) |
| F2  | The export batch carries a batch identity the vendor must echo back (`exportBatchRef`, in the filename) — ties every delivery to its request | v2 §6.3                  |
| F3  | Inbound trigger = the platform's object-commit event; every accepted delivery handed off exactly once; completeness = consumer contract | v2 §6.4 — ⚠ amends D2-04 (see flags below) |
| F4  | Every accepted delivery archived immediately — immutable, versioned; the archive is the system of record                               | audit brief §4, v2 §6.4  |
| F5  | Per-vendor isolation: one dedicated account + bucket per vendor, key-based, platform-enforced                                          | v2 §6.3                  |
| F6  | Origin proven by the vendor's dedicated bucket; integrity per hop (SSH + CRC32C); the end-to-end gap documented and compensated        | audit brief §4           |
| F7  | Exactly one pipeline event per accepted delivery; raw platform events never reach doc 4                                                | v2 §6.4                  |
| F8  | A reconciliation sweep against the batch registry — a lost event or stuck batch is found, never silently missed                        | v2 §6.5                  |
| F9  | Onboarding requirements, folder conventions, and alerting asks specified well enough for the platform team to onboard a vendor without consulting us | D2-30 (⚠ scope narrowed — see flags below) |

Non-functional numbers: module doc, *Performance and scale*.

**⚠ Proposed changes to v2 (amend only after review — §8.1):**

- **D2-04** ("manifest-last delivery; ingestion triggers on the manifest, never the data file") → replaced: ingestion triggers on the platform's object-commit event via the module receiver; completeness is guaranteed by row-grain idempotent ingestion, not by a producer signal. §6.3/§6.4 manifest wording updated accordingly.
- **D2-30** ("SFTP: we design, DevOps builds and operates") → narrowed: the SFTP platform already exists and is operated by the platform team; we author integration requirements and build only the module's receiver/export workers.
- **Instruction file §6** lists D2-04 among constraints not to reopen — update alongside the v2 amendment.
- **Doc 4 (ingestion, draft)** inherits: registry natural key `(bucket, object, generation)` + CRC32C re-send dedup (was: manifest checksum); sha256/rowCount/manifest validation steps removed; `exportBatchRef` parsed from filename (null → unattributable quarantine); **new requirement: row-grain idempotency** (deterministic row fingerprint, duplicate rows = no-op disposition); suspected-partial reconciliation feeding the Observability alert. Doc 4 is unticked — these land in its revision, not as amendments.

## Context from previous module docs

From **doc 1 — Cycle**, this doc inherits and does not re-decide:

- The export selects practitioners with **`OPEN` tasks** (overdue tasks are still `OPEN` — OVERDUE is derived, doc 1's D9; the scan index is the read path); every export row carries the **deterministic identity** `(tenant_id, practitioner_id, due_period)` unchanged (physical column `certify_practitioner_id`).
- **Task rows carry no OV data** (doc 1's D10): identity from the task row, data-to-verify fields from a live OV read at export time (through the platform's read APIs, per the §6.1 microservices direction).
- Export cadence per vendor from `attestation-module-config` (monthly for Candor).
- The trigger → 202 → Cloud Task pattern with deterministic task names, and the fact that Cloud Tasks has no dead-letter queue (the final-attempt record is the dead letter).
- The shared append-only audit store in attestation-db and its rules (7-year retention, indexed correlation columns).

From **doc 2 — Portal lane**: nothing structural; the contract style (one owner, guards where the data lives, implementation-ready payloads) carries over. The D2-21 refinement (non-PDM = direct API consumption, no webhooks) affects ticket DA-12 — recorded in *Ticket impact*.

Exports to later docs:

| Export                                                            | Used by                                  |
| ------------------------------------------------------------------ | ----------------------------------------- |
| The filename contract, both directions (*Contracts*)               | Ingestion (4) — `exportBatchRef` parsing; O-1 agenda |
| The archive layout + generations (*Data model*)                    | Ingestion (4) — where batches live       |
| The `accuracy-source.file.received` event payload (*Contracts step 5*) | Ingestion (4) — its wake-up contract  |
| The row-grain idempotency contract (*Approach step 4*)             | Ingestion (4) — its hardest requirement  |
| The export batch id format + `exportBatchRef` echo                 | Ingestion (4) — batch matching           |
| Custody audit events (*Audit trail*)                               | Database (6), Operations                 |

## Design rationale — anticipated questions

**"Why no manifest? The last version of this design required one."**

- The manifest's value — proof of completeness and an end-to-end checksum — is real, but its price is a per-vendor protocol change: an extra file, a checksum computation, a strict upload order. The shared platform's other clients follow no such convention, and each vendor negotiation would carry it as an integration ask.
- The guarantee it buys is one doc 4 must provide anyway: duplicates, re-sends, and replays already require row-grain idempotency (non-negotiable #1). With that in place, a partial file is a latency problem, not a correctness problem.
- A vendor that volunteers checksums (a `.sha256` sidecar) is verified opportunistically — welcomed, never required, never load-bearing.

**"What exactly happens when a vendor's upload drops at row 10 of 100?"**

- gcsfuse commits whatever was flushed; `OBJECT_FINALIZE` fires; the receiver settles, archives, and hands off a valid 10-row file. Doc 4 ingests 10 rows.
- The vendor's client retries (the normal case): a new generation with 100 rows → new event → new batch → doc 4 ingests rows 11–100; rows 1–10 are no-op duplicates. Data converges with zero human involvement.
- The vendor never retries (the pathological case): the suspected-partial alert (ingested rows ≪ export rows) plus the vendor-response-overdue window put a human on it — machinery detects, people chase.

**"Why the settle window instead of reacting to the event immediately?"**

- An interrupted-then-retried upload produces two generations minutes apart. Reacting instantly ingests the truncated generation, then the full one — correct but wasteful (a quarantine-and-converge cycle for nothing).
- Waiting 10 minutes and taking the newest generation absorbs that case for free. At monthly cadence the added latency is invisible; the window is per-vendor config (`settleWindowMinutes`).
- It is explicitly a mitigation: a partial that is never retried settles as-is — which is why the guarantee lives in doc 4, not here.

**"Why does the module need its own receiver — can't doc 4 subscribe to `sftp-bucket-events` directly?"**

- The raw topic is bucket-level noise: every partner's uploads (14+ accounts), our own outbound writes, temp files, one event per generation, no batch context.
- The receiver converts that into exactly one context-rich event per accepted delivery — vendor, tenant, `exportBatchRef`, both URIs, both generations, checksum — and is the natural home of the custody audit (`INBOUND_DELIVERY_DETECTED`, `INBOUND_ARCHIVED`).
- It also owns the transport-side mechanics (settle, temp filter, archive copy) that doc 4 should never know about — doc 4 starts at "an archived delivery exists."

**"Why archive to our own bucket when the file is already in GCS?"**

- The vendor bucket is the platform's, with platform-owned lifecycle (a shared archival function ages files out of `*sftp*` buckets). Our 7-year compliance evidence (D2-20) cannot depend on another team's hygiene rules.
- The archive bucket is versioned, retention-locked, and module-owned; the copy is server-side (no bytes transit our services) and records both generations — custody provable on both sides.

**"Isn't CRC32C weaker than the manifest's SHA-256?"**

- Different jobs. CRC32C is GCS's transfer-hop integrity check — it proves the committed object matches what the writer sent on that hop. The manifest's SHA-256 was end-to-end — producer's machine to our archive.
- Without a producer-side signal, end-to-end proof is impossible by construction — no checksum we compute on arrival can know what the vendor intended to send. The design says so explicitly (module doc, *Integrity*) instead of pretending a local hash adds proof.
- What CRC32C does earn here: recognizing an identical re-send (same bytes, new generation) so doc 4 skips it whole.

**"How do inbound rows find their obligation without a manifest's `exportBatchRef` field?"**

- The echo moved from a JSON field to the filename: the vendor names their delivery with our batch id. Doc 4 validates it against `attestation_export_batches` and attaches rows via `attestation_export_items` — the mechanism is unchanged, only the carrier moved.
- A filename that does not parse degrades safely: the delivery archives and hands off with `exportBatchRef: null`, and doc 4 quarantines it as unattributable — visible, counted, never guessed.

**"What about the in-flight `sftp-service` (CP-38069)?"**

- It is a transfer engine (CertifyOS buckets ↔ partner SFTP servers) — a different direction than our inbound lane (partners push to us), but its outbound half could eventually carry our export placement.
- Today only its skeleton is merged, so this design does not depend on it; Q6 keeps the coordination explicit before build.

**"Who cleans the vendor's `from/` folder — the vendor cannot delete there?"**

- Fetched-or-not, outbound copies are re-creatable from the archive at any time — the vendor-bucket copy is a courtesy buffer.
- The platform's existing archival function ages files out of the `*sftp*` buckets; our only ask is that its rules respect the settle window on `to/` (Q1).

**"Why one archive bucket rather than one per vendor?"**

- Isolation vendors need is at the *SFTP* layer (one bucket per partner) — vendors never see our archive. Inside it, `<vendor>/<tenant>/` prefixes give uniform retention/versioning config, one lifecycle policy, one place to audit; per-vendor archive buckets would multiply configuration with no security gain (access is ours alone either way).

## Open questions and sign-offs

| #   | Question                                                                                                                                                  | Owner                          |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| Q1  | Platform-team confirmations for onboarding: our vendors on the shared prod servers or an isolated instance (the Humana pattern)? Lifecycle rules (the `sftp-archive` function) on our vendor buckets vs the settle window? Alerting coverage and rotation-notification to us? Onboarding lead time? | SFTP platform team               |
| Q2  | Candor's side of the contract: the filename `exportBatchRef` echo, tenant prefixes, and the CSV schema (doc 4 / O-1) — the *Contracts → file naming* section is the agenda input | The O-1 technical call           |
| Q3  | Vendor response SLA — days after our export before "overdue" alerts fire                                                                                   | Product + vendor contract        |
| Q4  | Confirm raw-file retention = 7 years in the versioned archive bucket (aligning with D2-20)                                                                 | Compliance                       |
| Q5  | Key rotation: confirm the platform's rotation policy meets our vendor-contract needs, and that we are notified on rotation                                 | SFTP platform team               |
| Q6  | **Resolved 2026-08-31: per-practitioner export membership = `attestation_export_items`** (module doc, *Contracts step 1* + *Data model*): one row per exported practitioner per batch, carrying the identity and task. Export timing unchanged: runs at the **start** of each cadence period; still-open obligations re-export each run (v2 §6.3 as amended); a task opened and submitted entirely between two runs is never exported (lanes independent, D2-09). **Reopened leg 2026-09-05:** coordinate with `sftp-service` (CP-38069) — does our outbound placement ride its transfer engine once merged? | Closed (membership) / open (CP-38069 coordination) |
| Q7  | v2 amendments sign-off: D2-04 replacement, D2-30 narrowing, §6.3/§6.4 manifest wording, instruction §6 list — flagged in *Requirements traceability*       | Dev + team review                |
| Q8  | Settle window default (10 min) — long enough for real vendor client retry behavior? Revisit after the Candor dry-run's partial-upload rehearsal            | Module team, post-dry-run        |

## Ticket impact

| Ticket | Impact                                                                                                                                                                                                      |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DA-14 (source SFTP onboarding and pickup) | **Rescoped**: no VM, no pickup/mirror job, no manifest handling. New acceptance criteria: the filtered subscription + DLQ, the receiver (temp filter, generation dedup, settle window, archive copy, enriched event), the hourly sweep, and the platform onboarding request for the pilot vendor. |
| New — outbound export job | **No ticket exists for the outbound side** (the breakdown predates v2's outbound exchange). New ticket: export worker + Cloud Scheduler cadence + archive-first writes + vendor-bucket placement + idempotent batch ids + `attestation_export_batches`/`attestation_export_items`. No SFTP client code — pure GCS writes. |
| New — platform onboarding request | Replaces the former "DevOps build/operate" handoff ticket: vendor account + bucket via the platform team's standard process, notification confirmation, lifecycle/alerting answers (Q1). **Account + bucket already done for Candor (TS-111546, 2026-09-04); only the notification/lifecycle confirmations remain.** |
| New — doc 4 row-grain idempotency | The completeness contract this design delegates: deterministic row fingerprint, duplicate-row no-op disposition, registry key change to `(bucket, object, generation)`, unattributable-delivery quarantine, suspected-partial reconciliation. Lands in doc 4's revision (unticked draft). |
| DA-12 (non-PDM via webhook dispatcher) | **Flagged superseded** by the D2-21 refinement (doc 2): non-PDM clients consume the attestation-task APIs directly — no webhook dispatcher. Close or rescope when doc 5 lands the API-credential model. |
| DA-15 / DA-16 | Begin where this module ends (doc 4) — they inherit the doc 4 ripples above, not changes here. |
