# Directory Accuracy & Attestations — Audit and Observability Design Brief

**Prepared:** 2026-08-27 · **Companions:** `directory-accuracy-attestations-consolidated-source-of-truth-v2.md` (workflow), `directory-accuracy-attestations-module-architecture-brief.md` (per-module technology) · **Grounded in:** v1 §14 (reliability/security/operations), §19.3 (identifiers and idempotency), §19.7 (verified MDM lineage gap)

**What this is.** v2 states, in several places, that everything must be reconstructable: *"what changed, why, who acted, when, from which source, and what result reached the Golden record"* (§3.2), and that audit and observability apply at **every** step, not only ingestion (§6.5.6, §8). This document turns that requirement into a design: the event model, the per-stage catalogue of what is recorded, the retention and access rules, the telemetry and alerting, and the one confirmed gap in the existing platform that blocks a complete lineage story.

---

## 1. Audit and observability are two different systems

They are constantly conflated, and conflating them produces a system that satisfies neither. Separate them explicitly and design each for its own job.

| | **Audit trail** | **Observability telemetry** |
| --- | --- | --- |
| Question it answers | "What happened to Dr. Patel's attestation, who decided it, and what reached the Golden record?" | "Is the pipeline healthy right now, and where is it slow or stuck?" |
| Audience | Compliance, client, regulator, dispute resolution, internal investigation | Engineers, on-call, operations |
| Store | Spanner, inside the Attestation Module database, transactional with the state change | Cloud Logging / Cloud Monitoring / trace backend |
| Mutability | **Append-only. Never updated, never deleted inside the retention window.** | Freely sampled, aggregated, expired |
| Retention | **7 years (D2-20, final)** | 30–90 days for logs, 6–13 months for aggregated metrics |
| Completeness | **Total. Every state transition, no sampling, no dropped events.** | Sampled is fine; a lost trace span is an inconvenience |
| Failure semantics | **If the audit write fails, the business mutation fails.** | Best-effort; telemetry loss never blocks work |

The distinction has one hard consequence worth stating in the design review: **the audit trail is not "logs."** It is business state, it lives in the transactional database next to the records it describes, and it is written in the same transaction. Cloud Logging is a diagnostic surface with a retention window measured in weeks — it can never be the evidence store for a 7-year compliance obligation.

---

## 2. The questions the audit trail must be able to answer

The design is only correct if these queries are answerable directly from stored data, without reconstruction or inference. Use this as the acceptance test.

1. Show every event in Dr. Patel's 2026-Q3 attestation obligation, in order, with actors and timestamps.
2. Who attested for Dr. Patel, were they the practitioner or an admin acting on their behalf, and by what authorization?
3. This vendor recommendation was rejected — by whom, when, for what reason, and against which batch and row?
4. This value in the Golden record came from where? Which source won, under which ranking configuration, and which reviewer approved the item that produced it?
5. A reviewer approved a change and the Golden record does not show it. Why? *(Expected answer: a higher-ranked source won — approval is not the same as winning, v2 §6.11.)*
6. Prove that every one of the 43,812 rows in vendor batch `B-2026-08-01` received exactly one disposition, and reconcile that against the manifest count.
7. Which practitioners in this tenant have not attested, as of what read timestamp?
8. Was the two-business-day obligation met for this attestation? When did the clock start and when did the change reach the OV?
9. Did anyone touch this record outside the workflow — an operator replay, a configuration change, a paused tenant?
10. Which items are in quarantine right now, since when, and why?

Questions 4, 5, and 8 are the hard ones. Question 4 currently has **no complete answer on the existing platform** — see §9.

---

## 3. The correlation spine

One identifier ties the whole story together. v2 §6.1 already defines it: the deterministic identity `tenant_id + practitioner_id + due_period`, computed exactly once by the Attestation Module Backend and travelling **unchanged** through every stage.

That identity is the audit trail's primary correlation key. Around it, each stage adds its own narrower identifier, and every audit event carries the full set it knows about:

```
correlation_id       tenant + practitioner + due_period      — the obligation, the spine
  task_id            the attestation task
  submission_id      one Portal submission (Lane A)
  batch_id           one vendor delivery (Lane B), plus vendor_batch_id and export_batch_id
  row_id             one row inside that batch
  change_item_id     one staged change item (+ integer version)
  review_action_id   one reviewer decision
  release_id         one Sync Latest invocation, plus release_item_id per item
  slice_write_ref    source_id + crosswalk_id written toward the Golden record
  ov_outcome_ref     what actually survived into the OV
  export_ref         the client export that carried it back
request_id / trace_id / pubsub_message_id — the technical join to telemetry
```

**The rule:** an audit event that cannot name its `correlation_id` is a bug, not an event. The only exceptions are genuinely pre-identity events — a batch arriving before its rows are resolved to practitioners, and infrastructure-level events like a configuration change — and those carry `batch_id` or a tenant-level scope instead.

**Why this matters practically.** Question 4 in §2 is a five-hop join across staging, review, sync, MDM, and export. Without one identifier surviving all five hops, that query is a reconstruction exercise performed under time pressure during a client dispute. With it, it is one indexed read.

---

## 4. The actor model — "who did what"

Every audit event names an actor, and every actor resolves to one of these types. There is no "system" catch-all: a scheduler run and an operator replay are not the same kind of act, and the audit must not blur them.

| `actor_type` | Who | Identity recorded | Notes |
| --- | --- | --- | --- |
| `PRACTITIONER` | Attesting for themselves via the Portal | Portal user id, practitioner id, session/auth reference | `subject_practitioner_id` equals the actor's own practitioner |
| `PROVIDER_ADMIN` | Attesting **on behalf of** a practitioner | Portal user id + `on_behalf_of_practitioner_id` + the authorization basis | D2-17: authorization basis is tenant scope. Record it explicitly so a later policy change is visible in history |
| `PAYER_REVIEWER` | Approve / reject / skip / assign / bulk / rollback / Sync Latest | Admin user id, roles held at the time of the action | Record roles **as of the action**, not as looked up later — role grants change |
| `OPERATOR` | Support/ops replay, retry, quarantine handling, pause | Admin user id + the runbook action invoked | Every operator action is an audited replay; direct database edits are prohibited outright |
| `SCHEDULER` | Cycle scan, export job, client export job, reconciliation sweep | Job name + run id + trigger time | Job runs get their own audit events with counts, not just per-entity events |
| `WORKER` | Ingestion worker, sync worker, outbox relay | Worker/service name + deployment version + task/message id | Deployment version matters: "which build made this decision" |
| `EXTERNAL_VENDOR` | The party that deposited a file | SFTP identity (the account, not the filename) + `source_id` from the registry | Origin is proven by which account deposited the file, never by its name or checksum |
| `MDM_ENGINE` | Cleansing / matching / survivorship / Terminations | Engine name + config version (see §9) | Records the *outcome* that the workflow did not decide |

