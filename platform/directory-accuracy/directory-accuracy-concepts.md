# Directory accuracy service — concepts and pre-design companion

**What this file is:** the concepts companion for the directory accuracy service design doc (`directory-accuracy.md`, in this folder — **not yet written**; this companion precedes it and collects the vocabulary plus the pre-design analysis the doc will build on). Companion rules apply: local prep material, never shared, never referenced by the design doc.

**The service in one line:** the standalone program — decoupled from the attestation workflow on 2026-09-08 (v2 D2-38) — that selects practitioners by a tenant-configured query, exports them to an external accuracy vendor (Candor first), ingests the vendor's recommendations into its own staging store, lets a payer reviewer approve / reject / skip each row, and releases approved rows to the Golden record under the vendor's own source.

---

## Part 1 — Concepts

### The program

- **Directory accuracy program** — a recurring verification loop: send provider data to an external vendor, get back per-attribute recommendations ("this phone is wrong, the correct value is X"), review them, apply the accepted ones to the Golden record. Its outcome is a more accurate provider directory. It shares nothing with the attestation workflow except the Golden-record merge (D2-38).
- **External accuracy vendor** — a company that verifies provider-directory data against its own sources and returns recommendations with evidence. Candor is the first; the design is vendor-agnostic and each vendor is one adapter (D2-11).
- **Selection query** — a tenant-configured filter that decides which NPIs are sent to the vendor on each run. Stored in tenant configuration; language/format is an open decision (v2 O-17). Unrelated to attestation windows — that independence is the core of D2-38.
- **Cadence** — how often the export runs per tenant. Configurable; first of the month for Candor today.
- **Recommendation** — one vendor statement about one attribute of one practitioner: a correct/incorrect determination, the corrected value when incorrect, and usually an evidence link.

### Transport and arrival

- **SFTP** — file transfer over SSH: an account, a folder tree, upload/download. The exchange mechanism vendors already speak.
- **Shared SFTP platform** — CertifyOS's existing DevOps-operated SFTP service (gcsfuse-backed: the folder tree is really a GCS bucket). One account + bucket per vendor; fixed folders `from/` (CertifyOS drops files; vendor read-only) and `to/` (vendor drops files). Candor's account `candor-health` is live (TS-111546).
- **Bucket event** — GCS emits a notification when an object finishes uploading. These events can arrive duplicated, delayed, out of order, or (rarely) not at all — the pipeline must tolerate every case.
- **Pub/Sub** — GCP's durable message queue: persists events, retries delivery, supports dead-lettering. The bridge from "file arrived" to "worker runs".
- **Dead-letter queue (DLQ)** — where a message goes after exhausting its delivery attempts, instead of being dropped or retried forever. Parked messages are visible and replayable by an operator.

### Ingestion reliability vocabulary

- **Batch registry** — a table recording every inbound delivery ever seen (identity + checksum + status). A re-delivered or replayed file matches an existing row and is skipped — the duplicate-event shield.
- **Idempotency** — running the same work twice produces the effect once. Enforced with deterministic identities plus database unique constraints, never application memory.
- **Row fingerprint** — a deterministic key per data row (e.g. hash of normalized identity + attribute + value). Makes ingestion resumable at row grain: a crashed chunk re-runs and every already-inserted row is a no-op.
- **Chunking** — splitting one large file into row ranges and processing each range as its own retryable task. Bounds memory, parallelizes work, and shrinks the blast radius of a crash to one chunk.
- **Vendor adapter** — the single component holding one vendor's file knowledge: columns, enums, quirks, schema versions. An unknown schema version rejects the whole batch — nothing is guessed or partially staged. Everything outside the adapter is vendor-free.
- **Disposition** — the exactly-one outcome every inbound row receives (actionable, no-op, duplicate/stale, excluded, quarantined, previously rejected). All counted, all audited; counts must reconcile against the batch.
- **Quarantine** — "decide later": a row parked because a dependency it needed was unavailable or its scope is ambiguous. Retried or operator-replayed; never mislabeled, never lost.
- **Rejected-recommendations memory** — a lookup table keyed on (tenant, practitioner, attribute, operation, normalized value). A vendor re-sending something a reviewer already rejected is suppressed from the queue (audit kept) instead of wasting reviewer time (D2-07).

