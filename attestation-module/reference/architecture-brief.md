# Directory Accuracy & Attestations — Module Architecture Brief (pre-meeting)

**Prepared:** 2026-08-27 · **Companion to:** `directory-accuracy-attestations-consolidated-source-of-truth-v2.md` (workflow) · **Grounded in:** v1 §19 codebase analysis (2026-08-24 pass over `core-data-access-layer`, `api-layer`, `dal-mdm-*-layer`, `frontend`, `provider-portal-api`, `core-dal-egress-practitioner-async`)

**What this is:** a per-module technology position for the architecture discussion. v2 §5 grants "architecture freedom" to every new module. This brief takes a stance on how to use that freedom, module by module, with the alternative that was considered and the reason it loses. Nothing here is decided — it is the position to argue from.

**The one-line thesis:** *reuse the proven rails for everything except the two genuinely new things (the workflow store and the vendor ingestion lane), and even those follow existing in-house patterns.* Architecture freedom is a licence to pick the right thing, not an obligation to pick a new thing. Every new datastore, queue product, or runtime we introduce is a new on-call surface for a team that already has one.

---

## 0. One-glance summary

| # | Module | Recommendation | New or reuse |
| --- | --- | --- | --- |
| 1 | Attestation Cycle Scheduler | Cloud Scheduler → `api-layer` trigger endpoint (202) → Cloud Tasks fan-out, one task per practitioner | Reuse pattern (AutoRecred), new package |
| 2 | Outreach | Existing `smart_outreach` engine in `api-layer` + new reminder types/templates | Reuse, config + small code |
| 3 | Portal UI + API | `frontend/apps/provider-portal` + `provider-portal-api`, new task type on the existing attest flow | Reuse, extend |
| 4 | SFTP Exchange | CertifyOS-operated SFTP, per-vendor chrooted identity + key auth; **immediate mirror to GCS**; GCS is the event source and the archive | Reuse infra, new folders/identities; DevOps builds (D2-30) |
| 5 | External Source Ingestion Layer | GCS object notification → Pub/Sub (manifest object only) → batch registry → Cloud Tasks paged parser workers, one adapter per vendor | New lane, existing rails |
| 6 | Attestation Module Backend | New `directory_accuracy` package **inside `api-layer`** (Quarkus), not a separate microservice | New-inside-existing |
| 7 | Attestation Module Data Layer | Repository/gateway package inside the backend, owning the new DB. Not a change to `core-data-access-layer` | New |
| 8 | Attestation Module Database | **Spanner — dedicated database in the `app-data` instance**, roster-staging precedent. Not MongoDB | New DB, existing engine |
| 9 | Attestation Module UI | `frontend/apps/web`, new feature cloned from the monitoring-flags queue | Reuse, new feature |
| 10 | Sync worker | Cloud Tasks worker in the backend; writes **slices** under two new `core_sources` rows per tenant; terminations call existing endpoints | Reuse write path |
| 11 | Golden Record Processing | Untouched engines; config-only changes (per-tenant source-ranking rows) | Existing, extend-only |
| 12 | Client Export | Cloud Scheduler (30-day cadence) → export worker → SFTP `outbound/` + GCS archive; egress/webhook for the event | New job, existing rails |
| 13 | Ops & observability | Cloud Logging/Monitoring + DLQ alerts + internal admin screens in `apps/web` | Reuse |

---

## 1. Attestation Cycle Scheduler

**Recommendation.** Cloud Scheduler fires a daily HTTP trigger at a `@TenantAgnostic` internal endpoint in `api-layer`. That endpoint returns **202 immediately** (so it never hits the scheduler deadline), pages eligible practitioners with a keyset-paged window query, and enqueues **one Cloud Task per practitioner** to a process-practitioner endpoint. That endpoint asks the Attestation Module Backend to compute the deterministic identity and open the task.

**Why.** This is exactly the AutoRecred (auto-recredentialing) pattern already running in production: the window math (`leadDays` / `lookbackDays`) and the paged eligibility query exist. We add `next_attestation_date` denorm columns on `core_practitioners_ov` mirroring `next_credentialing_date` — the product spec explicitly asks for that mirror.