**Two rules people forget.**

- **Delegation is recorded as delegation, not as the subject acting.** When a provider admin attests for Dr. Patel, the actor is the admin and the subject is Dr. Patel. Recording it as "Dr. Patel attested" is a factual error in a compliance record, and D2-17's tenant-wide admin scope makes this more likely, not less.
- **The reviewer's roles are stamped at action time.** "Who was allowed to do this, at the moment they did it" is a different question from "what roles do they hold now," and only the first one is answerable if you stamp it.

---

## 5. The audit event record

One append-only table in the Attestation Module database, written through the Attestation Module Data Layer, which stamps the invariant fields on every write (v2 §6.8: tenant scoping, validation, and audit stamping are that layer's whole reason to exist).

```
audit_events
  audit_id             UUID, PK
  tenant_id            never nullable, never client-supplied
  occurred_at          when the act happened (business time)
  recorded_at          commit timestamp (system time)
  stage                STAGE_0 … STAGE_6 | OPS
  event_type           enum, e.g. ITEM_APPROVED, ROW_DISPOSITIONED
  outcome              SUCCEEDED | FAILED | QUARANTINED | SKIPPED
  correlation_id       tenant + practitioner + due_period
  task_id, submission_id, batch_id, row_id,
  change_item_id, change_item_version,
  review_action_id, release_id, release_item_id     (each nullable, all that apply populated)
  entity_type          TASK | SUBMISSION | CHANGE_ITEM | BATCH | ROW | RELEASE | CONFIG
  entity_id
  actor_type, actor_id, actor_display, actor_roles
  on_behalf_of_practitioner_id
  source_id            FK to the sources registry (D2-11)
  attribute            the field touched, when applicable
  operation            ADD | UPDATE | REMOVE | FLAG | ACKNOWLEDGE
  before_value, after_value        normalized, JSON
  reason_code, reason_text         mandatory on every rejection (D2-24)
  disposition                      the ingestion disposition, when applicable
  error_code, error_detail
  request_id, trace_id, pubsub_message_id, deployment_version
  event_schema_version
```

**Design notes worth defending in review.**

- **Both timestamps.** `occurred_at` and `recorded_at` differ whenever an event is replayed, backfilled, or delivered late. Collapsing them into one column destroys the ability to distinguish "this happened late" from "this was recorded late," which is exactly the distinction a two-business-day compliance question turns on.
- **Written in the same transaction as the state change**, alongside the outbox record. v1 §14.2 lists "audit/outbox persistence fails inside a local mutation" as a required test case. The correct behaviour is that the mutation fails too — a state change with no audit row is worse than no state change.
- **Append-only, enforced.** No UPDATE and no DELETE path exists in the data layer for this table. A correction is a new compensating event referencing the original `audit_id`, never an edit. This is also what makes the table safe to stream to an archive without reconciliation.
- **Normalized before/after values**, using the same versioned normalization rules as the `rejected_recommendations` match key (v2 §6.6). If normalization rules change, the version must change with them — a silent normalization change would make historical comparisons quietly wrong.
- **Indexes:** `(tenant_id, correlation_id, occurred_at)` for the per-practitioner story; `(tenant_id, actor_id, occurred_at)` for "what did this reviewer do"; `(tenant_id, entity_type, entity_id, occurred_at)` for the per-item history the UI renders; `(tenant_id, batch_id, disposition)` for batch reconciliation. Partition/interleave per tenant so one tenant's volume never slows another's reads — the same invariant v2 §6.6 already states for the rejections table.
- **Volume estimate to sanity-check the design:** ~70k practitioners per cycle, four cycles a year, plus one audit row per vendor row (a 100k-row batch is 100k disposition events). Order of magnitude: single-digit millions of rows per year per large tenant. Spanner handles this comfortably; the reason to care is the archive tiering in §8, not query performance.

---

## 6. What is audited at every stage

The catalogue below is the checklist. Each stage's design document owns its column; nothing may be dropped without a recorded decision.

### Stage 0 — Backfill
`BACKFILL_RUN_STARTED` / `BACKFILL_RUN_COMPLETED` (population size, stagger window, counts) · `PRACTITIONER_DATES_BACKFILLED` (before/after `last_attestation_date`, `next_attestation_date`) · `BACKFILL_RERUN_NOOP` (idempotent re-run, nothing changed — record it, because "nothing happened" is itself evidence).

### Stage 1 — Cycle detection and outreach
`CYCLE_SCAN_STARTED` / `COMPLETED` (window bounds, candidates examined, tasks created, duplicates skipped) · `IDENTITY_COMPUTED` · `TASK_CREATED` · `TASK_CREATION_SKIPPED_DUPLICATE` (the unique-index no-op — audited, because a silent skip and a bug look identical otherwise) · `OUTREACH_SCHEDULED` / `OUTREACH_SENT` / `OUTREACH_BOUNCED` / `OUTREACH_FAILED` (tier T-30/T-7/T+1, template id, delivery reference) · `TASK_MARKED_OVERDUE` · `CLOCK_SET` (due date and the rule that produced it).

### Stage 2, Lane A — Portal
`PORTAL_SESSION_STARTED` (auth method, resolved identity, practitioners visible to this actor) · `FORM_PREFILLED` (snapshot version and the OV read timestamp — this is the evidence of *what the attester was shown*) · `SUBMIT_REJECTED_STALE_SNAPSHOT` · `SUBMIT_BLOCKED_ALREADY_ATTESTED` (who had already attested, and when) · `ATTESTATION_SUBMITTED` (NO_CHANGE or the full delta, attester type, `on_behalf_of`) · `STAGED_ITEM_CREATED` per delta · `TASK_STATUS_CHANGED` → SUBMITTED · `CLOCK_ADVANCED` (submission + 90 days).

**The one to insist on: `FORM_PREFILLED` with its snapshot version.** An attestation is a statement about specific data at a specific moment. Without recording what was displayed, "I attested that this was correct" has no referent, and a disputed attestation cannot be defended.

### Stage 2, Lane B — Outbound, inbound, ingestion
`EXPORT_RUN_STARTED` / `COMPLETED` · `EXPORT_FILE_WRITTEN` (export batch id, row count, SHA, GCS archive URI) · `EXPORT_MANIFEST_WRITTEN` · `INBOUND_OBJECT_DETECTED` (object name, size, depositing SFTP identity) · `MANIFEST_RECEIVED` (vendor batch id, referenced export batch id, declared SHA and counts, produced-at) · `BATCH_REGISTERED` / `BATCH_SKIPPED_ALREADY_SEEN` · `CHECKSUM_VERIFIED` / `CHECKSUM_MISMATCH` · `ROWCOUNT_VERIFIED` / `ROWCOUNT_MISMATCH` · `SCHEMA_VERSION_RESOLVED` / `SCHEMA_VERSION_REJECTED` (whole batch rejected, nothing partially staged) · **`ROW_DISPOSITIONED` — one per row, always**, carrying the disposition, the reason, and for previously-rejected rows the `rejected_recommendation_id` that matched · `ROW_QUARANTINED` (which dependency was unavailable) · `QUARANTINE_RETRIED` / `QUARANTINE_RESOLVED` / `QUARANTINE_CLOSED_BY_OPERATOR` · `BATCH_COMPLETED` (per-disposition counts, reconciled against the manifest).

**The reconciliation invariant, stated as an assertion the batch must pass:**

```
manifest_row_count
  == no_op + duplicate_stale_superseded + excluded + quarantined + previously_rejected + actionable
```

If that equation does not balance, the batch is not complete, and `BATCH_COMPLETED` must not be written. This single check is what makes "nothing is ever silently discarded" (v2 §8) a verifiable property rather than an intention.

**Note on suppressed rows.** D2-07 suppresses previously-rejected rows from the reviewer queue in MVP. Suppressed is not invisible: the audit row is written in full, so the answer to "why did the reviewer never see this?" is always available, and turning suppression off later is a configuration change against data that already exists.

### Stage 3 — Staging
`IDENTITY_RESOLVED` (NPI/crosswalk resolution path, the resolved canonical practitioner, and the resolution method) · `IDENTITY_RESOLUTION_FAILED` → quarantine · `STAGED_ITEM_CREATED` (source_id, attribute, operation, normalized before/after, version 1) · `CROSS_LANE_JOINED` (this vendor item and this attestation now sit on the same practitioner).

Identity resolution deserves its own event because v2 §6.8 names misjoining two practitioners as "a worst-class failure." If it ever happens, the audit must show exactly which key resolved to which practitioner and by what method.

### Stage 4 — Review
`ITEM_ASSIGNED` / `UNASSIGNED` / `REASSIGNED` · `ITEM_APPROVED` · `ITEM_REJECTED` (reason code from the picklist **and** free text, both mandatory — D2-24) · `ITEM_SKIPPED` · `ACTION_UNDONE` · `NO_CHANGE_ACKNOWLEDGED` (D2-09) · `BULK_ACTION_EXECUTED` (the bulk invocation as one event, **plus one per-item event each** — partial failure is never hidden, v2 §6.9) · `REJECTED_RECOMMENDATION_RECORDED` (the write into the memory table) · `ITEM_ROLLED_BACK` (approved → pending, history preserved) · `DECISION_CONFLICT_REJECTED` (a version-mismatch write that lost — audited, because it proves the concurrency control actually engaged).

Every one of these carries `change_item_version` before and after. That is what makes "two reviewers cannot silently overwrite each other" auditable rather than merely implemented.

### Stage 5 — Sync and Golden Record Processing
`SYNC_REQUESTED` (actor, item ids, single or bulk) · `SYNC_ITEM_STARTED` · `SLICE_UPSERTED` (source_id, crosswalk_id, attribute, value) · `TERMINATION_REQUESTED` (scope and narrowers) · `TERMINATION_ALREADY_APPLIED` (the HTTP 400 "already terminated" replay — recorded as terminal success, not failure) · `SYNC_ITEM_RETRIED` (attempt number, backoff, error) · `SYNC_ITEM_QUARANTINED_POISON` (with the preserved error) · `SYNC_ITEM_COMPLETED` · **`OV_OUTCOME_VERIFIED`** · **`APPROVED_VALUE_DID_NOT_SURVIVE`**.

The last two are the most important events in the entire system, and the easiest to omit.

- `OV_OUTCOME_VERIFIED` exists because survivorship's HTTP resource returns **200 with `status:"error"` on failure**, and there is no consumer dedup or redelivery. The sync worker must not trust the 200 — it reads back the OV or consumes `mdm.slice.ov_generated`, and records **what actually landed**, not what it asked for.
- `APPROVED_VALUE_DID_NOT_SURVIVE` records the case v2 §6.11 warns about: a reviewer approved a value and a higher-ranked source won anyway. This is correct behaviour, it will happen, and it will generate client questions. The audit must state it plainly, with the winning source and value, rather than leaving a trail that reads as "approved" and an OV that disagrees.

Also at this stage: `SOURCE_RANKING_CONFIG_CHANGED` and `SYNC_REFUSED_MISSING_RANKING_CONFIG` (D2-18's guardrail firing). A ranking change silently alters who wins every future merge; it belongs in the audit trail with before/after and actor.

### Stage 6 — Client export
`ATTESTED_SET_COMPUTED` (as-of timestamp, practitioner count, inclusion rule) · `CLIENT_EXPORT_GENERATED` (row count, SHA, GCS archive URI) · `CLIENT_EXPORT_DELIVERED` (destination, manifest written last, delivery confirmation) · `CLIENT_EXPORT_FAILED`.

### Operations
`QUARANTINE_INSPECTED` · `REPLAY_TRIGGERED` (what, why, original idempotency key reused) · `RECONCILIATION_MISMATCH_DETECTED` / `RESOLVED` · `TENANT_PAUSED` / `RESUMED` · `SOURCE_PAUSED` · `WORKER_LEASE_RECOVERED` · `CONFIG_CHANGED`.

---

## 7. Access to provider data — the read-audit question

Write auditing is settled above. **Read auditing is a Compliance decision we should raise rather than assume.** The reviewer queue displays provider data, and some regimes require recording who *viewed* a record, not only who changed it.

Recommended position: audit **reads that are scoped to an individual** (opening a practitioner's side-by-side detail view, exporting the queue) and do **not** audit list/grid pagination, which would generate enormous volume for little evidentiary value. Implement it as a distinct `access_events` stream rather than mixing view events into `audit_events` — different volume profile, different retention, and mixing them makes the mutation history harder to read.

Flag this to Compliance as an open item with a recommendation attached, not as a solved problem.

---

## 8. Retention, archival, and immutability

- **Retention: 7 years, final (D2-20).** That is the design constraint, not a working figure any more.
- **Tiering.** Keep the recent window (proposal: 18 months) hot in Spanner for UI history rendering and operational queries. Age older events to GCS in a columnar format (Parquet/Avro) with a documented schema and object versioning, queryable from BigQuery for compliance requests. The archive is append-only by construction and the objects are immutable.
- **The archive must be verifiable.** Every archive batch records its source range, row count, and checksum, and a periodic job re-verifies that the archived count matches what was aged out. An archive nobody has ever read back is not evidence; it is a hope.
- **Raw vendor files are themselves audit artifacts.** The original file and its manifest stay in GCS, immutable and versioned, for the same 7 years. v1 §14.1 requires raw external input and evidence to be kept protected and **separate from indexed workflow fields** — so the audit row references the archive URI; it does not inline the vendor's payload.
- **Tamper evidence.** Append-only enforcement in the data layer plus IAM that grants no delete on the table or the archive bucket, bucket retention policy/object hold on the archive, and Cloud Audit Logs on the infrastructure itself (who changed the IAM, who touched the bucket policy). The application cannot be the only thing protecting the application's evidence.
- **Legal hold.** A hold must be able to suspend expiry for a tenant or a practitioner. Cheap to design now, expensive to retrofit.
- **Deletion requests.** A provider data-deletion request and a 7-year audit obligation conflict on their face. That conflict is Compliance's to resolve; the design should expose the hooks (crypto-shredding of PII fields while retaining the event skeleton) rather than pretending it will not arise.

---

## 9. The confirmed lineage gap — a cross-team dependency, not a detail

Verified against the code in v1 §19.7, and it is the one thing that makes question 4 in §2 unanswerable today:

**The platform cannot currently prove which survivorship configuration made a given merge decision.** `contribution_map` records the per-field winner (`value, sourceId, sourceType, cleansedSourceType, crosswalkId, certifyId, updatedAt`) — but no config version, no rule id, no rank, no reason. `tenant_configurations` has no version or history column; the 60-second refresh overwrites the in-memory config in place; the config's own `version` field is written only to logs. So a decision made under yesterday's ranking is indistinguishable from one made under today's.

For this feature, that gap is directly load-bearing: **D2-18 makes per-tenant source ranking the thing that decides whether an approved attestation or a vendor recommendation wins.** Without config-version lineage, "why did the vendor's value beat the provider's own attestation on 14 August?" has no stored answer.

The fix is three small additive changes to the **existing** survivorship service, not a new service:

1. Stamp `configVersion` and `configSource` (tenant-row / classpath-default / drl-fallback) plus the applied rule identifier into each `contribution_map` entry at decision time — additive JSON, no schema migration.
2. Make survivorship rows in `tenant_configurations` append-only versioned (a `tenant_configuration_versions` history table, or a monotonic `version` + `effective_at` on writes), so a stamped version resolves back to exact config content.
3. Carry `configVersion` in the `mdm.slice.ov_generated` payload so downstream audit events can persist it.

**Treat this as a flagged cross-team dependency on every audit and lineage ticket.** It sits in the MDM workstream, it is small, and until it lands our lineage story stops at "we asked for X and Y survived" without being able to say why.

---

## 10. Observability — the tooling and how we use it

### 10.0 What the platform already runs (verified against the repos, 2026-08-27)

This is not a greenfield telemetry decision. Most of the stack exists; the job is to plug the new modules into it and close three specific gaps.

| Capability | Tool in use | Where it is wired | Status |
| --- | --- | --- | --- |
| Distributed tracing | **OpenTelemetry** via `quarkus-opentelemetry`, exporting OTLP `http/protobuf` to a **self-hosted OTel Collector on Cloud Run** (`otel-collector-…us-central1.run.app/v1/traces`), with a per-export OIDC auth token; `opentelemetry-exporter-gcp` also on the classpath | `api-layer` `application.properties`; propagators `tracecontext,baggage`; sampler currently `always_on`; runtime kill switch via `OBSERVABILITY_ENABLED` → `quarkus.otel.sdk.disabled` | **Ready — reuse as-is** |
| Error tracking | **Sentry** — `logging-sentry` (Quarkus, level `ERROR`, `traces-sample-rate=0.1`, per-environment, `SENTRY_RELEASE` stamped) and `@sentry/node` / `@sentry/google-cloud-serverless` (Node) | `api-layer` + the Nx/NestJS monorepo | **Ready — reuse as-is** |
| Structured logs | **Cloud Logging**, fed by `nestjs-pino` + `pino-stackdriver` (Node) and `quarkus-logging-json` (Java) | Node services ship JSON today | **Partial — see gap 2** |
| Metrics | **Cloud Monitoring** (`@google-cloud/monitoring` client in the Node monorepo) | Node side only | **Gap — see gap 1** |
| Long-horizon analytics | **BigQuery** (`bigquery-client` package exists) | Precedent for the audit archive query surface in §8 | Available |
| Telemetry as code | **CDKTF** (`@cdktf/provider-google`) + GCP Workflows YAML in `infra/` | Alert policies, log-based metrics, dashboards, notification channels belong here — defined in code, never clicked into the console | Available |
| Health / readiness | `quarkus-smallrye-health` | `api-layer` | Ready |
| Resilience primitives | `quarkus-smallrye-fault-tolerance` (`@Timeout`, `@Retry`, `@CircuitBreaker`) | Dependency present | **Present but under-used — see gap 3** |

**Three gaps this feature must close, and they are specific:**

1. **There is no metrics library in `api-layer` at all.** No Micrometer, no Prometheus registry — traces yes, errors yes, metrics no. Every counter in §10.3 needs a path to exist first. **Recommendation:** add `quarkus-micrometer` with an OTLP registry and export metrics **through the OTel Collector we already run**, rather than standing up a second telemetry pipeline. One collector, all signals.

2. **JSON console logging is switched off** — `quarkus.log.console.json=false`, even though `quarkus-logging-json` is on the classpath. Plain-text logs in Cloud Logging cannot be filtered by field, so a `correlation_id`-scoped log search does not work today. Turn it on for anything this feature ships; otherwise §10.5's log queries are fiction.

3. **`api-layer.dal-service.read-timeout-ms=600000`** — a 600-second read timeout with no circuit breaker. This is precisely the inherited platform risk v1 flagged, and it is still in the config. `quarkus-smallrye-fault-tolerance` is already a dependency: every DAL and MDM call this feature makes gets an explicit `@Timeout`, `@Retry` with bounded backoff, and `@CircuitBreaker`. Do not inherit the default.

*Minor:* the Node monorepo runs **two** logging stacks (pino + `pino-stackdriver`, and winston + `@google-cloud/logging-winston`). Anything new should use the pino path only.

### 10.1 Four signals, four mechanisms, no overlap

| Signal | Tool | Emitted how | Retention | Answers |
| --- | --- | --- | --- | --- |
| **Traces** | OTel SDK → Collector (Cloud Run) → backend | Auto-instrumented HTTP/DB spans, plus manual spans around batch parse, per-row disposition, review action, sync item | Days | "Where did this request spend its time / where did it stop?" |
| **Metrics** | Micrometer → OTLP → Cloud Monitoring | Explicit counters, timers, gauges (§10.3) | 6–13 months | "Is the system healthy, and what is the trend?" |
| **Logs** | Quarkus JSON logging / pino-stackdriver → Cloud Logging | One structured line per meaningful step, always carrying the correlation fields | 30–90 days | "What exactly happened in this run?" |
| **Errors** | Sentry | Exceptions, tagged with release, environment, tenant, correlation id | Sentry policy | "What is breaking, how often, since which deploy?" |
| **Audit** | Spanner `audit_events` (§5) | Transactional, append-only, complete | **7 years** | "Who did what, when, and what reached the Golden record?" |

The audit trail is deliberately in this table so the boundary stays visible: it is not a fifth telemetry signal, it is business state. **No compliance question is ever answered from Cloud Logging.**

### 10.2 Trace propagation — the step everyone skips

W3C `traceparent` via the `tracecontext` propagator (already configured). The chain this feature needs:

```
Portal/UI HTTP request
  → api-layer (span starts, trace_id minted)
  → Pub/Sub publish        ← traceparent copied into MESSAGE ATTRIBUTES by the publisher
  → ingestion worker       ← traceparent EXTRACTED from attributes, span linked as parent
  → Cloud Task enqueue     ← traceparent copied into the task HTTP HEADERS
  → row worker → data layer → Spanner
  → sync worker → MDM/Terminations HTTP call (traceparent forwarded)
```

**Neither Pub/Sub nor Cloud Tasks propagates trace context on its own.** If the publisher does not copy `traceparent` into message attributes and the consumer does not extract it, every trace ends at the queue boundary and the pipeline becomes four disconnected traces instead of one story. Make this an explicit work item in the ingestion design document, not an assumption.

**Baggage** carries `tenant_id` and `correlation_id` so every downstream span is filterable by obligation without re-deriving it.

**Sampling:** `always_on` is currently set and is fine at this volume (a monthly vendor batch, not a high-QPS API). If cost becomes an issue, move to `parentbased_traceidratio` — but **pin error, quarantine, and sync paths to always-sample**, because those are the traces anyone will ever go looking for.

### 10.3 Metric catalogue — concrete names

Naming convention: `attestation.<stage>.<thing>.<unit>`. Labels stay low-cardinality — **`tenant_id` yes, `practitioner_id` never** (that is what the audit table is for; a per-practitioner label would explode the metric cardinality).

```
# Scheduler
attestation.scheduler.run.duration            timer     {outcome}
attestation.scheduler.candidates.examined     counter   {tenant}
attestation.task.created                      counter   {tenant}
attestation.task.duplicate_skipped            counter   {tenant}
attestation.task.overdue                      gauge     {tenant}

# Outreach
attestation.outreach.sent                     counter   {tenant, tier}          # tier = T-30|T-7|T+1
attestation.outreach.failed                   counter   {tenant, tier, reason}

# Portal
attestation.portal.submission                 counter   {tenant, outcome}       # DELTA|NO_CHANGE
attestation.portal.stale_snapshot_rejected    counter   {tenant}
attestation.portal.already_attested_blocked   counter   {tenant}

# Ingestion
attestation.ingest.batch.received             counter   {tenant, source_id}
attestation.ingest.batch.skipped_duplicate    counter   {tenant, source_id}
attestation.ingest.batch.rejected             counter   {tenant, source_id, reason}
attestation.ingest.row.dispositioned          counter   {tenant, source_id, disposition}
attestation.ingest.batch.duration             timer     {tenant, source_id}
attestation.ingest.rows_per_second            gauge     {tenant, source_id}
attestation.ingest.quarantine.depth           gauge     {tenant, reason}
attestation.ingest.quarantine.oldest_age      gauge     {tenant}

# Staging
attestation.identity.resolution               counter   {tenant, outcome, method}

# Review
attestation.review.queue.depth                gauge     {tenant, bucket}        # bucket = aging band
attestation.review.decision                   counter   {tenant, action, source_id}
attestation.review.version_conflict           counter   {tenant}
attestation.review.bulk.partial_failure       counter   {tenant}

# Sync
attestation.sync.requested                    counter   {tenant, mode}          # ITEM|BULK
attestation.sync.item.duration                timer     {tenant, outcome}
attestation.sync.item.retry                   counter   {tenant, attempt}
attestation.sync.item.poison_quarantined      counter   {tenant}
attestation.sync.ov_verification_mismatch     counter   {tenant}
attestation.sync.approved_did_not_survive     counter   {tenant, winning_source}

# Export
attestation.export.client.delivered           counter   {tenant, outcome}
attestation.export.client.practitioners       gauge     {tenant}

# Cross-cutting — the one that must always read zero
attestation.audit.write_failure               counter   {tenant, stage}
attestation.outbox.lag_seconds                gauge     {tenant}
attestation.reconciliation.mismatch           counter   {tenant, check}
```

**The compliance-facing metrics** are separate from health metrics and must be built deliberately, because the business will ask about them by name:

```
attestation.compliance.two_business_day_risk  gauge     {tenant, bucket}
   # bucket = >24h_remaining | 12-24h | <12h | BREACHED
   # Portal lane only — D2-29 attaches the clock to attestations, not vendor recommendations
attestation.compliance.non_attested           gauge     {tenant}
   # served from the canonical task table (v1 §19.8), same query path as the API and the grid
attestation.compliance.cycle_completion_rate  gauge     {tenant, due_period}
attestation.compliance.suppressed_rows        counter   {tenant, source_id}
   # how many recommendations D2-07 hid from reviewers — unexpected growth means the rule is
   # doing something nobody intended
```

### 10.4 What a structured log line looks like

Every line, from every service, carries the correlation fields. This is what makes a single Cloud Logging filter reconstruct one obligation's operational story:

```json
{
  "severity": "INFO",
  "timestamp": "2026-08-27T14:22:07.481Z",
  "message": "row dispositioned",
  "logging.googleapis.com/trace": "projects/certifyos-development/traces/4bf92f3577b34da6a3ce929d0e0e4736",
  "logging.googleapis.com/spanId": "00f067aa0ba902b7",
  "service": "api-layer",
  "deployment_version": "api-layer@2026.08.27-1",
  "tenant_id": "harbor-health",
  "correlation_id": "harbor-health:prac_8f21c4:2026Q3",
  "batch_id": "b_01J9…",
  "row_id": 41208,
  "source_id": "candor:harbor-health",
  "attribute": "primary_phone",
  "operation": "UPDATE",
  "disposition": "ACTIONABLE",
  "change_item_id": "ci_01J9…"
}
```

And the corresponding Cloud Logging query — the whole reason gap 2 in §10.0 has to be closed:

```
resource.type="cloud_run_revision"
jsonPayload.correlation_id="harbor-health:prac_8f21c4:2026Q3"
severity>=INFO
```

### 10.5 Dashboards — four, each with an owner

1. **Pipeline health** (on-call) — batch receipt vs. expected cadence, rows/second, disposition mix over time, quarantine depth and age, DLQ depth, sync success rate, OV verification mismatches.
2. **Review operations** (payer ops lead) — queue depth by aging bucket, decisions per reviewer per day, unassigned pool size, rejection rate by source, bulk partial-failure rate.
3. **Compliance** (Compliance + client-facing teams) — two-business-day risk buckets, non-attested counts per tenant, cycle completion rate, time from submission to OV landing, suppressed-row volume.
4. **Data integrity** (engineering) — audit write failures (must be flat zero), reconciliation mismatches by check, outbox lag, identity-resolution failure rate, `approved_did_not_survive` counts by winning source.

All four defined in CDKTF alongside the alert policies, so a dashboard change is a reviewed pull request.

### 10.6 Alerts, defined as code

Alert on *work being stuck or truth being at risk*, never on raw volume. Each alert names its runbook.

| Alert | Condition | Severity | Runbook action |
| --- | --- | --- | --- |
| Audit write failure | `attestation.audit.write_failure > 0` over 1 min | **Page** | State is changing without evidence — halt the affected worker, investigate before resuming |
| DLQ non-empty | Any new subscription's DLQ depth > 0 for 5 min | **Page** | A stuck business fact; inspect, fix, replay with the original idempotency key |
| Batch reconciliation failed | `attestation.reconciliation.mismatch{check="batch_counts"} > 0` | **Page** | Batch is incomplete; do not mark complete, reconcile against the manifest |
| OV verification mismatch | Rate above baseline over 15 min | **Page** | Sync asked for X and Y landed — stop the release, reconcile before replay |
| Poison quarantine | `attestation.sync.item.poison_quarantined > 0` | Ticket | Operator inspects, fixes or closes with reason |
| Quarantine ageing | `quarantine.oldest_age > 24h` | Ticket | Dependency may still be down, or rows are stranded |
| Vendor batch overdue | No batch from a source within its cadence window + grace | Ticket | A vendor going quiet is otherwise a silent failure |
| Scheduler anomaly | Missed run, or `task.created` deviates sharply from `candidates.examined` | Ticket | Window query should self-heal; verify it did |
| Two-business-day risk | `compliance.two_business_day_risk{bucket="<12h"} > 0` | Notify ops | Reviewer workload triage |
| Sync failure rate | > threshold over 15 min | Ticket | Check MDM/Terminations health first |

Two things worth deriving as **log-based metrics** in CDKTF rather than instrumenting by hand, because they are cheap and rarely change: batch rejection reasons, and identity-resolution failures. Everything in §10.3 that drives an alert should be a real metric, not a log-derived one — log-based metrics inherit log retention and sampling, which is the wrong foundation for a paging alert.

**Sentry** stays the exception channel: unhandled errors, tagged with `tenant_id` and `correlation_id` so a Sentry issue links straight back to the audit trail. Sentry alerts on *new* and *regressed* issues; Cloud Monitoring alerts on *conditions*. Do not duplicate one in the other.

### 10.7 Reconciliation — the checks that audit the audit

Scheduled jobs, each writing its own audit events and feeding `attestation.reconciliation.mismatch`:

1. **Inbound folder ↔ batch registry** — catches lost upload events (v2 §6.5.2).
2. **Batch disposition counts ↔ manifest counts** — the equation in §6, per batch.
3. **Approved items ↔ slice writes ↔ OV outcomes** — every approved item reached a terminal, recorded state.
4. **Task states ↔ `next_attestation_date`** — the clock and the workflow agree.
5. **Hot audit rows ↔ archived audit rows** — nothing lost in aging.
6. **Outbox ↔ published events** — nothing committed but never published.

---

## 11. Operator surface and runbooks

Operators get authorized, audited actions and **no direct database access**. The catalogue, carried from v1 §14.3: replay an outbox event; retry or quarantine vendor data; retry a release item **with its original idempotency key**; recover an expired worker lease; reconcile an uncertain MDM or Terminations result; rebuild reporting; pause a tenant, source, notification stream, or sync worker.

Two rules to state explicitly in the design doc:

- **Reconcile before replay.** When a cross-system result is uncertain (the MDM call timed out and may or may not have applied), the operator's first action is reconciliation, never a blind retry. A retry against an already-applied effect is how duplicates get created by the very people trying to prevent them.
- **Every operator action is an audit event with a reason.** "Why did this item change state at 03:14 on a Sunday?" must have an answer, and the answer must name a person.

---

## 12. Testing the audit trail

The audit trail is a feature and needs tests, not just presence checks. Minimum bar:

- For a full happy-path attestation, assert the **exact expected event sequence** — the story is complete and in order.
- Assert that a failed audit write **rolls back** the business mutation (v1 §14.2's listed case).
- Assert the batch reconciliation equation balances on a synthetic batch containing every disposition type.
- Assert that a cross-tenant read of `audit_events` returns nothing, from every path: API, report, replay, export.
- Assert append-only: attempt an update and a delete through the data layer; both must be impossible, not merely unused.
- Assert that a bulk action with a deliberate partial failure produces one bulk event plus per-item events with truthful outcomes.
- Assert `APPROVED_VALUE_DID_NOT_SURVIVE` fires when a higher-ranked source beats an approved value — construct the ranking to force it.
- Replay an archived range and confirm it reconstructs the same story as the hot table.

---

## 13. Open items to raise

| # | Item | Owner |
| --- | --- | --- |
| A-1 | **Read/access auditing** — is view-level auditing of provider data required? Recommendation in §7: audit individual-record views and exports, not grid pagination. | Compliance + Security |
| A-2 | **MDM lineage fix** (§9) — three additive changes to survivorship. Without it, "which config decided this" is unanswerable. | MDM workstream; flagged dependency on every audit ticket |
| A-3 | **Hot-window length** before archival (proposal: 18 months) and the archive query surface (BigQuery over GCS). | Engineering + Compliance |
| A-4 | **Data-deletion vs. 7-year retention** conflict, and whether crypto-shredding of PII fields with a retained event skeleton is acceptable. | Compliance + Security |
| A-5 | **Legal-hold mechanism** — scope (tenant, practitioner) and who can invoke it. | Compliance |
| A-6 | **Client-facing audit access** — do clients get an evidence export or a read-only view of their own audit trail, or is it internal-only with support-mediated requests? | Product |
| A-7 | **Audit event schema versioning** — events will gain fields over seven years; the archive must remain readable across versions. | Engineering |
| A-8 | **No metrics library in `api-layer`** (§10.0 gap 1) — every counter in §10.3 needs a path to exist. Recommendation: `quarkus-micrometer` with an OTLP registry through the OTel Collector we already run. | Engineering |
| A-9 | **JSON console logging is off** in `api-layer` (§10.0 gap 2) — until it is on, `correlation_id`-scoped log search does not work. | Engineering |
| A-10 | **600s DAL read timeout with no circuit breaker** (§10.0 gap 3) — use the `quarkus-smallrye-fault-tolerance` already on the classpath rather than inheriting the default. | Engineering |
| A-11 | **Trace context across Pub/Sub and Cloud Tasks** (§10.2) — neither propagates `traceparent` automatically; publisher and consumer work is required or every trace ends at the queue. | Engineering (ingestion design doc) |
| A-12 | **Metric label cardinality** — `tenant_id` yes, `practitioner_id` never. Worth stating as a review rule before the first counter ships. | Engineering |

---

## Appendix A — One obligation, end to end

Dr. Priya Patel, Harbor Health Plan, Q3 2026 obligation. She is listed at three locations but works only at Oak Street, and her phone number is wrong. Candor independently disputes her primary specialty. This is the trail the system must leave.

**Correlation id for everything below:** `harbor-health:prac_8f21c4:2026Q3`

| # | `occurred_at` | `event_type` | `actor_type` / actor | What is recorded |
| --- | --- | --- | --- | --- |
| 1 | 06-01 02:00 | `PRACTITIONER_DATES_BACKFILLED` | `SCHEDULER` / backfill-run-7 | `next_attestation_date`: null → 2026-08-15 (staggered) |
| 2 | 07-16 03:00 | `CYCLE_SCAN_COMPLETED` | `SCHEDULER` / cycle-scan-2026-07-16 | window `[2026-07-01, 2026-07-31]`, 4,182 examined, 3,914 created, 268 duplicate-skipped |
| 3 | 07-16 03:04 | `IDENTITY_COMPUTED` | `WORKER` / api-layer@2026.07.14-2 | `harbor-health:prac_8f21c4:2026Q3` |
| 4 | 07-16 03:04 | `TASK_CREATED` | `WORKER` | `task_id=t_9c11`, due 2026-08-15, clock rule `BACKFILL_STAGGERED` |
| 5 | 07-16 06:00 | `OUTREACH_SENT` | `WORKER` / outreach | tier `T-30`, template `attest_reminder_v2`, SendGrid ref `sg_88a1` |
| 6 | 08-01 04:00 | `EXPORT_FILE_WRITTEN` | `SCHEDULER` / vendor-export | export batch `E-2026-08-01`, 3,914 rows, SHA `9f2c…`, `gs://…/outbound/E-2026-08-01.csv` |
| 7 | 08-08 06:00 | `OUTREACH_SENT` | `WORKER` | tier `T-7` |
| 8 | 08-11 10:14 | `PORTAL_SESSION_STARTED` | `PRACTITIONER` / portal_u_3312 | auth `magic_link`, resolved `prac_8f21c4`, 1 practitioner visible |
| 9 | 08-11 10:14 | `FORM_PREFILLED` | `PRACTITIONER` | OV snapshot `v_44197`, read at 10:14:02 — **the evidence of what she was shown** |
| 10 | 08-11 10:21 | `ATTESTATION_SUBMITTED` | `PRACTITIONER` | snapshot `v_44197` still current; 3 deltas |
| 11 | 08-11 10:21 | `STAGED_ITEM_CREATED` ×3 | `WORKER` | `ci_a1` phone UPDATE `(555) 0100 → (555) 0199`; `ci_a2` location REMOVE River Road; `ci_a3` location REMOVE Hilltop. All `source_id=portal-attestation:harbor-health`, version 1 |
| 12 | 08-11 10:21 | `TASK_STATUS_CHANGED` | `WORKER` | OPEN → **SUBMITTED** (attester's job done — *not* workflow complete, D2-09) |
| 13 | 08-11 10:21 | `CLOCK_ADVANCED` | `WORKER` | `next_attestation_date`: 2026-08-15 → 2026-11-09 (submission + 90) |
| 14 | 08-14 23:47 | `MANIFEST_RECEIVED` | `EXTERNAL_VENDOR` / sftp id `candor-prod` | vendor batch `CD-4471`, refs export `E-2026-08-01`, declared SHA `1a77…`, 3,914 rows |
| 15 | 08-14 23:47 | `BATCH_REGISTERED` → `CHECKSUM_VERIFIED` → `ROWCOUNT_VERIFIED` → `SCHEMA_VERSION_RESOLVED` | `WORKER` / ingestion | schema `candor.v3` via adapter |
| 16 | 08-14 23:52 | `ROW_DISPOSITIONED` | `WORKER` | row 41208, `primary_phone` → `(555) 0199`, disposition **NO_OP** (Candor agrees with what she just attested) |
| 17 | 08-14 23:52 | `ROW_DISPOSITIONED` | `WORKER` | row 41209, `primary_specialty` `Cardiology → Interventional Cardiology`, disposition **ACTIONABLE** |
| 18 | 08-14 23:52 | `IDENTITY_RESOLVED` | `WORKER` | NPI `1477…` → `prac_8f21c4`, method `npi_crosswalk_exact` |
| 19 | 08-14 23:52 | `STAGED_ITEM_CREATED` | `WORKER` | `ci_b1`, `source_id=candor:harbor-health` |
| 20 | 08-14 23:58 | `BATCH_COMPLETED` | `WORKER` | 3,914 = 2,806 no-op + 41 dup/stale + 12 excluded + 3 quarantined + 8 prev-rejected + 1,044 actionable ✅ |
| 21 | 08-18 09:02 | `ITEM_ASSIGNED` ×4 | `PAYER_REVIEWER` / admin_u_77 | `ci_a1..a3`, `ci_b1` → reviewer admin_u_91 |
| 22 | 08-18 11:30 | `ITEM_APPROVED` | `PAYER_REVIEWER` / admin_u_91, roles `[payer_reviewer]` | `ci_a1` phone, version 1 → 2 |
| 23 | 08-18 11:31 | `ITEM_APPROVED` ×2 | `PAYER_REVIEWER` / admin_u_91 | `ci_a2`, `ci_a3` location removals |
| 24 | 08-18 11:34 | `ITEM_REJECTED` | `PAYER_REVIEWER` / admin_u_91 | `ci_b1`, reason code `CONTRADICTED_BY_PROVIDER`, text *"provider attested Cardiology on 08-11"* |
| 25 | 08-18 11:34 | `REJECTED_RECOMMENDATION_RECORDED` | `WORKER` | `(harbor-health, prac_8f21c4, primary_specialty, UPDATE, "interventional cardiology")` — future Candor batches match and suppress (D2-07) |
| 26 | 08-18 11:40 | `SYNC_REQUESTED` | `PAYER_REVIEWER` / admin_u_91 | mode BULK, 3 items, `release_id=r_5501` |
| 27 | 08-18 11:40 | `SLICE_UPSERTED` | `WORKER` / sync | `source_id=portal-attestation:harbor-health`, `crosswalk_id=1477…`, `primary_phone=(555) 0199` |
| 28 | 08-18 11:41 | `TERMINATION_REQUESTED` ×2 | `WORKER` / sync | scope `PRACTITIONER_LOCATION`, River Road and Hilltop, effective 2026-08-31 |
| 29 | 08-18 11:43 | `OV_OUTCOME_VERIFIED` | `MDM_ENGINE` | read-back: `primary_phone=(555) 0199`, winner `portal-attestation:harbor-health`, config version *(see §9 — **not currently recorded**)* |
| 30 | 08-18 11:43 | **`APPROVED_VALUE_DID_NOT_SURVIVE`** | `MDM_ENGINE` | `ci_a2`'s address normalization lost to a higher-ranked roster value on `address_line_2`; approved value and OV differ, recorded plainly |
| 31 | 08-18 11:44 | `SYNC_ITEM_COMPLETED` ×3 | `WORKER` | release `r_5501` terminal |
| 32 | 09-01 02:00 | `CLIENT_EXPORT_GENERATED` → `DELIVERED` | `SCHEDULER` / client-export | 3,776 attested practitioners, SHA `c30b…`, manifest written last |

**What this trail proves, and where it still cannot.** Events 9, 10, 24, and 29 answer "what was she shown, what did she claim, why was the vendor overruled, and what actually landed." Event 30 answers the question that otherwise generates an angry client call. Event 29's missing config version is §9's gap — the one thing in this whole story we cannot currently evidence.

**A single audit row, in full:**

```json
{
  "audit_id": "9a1f2e7c-…",
  "tenant_id": "harbor-health",
  "occurred_at": "2026-08-18T11:34:12.882Z",
  "recorded_at": "2026-08-18T11:34:12.906Z",
  "stage": "STAGE_4",
  "event_type": "ITEM_REJECTED",
  "outcome": "SUCCEEDED",
  "correlation_id": "harbor-health:prac_8f21c4:2026Q3",
  "task_id": "t_9c11",
  "batch_id": "b_01J9CD4471",
  "row_id": 41209,
  "change_item_id": "ci_b1",
  "change_item_version": 1,
  "review_action_id": "ra_31f8",
  "entity_type": "CHANGE_ITEM",
  "entity_id": "ci_b1",
  "actor_type": "PAYER_REVIEWER",
  "actor_id": "admin_u_91",
  "actor_display": "J. Alvarez",
  "actor_roles": ["payer_reviewer"],
  "on_behalf_of_practitioner_id": null,
  "source_id": "candor:harbor-health",
  "attribute": "primary_specialty",
  "operation": "UPDATE",
  "before_value": "cardiology",
  "after_value": "interventional cardiology",
  "reason_code": "CONTRADICTED_BY_PROVIDER",
  "reason_text": "provider attested Cardiology on 08-11",
  "disposition": null,
  "error_code": null,
  "request_id": "req_7d21…",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "deployment_version": "api-layer@2026.08.18-1",
  "event_schema_version": 1
}
```

Note `before_value` / `after_value` are the **normalized** forms (§5) — the same normalization, at the same version, that the `rejected_recommendations` match key in event 25 uses. That is what makes the next batch's suppression work.

---

## Appendix B — Query cookbook

Answers to the ten questions in §2, as queries someone can actually run.

**Q1 — the whole story for one obligation:**
```sql
SELECT occurred_at, stage, event_type, actor_type, actor_display,
       attribute, operation, before_value, after_value, reason_text, outcome
FROM audit_events
WHERE tenant_id = @tenant AND correlation_id = @correlation_id
ORDER BY occurred_at, recorded_at;
```

**Q2 — who attested, and were they acting for someone else:**
```sql
SELECT occurred_at, actor_type, actor_id, actor_display,
       on_behalf_of_practitioner_id, actor_roles
FROM audit_events
WHERE tenant_id = @tenant AND correlation_id = @correlation_id
  AND event_type = 'ATTESTATION_SUBMITTED';
```

**Q3 — why a recommendation was rejected:**
```sql
SELECT occurred_at, actor_display, reason_code, reason_text,
       batch_id, row_id, source_id, before_value, after_value
FROM audit_events
WHERE tenant_id = @tenant AND change_item_id = @change_item_id
  AND event_type = 'ITEM_REJECTED';
```

**Q5 — approved but not in the Golden record:**
```sql
SELECT occurred_at, change_item_id, attribute,
       after_value      AS approved_value,
       JSON_VALUE(error_detail, '$.winning_value')  AS ov_value,
       JSON_VALUE(error_detail, '$.winning_source') AS winning_source
FROM audit_events
WHERE tenant_id = @tenant
  AND event_type = 'APPROVED_VALUE_DID_NOT_SURVIVE'
  AND occurred_at BETWEEN @from AND @to;
```

**Q6 — prove a batch is fully accounted for:**
```sql
SELECT disposition, COUNT(*) AS rows
FROM audit_events
WHERE tenant_id = @tenant AND batch_id = @batch_id
  AND event_type = 'ROW_DISPOSITIONED'
GROUP BY disposition;
-- must sum to the manifest count recorded on MANIFEST_RECEIVED
```

**Q8 — two-business-day evidence for one attestation:**
```sql
SELECT
  MIN(IF(event_type = 'ATTESTATION_SUBMITTED',   occurred_at, NULL)) AS clock_start,
  MAX(IF(event_type = 'OV_OUTCOME_VERIFIED',     occurred_at, NULL)) AS landed_in_ov,
  MAX(IF(event_type = 'ITEM_APPROVED',           occurred_at, NULL)) AS last_decision
FROM audit_events
WHERE tenant_id = @tenant AND correlation_id = @correlation_id;
```

**Q9 — did anyone act outside the workflow:**
```sql
SELECT occurred_at, event_type, actor_id, actor_display, reason_text, entity_type, entity_id
FROM audit_events
WHERE tenant_id = @tenant
  AND (actor_type = 'OPERATOR' OR event_type IN ('CONFIG_CHANGED',
       'SOURCE_RANKING_CONFIG_CHANGED', 'TENANT_PAUSED', 'REPLAY_TRIGGERED'))
  AND occurred_at BETWEEN @from AND @to
ORDER BY occurred_at;
```

**Q10 — what is stuck right now:**
```sql
SELECT batch_id, disposition, reason_code, COUNT(*) AS rows,
       MIN(occurred_at) AS oldest
FROM audit_events
WHERE tenant_id = @tenant AND event_type = 'ROW_QUARANTINED'
  AND NOT EXISTS (
    SELECT 1 FROM audit_events r
    WHERE r.tenant_id = audit_events.tenant_id AND r.row_id = audit_events.row_id
      AND r.event_type IN ('QUARANTINE_RESOLVED', 'QUARANTINE_CLOSED_BY_OPERATOR'))
GROUP BY batch_id, disposition, reason_code
ORDER BY oldest;
```

**The operational counterparts** — same investigation, different tool:

```
# Cloud Logging — the operational story for one obligation
jsonPayload.correlation_id="harbor-health:prac_8f21c4:2026Q3"
severity>=INFO

# Cloud Logging — everything that went wrong in one batch
jsonPayload.batch_id="b_01J9CD4471" severity>=WARNING

# Cloud Trace — find the slow span in one ingestion run
trace_id from the audit row's trace_id field → open in Cloud Trace

# Sentry — exceptions for one tenant since a release
tenant_id:harbor-health release:api-layer@2026.08.18-1
```

**The rule this appendix illustrates:** the SQL answers compliance questions and is authoritative for seven years; the log/trace/Sentry queries answer operational questions and expire in weeks. The `trace_id` on every audit row is the bridge between the two — for as long as the telemetry still exists.