### Review and release

- **Staging store** — the service's own database tables where parsed recommendations wait for review. Nothing here touches the Golden record.
- **Reviewer actions** — approve, reject (mandatory reason; feeds the rejected-recommendations memory), skip. Exact skip semantics and cross-cadence re-send policy are open (v2 O-18).
- **Sync Latest** — the explicit release action: approved rows are handed to Golden-record processing. The only path by which this service's data reaches provider truth.
- **Source registry / slice source** — every staged row carries a `source_id` from a registry; at release the service writes under its tenant-scoped source `candor:{tenantId}` (one per vendor, D2-37). Keeps the vendor's released values in their own slice, unable to overwrite any other source's.

### Golden-record integration (the only external touchpoint)

- **Golden record / OV** — the platform's consolidated one-record-per-provider truth, merged from many sources.
- **Survivorship** — per-tenant ranking rules deciding which source's value wins a field when sources disagree. The only place this program and the attestation program ever meet: if both release a value for the same field, ranking arbitrates (D2-18).
- **contribution_map** — per-field provenance the OV keeps: which source won each field. "This address came from Candor" stays answerable from the Golden record itself.
- **Slice** — one source's row per practitioner in the core tables. The DAL's slice UPSERT replaces the whole `data` JSON, so the release worker must rebuild the complete slice from its released-items ledger on every sync — never sparse-write (D2-37 consequence).

### Architecture vocabulary

- **Bounded context** — a domain area with one owner, one model, one datastore. This whole program (export → ingest → review → release) is a single bounded context; that is why it is one service, not two.
- **Deployable** — one independently deployed runtime. One service can ship several deployables sharing one codebase and one database.
- **Worker/API split** — the standard batch pattern: a small always-on API deployable for serving (reviewers query and act all month) and a heavy worker deployable for bursts (parse days), scaled and sized independently.
- **Control plane vs data plane** — big-batch systems keep workflow state (batches, checkpoints, decisions, audit) in a small transactional store and bulk rows in object storage/warehouse. Named scale-out path for this service if volumes ever demand it; not built initially.
- **Write-Audit-Publish (WAP)** — industry batch idiom: land data in staging, run audits/gates, publish only what passes. This service is WAP with a human reviewer as the audit gate.
- **Streaming parse** — reading a file record-by-record with bounded memory, never loading it whole. Mandatory above a few hundred thousand rows.

---

# Supplementary material (pre-design)

## 1. Status and inputs

- The design doc `directory-accuracy.md` is **not yet written**; it follows the module-doc template and house style from `cos-docs/attestation-module/instructions.md` (§4) when authored.
- This companion was created 2026-09-09 to hold the vocabulary and the analysis accumulated between the decoupling decision (2026-09-08) and the doc's authoring, so nothing is lost in between.
- Source material for the doc: the retired attestation-series drafts, **moved into this folder on 2026-09-09** as `source-material/sftp-exchange.md`, `source-material/sftp-exchange-concepts.md`, `source-material/ingestion.md`, `source-material/ingestion-concepts.md` (mechanics largely reusable; task-attribution machinery is dead), the `attestation-module/reference/candor-*` files (contract proposal, data-dictionary reading guides, Sept-2026 sample analysis), and source-of-truth v2 §6.3–§6.6.

## 2. Decisions already made that bind the doc