**Key correctness properties.**
- Eligibility is a **window query** `[today − lookback, today + lead]`, never a point-in-time check — a missed scheduler run is picked up by the next run automatically.
- Duplicate suppression is a **database unique index** on `(tenant_id, practitioner_id, due_period)`, not application memory. Scheduler double-fire → second insert is a no-op. Mirrors the one true existing idempotency key, `scheduled_outreaches(idempotency_key, evaluation_cycle)`.
- Task creation and the outreach-kickoff outbox record commit in **one transaction**.

**Alternatives considered.** Quartz/in-process cron inside the service (loses the retry, deadline, and observability story Cloud Scheduler + Tasks give free; scales badly with ~70k practitioners). Temporal or Airflow (a whole new runtime and on-call surface for what is a daily query and a fan-out — not justified at MVP scale).

**Say in the meeting:** "The scheduler is not new work. It is the recredentialing scheduler pattern pointed at `next_attestation_date`. Cloud Scheduler → 202 → Cloud Tasks, idempotent by unique index."

---

## 2. Outreach

**Recommendation.** No new infrastructure. Extend the existing `smart_outreach` engine in `api-layer` with three new attestation reminder types (T-30, T-7, T+1) and their templates.

**Why.** `scheduled_outreaches` already carries the idempotency key + evaluation-cycle model, the exclusion engine, per-tenant templates, and SendGrid delivery/bounce tracking. Reminder identity is `(task_id, reminder_type)` under a unique index — a retry cannot double-send.

**The one rule to state out loud:** email failure must never hide a task. T+1 is the last automated email; the task stays visible and submittable indefinitely (D2-23). Outreach is a notification channel, never the source of task state.

**Say in the meeting:** "Outreach is templates and reason codes on an engine we already run. Days, not architecture."

---

## 3. Portal UI + Portal API

**Recommendation.** Extend `frontend/apps/provider-portal` and `provider-portal-api`. Directory attestation becomes a **new task type + form** on the attestation rails that already exist there (magic-link JWT → session cookie, the `attest/{verify,session-data,validate-npi,submit}` endpoints, the dynamic form renderer, the portal user/entity/role tables).

**The one architectural change:** the Portal's submit no longer terminates in the portal's own tables — it posts **delta + metadata** to the Attestation Module Backend, which is the workflow owner (v2 §5 interaction rule). The Portal keeps the UX; the backend keeps the state.

**Two guards to design explicitly.**
- **Staleness guard** — the submission carries the snapshot version of the Golden record the form was rendered from. Version mismatch is rejected with a friendly refresh, never accepted silently. This is optimistic concurrency, not a nicety.
- **Already-attested guard** — a second eligible actor on the same task sees "already attested by X on Y" rather than double-attesting.

**Authorization note.** D2-17 settled this: provider admins are **tenant-scoped only**, no group-level narrowing. That means the check is the existing tenant scoping — no new association model to build. Worth saying, because it removes work people may still assume is in scope.

---

## 4. SFTP Exchange

**Recommendation.** CertifyOS-operated SFTP with `outbound/` and `inbound/` folders, **one chrooted identity per vendor with key-based auth and rotation**. Files landing on SFTP are **mirrored to GCS immediately** by a pickup job; from that moment GCS is the durable archive and the event source, and SFTP is only a transport doorway.

**Why the GCS mirror matters.** GCP has no managed SFTP service, so the SFTP endpoint is infrastructure someone must run (self-hosted, on the existing client SFTP infra). We do not want the ingestion pipeline coupled to a box: the moment a file is in GCS we get object notifications, immutable versioned archival, lifecycle policy, IAM, and replay-from-source for free. **The raw file in GCS is the reprocessing source of truth.**

**Security position — worth stating plainly.** A file's origin is proven by **which account deposited it**, not by its name or its checksum. The checksum in the manifest proves *integrity*, not *origin*. So: per-vendor dedicated SFTP identity, key auth, rotation, folder permissions that stop vendor A ever seeing vendor B's folder. Until vendors support file signing, possession of the per-vendor key is the explicit, documented trust boundary — an accepted risk, written down rather than assumed.

