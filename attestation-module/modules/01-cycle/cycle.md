# Design: Cycle — backfill, scheduler, task creation, outreach trigger

## Purpose and scope

This module answers one question on a loop: **which practitioners owe an attestation right now, and did we open exactly one task and start the reminder emails for each of them?**

**This module owns:**

- **Stage 0 backfill** — the one-time job that gives every in-scope practitioner an attestation schedule, spread out (staggered) so ~70k practitioners do not all become due on the same day.
- **The attestation schedule itself** — next attestation dates live in the module's **own database**, not on the practitioner OV row (decided 2026-08-29, strengthened 2026-09-01 by D2-32; see *Data model*).
- **The eligibility scan** — the recurring job that finds obligations entering their window and opens them.
- **The deterministic identity** — `tenant_id + practitioner_id + due_period` (D2-12; `practitioner_id` is stored physically as `certify_practitioner_id`, referencing the OV's `certify_id`), computed exactly once per obligation. Every later module reuses it unchanged.
- **Task opening** — flipping a scheduled obligation to an open task, safely (running it twice opens it once).
- **Outreach trigger** — publishing the T-30 / T-7 / OVERDUE reminder command (transactional outbox → `SCHEDULE_SENDS` on the Smart Outreach Service's command topic) (v2 §6.2; contracts in the Smart Outreach Service design doc). The recipient registry itself is kept in sync by the PDM backend, not this module (D14).

**This module does NOT own:**

- **Advancing the clock** — creating the *next* obligation after a submission belongs to the submission flow (docs 2/5). This module defines the row shape it inserts; the rules are recorded in *Contracts*.
- **Storing provider data** — the OV stays the golden record of provider facts. This module reads it (backfill population, export, prefill) and never writes it.
- Building and sending emails — the **Smart Outreach Service** (platform doc `smart-outreach-service.md`) does that: it owns the templates, the recipient registry, the send queue, SendGrid, and the delivery outcomes. This module publishes commands to it and consumes its outcomes (doc 5 hosts the consumer). The legacy `smart_outreach` engine in `api-layer` is not involved.
- The vendor export file — open tasks feed it; the export job belongs to doc 3.

**Neighbors:** Portal lane (doc 2) shows open tasks and receives the email deep links; SFTP Exchange (doc 3) exports open-task practitioners; Backend (doc 5) hosts the endpoints and the submission flow; Database (doc 6) owns the final DDL.

## Section triage

| Section                       | Included | Reason                                                                                  |
| ----------------------------- | -------- | ---------------------------------------------------------------------------------------- |
| Approach                      | Yes      | The chosen design as numbered steps                                                     |
| Alternatives considered       | Yes      | Condensed — each alternative with its deciding criteria and cost                        |
| Components and files touched  | **No**   | Pre-implementation design doc — no file lists yet                                       |
| Contracts and interfaces      | Yes      | Full lifecycle with payloads, the identity, task states, config entry                   |
| Data model and migration      | Yes      | The module tables, where they live, the backfill                                        |
| Security, privacy, and access | Yes      | Internal-endpoint auth, tenant isolation, email content policy                          |
| Performance and scale         | Yes      | The NFR numbers all cost estimates use                                                  |
| Observability                 | Yes      | A compliance clock must be seen to be trusted                                           |
| Audit trail                   | Yes      | **Added section** — compliance evidence is a first-class requirement                    |
| Failure modes and rollback    | Yes      | Queue failure handling, edge cases, rollback                                            |
| Test strategy                 | **No**   | Design phase; decisive failure cases are in Failure modes                               |
| Rollout                       | Yes      | Migration → backfill → pilot ordering, kill switch, DevOps handoff                      |


## Approach

The AutoRecred scheduling pattern (Cloud Scheduler → trigger → Cloud Tasks chunks), running inside the module's own service (D2-32) against the module's **own database**, with every cross-service effect leaving through a transactional outbox to the Smart Outreach Service's command topic. Each step: what we do, why, and why not the alternative a reviewer would ask about.

**1. Create the module's own tables — in the module's own database (`attestation-db`).**

- **What:**
  - One `attestation_tasks` table, its append-only audit table, and one `attestation_outbox` table (step 5). Final DDL is doc 6's; the shape is this doc's contract.
  - The tables live in a **new Spanner database owned by this module** — proposed name **`attestation-db`** — on the existing "app-data" Spanner instance, a sibling of the primary PDM database (`data-production-b`) and the other platform databases (`app-db`, `roster_db`, …). A new database, never new tables inside a platform database. Decided 2026-09-01 (D2-32; supersedes the 2026-08-29 same-database decision).
  - One row = **one obligation cycle**: identity, next attestation date, state — and **no copied OV data**: `certify_practitioner_id` is the only link to the practitioner; consumers read the OV live when they need provider fields.

- **Why its own database:**
  - D2-32: the module is its own service, so it owns its schema, migrations, and backup/restore scope end to end — no shared-database coordination, no cross-team DDL, ever.
  - Spanner bills the instance, not the database count — a sibling database on the same instance adds no infrastructure cost.
  - The one thing a shared database bought — a single transaction spanning our tables and the reminder rows — is replaced by the transactional outbox (step 5): everything module-owned still commits atomically, and the cross-service effect leaves as a published event that cannot be lost.
  - All access goes through the module's own data layer (v2's Attestation Module Data Layer). The DAL service and its `/batch` endpoint are not in this module's write path.

- **Why the dates live here and not on the OV row:**
  - The next attestation date and the task are one thing — a row *is* an obligation with a date and a state. Splitting them (date on OV, task here) forces every scan to stitch two reads together.
  - The OV is the golden record of *provider facts*. An attestation schedule is *our workflow state* — it stays behind the module's staging boundary.
  - No migration on `core_practitioners_ov` — the platform's most shared table — and no cross-team DDL coordination.

- **Why one table, not a tasks table plus a dates table:**
  - A "current schedule" table would duplicate what the newest task row already says.
  - `state = SCHEDULED` already *means* "future obligation, not yet opened" — the state machine carries the schedule for free.

**2. Backfill once (Stage 0).**

- **What:**
  - The **first, supervised run of the module's one reconcile job** (`POST /internal/attestation-cycles/reconcile` — the same endpoint the weekly check fires; one job, two callers, *Contracts → lifecycle step 0*), scoped to the population check and run per tenant: read the in-scope practitioner list through the **existing api-layer practitioner read endpoints** (paginated; OV data, read-only — this module never reads a platform database directly; the exact endpoint is pinned in doc 5), and insert **one `SCHEDULED` row per practitioner** into `attestation_tasks` in `attestation-db`.
  - Each row stores the **`certify_id`** (as `certify_practitioner_id`) — the permanent join key back to the OV, so the practitioner can always be correctly identified from our row at any later point (export, prefill, seeding checks).
  - Each row stores both dates: `next_attestation_date` and `last_attestation_date` (null at backfill; set on successor rows from the prior submission).
  - Next attestation date: `run_date + (hash(certify_id) mod staggerDays)` — on this first run, `run_date` **is** the go-live date; hashing spreads ~70k practitioners evenly (~780/day at 90 days).
    - **Why a hash, not random or a running counter:** deterministic within a run — every batch computes the same date for the same practitioner, so parallel batches need no shared counter; uniform — a cryptographic hash mod 90 puts ~70,000 / 90 ≈ 780 in each bucket, no hot days. Across runs the anchor moves with `run_date`, and that is fine: rows are insert-if-missing, so a practitioner's date is fixed forever by whichever run first inserts it.
    - **Worked example** (first run: run_date = go_live = 2026-09-01, staggerDays = 90): `hash(certify_id) mod 90 = 7` → 2026-09-08; `= 42` → 2026-10-13; `= 0` → 2026-09-01; `= 89` → 2026-11-29 (the last possible day).
    - The exact hash computation is pinned in *Contracts → lifecycle step 0* — it is part of the contract, not an implementation detail, because changing it moves every backfilled date.
  - Inserts only where no row exists → re-running is a no-op (its `RECONCILE_RUN_COMPLETED` simply reports zero inserts).
  - **In-scope = active practitioners only:** terminated practitioners are excluded at backfill (and at seeding); one who is later reinstated re-enters through the seeding check (D18).

- **Why:** without the stagger, all ~70k practitioners become due the same day — 70k emails at once and 70k submissions hitting review in the same week.

- **The dates stay here — no copy back to the primary database.** Product confirmed (2026-09-01) that no PDM-side filter on attestation dates is needed today, so nothing is synced to `data-production-b`. Parked as a future improvement: a date-sync event type on the same outbox, if a PDM list-view filter is ever wanted (D13).

- **What if it fails:** a crash mid-run is harmless — the re-run inserts only the missing rows.

- **Why not seed dates onto the OV or through the practitioner update endpoints:** the OV write path either fires the full merge pipeline per practitioner (70k pipeline runs) or needs a denorm-column migration on a shared table. Our own insert does neither.

- **Payloads:** *Contracts → lifecycle step 0*.

**3. Scan daily — one query, on our own table.**

- **What happens:**
  - Cloud Scheduler (GCP's managed cron service) calls `POST /internal/attestation-cycles/trigger` once a day.
  - It runs **one query on the module's own table**: `WHERE state = 'SCHEDULED' AND next_attestation_date <= today + leadDays` — every obligation whose window has arrived, however old.
  - Results are read in pages of ~500 (keyset pagination — "continue after this id", so memory stays flat).
  - For each page the endpoint creates **one Cloud Task** carrying that page (step 4).
  - Only then does it reply **202 with the run's counts** — the scan and the chunk creation finish inline (seconds); "accepted" refers to the real work (opening the tasks), which continues in the queue after the response. The scheduler never waits on that work.

- **Why yesterday's practitioners never reappear in the scan:**
  - The scan only ever selects `state = 'SCHEDULED'` rows.
  - When the chunk handler processes a practitioner, the same transaction flips the row to `OPEN` — the row leaves the scan's result set permanently (and the flip is audited).
  - So if today's run opens 500 obligations, tomorrow's query simply cannot see them — only the new entrants (say 5) match. No date window, no re-filtering, no memory needed: the state **is** the processed-marker.

- **Why there is no `lookbackDays` anymore:**
  - An unopened obligation just **stays `SCHEDULED` forever** until a scan opens it. A missed run delays by a day; nothing can fall off any edge — so there is no rear window to protect.

- **What if it fails:**
  - Scheduler call fails → the scheduler service retries; a fully missed day is covered by the next run (SCHEDULED rows wait).
  - Endpoint crashes after creating 2 of 5 chunk tasks → the 2 proceed; the rest stay SCHEDULED for tomorrow's scan.
  - One chunk-task creation fails → retried inline; "a task with this name already exists" counts as success.

- **Why Cloud Scheduler:** the platform's existing pattern for recurring jobs (AutoRecred, smart_outreach); managed, retried, zero new infrastructure. Alternatives (in-process timer, Temporal) analyzed and rejected — *Alternatives considered*.

- **Why not event-driven:** a calendar date arriving is not an event — nothing publishes a message when a day passes. A scheduled check is the only mechanism.

- **Payloads:** *Contracts → lifecycle steps 1–2*.

**4. Split the work: one Cloud Task per page of obligations.**

- **How it works:**
  - Each page of ~500 becomes one Cloud Task; 5,000 due obligations means 10 tasks, not 5,000.
  - Each task's **name** is deterministic: `(runDate, tenant, page number)` — Cloud Tasks refuses a second task with the same name, so a re-fired trigger cannot double-enqueue.
  - The queue delivers each task to `POST /internal/attestation-cycles/process-chunk` (step 5) and handles retries with increasing delays.
  - **How the "queue" actually works** (Cloud Tasks is a managed dispatcher, not a queue we read from): each created task is stored by the service, then *pushed* as an HTTP POST to our endpoint at the configured rate. Our `2xx` response marks the task done and it is deleted; a non-`2xx` or a timeout makes the service redeliver the same task later with growing delays. We never poll anything — the "queue" calls us.

- **Why chunks, not one task per practitioner:** the per-item work is tiny (a state flip and a few inserts). Chunks keep per-chunk retry isolation with ~10 tasks a day instead of ~780.

- **Chunk size is configuration, not architecture:** `chunkSize` is a module-level config value (default 500), not a hardcoded constant — the page size in the eligibility query and the task payload both read it. Setting it to 1 *is* the one-task-per-practitioner variant; nothing in the design assumes 500. We start at 500 because the per-item work is tiny and batching amortizes the HTTP/handler overhead; if operational experience ever favors finer granularity (for a tenant or for the whole module), it is a config change, not a redesign.

- **Why not inline in the trigger request:** works at steady state, fails on the guaranteed high-volume days (backfill day, large-tenant onboarding) — request timeouts and all-or-nothing retries.

- **Why Cloud Tasks, not Pub/Sub:**
  - Point-to-point work ("process this page once"), not an event broadcast.
  - **Name-based duplicate refusal at creation** — Pub/Sub has no equivalent; it is at-least-once by contract, so duplicates are normal and everything falls on the database.
  - **Pacing** — the queue dispatches at a configured rate instead of all at once.
  - Pub/Sub's one real advantage (a native dead-letter queue) buys nothing here: tomorrow's scan re-derives any dropped work.

- **What if a chunk fails:** Cloud Tasks retries automatically. If every retry fails, the task is silently deleted (Cloud Tasks has no dead-letter queue) — the handler writes `CHUNK_PROCESSING_FAILED` on its final attempt, and the obligations stay SCHEDULED for the next scan. Delay possible, loss impossible.

- **Payloads:** *Contracts → lifecycle step 3*.

**5. Open the tasks — one atomic transaction per group; outreach intent leaves through the outbox.**

- **What the chunk handler does:**
  - First, one batched OV read per chunk (through the existing api-layer endpoints) re-checks each practitioner is still **active**: a practitioner terminated since backfill gets no task and no outreach — the row is skipped (`TASK_OPEN_SKIPPED_TERMINATED`) and left `SCHEDULED` for the termination consumer to close (D18).
  - For each remaining practitioner in the chunk, in groups of ~50, one transaction on the module's data layer commits **atomically** in `attestation-db`:
    1. the task row's state flip `SCHEDULED → OPEN`,
    2. the audit rows (`TASK_OPENED`, `OUTREACH_SCHEDULED`),
    3. **one `attestation_outbox` row** (type `OUTREACH_SCHEDULE`) carrying a `SCHEDULE_SENDS` command for the Smart Outreach Service: three sends (`T-30`, `T-7`, `OVERDUE`), each with an absolute `scheduledAt`, an `expiresAt`, a template key (`attestation.reminder.t30` / `.t7` / `.overdue`), the recipient ids, and the variables, all under `cancellationKey = attestation-task:<taskId>`.
  - Everything module-owned commits or fails together. The send rows themselves live in the Smart Outreach Service's own database (`outreach-db`) and are written by that service when it consumes the command — next bullets.
  - Groups of ~50 keep each transaction well inside Spanner's per-transaction mutation limit.

- **How the reminder command reaches the Smart Outreach Service — publish, don't write:**
  - **Post-commit dispatch:** right after the transaction commits, the handler publishes each new outbox row as a command to the Smart Outreach Service's topic **`outreach.commands.v1`** (ordering key = the command's `cancellationKey`), then marks the row `PUBLISHED` (`published_at` stamped) — so the sweep below never re-picks a delivered row.
  - **Relay sweep (the safety net):** a Cloud Scheduler job calls the backend's relay endpoint (path and contract pinned in doc 5) on a short cadence; it publishes any row still `PENDING` older than a grace age, then marks it. A dispatch that failed (process crash after commit, Pub/Sub blip) is caught here — the outbox row is durable proof of unfinished intent.
  - **The Smart Outreach Service consumes the command:** it deduplicates on `commandId` (its inbox table), checks the recipient ids exist in its registry, creates three send rows (`UNIQUE (producer, sendKey)` — a duplicate command creates nothing twice), and publishes a `SCHEDULED` outcome per send. It processes first and acknowledges after (its design doc, step 2).
  - **Why publish a command instead of writing reminder rows ourselves:** the module publishes *intent* and owns nothing on the other side — no knowledge of the outreach tables, no write access to another database, no coupling to how outreach stores or sends its work. The owning service consumes the command and writes its own tables (the D2-32 boundary). Pub/Sub adds redelivery, ordering, and a dead-letter queue for free.
  - **Why not publish straight from the handler, without an outbox:** a database commit and a publish can never be atomic — commit-then-publish can crash in between and lose the intent silently. The outbox row commits *with* the flip, so the intent survives any crash; publishing becomes retryable bookkeeping.

- **Why it is safe to run twice ("idempotent" — running it twice produces the effect once):**
  - The state flip is the guard: the handler builds the transaction only for rows still `SCHEDULED`.
  - A redelivered chunk finds the rows already `OPEN` → skips them (`duplicateNoOps`) → cannot write a second outbox row.
    - **How, concretely:** the handler's first action per practitioner is reading its row's `state`. `SCHEDULED` → it builds that practitioner's transaction (flip + audit + outbox row). `OPEN` → it builds **nothing** and counts a `duplicateNoOp`.
    - The outbox row is only ever *inside* a flip's transaction — no code path writes outreach intent alone.
  - The remaining window — a redelivery racing a still-running original, both reading `SCHEDULED` — closes under Spanner's serializable read-write transactions: both cannot commit the same flip; the loser aborts, retries, sees `OPEN`, and skips.
  - Underneath, the unique index `(tenant_id, certify_practitioner_id, due_period)` makes a duplicate obligation row impossible.

- **If published but never consumed — three layers, no permanent loss:**
  1. **Pub/Sub redelivers until acked.** The Smart Outreach Service acks only after its writes commit; if it is down the messages wait — its subscription retention is ≥ 7 days.
  2. **Dead-letter topic.** A command that fails every delivery attempt (malformed payload, unknown recipient id on every send) moves to the outreach service's dead-letter topic after a bounded attempt count; its DLQ depth alerts; replay is an audited operator action. A per-send rejection (one unknown recipient id) comes back to us as a `SCHEDULE_REJECTED` outcome instead — visible, not lost.
  3. **End-to-end audit evidence.** Intent and confirmation are both audit events (`OUTREACH_SCHEDULED` vs `OUTREACH_SCHEDULE_CONFIRMED`), so "did every command land?" is one on-demand report; a gap found there is repaired by re-emitting the outbox row — an audited ops action, safe by construction (the outreach service's `commandId` dedup and unique send key absorb duplicates). Deliberately a report, not a scheduled job (D19): the live failure modes already page through the outbox-age, DLQ, and `schedule_rejected` alerts.

- **How long Cloud Tasks waits for the 200:** dispatch deadline is 10 minutes by default (configurable 15s–30m); a chunk finishes in seconds.

- **Why the audit rows stay separate from the task row:**
  - The task row holds *current state*, updated in place; the audit trail holds *history*, append-only.
  - Append-only rows are the only shape satisfying "never updated, never deleted, queryable for seven years" (D2-20).

- **What if the reminder writes fail but the task flip succeeds?** Transiently possible, never permanent. The flip and the outbox row are one transaction, so the intent cannot be lost: a failed dispatch is republished by the sweep, a failed write in the outreach service is redelivered by Pub/Sub, and anything that slips past both is provable from the intent-vs-confirmation audit (the consistency report) and re-emitted by ops. A task open without its ladder is a delay measured in minutes — never a silent miss.

- **What if it fails:** a failed group's transaction rolls back whole — nothing committed for those ~50; Cloud Tasks retries the chunk; rows from groups that did commit are now `OPEN` with their outbox rows written, so the state guard skips them and only the failed group is reprocessed. Retries exhausted → `CHUNK_PROCESSING_FAILED` + the rows stay SCHEDULED for tomorrow.

- **Poison-row mitigation:** one deterministically bad row (corrupt data, constraint violation) would otherwise fail its group of ~50 on every retry, delaying 49 healthy rows alongside it. On the final retry attempt (detected via the `X-CloudTasks-TaskRetryCount` header), the handler switches from group transactions to per-practitioner transactions: healthy rows commit individually, and only the poison row is recorded (`CHUNK_PROCESSING_FAILED` with the practitioner id) and left `SCHEDULED` for investigation. Blast radius of a bad row: itself, not its group.

- **Payloads:** *Contracts → lifecycle steps 4–4b*.

**6. Send the reminders — the Smart Outreach Service does it; this module only commands and listens.**

- **How it executes (in the Smart Outreach Service, its design doc steps 3–9):**
  - Our `SCHEDULE_SENDS` becomes three send rows in `outreach-db`, each due at the absolute `scheduledAt` we sent. A one-minute sweep sends what is due: it resolves each `recipientId` to the **current** address in its registry, renders the attestation template (platform default or tenant override), sends via SendGrid, and publishes a `SENT` outcome; delivery, bounce, and complaint events arrive as `DELIVERED` / `BOUNCED` / `COMPLAINED` outcomes.
  - The service knows nothing about attestation: it does not resolve our task id, does not check whether the task is still `OPEN`, and computes no dates. It sends what we scheduled unless we cancel it or the send passes its `expiresAt`.

- **What this module must do for that to work — two producer duties (doc 5 owns the wiring) and one dependency:**
  1. **Register the templates.** `attestation.reminder.t30`, `.t7`, `.overdue` with variables schema `{ nextAttestationDate, portalLink }` and category `compliance-reminder` (mandatory — practitioners cannot opt out). The recipient's name comes from the registry, never from our payload.
  2. **Consume outcomes.** A subscription on `outreach.outcomes.v1` filtered `attributes.producer = "attestation-module"`; the consumer deduplicates on `outcomeId`, processes first and acks after, and writes **one audit event per outcome** — `OUTREACH_SCHEDULE_CONFIRMED`, `OUTREACH_SENT`, `OUTREACH_DELIVERED`, `OUTREACH_BOUNCED`, `OUTREACH_FAILED`, `OUTREACH_SUPPRESSED`, `OUTREACH_EXPIRED`, `OUTREACH_CANCEL_CONFIRMED`, `OUTREACH_CANCEL_TOO_LATE` (full mapping in *Contracts → lifecycle step 5*) — into `attestation_audit_events` keyed by the task id in `correlationId` and the tier in `sendKey`.
  - **Dependency — the recipient registry is the PDM backend's (D14):** `api-layer`, as registered producer `pdm-platform`, publishes one `UPSERT_RECIPIENT` per active practitioner (`recipientId = practitioner:<certify_id>`, current email, display name) — initial load before our backfill, then on every contact change and on termination (`status: INACTIVE`). This module never syncs contact records; a send naming an unsynced practitioner comes back as `SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)`, audited and alerted.

- **The reminder lifecycle:**
  - Commanded in step 5's transaction (the `OUTREACH_SCHEDULE` outbox row) — the default ladder; the offsets come from the task row's `reminder_offsets`, stamped from the tenant's `reminderOffsets` config at row insert (*Data model — Configuration*), and are turned into absolute timestamps **here**, before publishing. The outreach service never sees an offset.
  - **Late-opening rule** — what happens when an obligation is opened after some reminder dates already passed (an outage, or a practitioner seeded late):
    - The handler computes each tier's send date from the next attestation date, then **drops every tier whose date is already in the past** — a reminder for a moment that already went by is noise, not information.
    - It adds **one immediate kickoff** email (send now) so the practitioner still gets a first notice.
    - Example: next attestation date Oct 15, opened Oct 5 → T-30 (Sep 15) is past → dropped; ladder becomes: kickoff today + T-7 (Oct 8) + OVERDUE (Oct 16).
    - Result: never a burst of stale reminders, and never a silent obligation with zero emails. This is our rule, applied on our side — the outreach service would send a past-dated row at its next sweep.
  - **`expiresAt` on every send** — our safety net if a cancel is ever lost: `T-30` and `T-7` expire at the due date + 1 day (an overdue task gets the overdue email, not a stale "7 days left"); `OVERDUE` expires 30 days after the due date; a `KICKOFF` send expires 7 days after its own `scheduledAt` (an immediate notice needs only a short bound, and it may be born after the due date, where the pre-due rule cannot apply). Past `expiresAt` the outreach service marks the send `EXPIRED` and tells us.
  - On submission: the submission transaction writes an `OUTREACH_CANCEL` outbox row → `CANCEL_SENDS` on `cancellationKey = attestation-task:<taskId>`; the outreach service flips remaining pending sends to `CANCELLED` and reports each as `CANCELLED`, or `CANCEL_TOO_LATE` for a tier no longer pending (already claimed, sent, failed, or expired — `currentStatus` on the outcome says which). Audited here as `OUTREACH_CANCELLED` (the intent) and per outcome. Ordering key = `cancellationKey`, so the cancel can never overtake the schedule.
  - **The race, stated plainly:** a tier claimed by the outreach sweep in the same minute the cancel lands goes out — one extra email, worst case, reported as `CANCEL_TOO_LATE`. There is no send-time "is the task still OPEN?" check anywhere: that check would require the outreach service to know what an attestation task is (its design doc, D4).

- **What if a send fails:** the outreach service retries transient provider errors (five attempts, backoff) and reports `FAILED` only when exhausted or permanent. Blast radius is one tier; the 5% failure alert fires on our side from the outcomes, and the task stays visible in the Portal regardless.

- **Why not the legacy `smart_outreach` engine in `api-layer`:** its send path resolves every row as a credentialing workflow and applies 14 credentialing skip rules; serving us meant a per-producer branch inside the monolith, a shared table with no unique key, and no outcome events. Analyzed and rejected in the Smart Outreach Service design doc.

- **Why not Pub/Sub for the reminders themselves:** reminders are time-scheduled future work — there is no event to react to at T-7. The outreach service's table-as-queue handles the time; Pub/Sub carries only the command and the outcomes.

- **Why not per-email Cloud Tasks with a future `scheduleTime`:** Cloud Tasks caps future scheduling at 30 days — the OVERDUE row (due date + 1 day) is born ~31 days early. The outreach service's table is horizonless and cancellable by key.

- **Payloads:** *Contracts → lifecycle steps 4b–6*.

**What the approach deliberately does NOT do:**

- No writes to the OV or any core practitioner table — the module reads them (through the existing api-layer endpoints), never writes.
- No direct writes to any other service's database — cross-service effects leave only as published commands on the Smart Outreach Service's topic (D12).
- No scheduled batch release of anything (D2-01 lives downstream).
- No per-practitioner queue tasks; no email sending of its own.
- New infrastructure kept minimal: three Cloud Scheduler jobs (daily scan, relay sweep, weekly reconcile), one Cloud Tasks queue, publish rights on the outreach command topic and one filtered outcome subscription (DevOps request).
- **No clock advancing.** The successor obligation after a submission is inserted by the submission flow (docs 2/5).

### Key decisions

| #   | Question                                | Resolution                                                                                                            |
| --- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| D1  | Per-practitioner tasks vs inline vs chunks | One Cloud Task per chunk — `chunkSize` configurable (default 500, per-practitioner = chunkSize 1); the inline-batch variant analyzed and rejected; chunks close its timeout/retry holes |
| D2  | `due_period` definition                 | The obligation's next attestation date, ISO `YYYY-MM-DD` — **confirmed 2026-09-08 (Q2 closed)**: the date-based definition keeps every obligation naturally unique and avoids period-label collision edge cases |
| D3  | Clock anchor                            | **Submission-anchored — confirmed by Product 2026-09-01** (closes v2 O-11, Q6): the clock advances from the submission date, and a later rejection does not reset it |
| D4  | Where the dates live                    | **In the module's own table, not on the OV** (2026-08-29) — the OV-columns design analyzed and rejected    |
| D5  | Late task opening                       | Ladder built from remaining tiers + one immediate kickoff                                                             |
| D6  | Missed-run safety                       | Structural: unopened obligations stay `SCHEDULED` until a scan opens them — no lookback window needed                 |
| D7  | Cycle vs task tables                    | One `attestation_tasks` table — "cycle" is a concept, not a table; one row per obligation                             |
| D8  | Database placement                      | **Own `attestation-db` database on the app-data Spanner instance** (D2-32, 2026-09-01 — supersedes the 2026-08-29 same-database decision; that design analyzed and rejected in *Alternatives*) |
| D9  | OVERDUE                                 | **Derived, never stored** (2026-08-30): `state = OPEN AND next_attestation_date < today`, computed server-side — stored states are `SCHEDULED → OPEN → SUBMITTED` (plus terminal `CLOSED`, D18), every transition action-owned |
| D10 | OV data on task rows                    | **None** (2026-08-30): no cached `npi`/name — `certify_practitioner_id` is the only practitioner link; consumers read the OV live |
| D11 | Cross-database effects                  | **Transactional outbox** (2026-09-01): one `attestation_outbox` table for the whole module; post-commit dispatch to Pub/Sub + Cloud Scheduler relay sweep as the safety net |
| D12 | Outreach handoff                        | **Commands to the Smart Outreach Service** (2026-09-07, supersedes 2026-09-01): `SCHEDULE_SENDS` / `CANCEL_SENDS` on `outreach.commands.v1`, ordering key `cancellationKey = attestation-task:<taskId>`; the service owns templates, registry, sends, and SendGrid; this module never touches its tables. The earlier api-layer consumer + engine branch is withdrawn |
| D13 | Attestation dates in the primary DB     | **Not synced in MVP** (Product, 2026-09-01 — no PDM-side filter wanted); parked future improvement: a date-sync event type on the same outbox |
| D14 | Recipient registry sync                 | **Owned by the PDM backend (`api-layer`) from day one** (revised 2026-09-07): `practitioner:` contact records are synced by the provider-data platform as its own registered producer (`pdm-platform`) — initial load before this module's backfill, then on every contact change (it owns the practitioner change path). This module is a **sender only** and owns no recipient namespace; recipient-level outcomes (`RECIPIENT_ADDRESS_FAILED`, `RECIPIENT_UNSUBSCRIBED`) route to `pdm-platform`, while this module sees the per-send effects (`SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)`, `SUPPRESSED`) on its own outcomes. An `org-contact:` namespace is **deferred** — no v1 send targets one; the outreach service extends without contract change when an owner registers it. Cross-team dependency: the sync is a new `api-layer` deliverable (needs PDM team sign-off). Sends name people by id, never by address |
| D15 | Outcome consumption                     | **Outcome consumer in the backend (doc 5)** (2026-09-07): subscribes to `outreach.outcomes.v1` filtered on `producer = attestation-module`; writes one audit event per outcome (mapping in *Contracts → lifecycle step 5*), never by reading outreach tables |
| D16 | Send-time relevance check               | **None** (2026-09-07): no "task still OPEN?" check in the outreach service; cancellation + `expiresAt` bound stale sends; one extra email is the accepted worst case |
| D17 | Email deep link                         | **Tokenless stable portal entry URL** (2026-09-07): `portalLink = <portalBaseUrl (tenant config)>/attest/<taskId>`; the path is a fixed entry-point contract owned by the portal stack (free to redirect internally); no token is minted here — authentication happens at the portal. This module knows nothing of portal routing beyond the entry URL |
| D18 | Terminated practitioners                | **Three layers** (2026-09-07): excluded at backfill and seeding; re-checked at task opening (skip + `TASK_OPEN_SKIPPED_TERMINATED`, row stays `SCHEDULED`); a PDM practitioner-terminated event (dependency — does not exist today, ask to the PDM platform team) closes rows to the terminal `CLOSED` state and cancels outreach; the weekly reconcile's `terminated` check is the net until that event exists |
| D19 | Backfill vs weekly check                | **One reconcile endpoint, two checks only** (2026-09-07): backfill = the supervised first run (per tenant, `checks: ["population"]`, dry-run first); the weekly check = Cloud Scheduler firing the same endpoint with both checks (`population`, `terminated`); detection inline, remediation via Cloud Tasks chunks; one date formula, anchored to the inserting run's date. Other consistency questions (aged unopened, reminder consistency, unresolved recipients) stay **on-demand audit reports** — real-time alerts cover the live failures; a report proving recurring value is promoted to a check later |

## Alternatives considered

Each alternative in full: what it is, its genuine strengths, its costs, and the direct reason it lost to the chosen approach.

### OV denorm date columns (the earlier draft of this design)

- **What it is:**
  - Add `next_attestation_date` / `last_attestation_date` columns to `core_practitioners_ov` — the pattern credentialing uses (changeset 012).
  - The daily scan reads the OV by date window; tasks live in a separate table; a second query filters out practitioners with open tasks.

- **Pros:**
  - Exact production precedent — credentialing's denorm columns feed AutoRecred today.
  - The merge pipeline provably cannot clobber app-owned columns (survivorship's OV write is column-scoped).
  - Dates visible to anything that already reads the OV.

- **Cons:**
  - Requires a migration on the platform's most shared table, with cross-team DDL coordination.
  - Permanent two-place stitch: dates on the OV, tasks in our table — every scan is two queries plus an in-memory filter.
  - Workflow state leaks onto the golden-record surface every consumer and egress feed reads.
  - Clock advances become OV writes — an extra write path to keep consistent.

- **Why rejected:** the module-owned table gives the identical scan with **one** query, zero shared-table changes, and keeps workflow state behind the module's boundary (task + audit + outbox commit in one transaction). The OV design's only unique advantage — dates visible to other OV readers — serves no consumer we have (Product confirmed 2026-09-01: no PDM-side filter wanted).

- **Cost:** same as the chosen approach (≈ $5–10/mo).

### Same-database placement (the superseded 2026-08-29 design)

- **What it is:**
  - `attestation_tasks` and the audit table live as new tables inside the platform's primary database, alongside `core_practitioners_ov` and `scheduled_outreaches`; writes go through the DAL's transactional `/batch` endpoint.
  - Task flip, audit rows, and the three reminder rows commit in **one** transaction — no outbox, no relay, no consumer.

- **Pros:**
  - Single-transaction task opening across our tables *and* the legacy engine's `scheduled_outreaches` — the strongest possible consistency; a task open without its ladder is impossible by construction, not just eventually repaired.
  - Rides existing rails: DAL repositories, Liquibase migrations, `/batch` — the least new code of any option.

- **Cons:**
  - Contradicts D2-32: the module's state lives inside the monolith's database — schema, migrations, backups, and every DDL change coordinate with the platform team forever.
  - The module's "database boundary" becomes a naming convention (`attestation_*` prefixes), not a real one; extracting it later is a data migration under load.
  - Couples the module's write path to the DAL service's availability and release cadence.
  - Workflow tables grow inside the platform's most critical database — attestation volume and retention (7-year audit) inflate a database other teams operate.

- **Why rejected:** D2-32 (2026-09-01) decided the service boundary, and a module-owned database is the storage half of that decision. The one real loss — the cross-table single transaction — is answered by the transactional outbox: everything module-owned still commits atomically, and the reminder intent survives every crash by construction (delay in minutes, never loss).

- **Cost:** infrastructure same as the chosen approach (Spanner bills the instance, not the database count); the saved outbox/relay/consumer code is the option's only real economy, paid for with permanent shared-database coupling.

### AWS equivalent (EventBridge Scheduler → SQS → Lambda → SES)

- **What it is:**
  - EventBridge Scheduler (AWS's managed cron) fires a Lambda trigger daily.
  - The trigger runs the same scan (calling back into GCP for the data), fans work into SQS messages, Lambda consumers process them, SES sends the emails.

- **Pros:**
  - First-class managed equivalents for every part; SQS+Lambda retry and dead-letter semantics are excellent.
  - SES is the cheapest email at our volume (~$7/mo vs a possible ~$90/mo SendGrid tier step).

- **Cons:**
  - The compliance clock lives in a second cloud: identity federation, network/egress design, a second bill, a **second on-call rotation**.
  - The data stays in Spanner (GCP), so every worker call crosses clouds — and task writes + audit rows can no longer commit in one transaction.
  - Zero AWS operational muscle on this platform.

- **Why rejected:** it breaks the same-transaction audit rule (a non-negotiable) and adds a whole cloud of operational surface — to save roughly $80/month on email.

- **Cost:** ≈ $40–70/mo (SES ~$7, cross-cloud NAT/VPN/secrets/monitoring $30–60) plus the un-dollared second on-call.

### In-process timer (Quarkus `@Scheduled` + a database lock)

- **What it is:**
  - A `@Scheduled(cron = "…")` method inside our own backend fires once a day — a cron job living in the application code instead of an external scheduler.
  - Because the service runs as several copies (Cloud Run instances), a database lock elects **one** copy to do the work; that copy runs the scan and processes obligations in a loop.

- **Pros:**
  - **Zero new infrastructure** — no Scheduler job, no queue, no DevOps request, $0.
  - Simplest mental model: "a cron method in our own code."
  - Same database, so the same-transaction audit rule holds natively.

- **Cons:**
  - **Every instance fires the timer.** The service runs as several identical copies, and the copies share nothing — they do not know about each other. Five instances = five timers firing at the same moment = the same scan running five times, unless we build coordination ourselves.
  - **The only coordination is a hand-built database lock (leader election):** every instance races to grab a lock row; the winner works, the losers skip. We then own the failure cases — the winner dying mid-run while holding the lock (the job never runs again until the lock expires), a slow run overlapping the next day's election, clock differences between instances. Cloud Scheduler makes this whole problem not exist: one external trigger, one call.
  - **It only fires while the service is actually running and healthy.** Cloud Run scales to zero, restarts on every deploy, and a process can be alive but stuck (a deadlock or exhausted thread pool — health checks pass, the timer thread never runs). In all three cases the timer silently does not fire.
  - **No signal when it does not fire — the alerting asymmetry:** Cloud Scheduler is *external* — it fires even when our service is down, the call then fails, and that failure is a recorded, visible event (run history, retries, Cloud Monitoring alerts on failed executions). An in-process timer inside a dead service never fires *at all* — no log, no error, no event; silence is indistinguishable from success. The only detection left is absence-based (our 26-hour liveness alert), which by definition fires a day late.
  - **Hand-builds everything else the managed pair gives free:** retries with backoff, pacing, resume-after-crash, stuck-work visibility.
  - **Worst failure mode of any option:** a silently dead scheduler on a compliance clock — days pass, nothing pages, a human eventually notices.
  - The platform already decided against this idiom: exactly **one** `@Scheduled` exists in the entire workspace (a cache refresh); every per-entity job uses the external pattern.

- **Why rejected:** it fails the single highest-weighted criterion — trigger reliability — in the worst possible way (silent failure), and reverses an existing platform-wide decision. Its $0 price is paid in correctness risk, the expensive currency.

- **Cost:** $0 infrastructure + the engineering time to build and maintain lock/retry/resume/visibility + the un-priced risk of a silent miss.

### Temporal (durable per-practitioner workflow timers)

- **What it is:**
  - A workflow engine where each obligation is a long-running program that *sleeps* until T-30, T-7, OVERDUE, waking to send each reminder.
  - Temporal persists timer state, so workflows survive restarts and deploys.

- **Pros:**
  - Purpose-built for "remind, then wait": durable timers, automatic retries, full execution history, first-class cancellation.
  - Effortless at 10× and beyond.

- **Cons:**
  - A **new runtime, a persistence cluster, and a new programming model** for a team with zero Temporal footprint.
  - Bought to solve a daily query and three reminders — work the incumbent pattern already solves.
  - A cluster outage stalls every timer; upgrades add operational process.
  - Audit rows would need side-writes into our table — the same-transaction rule is not native.

- **Why rejected:** massive new operational surface (criteria: ops burden, team familiarity) for capability we do not need. Revisit only if the platform adopts Temporal broadly.

- **Cost:** ≈ $200–600/mo (Temporal Cloud tiers start ≈ $200; self-hosted = 3-node cluster + database ≈ $300–600 plus real on-call).

### Inline batch in the trigger request

- **What it is:**
  - One endpoint runs the scan and processes **every** due obligation inline, in that same HTTP request — no queue at all.

- **Pros:**
  - Simplest possible shape; genuinely works at steady state (~780/day finishes in seconds).
  - Same unique-index idempotency; its first half *is* the chosen approach's first half — the designs differ only after the query returns.

- **Cons:**
  - **Timeouts on guaranteed high-volume days:** backfill day and large-tenant onboarding exceed the request time limits — and those days are certainties, not tail risks.
  - **All-or-nothing retries:** one poison item (an item failing identically every attempt) means the whole batch never succeeds — unless we hand-write catch-and-continue, per-item error records, and retry-only-failures, i.e. re-implement the queue.
  - No pacing; a batch dead at item 400 is one opaque failed run.

- **Why rejected:** correct at steady state, unsafe on days that are guaranteed to happen. The chosen approach is this idea made safe: same scan, with the processing handed to a queue that retries per chunk.

- **Cost:** ≈ $0–5/mo — cost was never the differentiator.

## Contracts and interfaces

**Where the endpoints live:** the **Attestation Module Backend** (doc 5) — internal-only, shared-secret header auth (`X-Attestation-Cycle-Key`, validated fail-closed like `SmartOutreachSchedulerResource.java:140-150`). Behavior clones the verified patterns (`AutoRecredResource.java:79-110`, the `:119` callback).

### The lifecycle, end to end — every call with its request and response

> The full chain in execution order with live payload shapes: scheduler → scan → chunks → task opening → reminders → cancellation. Field names are the contract for docs 5–6; DTO classes are doc 5's. Rationale per step: *Approach* (0→step 2, 1–2→step 3, 3→step 4, 4→step 5, 5–6→step 6).

**Step 0 — reconcile: one endpoint, two callers (the supervised backfill run and the weekly check).**

- `POST /internal/attestation-cycles/reconcile` — header `X-Attestation-Cycle-Key`.
- **One job, two invocations — the underlying logic is identical** ("make the module's state agree with the OV"), so backfill and the weekly check share this endpoint:
  - **Backfill** = the first, supervised run: per tenant, `checks: ["population"]`, dry-run → report → execute.
  - **Weekly check** = Cloud Scheduler firing the same endpoint unattended: all tenants, both checks, no dry-run.
  - Either way the endpoint is a **detector**: checks run as queries inline, every remediation batch leaves as Cloud Tasks chunks (*Observability → weekly reconcile*).

Request:

```json
{
  "tenantId": "org-xyz",
  "staggerDays": 90,
  "dryRun": true,
  "checks": ["population"]
}
```

- `staggerDays` — spread next attestation dates across N days so 70k practitioners don't fall due together.
- `dryRun` — compute and report, write nothing.
- `checks` — which checks to run: `population` (seed missing rows — the backfill and the weekly seeding are this same check) | `terminated` (close the obligations of terminated practitioners); omitted = both (the scheduler's mode). `tenantId: null` = all tenants.
- **Deliberately only two checks (D19):** other consistency questions — aged unopened rows, unconfirmed reminder commands, unresolved recipients — are answerable on demand from the audit table and are already covered live by the real-time alerts. They are reports, not day-one automation; a report that proves recurring value can be promoted to a check later (*Observability*).

Stagger computation (pinned — changing it moves every newly seeded date):

```
offset_days = first 4 bytes of SHA-256(certify_id), read as an unsigned
              big-endian integer, mod staggerDays        // 0 .. staggerDays-1
next_attestation_date = run_date + offset_days
    // run_date = the run that first inserts the row:
    // go-live backfill → the go-live date; weekly seeding → that week's run date
```

- **SHA-256, not a language hash:** Java `String.hashCode()` distributes poorly on ids sharing a prefix and differs across languages; SHA-256 is uniform and identical everywhere it might be recomputed (backfill job, seeding check, an ops replay).
- Input is the raw `certify_id` string, no normalization. The function is fixed for the life of the table.

Response `200`:

```json
{
  "runId": "rec-2026-09-01-org-xyz",
  "tenantId": "org-xyz",
  "practitionersInScope": 70432,
  "rowsCreated": 70410,
  "skippedExisting": 22,
  "dryRun": true
}
```

- Inserts one `SCHEDULED` row per practitioner (identity + next attestation date — no OV fields copied); skips practitioners that already have any row.
- **Audit written in this step:** `RECONCILE_RUN_STARTED` at start (detail: `trigger` `MANUAL` \| `SCHEDULED`, `checks[]`, `dryRun`); one `OBLIGATION_SCHEDULED` per row inserted (inside its chunk's transaction); `RECONCILE_RUN_COMPLETED` with the per-check counts (a re-run that found nothing completes with zero inserts — no separate no-op event). All stamped with `runId` — the id in the response above.

**Step 1 — Cloud Scheduler fires the daily scan.**

- `POST /internal/attestation-cycles/trigger` — header `X-Attestation-Cycle-Key`, `@TenantAgnostic`.

Request (empty for the normal run; overrides for replays):

```json
{
  "runDate": "2026-09-15",
  "tenantId": null
}
```

Response `202`:

```json
{
  "runId": "cyc-2026-09-15-a1b2",
  "runDate": "2026-09-15",
  "tenantsScanned": 12,
  "dueFound": 3120,
  "chunksEnqueued": 7,
  "chunkSize": 500
}
```

- `401` bad key; `5xx` → Cloud Scheduler retries — harmless, the run is idempotent end to end.
- **Audit written in this step:** `CYCLE_SCAN_STARTED` when the scan begins; `CYCLE_SCAN_COMPLETED` with window bound, due found, chunks enqueued. Both stamped with `runId`.
- **What the `runId` is for:** not just a response field — it is stamped on both scan events, embedded in every chunk task's name and body, and carried onto every audit event the run's chunks produce. Anyone holding a `runId` gets the run's complete story with one audit query.

**Step 2 — the eligibility query (backend → its own table; not an HTTP endpoint).**

```sql
SELECT id, certify_practitioner_id, next_attestation_date
FROM attestation_tasks
WHERE tenant_id = @t
  AND state = 'SCHEDULED'
  AND next_attestation_date <= @windowEnd      -- today + leadDays
ORDER BY id
LIMIT @chunkSize                  -- configurable, default 500; keyset pagination: next page continues after the last id
```

- One query — date and state live on the same row. No cross-table filter, no cross-database stitch.
- Backed by an index on `(tenant_id, state, next_attestation_date)` (doc 6).

**Step 3 — one Cloud Task per page.**

- Name: `attest-2026-09-15-org-xyz-0003` — the queue refuses a duplicate name.

Body:

```json
{
  "runId": "cyc-2026-09-15-a1b2",
  "runDate": "2026-09-15",
  "tenantId": "org-xyz",
  "chunkNumber": 3,
  "tasks": [
    { "taskId": "task-7f3a…", "certifyPractitionerId": "cert-000123", "nextAttestationDate": "2026-10-15" },
    { "taskId": "task-7f3b…", "certifyPractitionerId": "cert-000124", "nextAttestationDate": "2026-10-16" }
  ]
}
```

**Step 4 — Cloud Tasks delivers the chunk.**

- `POST /internal/attestation-cycles/process-chunk` — shared-secret header; request = the body above.

Response `200` (only after all groups commit):

```json
{
  "processed": 500,
  "opened": 483,
  "duplicateNoOps": 17,
  "remindersWritten": 1449,
  "failed": [
    { "certifyPractitionerId": "cert-000999", "error": "row not found" }
  ]
}
```

- `duplicateNoOps` — rows already `OPEN` (a redelivered chunk); expected on retries, never an error.
- One bad item never fails the chunk; a chunk-level crash returns `5xx` and the queue retries.

**Step 4a — the task row (shape after opening).**

```json
{
  "id": "task-7f3a…",
  "tenantId": "org-xyz",
  "certifyPractitionerId": "cert-000123",
  "duePeriod": "2026-10-15",
  "nextAttestationDate": "2026-10-15",
  "lastAttestationDate": null,
  "state": "OPEN",
  "source": "BACKFILL",
  "reminderOffsets": [-30, -7, 1],
  "createdAt": "2026-09-01T06:00:03Z",
  "openedAt": "2026-09-15T06:00:03Z"
}
```

- `duePeriod` — the identity's third leg; `UNIQUE (tenant_id, certify_practitioner_id, due_period)` makes duplicate obligations impossible.
- `source` — how the row was born: `BACKFILL` / `SUBMISSION_ADVANCE` / `SEEDED` (the weekly seeding check, for practitioners onboarded after the backfill; the scan never creates rows — it only opens them), so reporting can tell the origins apart.
- `certifyPractitionerId` **is the `certify_id`** — the permanent OV join key, stored at backfill so the practitioner is always identifiable from our row alone.
- `lastAttestationDate` — null on backfill rows; on successor rows, the predecessor's submission date (so "last attested on X" reads from one row).
- **No OV data on the row** — no cached `npi`, no cached name. A copy goes stale the moment the OV changes; `certifyPractitionerId` is the only link, and every consumer (portal display, export, prefill) reads the OV live at use time. Point-in-time *evidence* (values recorded at submission) is different and is stored where it happens (doc 2).

**Step 4b — the atomic opening transaction (per group of ~50) and the outbox dispatch.**

One read-write transaction in `attestation-db`, through the module's own data layer — state flips + audit rows + outbox rows commit or fail **together**. Shown for one practitioner:

```json
{
  "transaction": [
    { "table": "attestation_tasks", "operation": "UPDATE",
      "row": { "id": "task-7f3a…", "state": "OPEN", "openedAt": "2026-09-15T06:00:03Z" } },
    { "table": "attestation_audit_events", "operation": "INSERT",
      "row": { "type": "TASK_OPENED", "taskId": "task-7f3a…", "tenantId": "org-xyz" } },
    { "table": "attestation_audit_events", "operation": "INSERT",
      "row": { "type": "OUTREACH_SCHEDULED", "taskId": "task-7f3a…", "tenantId": "org-xyz" } },
    { "table": "attestation_outbox", "operation": "INSERT",
      "row": {
        "id": "obx-55aa…",
        "eventType": "OUTREACH_SCHEDULE",
        "orderingKey": "attestation-task:task-7f3a…",
        "aggregateId": "task-7f3a…",
        "tenantId": "org-xyz",
        "status": "PENDING",
        "payload": {
          "cancellationKey": "attestation-task:task-7f3a…",
          "category": "compliance-reminder",
          "sends": [
            { "sendKey": "attestation-task:task-7f3a…:T-30",    "templateKey": "attestation.reminder.t30",
              "scheduledAt": "2026-09-15T13:00:00Z", "expiresAt": "2026-10-16T00:00:00Z",
              "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
              "variables": { "nextAttestationDate": "2026-10-15", "portalLink": "https://…/attest/task-7f3a…" } },
            { "sendKey": "attestation-task:task-7f3a…:T-7",     "templateKey": "attestation.reminder.t7",
              "scheduledAt": "2026-10-08T13:00:00Z", "expiresAt": "2026-10-16T00:00:00Z",
              "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
              "variables": { "nextAttestationDate": "2026-10-15", "portalLink": "https://…/attest/task-7f3a…" } },
            { "sendKey": "attestation-task:task-7f3a…:OVERDUE", "templateKey": "attestation.reminder.overdue",
              "scheduledAt": "2026-10-16T13:00:00Z", "expiresAt": "2026-11-14T00:00:00Z",
              "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
              "variables": { "nextAttestationDate": "2026-10-15", "portalLink": "https://…/attest/task-7f3a…" } }
          ]
        }
      } }
  ]
}
```

Post-commit, the handler publishes each new outbox row to the Smart Outreach Service's command topic `outreach.commands.v1` and marks it `PUBLISHED` (`published_at` stamped); the relay sweep publishes anything a crash left `PENDING`. The published command (the outreach service's envelope — its design doc, *Contracts*):

```json
{
  "commandId": "obx-55aa…",
  "commandType": "SCHEDULE_SENDS",
  "schemaVersion": 1,
  "producer": "attestation-module",
  "tenantId": "org-xyz",
  "correlationId": "task-7f3a…",
  "occurredAt": "2026-09-15T06:00:03Z",
  "payload": { "…": "the outbox row's payload, verbatim" }
}
```

- `commandId` **is the outbox row id** — the outreach service's dedup key. Pub/Sub ordering key = `payload.cancellationKey` (`attestation-task:<taskId>`), so a later `CANCEL_SENDS` can never overtake its `SCHEDULE_SENDS`.
- `correlationId` carries **our task id**; the outreach service echoes it on every outcome, so our outcome consumer joins outcomes back to tasks without a lookup table. `sendKey` carries the tier (`…:T-30`, `…:T-7`, `…:OVERDUE`; a custom offset labels itself, e.g. `…:T-14`; the kickoff is `…:KICKOFF`).
- `recipientId = practitioner:<certify_id>` — resolved to the current address by the outreach service at send time from its registry, which the PDM backend keeps in sync (step 0b below, D14). No address, no name in our payload.
- `recipients.cc` is **deferred**: v1 emails the practitioner only. Copying a tenant's credentialing contact would need an `org-contact:` recipient namespace and an owner for it — the outreach service extends without contract change when that owner registers (D14).
- `portalLink` = `<portalBaseUrl>/attest/<taskId>` — `portalBaseUrl` comes from the tenant's config entry (*Configuration and flags*); the `/attest/<taskId>` path is a **stable entry-point contract owned by the portal stack**, which may redirect internally however it likes. The link is **tokenless** — no credential, no data; authentication happens at the portal (D17). Why not publish an event and let the portal stack build the link and command the outreach itself: that would move email ownership, the ladder, cancellation, and the send audit across a team boundary to avoid one config value — rejected.
- `expiresAt` — our bound on a lost cancel: due date + 1 day for the pre-due tiers, due date + 30 days for `OVERDUE`.
- Idempotency, three layers: the state guard (writes built only for rows still `SCHEDULED`) means at most one outbox row per opening; `commandId` dedup absorbs republishes; the outreach service's `UNIQUE (producer, sendKey)` absorbs anything that slips past both.
- **Audit written in this step (inside the same transaction):** `TASK_OPENED` and `OUTREACH_SCHEDULED` per practitioner opened; `TASK_OPEN_SKIPPED_DUPLICATE` per no-op; `CHUNK_PROCESSING_FAILED` on the final failing attempt. All carry the `runId` from the chunk body plus the task id. The outbox row's own lifecycle (`PENDING → PUBLISHED`, attempts) lives on the row itself — no separate audit events for it.
- **Nothing here calls any outreach endpoint.** The outreach service exposes no producer API; the topic is the only way in.

**Step 0b — recipient registry sync (a PDM dependency, not this module's work — D14).**

Before any `SCHEDULE_SENDS` can be accepted, the practitioner must exist in the outreach service's registry. That registry is kept by the **PDM backend (`api-layer`)** as its own registered producer, `pdm-platform` — it owns the practitioner contact data and the change path, so it owns the sync:

```json
{
  "commandId": "pdm-evt-0a01…",
  "commandType": "UPSERT_RECIPIENT",
  "schemaVersion": 1,
  "producer": "pdm-platform",
  "tenantId": "org-xyz",
  "correlationId": "cert-000123",
  "occurredAt": "2026-09-01T02:10:00Z",
  "payload": {
    "recipientId": "practitioner:cert-000123",
    "channels": { "email": { "address": "dr.smith@clinic.example" } },
    "displayName": "Dr. A. Smith",
    "locale": "en-US",
    "timezone": "America/New_York",
    "status": "ACTIVE",
    "groups": ["practitioners"]
  }
}
```

- **What PDM publishes, and when:** one `UPSERT_RECIPIENT` per active practitioner as an initial load (before this module's backfill — *Rollout*), then on every contact change; a terminated practitioner is republished with `status: INACTIVE`, which also makes the outreach service suppress anything still pending to them.
- **What this module does: nothing.** It sends to `practitioner:<certify_id>` ids and observes the consequences — an unknown or inactive recipient at schedule time comes back as `SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)`: audited and alerted to `pdm-platform`; once the sync catches up, the ladder is re-emitted from the consistency report (an audited ops action, idempotent on the outreach side).
- **Recipient-level outcomes route to the owner:** `RECIPIENT_ADDRESS_FAILED` and `RECIPIENT_UNSUBSCRIBED` go to `pdm-platform`'s subscription (fixing a contact record is a PDM job); this module sees the per-send effect (`SUPPRESSED`, with its cause) on its own outcomes.
- **Audit written in this step:** none here — the sync is `pdm-platform`'s, recorded by the outreach service (`RECIPIENT_UPSERTED`) and by whatever `api-layer` keeps on its side.

**Step 5 — the Smart Outreach Service sends what is due, and tells us (its design doc, steps 6–9).**

- Its one-minute sweep claims our due send rows, resolves `practitioner:cert-000123` to the current address, renders `attestation.reminder.t30` (tenant override or platform default) with `vars.nextAttestationDate`, `vars.portalLink`, `recipient.displayName`, `tenant.*`, sends via SendGrid, and publishes outcomes.
- **No attestation logic runs there.** No task lookup, no `OPEN` check, no date arithmetic. Stale sends are prevented by our `CANCEL_SENDS` and bounded by our `expiresAt`.
- **Our outcome consumer (doc 5)** subscribes to `outreach.outcomes.v1` with the filter `attributes.producer = "attestation-module"`, deduplicates on `outcomeId`, processes first and acks after, and writes our audit. Outcome → audit event:

| Outcome | Our audit event | `detail` carried over |
| --- | --- | --- |
| `SCHEDULED` | `OUTREACH_SCHEDULE_CONFIRMED` — the outreach service accepted and stored the send (the *intent* was `OUTREACH_SCHEDULED`, written in the opening transaction); the on-demand consistency report reads this event | `sendKey`, `scheduledAt`, `expiresAt`, `outcomeId` |
| `SCHEDULE_REJECTED` | `OUTREACH_SCHEDULE_REJECTED` | `sendKey`, `reasonCode`, `message` |
| `SENT` | `OUTREACH_SENT` | `sendKey` (tier), `providerMessageId`, `templateVersion`, `resolvedAddress`, `outcomeId` |
| `DELIVERED` | `OUTREACH_DELIVERED` | `sendKey`, `providerEventAt` |
| `BOUNCED` / `COMPLAINED` | `OUTREACH_BOUNCED` | `sendKey`, `bounceType`, `providerReason` |
| `FAILED` | `OUTREACH_FAILED` | `sendKey`, `reasonCode`, `message`, `attempt` |
| `SUPPRESSED` | `OUTREACH_SUPPRESSED` | `sendKey`, `suppressed[]` (`recipientId` + `cause` per dropped recipient), `sentToRemaining`, `outcomeId` — the outreach service's outcome shape (its *Contracts*, outcome table) |
| `EXPIRED` | `OUTREACH_EXPIRED` | `sendKey`, `expiresAt` |
| `CANCELLED` | `OUTREACH_CANCEL_CONFIRMED` — the pending send is cancelled (the *intent* was `OUTREACH_CANCELLED`, written in the submission transaction); the on-demand consistency report reads this event | `sendKey`, `outcomeId` |
| `CANCEL_TOO_LATE` | `OUTREACH_CANCEL_TOO_LATE` | `sendKey`, `currentStatus`, `sentAt` |

- Every event above carries the task id from `correlationId`. Recipient-level outcomes (`RECIPIENT_ADDRESS_FAILED`, `RECIPIENT_UNSUBSCRIBED`) route to the record's owner, `pdm-platform` — this module never receives them (D14); a broken address shows up here as `SUPPRESSED` with its cause. "Why no email?" is answered from our own table.

**Step 6 — the loop closes on submission (owned by docs 2/5).**

- The submission transaction (in `attestation-db`, atomic):
  - task `OPEN → SUBMITTED` (with `submitted_at` stamped),
  - one `OUTREACH_CANCEL` outbox row — published as `CANCEL_SENDS` on `cancellationKey = attestation-task:<taskId>`, same dispatch path as step 4b; the Smart Outreach Service flips its remaining pending sends to `CANCELLED` and reports each — `CANCEL_TOO_LATE` for any row no longer pending (`OUTREACH_CANCELLED` audited here for the intent; `CANCELLED` / `CANCEL_TOO_LATE` outcomes recorded by the consumer),
  - the **successor obligation inserted**: a new `SCHEDULED` row — same `certify_practitioner_id`, `next_attestation_date = submission + 90 days`, `last_attestation_date = the submission date`, new `due_period`, new identity.
- **Audit written in this step (docs 2/5, same transaction):** the submission's own events, `OUTREACH_CANCELLED` (recording the cancel intent and its `outboxEventId`), and `OBLIGATION_SCHEDULED` for the successor (recording which rule set its date).
- Ordering key `cancellationKey` keeps the cancel behind its schedule command; a tier already claimed by the outreach sweep can send before the cancel lands — one extra email worst case, reported as `CANCEL_TOO_LATE`. There is no send-time task-state check (D16); `expiresAt` is the second bound.
- The next scan finds the successor when its window arrives. The cycle is closed — no OV write, no write to any other service's database, anywhere.

**The deterministic identity (this module's defining export):**

```
identity = (tenant_id, practitioner_id, due_period)
  practitioner_id = the OV's certify_id — stored physically as certify_practitioner_id,
                    the platform's convention for referencing a practitioner
                    (same column name as credentialing_workflows and monitoring_workflows)
  due_period      = the obligation's next attestation date, ISO 'YYYY-MM-DD'
```

- Computed **once**, at row creation — recorded by that insert's `OBLIGATION_SCHEDULED` event, whose envelope carries the full tuple; there is deliberately no separate identity event. Stamped onto the vendor export row, staged change items, review rows, the sync, and the client export. Downstream modules read it, never recompute it.
- Why the next attestation date: human-readable and naturally unique per obligation — each successor row carries a new next attestation date, so each obligation keeps its own identity and the old task's history stays intact.
- The stronger rule on top: **no second task while any task is open** (F5) — structural, because only a submission inserts a successor row.

**Task states (contract for docs 5–7):** stored states are `SCHEDULED → OPEN → SUBMITTED`, plus the terminal **`CLOSED`** (terminations only: `SCHEDULED → CLOSED` or `OPEN → CLOSED`, written by the termination consumer — D18). Every transition has an owner (the scan opens; a submission closes; a termination closes).

- **OVERDUE is derived, never stored:** a task is overdue when `state = 'OPEN' AND next_attestation_date < today` — a fact about a date, computed **server-side** at read time, so it is always exactly right and no daily job has to keep a status column in sync with the calendar.
- Consumers still see and filter by OVERDUE (doc 2's API translates it to the predicate above); "before today" is anchored to one clock rule (UTC vs tenant timezone — decided with the scan cadence, doc 5).
- `SCHEDULED` = obligation exists, window not reached. No EXPIRED state — overdue tasks stay submittable forever (D2-23). `SUBMITTED` = the attester's job is done; the workflow finishes in review (D2-09). `CLOSED` = obligation ended without submission (terminated practitioner) — terminal, never consumer-visible (doc 2: excluded from lists, detail 404).

**Practitioner termination (D18)** — three layers, one dependency:

- **Never created:** backfill and seeding skip terminated practitioners.
- **Never opened:** the chunk handler re-checks at opening (one batched OV read per chunk); a terminated practitioner's row is skipped (`TASK_OPEN_SKIPPED_TERMINATED`) and stays `SCHEDULED`.
- **Closed on the event:** the provider-data platform publishes a practitioner-terminated event (**dependency — no such event exists today; ask to the PDM platform team, topic and payload to be agreed**). The module's consumer (doc 5) handles it in one transaction: every `SCHEDULED`/`OPEN` row for the practitioner flips to `CLOSED` (`TASK_CLOSED`, reason `PRACTITIONER_TERMINATED`) and one `OUTREACH_CANCEL` outbox row per open task cancels the reminders. Deactivating the recipient record (`UPSERT_RECIPIENT`, `status: INACTIVE`) is `pdm-platform`'s own termination handling (D14) — a second, independent brake on pending sends.
- **The net:** until the event exists, the weekly reconcile's `terminated` check finds terminated practitioners with non-`CLOSED` rows and closes them the same way.

**Clock rules — a dependency of this module, not owned by it.** "Advancing the clock" now means "inserting the successor SCHEDULED row":

- On submission: successor due `submission + 90 days` — owned by the submission flow (docs 2/5). Submission-anchored, **confirmed by Product 2026-09-01** (closes v2 O-11).
- **A rejection does not reset the clock** (Product, 2026-09-01 — supersedes the earlier rejection + 30 working rule): the submission's `+90` successor stands unchanged; review outcomes never touch the schedule. A practitioner whose submission is rejected gets no early re-attestation task — the next opportunity is the next 90-day cycle. One future obligation per practitioner, never two.
- Never submitted: no successor; the open task stays, and no new row exists for the scan to find.
- All in the module's own table and database; the only cross-service effect (reminder cancellation) leaves as an `OUTREACH_CANCEL` outbox row → `CANCEL_SENDS` through the same outbox (docs 2/5 own the wiring).

**Reminder contract:** tiers are `sendKey` suffixes — `T-30 / T-7 / OVERDUE` for the default ladder, `KICKOFF` for the late-opening notice, a custom offset labels itself (e.g. `T-14`); templates `attestation.reminder.t30 / .t7 / .overdue` (kickoff reuses `.t30`) are registered in the Smart Outreach Service under category `compliance-reminder` (mandatory — no opt-out). The offsets come from `reminderOffsets` (tenant config, stamped on the task row at insert) and are turned into absolute `scheduledAt` values here. Duplicate ladders impossible: the command is born only inside the opening transaction (one outbox row per opening) and the outreach service enforces `UNIQUE (producer, sendKey)`; emails carry only the tokenless portal deep link (D17) — never provider data; the last offset is the final automated email (default +1 day, the OVERDUE tier — D2-23).

### Configuration and flags

Every tenant-level knob this module reads, in one place. All keys live in one `tenant_configurations` entry, type `attestation-module-config` (existing pattern). **No feature flags** — this module uses no Flagsmith or other flag system; the kill switch is a config key, and enablement itself is the presence of the config entry (no entry → module inactive).

| Key | Lives in | Type | Default | Controls |
| --- | --- | --- | --- | --- |
| `leadDays` | `attestation-module-config` | integer (days) | 30 | How many days before the next attestation date an obligation opens |
| `reminderOffsets` | `attestation-module-config` | integer array | `[-30, -7, 1]` | The reminder ladder, in days relative to the next attestation date |
| `staggerDays` | `attestation-module-config` | integer (days) | 90 | Backfill spread — how many days the initial dates are staggered across |
| `cyclePaused` | `attestation-module-config` | boolean | `false` | Per-tenant kill switch: pauses new scans, no deployment |
| `portalBaseUrl` | `attestation-module-config` | string (URL) | none — set per tenant at onboarding | Base of the tokenless email deep link `<portalBaseUrl>/attest/<taskId>` (D17); the path is the portal stack's stable entry-point contract |
| `chunkSize` | module-level config, not per tenant (exact home pinned in doc 5) | integer | 500 | Page size of the eligibility query and of each Cloud Task chunk; setting it to 1 is the per-practitioner variant |

Key-by-key rules:

- `leadDays` **(default 30)** — how many days before the next attestation date an obligation opens. The first reminder is T-30, so the task must exist by then. Tenant-tunable.
- `reminderOffsets` **(default `[-30, -7, 1]`)** — the reminder ladder, in days relative to the next attestation date: negative = before, positive = after, sorted ascending. The last entry is the final automated email (default +1 day — the OVERDUE tier, D2-23). Tenant-tunable, with two rules:
  - **Stamped at row insert:** the value is copied onto each row's `reminder_offsets` when the row is born (backfill or successor insert) and recorded in `OBLIGATION_SCHEDULED` — the opening transaction builds the ladder from the row, never by re-reading config. A config change therefore applies to rows born after it and never rewrites ladders already scheduled.
  - **A tenant's first reminder must fit the window:** an offset earlier than `-leadDays` can never fire (the task does not exist yet) — the config schema validates `min(reminderOffsets) >= -leadDays` (doc 5).
- `staggerDays` **(default 90)** — backfill spread.
- **`cyclePaused`** — the per-tenant kill switch: pauses new scans, no deployment.
- *(Removed 2026-08-29: `lookbackDays` — obsolete; unopened obligations wait in `SCHEDULED`, so there is no rear window edge to protect.)*
- **Infrastructure knobs are deployment configuration, not tenant config:** relay sweep cadence and grace age, and the outcome subscription's retention (≥ 7 days) and dead-letter attempts live with the module service's deployment (doc 5). The command topic's retention and dead-lettering belong to the Smart Outreach Service. Sender identity and branding for the emails are the tenant's `outreach-config` entry, owned by that service's design.
- **The entry is extensible:** these are the keys this module needs. Each later module doc adds its own keys to the same entry (doc 2: portal mode + field editability; doc 3: per-vendor export cadence and pause) and lists them in its Contracts section; doc 5 (the backend, which reads the config) owns the complete consolidated JSON schema.

## Data model and migration

- **Where everything lives (D8, revised 2026-09-01):** the module's **own Spanner database — `attestation-db`** — on the existing "app-data" instance, a sibling of the primary PDM database `data-production-b` (and `app-db`, `roster_db`, …). Migrations are module-owned (tooling pinned in doc 6); nothing is added to any platform database; no new instance (Spanner bills the instance, so the extra database costs nothing).

- **Tables this module creates** (all in `attestation-db`; final DDL doc 6): `attestation_tasks`, `attestation_audit_events`, `attestation_outbox`.

- **Tables it interacts with, never owns:** `core_practitioners_ov` / the OV read model — read-only, through the existing api-layer endpoints (backfill population, export, prefill). The Smart Outreach Service's tables (`outreach_sends`, `outreach_recipients`, …) are **never touched**: they are written by that service reacting to our commands; we learn their state only from outcomes.

- **`attestation_tasks`** (D7 — one table; final DDL doc 6; this is the contract):
  - One row per obligation cycle: `id`, `tenant_id`, `certify_practitioner_id` (**holds the OV's `certify_id`** — the platform's referencing convention, matching `credentialing_workflows`), `due_period`, `next_attestation_date`, `last_attestation_date`, `state`, `source`, `reminder_offsets`, and one timestamp per stored state: `created_at`, `opened_at`, `submitted_at`.
  - `UNIQUE (tenant_id, certify_practitioner_id, due_period)` — the idempotency backbone.
  - Index `(tenant_id, state, next_attestation_date)` — the scan's read path.
  - **Three structures, three different jobs — not to be confused:**
    - **`id` — a plain UUID, the primary key.** The row's *address*: an opaque random string generated when the row is born. Everything that points at the row uses it (the Portal's `/attestation-tasks/{id}` URL, the outreach command's `correlationId` and `cancellationKey`, the chunk payload). It is **not** built from tenant/practitioner/date — nothing is encoded in it.
    - **`UNIQUE (tenant_id, certify_practitioner_id, due_period)` — a rule, not an id.** A constraint on three ordinary columns: the database refuses a second row with the same combination. It never appears in a URL — its only job is making a duplicate obligation physically impossible, whatever retries or double-fires happen.
    - **`(tenant_id, state, next_attestation_date)` — a plain speed index.** Not unique, identifies nothing — it lets the daily scan jump straight to due `SCHEDULED` rows instead of reading the table.
  - **Why `certify_id` is not the identifier — grain, not correctness:** in the OV, `certify_id` is unique because that table has one row per *practitioner*. Our table has one row per *obligation*, and a practitioner owes one every 90 days — so the same `certify_id` appears on a new row every cycle, for 7 years. It is stored as the permanent OV join key; what uniquely names an obligation is `certify_id` **plus the next attestation date** (`due_period`) — the natural key above.
  - **No identifier is minted at opening time** — the `id` is generated when the row is *inserted* (backfill or successor insert; the DAL's create path generates it automatically). Opening only changes `state` on an existing row; the chunk payload already carries the id.

- **What the rows actually look like** (one practitioner, across time):

  After the backfill (nothing has happened yet — the row just waits):

```json
{
  "id": "task-7f3a…",
  "tenantId": "org-xyz",
  "certifyPractitionerId": "cert-000123",
  "duePeriod": "2026-10-15",
  "nextAttestationDate": "2026-10-15",
  "lastAttestationDate": null,
  "state": "SCHEDULED",
  "source": "BACKFILL",
  "reminderOffsets": [-30, -7, 1],
  "createdAt": "2026-09-01T06:00:03Z",
  "openedAt": null
}
```

  After the scan opens it (only `state` and `openedAt` changed — same row, same id):

```json
{ "id": "task-7f3a…", "state": "OPEN", "openedAt": "2026-09-15T06:00:03Z", "…": "all other fields unchanged" }
```

  After submission, the table holds **two** rows for this practitioner — the finished one and its successor:

```json
{ "id": "task-7f3a…", "certifyPractitionerId": "cert-000123", "duePeriod": "2026-10-15", "state": "SUBMITTED", "submittedAt": "2026-10-10T17:21:04Z", "…": "…" }
{ "id": "task-9c1d…", "certifyPractitionerId": "cert-000123", "duePeriod": "2027-01-08", "nextAttestationDate": "2027-01-08", "lastAttestationDate": "2026-10-10", "state": "SCHEDULED", "source": "SUBMISSION_ADVANCE", "…": "…" }
```

  Same `certify_id` on both — the next attestation date is what tells the obligations apart.

- **What one audit record looks like** (`attestation_audit_events`, append-only, committed in the same transaction as the change it records):

```json
{
  "id": "ae-31b0…",
  "type": "TASK_OPENED",
  "taskId": "task-7f3a…",
  "tenantId": "org-xyz",
  "certifyPractitionerId": "cert-000123",
  "duePeriod": "2026-10-15",
  "runId": "cyc-2026-09-15-a1b2",
  "detail": { "nextAttestationDate": "2026-10-15", "reminderOffsets": [-30, -7, 1] },
  "occurredAt": "2026-09-15T06:00:03Z"
}
```

- **`attestation_audit_events`** — append-only, 7-year retention (D2-20), same database, written in the same transaction as the change it records.

- **`attestation_outbox`** — the module's single outbox table (D11): every cross-service intent leaves through it, this module's and later docs' alike.
  - Types today: `OUTREACH_SCHEDULE` → `SCHEDULE_SENDS`, `OUTREACH_CANCEL` → `CANCEL_SENDS`; docs 2/5 add their own; the parked date-sync would ride it too. (Recipient upserts are not ours — the PDM backend publishes them, D14.)
  - Columns: `id` (UUID — doubles as the published `commandId`, the outreach service's dedup key), `event_type`, `ordering_key` (`attestation-task:<taskId>` for send commands — the Pub/Sub ordering key), `aggregate_id` (the task id), `tenant_id`, `payload` (JSON), `status` (`PENDING` / `PUBLISHED`), `attempts`, `created_at`, `published_at`.
  - Written only inside business transactions; index `(status, created_at)` for the relay sweep; rows kept, not deleted, for correlation (retention with doc 6).

- **No OV migration. No writes to any core practitioner table.** The OV is read at backfill (population only), at export, and at prefill — read-only, through the existing api-layer endpoints, and nothing read is copied onto our rows. *(Replaces the earlier OV-denorm-columns design, analyzed and rejected — see Alternatives considered.)*

- **The backfill is the migration's data half:** per-tenant; inserts only missing rows; audited run events; selected-vs-created reconciliation report.

- ✅ **v2 §6.0 amended (Q1 closed 2026-09-01):** v2 now states the backfilled dates live only in the module's own database — never on the core practitioner table or the OV — and nothing is synced back to the primary database (Product 2026-09-01: no PDM-side filter needed; a date-sync event on the outbox is the parked future improvement, D13). §6 diagram amended to match (Stage 0 and Stage 1 wording, 2026-09-08).

- **New-practitioner seeding (new consequence of owning the schedule):** practitioners onboarded after the backfill have no row, and no scan will find them. A recurring seeding check (active practitioners in the OV with no `attestation_tasks` row → insert a `SCHEDULED` row) closes the gap — the reconcile job's `population` check (Q7). Seeded rows carry `source = SEEDED` and `OBLIGATION_SCHEDULED (rule: SEEDED)`; their date uses the very same pinned formula — `next_attestation_date = run_date + (hash(certify_id) mod staggerDays)` — backfill and seeding differ only in which run's date anchors it, and insert-if-missing means whichever run first inserts the row fixes the date forever. A bulk onboarding staggers exactly the way the backfill did. Terminated practitioners are never seeded (D18). Detection runs in the weekly reconcile call — the same `POST /internal/attestation-cycles/reconcile` endpoint the backfill uses — and execution is **chunked through Cloud Tasks**, reusing the identical insert machinery (insert-if-missing, groups of ~50, audited), never inline in the weekly request: a burst of late-onboarded practitioners (group acquisition, large roster ingest) seeds at backfill scale safely (*Observability → weekly reconcile*).

## Security, privacy, and access

- Trigger and backfill endpoints are internal, `@TenantAgnostic`, reachable only by Cloud Scheduler; the chunk callback uses the shared-secret header pattern (`X-Credentialing-Api-Key` precedent, `CredentialingApiKeyValidator.java:12`).
- Tenant isolation is server-side on every query, job, and event (v2 §8).
- The attestation backend's service account is registered with the Smart Outreach Service as producer `attestation-module` — **sender only**: template namespace `attestation.`, no recipient namespaces (the `practitioner:` records are owned and synced by `pdm-platform`, D14) — and holds publish rights on `outreach.commands.v1` and a filtered subscription on `outreach.outcomes.v1`. Send commands carry ids and dates only — no address, no name, no provider data.
- **Emails contain no provider data and no credentials** — only the tokenless portal deep link (D17); authentication happens at the portal (doc 2). No new PHI surface.
- Opt-in per tenant: no `attestation-module-config` entry → inactive.

## Performance and scale

| #   | Item             | Value / assumption                                                                                  |
| --- | ---------------- | ----------------------------------------------------------------------------------------------------- |
| N1  | Population       | ~70,000 practitioners on rolling 90-day cycles → **~780 openings per day** after staggering         |
| N2  | Email volume     | Up to 3 reminders per task → **~2,300 sends/day ≈ 71k/month** (upper bound)                         |
| N3  | Scan cadence     | Daily; a missed run must lose nothing (structural: SCHEDULED rows wait)                             |
| N4  | Idempotency      | Double-fire, redelivery, backfill re-run: zero duplicate effects                                    |
| N5  | Audit            | Every event in *Audit trail*, same transaction as the change, 7 years (D2-20)                       |
| N6  | Tenant isolation | Server-side for every query, job, event                                                             |
| N7  | Growth headroom  | No degradation at 10× (~7,800/day) — trivial for the chosen approach                                |
| N8  | Outbox/command volume | ~780 `SCHEDULE_SENDS` + ~780 `CANCEL_SENDS` commands/day at steady state, (the ~70k `UPSERT_RECIPIENT` initial load is `pdm-platform`'s, D14); ~2,300 sends × up to 3 outcomes each/day inbound — trivial for Pub/Sub |

## Observability

Telemetry is best-effort and never blocks the workflow — metrics and logs are emitted outside the database transaction.

**Metrics** (low-cardinality labels: tenant yes, practitioner never):

```
attestation.scheduler.run.duration        timer    {outcome}
attestation.scheduler.due.found           counter  {tenant}
attestation.task.opened                   counter  {tenant}
attestation.task.duplicate_skipped        counter  {tenant}
attestation.task.open_skipped_terminated  counter  {tenant}
attestation.task.closed                   counter  {tenant, reason}
attestation.task.overdue                  gauge    {tenant}
attestation.reconcile.rows.seeded         counter  {tenant, trigger}
attestation.outreach.sent                 counter  {tenant, tier}      (from SENT outcomes)
attestation.outreach.failed               counter  {tenant, tier, reason}  (from FAILED / SUPPRESSED / EXPIRED outcomes)
attestation.outreach.schedule_rejected    counter  {tenant, reason}
attestation.outreach.cancel_too_late      counter  {tenant, tier}
attestation.outcome.consumer.lag          gauge
attestation.cycle.chunk.failed            counter  {tenant}
attestation.outbox.pending.depth          gauge    {eventType}
attestation.outbox.oldest_pending.age     gauge
attestation.outbox.publish.failed         counter  {eventType}
```

**Alerts:**

- **Scan liveness (the important one):** no `CYCLE_SCAN_COMPLETED` in 26 hours → page. Absence-based — fires even if the scheduler silently disappears.
- Queue depth / oldest-task age above threshold.
- `outreach.failed` rate > 5% per tenant/tier.
- `task.duplicate_skipped` spiking — harmless, but it means double-firing; investigate.
- Outbox oldest `PENDING` age > 15 minutes — dispatch and sweep both failing.
- Our outcome subscription's dead-letter depth > 0 or oldest-unacked age > 1 hour — our consumer down or stuck. (The command topic's DLQ is the Smart Outreach Service's alert; a burst of `schedule_rejected` here is our signal that something we sent was refused.)
- `schedule_rejected` > 0 in a new deployment window — contract or registry-sync mismatch shipped.
- Kill switch engaged > 24 hours.
- **Weekly reconcile** — its own Cloud Scheduler job (weekly) calling `POST /internal/attestation-cycles/reconcile` with both checks (the **same endpoint the backfill runs supervised** — one job, two callers, *Contracts step 0*; same shared-secret + 202 pattern; doc 5 hosts it; the run writes `RECONCILE_RUN_COMPLETED` with per-check counts). The endpoint is a **detector, not a worker**: checks run as queries, and every remediation batch leaves as the same Cloud Tasks chunks the daily scan uses — deterministic task names `(checkRunDate, check, tenant, page)`, idempotent chunk handlers, poison-row fallback. A bulk-onboarding week that finds 20k missing rows therefore fans out like a backfill instead of running inside one unattended HTTP request (the inline-batch failure shape, rejected in *Alternatives*).
  - **`population`** — active practitioners with no `attestation_tasks` row at all → insert the missing `SCHEDULED` row (the seeding check, Q7 — `source: SEEDED`, date rule in *Data model*); chunks reuse the backfill's insert path (insert-if-missing, groups of ~50, `OBLIGATION_SCHEDULED` audit).
  - **`terminated`** — practitioners terminated in the OV whose rows are still `SCHEDULED`/`OPEN` → close them (D18) — the only closer until the PDM termination event exists.
- **On-demand audit reports — deliberately not automated (D19).** Three more consistency questions are answerable any time from the audit table, and their live failure modes already page through the real-time alerts; they run as reports when someone asks, promotable to reconcile checks if one proves recurring value:
  - **Aged unopened rows** — `state = 'SCHEDULED' AND next_attestation_date < today - 7 days`; the scan-liveness alert already pages within 26 hours of a dead scan.
  - **Reminder consistency** — intent events without confirmation events (`OUTREACH_SCHEDULED` vs `OUTREACH_SCHEDULE_CONFIRMED`; `OUTREACH_CANCELLED` vs `OUTREACH_CANCEL_CONFIRMED` / `OUTREACH_CANCEL_TOO_LATE`); the outbox-age, DLQ, and consumer-lag alerts cover the live paths. A re-emit driven by this report is an audited ops action, safe by construction — the outreach service's `commandId` dedup and unique send key absorb duplicates.
  - **Unresolved recipients** — `OUTREACH_SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)` on still-`OPEN` tasks; the `schedule_rejected` alert fires in real time and the fix is `pdm-platform`'s sync (D14).

**Correlation:** run id → task ids → outbox `commandId` → outreach `sendKey` / `outcomeId` → `providerMessageId`, through logs, queue headers, and the outcome payloads.

## Audit trail

*Added section — compliance evidence is a first-class requirement.*

**Audit table: `attestation_audit_events`** — created by this module (final DDL doc 6), in the module's own `attestation-db` database alongside `attestation_tasks`. Every event in this section is one row in that table; later modules write into the same table.

Rules: append-only; same transaction as the change; 7-year retention (D2-20); every event carries the identity plus run/task ids.

- **`run_id` and `task_id` are first-class, indexed columns on the audit table** (doc 6 DDL) — not fields buried in a JSON blob.
- The ids returned by the endpoints (`runId`, task ids) are the same values stamped on every event — a returned id is a **lookup key**, not a decoration: `WHERE run_id = @x` returns everything that run did; `WHERE task_id = @y` returns one obligation's whole life.

### The common envelope — every event carries these fields

```json
{
  "id": "ae-31b0…",
  "type": "TASK_OPENED",
  "tenantId": "org-xyz",
  "certifyPractitionerId": "cert-000123",
  "duePeriod": "2026-10-15",
  "taskId": "task-7f3a…",
  "runId": "cyc-2026-09-15-a1b2",
  "actor": "system:attestation-cycle",
  "occurredAt": "2026-09-15T06:00:03Z",
  "detail": { }
}
```

- `id` — UUID, generated on insert.
- `type` — one of the events below.
- `tenantId` — always set (tenant isolation applies to audit reads too).
- `certifyPractitionerId` / `duePeriod` / `taskId` — the identity and row correlation; **null on run-level events** (`RECONCILE_RUN_*`, `CYCLE_SCAN_*`).
- `runId` — the scan or backfill run that caused the event; null on events caused outside a run (submission-time events carry the submission's own correlation id instead).
- `actor` — who did it: a system component name (`system:attestation-cycle`, `system:attestation-outreach-consumer` for events written from outreach outcomes, `system:attestation-termination-consumer` for termination handling) or a user id for human-triggered actions (a manual backfill, an ops replay).
- `detail` — event-specific fields, contract per event below.

**Envelope carry-forward rule:** later module docs inherit this envelope unchanged and may **add** their own correlation fields (nullable, additive — a base field's meaning never changes). Doc 6 owns the consolidated shape and the final DDL.

### Per-event contracts — the `detail` fields and where each commits

| Event | Committed in | `detail` fields |
| --- | --- | --- |
| `RECONCILE_RUN_STARTED` | its own write, at run start | `trigger` (`MANUAL` \| `SCHEDULED`), `checks[]`, `staggerDays`, `dryRun`, `practitionersInScope` |
| `OBLIGATION_SCHEDULED` | the row-insert transaction (backfill batch / submission) | `nextAttestationDate`, `rule` (`BACKFILL` \| `SUBMISSION_PLUS_90` \| `SEEDED`), `predecessorTaskId` (null for backfill and seeded rows), `reminderOffsets` |
| `RECONCILE_RUN_COMPLETED` | its own write, at run end | per-check counts (`rowsSeeded`, `skippedExisting`, `terminatedToClose`), `chunksEnqueued`, `dryRun`, `durationMs` — found-and-enqueued counts; the chunk handlers' own audit records what committed |
| `CYCLE_SCAN_STARTED` | its own write, before the first page | `runDate`, `windowEnd` (today + leadDays) |
| `CYCLE_SCAN_COMPLETED` | its own write, after the last chunk is enqueued | `dueFound`, `chunksEnqueued`, `chunkSize`, `durationMs` |
| `TASK_OPENED` | the opening transaction | `nextAttestationDate`, `reminderOffsets`, `formPolicy` snapshot (the tenant's `portalMode` + field editability at opening) |
| `TASK_OPEN_SKIPPED_DUPLICATE` | its own write (nothing else changed) | `existingTaskId`, `existingState` |
| `OUTREACH_SCHEDULED` | the opening transaction | `cancellationKey`, `sends`: array of `{ sendKey, templateKey, scheduledAt, expiresAt }`, `outboxEventId` (= the `commandId`) — records the intent; the outreach service's send ids arrive later on outcomes |
| `OUTREACH_SCHEDULE_CONFIRMED` | the outcome consumer's write (doc 5), one per `SCHEDULED` outcome | `sendKey`, `scheduledAt`, `expiresAt`, `outcomeId` |
| `OUTREACH_SCHEDULE_REJECTED` | the outcome consumer's write (doc 5) | `sendKey`, `reasonCode`, `message`, `outcomeId` |
| `OUTREACH_SENT` / `DELIVERED` / `BOUNCED` / `FAILED` / `SUPPRESSED` / `EXPIRED` | the outcome consumer's write (doc 5), one per outcome | `sendKey` (tier), `outcomeId`, `providerMessageId`, `templateVersion`, `resolvedAddress` (SENT), `bounceType` / `providerReason` (BOUNCED), `reasonCode` / `message` / `attempt` (FAILED), `suppressed[]` + `sentToRemaining` (SUPPRESSED), `expiresAt` (EXPIRED) |
| `OUTREACH_CANCELLED` | the submission transaction (docs 2/5) | `cancellationKey`, `outboxEventId`, `cause` (`SUBMITTED`) — records the cancel intent; the flips happen in the outreach service |
| `OUTREACH_CANCEL_CONFIRMED` | the outcome consumer's write (doc 5), one per `CANCELLED` outcome | `sendKey`, `outcomeId` |
| `OUTREACH_CANCEL_TOO_LATE` | the outcome consumer's write (doc 5) | `sendKey`, `currentStatus`, `sentAt`, `outcomeId` |
| `TASK_OPEN_SKIPPED_TERMINATED` | its own write (nothing else changed) | `certifyPractitionerId`, `ovStatus` |
| `TASK_CLOSED` | the termination consumer's transaction (doc 5) | `from` (`SCHEDULED` \| `OPEN`), `reason` (`PRACTITIONER_TERMINATED`), `sourceEventId` |
| `CHUNK_PROCESSING_FAILED` | its own write, on the final failing attempt | `chunkNumber`, `certifyPractitionerIds`, `attempt`, `error` |

- "Committed in" is a contract, not a note: events listed with a transaction are written **inside** it — if the change rolls back, so does its event; events written standalone record facts that have no accompanying state change.
- An engineer implementing from this table should never have to invent an audit field. A field found missing during implementation is a gap in this doc — fix it here first.

- There is deliberately **no overdue event**: OVERDUE is derived from `next_attestation_date` (task states, *Contracts*) — no action occurs when a date passes, and "when did it become overdue" is answerable forever from the row itself.

Questions this answers directly: "why is this task due 2026-10-14?" (`OBLIGATION_SCHEDULED` trail) · "why no email?" (`OUTREACH_SCHEDULE_REJECTED`, `OUTREACH_SUPPRESSED`, `OUTREACH_EXPIRED`, `OUTREACH_CANCELLED`) · "which address got the T-7?" (`OUTREACH_SENT.resolvedAddress`) · "two scans ran — why one task?" (`TASK_OPEN_SKIPPED_DUPLICATE`).

## Failure modes and rollback

**Queue failure handling — three failure points, three answers:**

1. **Creating a chunk task fails.** The trigger retries inline; same-name "already exists" = success. Trigger dies mid-way → un-enqueued obligations stay `SCHEDULED`; tomorrow's scan re-finds them. The scan is the retry of last resort.

2. **Executing a chunk fails.** Cloud Tasks retries with exponential backoff (queue-side config). We write no retry code — the state-flip guard makes replays no-ops.

3. **Retries run out.** Cloud Tasks has **no dead-letter queue** — an exhausted task is silently deleted. Compensations: on the final attempt (via the `X-CloudTasks-TaskRetryCount` header) the handler falls back to per-practitioner transactions so healthy rows commit and only the poison row is recorded in `CHUNK_PROCESSING_FAILED`; all uncommitted rows stay `SCHEDULED` for the next scan. Delay possible, loss impossible.

**Outbox/event failure handling — the published-but-never-consumed chain, three layers:**

1. **Pub/Sub redelivers until acked.** The Smart Outreach Service processes first and acks after, so a crash mid-write on its side is redelivered, never lost; if it is down the commands wait — retention ≥ 7 days. The same rule applies to our outcome consumer on the way back.
2. **Dead-letter topics.** A command failing every delivery attempt moves to the outreach service's DLQ (its alert, its audited replay); an outcome failing every delivery to us moves to our subscription's DLQ (our alert). A per-send refusal (`SCHEDULE_REJECTED`) is an outcome, not a dead letter.
3. **End-to-end audit evidence.** Intent vs confirmation events make a lost command provable on demand (the consistency report, *Observability*); re-emitting the outbox row is an audited ops action, idempotent on the outreach side. The live failure modes page in real time (outbox-age, DLQ, `schedule_rejected` alerts); the report covers the residual — retention expiry, an acked-but-lost bug — which is rare enough to be human-driven (D19).

**Why there is no horizon cliff anymore:** the old date-window scan could lose a practitioner after ~75 days of continuous failure. Now an unopened obligation stays `SCHEDULED` forever — every scan asks "anything due?", not "anything due *recently*?". The weekly reconcile still watches population (practitioners with no row); aged unopened rows are one audit query away, and the scan-liveness alert pages within a day of a dead scan.

**Edge cases:**

| Case                                                   | Handling                                                                                          |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| Scheduler double-fires / queue redelivers              | State-flip guard + task-name dedup + unique index; audited                                        |
| A scan run is missed                                   | SCHEDULED rows wait; next run opens them; liveness alert fires so it is *known*                   |
| Trigger crashes mid-enqueue                            | Created chunks proceed; the rest stay SCHEDULED for the next run                                  |
| Chunk exhausts all retries                             | `CHUNK_PROCESSING_FAILED` + next scan re-finds — delay possible, loss impossible                  |
| Obligation opened late — reminder dates in the past    | Remaining tiers + one immediate kickoff (D5)                                                      |
| Dispatch fails after the opening commit                 | Outbox row stays `PENDING`; relay sweep republishes — reminders delayed minutes, never lost       |
| Crash between publish and the `PUBLISHED` mark          | Sweep republishes → duplicate command; the outreach service's `commandId` inbox + unique `sendKey` absorb it |
| Smart Outreach Service down                             | Pub/Sub holds commands (retention ≥ 7 days) and redelivers until acked; emails delayed, never lost |
| Recipient not yet in the outreach registry              | `SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)` outcome → `OUTREACH_SCHEDULE_REJECTED` audited + alert to `pdm-platform` (sync owner, D14); once the sync catches up, the ladder is re-emitted from the consistency report (audited ops action, idempotent) |
| Practitioner's address hard-bounces                     | Further tiers come back `SUPPRESSED` (cause on our outcomes); `RECIPIENT_ADDRESS_FAILED` routes to `pdm-platform`, which owns fixing the contact record (D14); task stays visible and submittable |
| Cancel lost or late                                     | `expiresAt` bounds every tier (due + 1 day pre-due, due + 30 days overdue) → `EXPIRED`, never a stale send months later |
| Poison command                                          | Outreach service's dead-letter topic after bounded attempts; its DLQ alert; audited operator replay |
| Command or outcome lost beyond all retries              | Provable on demand from intent-vs-confirmation events (the consistency report); ops re-emits the outbox row — audited, idempotent on the outreach side (D19) |
| Backfill re-run                                        | Inserts only missing rows; audited no-op                                                          |
| 70k due the same day                                   | Prevented by the hash stagger (~780/day)                                                          |
| Practitioner onboarded after backfill                  | Weekly reconcile `population` check inserts the missing SCHEDULED row (Q7)                        |
| Practitioner terminated (before or after opening)      | Backfill/seeding exclude; opening re-check skips (`TASK_OPEN_SKIPPED_TERMINATED`); the PDM termination event closes rows (`CLOSED`) + cancels outreach; the weekly `terminated` check is the net (D18) |
| All emails fail or bounce                              | Task stays visible in the Portal (F7); `OUTREACH_FAILED` / `OUTREACH_BOUNCED` in the report        |
| Submission rejected in review                          | Nothing changes here — a rejection never resets the clock (Product, 2026-09-01); the submission's `+90` successor stands |
| Practitioner in multiple plans of one tenant           | Identity is per practitioner-per-tenant; plans are irrelevant to this module — practitioner↔plan linking is NSCP-solutioning scope (doc 2 D13), nothing plan-related stored on task rows |
| Tenant with no config entry                            | Module inactive — explicit opt-in                                                                 |

**Rollback:**

- Per-tenant kill switch (`cyclePaused`) stops new scans immediately; pending reminders for already-open tasks keep their schedule in the outreach service (pause them there with the tenant's `outreachPaused` if needed). No deployment.
- Created rows and sent emails stay (rows are inert without the scan; emails cannot be recalled).
- Re-enabling resumes cleanly — SCHEDULED rows waited.
- Schema is additive-only (new tables) — schema rollback never required.

## Rollout

1. Create the `attestation-db` database on the app-data instance; module-owned migrations create the three tables + indexes (doc 6).
2. Register `attestation-module-config` + JSON schema; seed the pilot tenant.
3. DevOps request: three Cloud Scheduler jobs (daily scan, relay sweep, weekly reconcile), the `attestation-cycle-queue` Cloud Tasks queue, publish rights on `outreach.commands.v1` for the backend's service account, and the module's filtered subscription on `outreach.outcomes.v1` (retention ≥ 7 days, dead-letter topic, bounded delivery attempts).
4. Smart Outreach Service live with `attestation-module` registered as a **sender-only** producer (template namespace `attestation.`, no recipient namespaces) and the three attestation templates activated; `pdm-platform` registered as the owner of `practitioner:` with its registry initial load complete for the pilot tenant (the PDM backend's deliverable, D14); the module's outcome consumer deployed in the backend (doc 5) before any tenant is enabled.
5. Backfill the pilot tenant — the reconcile endpoint's supervised first run (dry-run → report → execute); the registry load (step 4) must already cover every in-scope practitioner; verify `schedule_rejected` stays at zero through the first opened tasks.
6. Enable via config; watch the alerts — outbox depth, outcome-consumer lag, and `schedule_rejected` included — through one full reminder ladder.
7. Tenant waves; the kill switch is the brake at every step.