| Decision | Substance |
| --- | --- |
| D2-38 (2026-09-08) | The program is decoupled from attestation: query-selected population, own cadence, own staging and review, no task coupling, own service, **one** standalone design doc (replaces the former SFTP-exchange + ingestion module docs). |
| One doc, not two (2026-09-08) | Transport shrank to "reuse the shared SFTP platform" and the task-attribution machinery died with D2-38; the remaining content is one bounded workflow. Split later only if review size demands it. *(Executed 2026-09-09: the two attestation-series drafts were retired out of that series into `source-material/` here; this folder's single doc is the only live design doc for the program.)* |
| D2-37 | Release under `candor:{tenantId}` (one tenant-scoped source per vendor); rebuild-full-slice sync, never sparse-write. |
| D2-03/04/05/06/07 | SFTP folders `from/`/`to/`; manifest-last delivery; event → durable queue → batch registry → idempotent worker; internal contract proposal before the Candor call (O-1); rejected-recommendations lookup. All re-homed from the attestation series to this doc. *(The 2026-09-05 SFTP rewrite proposed dropping the manifest for filename identity + row-grain idempotency — that proposal is pending review and this doc inherits the open question.)* |
| Attestation-module non-negotiables (instructions §5) | Apply unchanged: idempotency, same-transaction audit with 7-year retention, observability, server-side tenant isolation, source-agnostic naming, quarantine over guessing, nothing silently discarded, Golden-record engines extend-only, ISO 8601. |
| Vendor-agnostic naming (instructions §5 item 6) | No vendor name in any table, topic, class, or API name. Vendor names live only in registry/config values (`candor` source row, `candor:{tenantId}` slice source), adapter registration keys, per-vendor infrastructure identities (`candor-health` account), and prose. |

## 3. Pre-design analysis (2026-09-08/09)

### 3.1 Why a separate service — the concrete forcing functions

Domain separation alone never justifies a service; these four boundaries do:

- **Resource-class conflict.** Ingestion's worst case is a multi-GB file: GB-scale working set, minutes of saturated CPU. The review API's steady state is 512MB and millisecond reads. Co-located, every instance is sized for the worst batch year-round, and a parse burst competes with reviewer latency. Separated, the batch class scales to zero between cadence runs.
- **Untrusted input needs a credentials boundary.** The service's core job is parsing hostile external files (malformed CSV, parser CVEs, decompression bombs). Its runtime gets a service account scoped to vendor-bucket read + own DB + the release call — least privilege enforced in IAM, not code review. Inside another backend, the parser would run with that backend's full credentials.
- **Release cadence divergence.** Vendor adapters churn (Candor's dictionary drifted twice already — CT-011); the attestation workflow is stable post-MVP. Merged, every adapter hotfix redeploys and re-risks an unrelated user-facing system.
- **The transactional test — and the precedent that flipped.** The one argument that kills service splits is a shared transaction or chatty synchronous call across the boundary. The cycle-doc analysis of 2026-09-01 rejected a separate attestation database on exactly that test (reminder writes inside both critical transactions; companion 5.F, weightage 21 vs 34). Run the same test on this boundary post-D2-38: shared transactions zero, shared tables zero, synchronous latency-path calls zero — inputs are files and events, the release write is an HTTP hop in either design. The same test that rejected the last split approves this one.
- **Asymmetry.** A wrong split costs one extra pipeline and dashboard, and folding back is mechanical. A wrong merge costs OOM-coupled reviewer outages, hostage release trains, and a later extraction. Under uncertainty, take the boundary.

### 3.2 One service, two deployables

- **One microservice** in the architectural sense — one bounded context, one database, one repo, one design doc. The staged rows are written by ingestion and consumed by review: same lifecycle, same schema, same owner.
- **Two deployables** inside it: `*-api` (review/serving: queue queries, actions, Sync Latest trigger — small instances, always-on, low QPS all month) and `*-worker` (parse/chunk processing — large instances, scales to zero between cadence runs).
- Both use the same data-layer code from the same repo; the worker never inserts bulk rows through the API.
- Per-deployable service accounts: the worker's SA reads the vendor bucket and writes the DB; the API's SA holds the DB and the release path. The credentials boundary is per-deployable, not per-repo.
- Rejected shapes: **two separate services** forces either a shared database (two owners of one schema — antipattern) or data replication between two halves of one workflow (pure waste). Sync worker starts in the API deployable (reviewer-driven, modest volume); a bulk release path can reuse the chunk-worker infrastructure later.

### 3.3 Language: JVM vs Node/TypeScript

- The workload is **not** purely I/O-bound: SFTP/GCS/DB calls are I/O, but the hot loop — streaming parse, per-row normalization, fingerprinting — is CPU interleaved with I/O. Node's event-loop advantage applies to high-concurrency request serving, which this service is not (one batch at a time + a low-traffic API).
- Node/TS fundamentals: non-blocking I/O by default, JSON-native, fast cold start, low idle memory, fast iteration. Weaknesses: single-threaded CPU (a tight parse loop blocks the event loop — needs worker_threads discipline), compile-time-only types (runtime validation must be hand-built), backpressure care in stream pipelines.
- JVM fundamentals: JIT-compiled hot loops (typically 2–5× Node on tight transforms), real + virtual threads (batch isolated from API natively), runtime-enforced types, mature streaming parsers. Weaknesses: baseline memory, cold start, ceremony.
- **Verdict:** at realistic volumes both work and the choice is team fluency. At the 1M-row stress scenario the JVM edge becomes material (parse throughput + fewer footguns on exactly the hot path) — if that scale is a stated requirement, pick JVM. Industry data planes (Kafka, Spark, Flink, Beam, Gobblin) are JVM for these reasons; Node is essentially absent from bulk data planes while ubiquitous in serving APIs. A TS api-deployable + JVM worker is a coherent split if the team wants it.

### 3.4 Database: relational + JSON column vs MongoDB

- What Mongo is fundamentally good at — schema-flexible documents (heterogeneous vendor rows land as-is) and horizontal sharding — is respectively **capturable elsewhere** and **unused** at this volume.
- What the workload fundamentally is: a constraint-and-transaction workflow. Idempotency = unique constraints (batch checksum, row fingerprint, one decision per row); disposition + audit + state flip must commit atomically (non-negotiable: audit in the same transaction); counts must reconcile; 7-year audit favors SQL analytics. Relational gives these as core primitives; Mongo's multi-document transactions are a bolted-on feature with real costs.
- **Sharding trap:** a sharded Mongo collection cannot enforce a global unique index that excludes the shard key — Mongo's scale mechanism breaks the idempotency primitive exactly when invoked. Spanner keeps global unique indexes while scaling horizontally.
- **Recommendation to argue in the doc:** relational store (Spanner-class) with a JSON column holding the raw vendor row — rigid where the workflow needs rigidity, flexible where vendors vary. Spanner specifics: hash/UUID-prefix keys to avoid hot-spotting on monotonic (batch, row-number) keys; size commit batches under the per-commit mutation cap.

### 3.5 Scale stress test (1M-row file; 10,000 files)

- **The file format breaks first.** XLSX's hard ceiling is 1,048,576 rows and XLSX libraries load workbooks into memory. The vendor contract (O-1) must mandate CSV/NDJSON (gzip acceptable) above ~500k rows, plus declared row counts and a duplicate-rate ceiling.
- **Chunking is mandatory, not optional.** One process on 1M rows hits request timeouts, ack deadlines, and all-or-nothing restarts. Row-range chunks (~5–10k rows) over seekable GCS reads, one retryable task each, row-fingerprint idempotency so crashed chunks re-run as no-ops.
- **Humans break next.** 1M staged rows × all-manual review ≈ 700 reviewer-days per file. Manual review has a numeric ceiling (roughly thousands of actionable rows per tenant per month); above it, the no-op filter rate carries the load or auto-approval thresholds return from the out-of-scope list. Product needs to own that number.
- **10k files alone is boring** (10k events, 10k registry rows, autoscaled workers). 10k × 1M rows = 10B rows is not reachable in this domain (rows bounded by practitioner × address combinations; US NPI registry ≈ 8M) and the platform caps first anyway: survivorship runs per practitioner and review capacity is human. Ingestion is not the bottleneck at extreme scale.
- **Database consequences at extreme scale:** single-node Postgres exits; sharded Mongo sacrifices global uniqueness; Spanner scales without giving it up. The named escape hatch is the control-plane/data-plane split (rows to GCS/BigQuery, state + decisions in the transactional store) — one paragraph in the doc, not built.

### 3.6 Industry patterns — adopted and rejected

- **Adopted (already in the mechanics):** raw-immutable archive before any parsing (bronze-zone rule); Write-Audit-Publish with the reviewer as the audit gate; split + checkpoint + idempotent re-run (MapReduce lineage); dedup keys everywhere ("exactly-once" is engineered, not delivered); schema-versioned adapters that reject unknown versions whole; DLQ + quarantine + count reconciliation.
- **Worth reading before authoring:** Apache Gobblin (LinkedIn) — a framework built precisely for bulk external-partner file ingestion: connectors ≈ our adapters, converters, quality checkers, state store for incremental resume. Free design review.
- **Rejected as cargo cult at this volume:** Kafka cluster, Spark/Flink/Beam jobs, lakehouse tables, Airflow. They amortize at TB/day and thousands of pipelines; low-millions rows/month is streaming-parser + task-queue territory.

### 3.7 Naming

- Service name lean: **`directory-accuracy`** — names the purpose (the program Product described) rather than the mechanism; "external source ingestion" understates the review/release half and ages badly as scope grows. Deployables `directory-accuracy-api` / `directory-accuracy-worker`; database `directory-accuracy-db`; event stem `accuracy.*` (the retired drafts already coined `accuracy-source.file.received`, `accuracy-source-dlq`).
- Final name is a design-doc decision; attestation-module docs currently say "External Source Ingestion Service" and are renamed only when the doc fixes the name.

## 4. Open questions and sign-offs

| # | Question | Owner |
| --- | --- | --- |
| Q1 | Service ownership: this team or the matching-pipeline team (S14 assigned "Candor ingestion mechanics" there)? Decides whose review gates the doc. | Engineering leads |
| Q2 | Selection query configuration (v2 O-17): language/format (structured filter JSON vs SQL vs saved-view reference), storage location, authoring/validation, injection safety. | Engineering, this doc |
| Q3 | Skip semantics and cross-cadence re-send (v2 O-18): does a skipped row reappear next batch; is an unresolved NPI re-sent; how does D2-07 suppression interact with monthly re-sends? | Product + Engineering |
| Q4 | Review surface and release path (v2 O-19): own UI or a surface inside the Attestation Module UI application; own Sync Latest machinery or shared? | Engineering, this doc |
| Q5 | Vendor contract (v2 O-1): columns/enums/formats both directions; **add the stress-test clauses** — CSV/NDJSON above ~500k rows, gzip, declared row counts, duplicate-rate ceiling. | Engineering drafts; Candor call decides |
| Q6 | Manifest vs filename-identity delivery: the 2026-09-05 SFTP rewrite (manifest dropped, filename echo + row-grain idempotency) is pending review; this doc must adopt one. | Engineering review |
| Q7 | Manual-review ceiling: the actionable-rows-per-month number above which auto-approval thresholds return to scope. | Product |
| Q8 | Final service name (`directory-accuracy` vs `external-source-ingestion`) and runtime choice (JVM vs TS, or split per deployable). | This doc's decision sections |

## 5. Ticket impact

- The former ingestion/SFTP tickets in the attestation breakdown (the DA rows covering export, pickup, adapter, dispositions, rejected-recommendations) re-home to this service; per the standing rule, Jira is updated when the design doc is finalized — no separate re-derivation pass.
- New tickets to expect: selection-query configuration + validation, export job, review surface (pending Q4), release worker under `candor:{tenantId}`, service scaffolding (two deployables, SAs, pipelines).