**Ownership.** D2-30 already settled it: **we write the design document** (folders, identities, rotation, permissions, monitoring, alerting), **DevOps builds and operates it.** So the deliverable from us is a document, not a server.

**Say in the meeting:** "SFTP is the doorway; GCS is the system of record. We spec it, DevOps runs it."

---

## 5. External Source Ingestion Layer

**Recommendation — the pipeline shape:**

```
vendor drops file, then manifest (manifest last)
  → SFTP inbound/  → pickup job mirrors both to GCS
  → GCS object notification, filtered to the MANIFEST object only
  → Pub/Sub topic (durable queue; DLQ configured)
  → batch registry check (vendor batch id + checksum) — already seen? ack and stop
  → ingestion worker: authenticate source, verify SHA + row counts, resolve schema version via vendor adapter
  → paged row processing via Cloud Tasks — one disposition per row, all counted
  → actionable rows staged through the Attestation Module Backend
```

**The three design points to defend.**

1. **Trigger on the manifest, never the data file.** Because the manifest is written last, its existence proves the data file is complete. This is what eliminates partial-file reads. Implementation detail: the GCS notification filter must match the manifest object name pattern, or we reintroduce the very bug the contract removes.
2. **Pub/Sub is at-least-once, and the platform has no processed-message dedup anywhere.** So this feature carries its own: a `processed_messages` table keyed on the Pub/Sub message id, on every new subscription. Plus the batch registry (batch id + checksum) as the business-level idempotency gate. Two layers, because they catch different failures — the registry catches a re-delivered *file*, the message table catches a re-delivered *event*.
3. **A lost event is caught by a reconciliation sweep**, not by hoping. A periodic job lists the inbound prefix and reconciles it against the batch registry. Anything present in the folder and absent from the registry is picked up.

**Vendor-agnostic by construction.** All vendor-specific knowledge (columns, enums, quirks, schema versions) lives in **exactly one adapter per vendor**. Column mapping per schema version is stored as configuration — precedent exists: `tenant_configurations` already holds `crosswalk-generation-config` this way. Onboarding vendor #2 = one `core_sources`-style registry row + one adapter + SFTP credentials. **No vendor name appears in any table, topic, class, or API name.**

**Explicit non-reuse.** Do **not** route this through roster ingestion. The spec forbids it and roster is template-driven and manual-touch-heavy. We reuse the roster *staging pattern* (separate database, per-row status lifecycle, quarantine lane) and not the roster *pipeline*.

**Throughput target to quote:** 100k rows parsed and dispositioned in under 30 minutes, ≥100 rows/s sustained, with a quarantine lane so one bad row never stops a batch.

---

## 6. Attestation Module Backend — new service or reuse `api-layer`?

**Recommendation: a new `directory_accuracy` package inside `api-layer` (Quarkus). Not a separate microservice.**

**Why.**
- Everything this backend needs already lives there: RBAC (`admin_roles`/`admin_permissions`/`admin_user_tenant_roles` and `use-has-permission` on the frontend), tenant scoping, DAL access, Cloud Tasks worker endpoints, the outreach engine it must trigger, deployment, and on-call.
- A separate service buys isolation we do not need yet and costs a new contract surface, a new deployment, new auth plumbing between it and `api-layer`, and a second on-call rotation — for a workload measured in tens of thousands of rows per month.
- The module boundary that actually matters is **logical**, and we get it for free: a package with its own database, its own data layer, and no other component allowed to touch that database. If we later need to extract it into its own service, a clean package with its own datastore is precisely the thing that extracts cleanly.

**Honest counter-argument (be ready for it).** v2 §5 grants architecture freedom, and someone will reasonably ask why we are not using it here. The answer: freedom was granted so we would not be *forced* into a bad fit. `api-layer` is a good fit. If the team wants isolation anyway, the credible alternative is a **separate Cloud Run service in the same language/framework**, sharing the DAL — that is a deployment decision we can take later without redesigning anything, precisely because the package boundary is clean from day one. What we should *not* do is introduce a different language/runtime for this module.

**What the backend owns (say this list — it is the module's identity):** tasks and their lifecycle, the deterministic identity, submissions, source-tagged staged change items, `rejected_recommendations`, review actions and assignments, releases, audit, the outbox, and the sync worker.

**Concurrency rule to name:** every staged change item carries a UUID plus an **integer version**; every reviewer decision, rollback, and bulk action is a conditional update on the expected version. Integer versioning is deliberately stronger than the platform's usual `updated_at` comparison, which is exposed to commit-timestamp collisions. This is what makes "two reviewers cannot silently overwrite each other" (v2 §6.9) true rather than aspirational.

---

## 7. Attestation Module Data Layer

**Recommendation.** A repository/gateway package **inside the backend**, owning the new database exclusively. Not an extension of `core-data-access-layer` — that repo is the access layer for the core slice tables, and the workflow store is deliberately a different database with a different lifecycle.

**Why a data layer at all, rather than repositories scattered through the service.** Three cross-cutting invariants have to be true on *every* read and write, and the only reliable way to guarantee them is to make one place the sole door: **tenant scoping** (server-side, always, never a UI filter), **validation**, and **audit stamping**. v2 §5 already states this as an interaction rule; this is the mechanism that enforces it.

**Inherited risk to flag:** the DAL currently trusts unsigned identity headers. This feature must not widen that surface — staging access stays behind `api-layer` authorization, and the new data layer never accepts a tenant id it was merely told.

---

## 8. Attestation Module Database — Spanner or MongoDB?

**Recommendation: Spanner. A dedicated database in the existing `app-data` instance**, following the roster-staging precedent (`roster_rows` lives in exactly this arrangement today).

**Why Spanner, concretely — the workload is relational and correctness-critical:**

| Requirement | What it needs | Spanner | MongoDB |
| --- | --- | --- | --- |
| No duplicate task ever | Unique index on `(tenant, practitioner, due_period)` | Native, enforced | Unique index exists, but the pattern is not how the platform reasons today |
| Task + outbox commit together | Multi-row ACID transaction | Native | Multi-document transactions exist but are an operational cliff |
| Rejected-recommendations lookup | Composite index on `(tenant, practitioner, attribute, operation, normalized_value)`, exact-match, millions of rows | Native, index-served | Fine, but no advantage |
| Reviewer concurrency | Conditional update on expected version | Native | Achievable |
| Audit, 7-year retention (D2-20) | Point-in-time recovery, backups, retention policy | PITR + backups, in place | New backup/retention story to build |
| Ops | Alerting, IAM, DR runbooks | Already exist | All new |

**The decisive argument is not technical elegance, it is this:** MongoDB would be a **brand-new datastore in the platform**, with new backups, new DR, new IAM, new monitoring, new on-call knowledge — bought for a workload that is joins, uniqueness constraints, and transactional audit. That is the workload relational databases are best at. There is no document-shaped requirement here that Spanner's JSON columns do not already cover (the platform already stores entity payloads in a `data` JSON column, so the mixed-shape parts of a staged change item have a precedent too).

**Why a *separate* database rather than new tables in the DAL database:** isolates high-churn workflow tables from core practitioner load, keeps retention and archival policy independent (7 years of audit should not be entangled with core record policy), and matches the roster precedent that already proved this shape in production.

**Say in the meeting:** "Spanner, own database, roster precedent. Mongo would be a new datastore with a new ops surface for a workload that is fundamentally relational."

---

## 9. Attestation Module UI

**Recommendation.** New feature in `frontend/apps/web`, cloned from the **monitoring-flags queue**: the grid + filters, the generic `bulk-actions-bar`, the bulk-confirmation modal, and `use-has-permission` gating. The legacy `client-web-app` is not the target.

**Why.** The review queue is, structurally, the monitoring queue: tenant-scoped list, filters and counts, per-row actions, bulk actions with per-item results, permission gating. That component set is in production and its bulk-action semantics (explicit per-item results, no hidden partial failure) are exactly what D2-10 and v2 §6.9 require.

**What is genuinely new UI work** (worth naming so it is estimated honestly):
- The **side-by-side view**: self-attested record next to vendor recommendations for the same practitioner, joined on the deterministic identity, with vendor evidence rendered as a plain copyable string (D2-27).
- **Assignment** — manual in MVP (D2-10), so: assign/unassign, a reviewer filter, and an unassigned pool. Rule-based assignment is a later backend extension, not a redesign.
- **Sync Latest with live async status** (started / in-progress / done / failed) and the pre-sync rollback affordance with its warning.

**RBAC.** Role and permission rows are data. Enforcement needs new permission-type enum constants in `api-layer` plus the frontend permission-type mirrors — hours of work, not architecture. A `directory` resource already exists.

---

## 10. Sync worker and the write path to the Golden record

**Recommendation.** The sync worker is part of the Attestation Module Backend, driven by Cloud Tasks. On Sync Latest it takes approved items and, for adds/updates, **upserts each approved value into the entity's slice table under two new source identifiers per tenant** — `portal-attestation:{tenantId}` and `candor:{tenantId}`, matching the existing `tenant:{tenantId}` / `roster:{tenantId}` convention. Removals call the **existing termination endpoints**.

**Why this is the whole integration.** Any slice write automatically triggers the existing pipeline: cleansing → matching → survivorship → OV → downstream projections. So writing a slice *is* the integration — zero new merge code, and a single point of write to the Golden record, which is exactly what the spec demands. The upsert is idempotent by the `(source_id, crosswalk_id)` unique index.

**Four guardrails that must be in the design doc — these are the traps:**

1. **Never stage proposals as slices.** A slice write fires the pipeline immediately; unreviewed data would reach the OV before a reviewer sees it. Slices are also upsert-only per `(source, crosswalk)` — no room for pending/approved/rejected state, versions, or two competing proposals on one field. This is *the* reason the staging database exists.
2. **An unconfigured source type silently ranks 997**, which outranks NPPES (998) and CAQH (999). So the sync worker must **refuse to sync for a tenant whose survivorship ranking config lacks explicit entries for the new sources** (D2-18). The cleansed variants (`certify-cleanser:candor:*`) need ranking entries too; wildcards are supported.
3. **List-attribute identity must match survivorship's grouping keys** — practice locations/addresses group on `[address1, city, state, zipcode]`, licenses on `[licenseNumber]`, and those keys are hardcoded Java. A staged "remove location X" delta must carry the same composite key the merge engine uses, or the released change and the merged result will disagree.
4. **Do not trust a 200.** Survivorship's HTTP resource returns 200 with `status:"error"` on failure, and there is no Pub/Sub redelivery or consumer dedup. The sync worker must verify the outcome — read back the OV, or consume `mdm.slice.ov_generated` and reconcile — before reporting an item synced. Related: a replayed termination returns HTTP 400 "already terminated"; treat that as terminal success, not failure. Note `terminate-partial` sits behind a default-off kill switch.

**Say in the meeting:** "Approval is not the same as winning. A higher-ranked source can still beat an approved value, and the audit records the actual outcome, not the intended one."

---

## 11. Golden Record Processing

Nothing to design. Extend-only (D2-14). Our work here is **configuration**: per-tenant `source-ranking-rule` rows seeding `portal-attestation` > `candor` > roster/UI, including the cleansed variants. One caveat to know: the `_default` survivorship config is baked into the survivorship JAR, so a global default change is a redeploy — per-tenant configuration rows are the only runtime-changeable path, which is why D2-18's per-tenant JSON is the correct and only mechanism.

---

## 12. Client Export

**Recommendation.** Cloud Scheduler on the **30-day cadence** (D2-13) → export worker in the backend → file to SFTP `outbound/` with the same **manifest-last** discipline we ask of vendors, plus a GCS archive copy. The sync-complete event (`mdm.slice.ov_generated` consumption, or our own outbox event) is what marks practitioners as freshly attested.

**Why cadence-driven rather than purely event-driven:** D2-13 fixes a 30-day cadence, and a per-sync export would produce a stream of tiny files no client wants to consume. The event tells us *who* to include; the schedule decides *when* to send. Attested practitioners only — never a full-population dump.

**Reuse note.** OV changes already flow through `core-dal-egress-practitioner-async` and the existing egress templates and webhook dispatcher. If the client wants push notification rather than a file, that is an existing rail, not new work. **Open:** format and channel land in the ingestion module design doc (O-9 closed on cadence only).

---

## 13. Cross-cutting: the patterns every module inherits

- **Outbox pattern everywhere.** State change and its domain event commit in one transaction; a relay publishes to Pub/Sub. This is what makes "the task was created but the email never fired" impossible.
- **Deterministic identity + unique index, never application memory.** `tenant_id + practitioner_id + due_period`, computed once by the backend, travelling unchanged through task → export row → staged item → review action → sync → client export. It is also the correlation id for the audit trail.
- **Processed-message dedup on every new subscription.** None exists platform-wide; at-least-once is the delivery contract; this feature brings its own table.
- **Quarantine over guessing.** Dependency outage → park and retry. Poison item → bounded retries, then quarantine with the error preserved so siblings continue. Never mislabel a row as invalid because a lookup was down.
- **Every failure has a state:** retryable, quarantined, or closed-with-reason. Counts reconcile per batch. Nothing is silently discarded.
- **No direct database edits, ever.** Operator recovery is an audited replay through the ops surface.
- **Observability:** Cloud Logging/Monitoring, DLQ-depth alerts on every new subscription, and explicit timeouts and circuit breakers on every DAL/MDM call — the platform's 600s read-timeout-with-no-breaker default is a known outage risk and must not be inherited.
- **Targets to quote:** API availability 99.9% monthly non-5xx; RPO ≤ 15 min, RTO ≤ 4 h via Spanner PITR; zero duplicate cycles or reminders per run, measured.

---

## 14. The three questions likely to be pushed back on

**"Architecture freedom was granted — why does this look like reuse?"**
Because freedom was granted so no module gets forced into a bad fit, not to mandate novelty. Two modules genuinely are new — the workflow store and the vendor ingestion lane — and both are designed fresh. Everything else has a production-proven rail that already solves the problem, and each new runtime or datastore we add is a permanent operational cost against a fixed team.

**"Why not a dedicated microservice for the Attestation Module?"**
The boundary that matters is logical, and we enforce it fully: own package, own database, own data layer, no other component allowed through. That is also precisely the shape that extracts into a service cleanly if load or team structure later demands it. Extracting later is cheap; running a second service now is not.

**"Why not MongoDB for the staging data?"**
Because the workload is uniqueness constraints, transactional audit, and joined queries, and because it would introduce the platform's first Mongo deployment with an entirely new backup, DR, IAM, and on-call story. Spanner is already run in production, already has the retention and PITR story that 7-year audit (D2-20) requires, and its JSON columns cover the mixed-shape parts.

---

## 15. What is still genuinely open going in

| Item | Status |
| --- | --- |
| Candor file contract — columns, enums, manifest fields, schema versioning (O-1) | Open. Blocks the vendor adapter and the outbound export format. Internal proposal (D2-06) must be drafted before the technical call. |
| Partial-approval compliance semantics and the exact two-business-day boundary (O-5) | Open. Compliance owns it. |
| Client export format and delivery channel | Cadence decided (30 days, D2-13); format/channel land in the ingestion module design doc. |
| Backend deployment shape — package in `api-layer` vs. separate Cloud Run service | Recommendation above is "package". Deferrable without redesign if the package boundary is kept clean. |
| Disposition comparison semantics — is supersede keyed on `(practitioner, attribute)` or `(practitioner, attribute, operation)`, and is ordering taken from manifest `produced-at` or our batch sequence? | Needs a decision in the ingestion design doc. Also: does supersede reach approved-but-unsynced items, or pending only? |
| Cross-lane duplicate suppression | Must be stated explicitly: a vendor row equal to a portal delta is **not** deduped — the reviewer is meant to see both side by side. Ingestion must not collapse it. |

---

## 16. Next documents, in dependency order

1. Attestation Module Backend + data layer + database
2. External Source Ingestion Layer (includes the client export contract)
3. Attestation Module UI
4. SFTP Exchange (we author, DevOps builds — D2-30)
5. Scheduler / Outreach extensions
6. Client Export
