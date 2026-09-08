# Design: Smart Outreach Service — reliable, domain-blind email delivery for every service

## Purpose and scope

This service answers one question for every other service on the platform: **"send this templated email to these people at this time, unless I cancel it first" — and tell me what happened.**

**This service owns:**

- **The command intake** — a Pub/Sub topic on which any producer service publishes three commands: *keep a recipient's contact record fresh*, *schedule sends*, and *cancel sends*. Nothing else is accepted.
- **The recipient registry** — the service's own list of people it can email: one record per person per tenant with an id, the current address, a display name, a language, address health, and preferences. Producers name people by id; the service resolves the address at send time from this list and nowhere else.
- **Recipient groups** — named sets of recipients (`practitioners`, `tenant-admins`) maintained through the same sync command, so "notify everyone in this group" is one command.
- **The send queue** — every scheduled send is a row in the service's **own database**; a sweep sends whatever is due.
- **Templates and rendering** — templates live here, keyed by a stable `templateKey`, versioned, with per-tenant overrides. Producers send a key and variables, never a subject or body.
- **Tenant sender identity** — from-address, reply-to, display name, branding, and the per-tenant pause switch.
- **Provider delivery** — one provider adapter (SendGrid in v1) behind a channel-neutral interface; delivery, bounce, and complaint events arrive on the service's own webhook and are recorded against the person, not just the message.
- **Outcome events** — a second topic on which the service publishes what became of each send (`SENT`, `FAILED`, `CANCELLED`, `EXPIRED`, `SUPPRESSED`, `DELIVERED`, `BOUNCED`) and what it learned about a person (`RECIPIENT_ADDRESS_FAILED`), so every producer can write its own audit trail from facts, not guesses.
- **Preferences and unsubscribe** — an unsubscribe link and preference page for optional message categories; mandatory categories (compliance reminders) cannot be opted out of.
- **Idempotency, retries, dead-lettering, audit** of everything above.

**This service does NOT own — the boundary rule of the whole design:**

- **Any producer's business meaning.** The service never knows what an "attestation task", a "credentialing workflow", or a "roster batch" is. It cannot resolve a producer's ids, read a producer's tables, or decide whether a send is "still relevant". A producer that wants a send to stop must cancel it.
- **When to send.** Producers send absolute timestamps. The service computes no offsets from any business date.
- **Which people a message goes to.** Producers choose the recipients and name them by id. The service knows *that* a person has an address, never *why* a producer wants to reach them. Producers keep the registry in sync; the service never reads a producer's contact tables.
- **Recipient-level intelligence** beyond preferences — channel choice, quiet hours, digesting, frequency capping. Out of scope for v1; the registry is where they attach later.
- **Non-email channels.** Email only in v1. The registry's `channels` object and the command shape are channel-neutral so SMS or in-app can be added without a producer change.
- **The existing credentialing `smart_outreach` engine.** It stays where it is, untouched, on its own tables. Nothing is migrated.

**Neighbors:** every producer service (the Attestation Module's cycle and submission flows are the first; a second worked example uses roster ingestion), SendGrid (delivery provider), DevOps (Pub/Sub topics, Cloud Scheduler jobs, Cloud Run service, the Spanner database).

## Section triage

| Section                       | Included | Reason                                                                                   |
| ----------------------------- | -------- | ---------------------------------------------------------------------------------------- |
| Approach                      | Yes      | The chosen design as numbered steps                                                      |
| Alternatives considered       | Yes      | Self-contained per alternative — what, pros, cons, why rejected, cost                    |
| Components and files touched  | **No**   | Pre-implementation design doc — new repository, no file lists yet                        |
| Contracts and interfaces      | Yes      | Command and outcome envelopes, per-command payloads, three worked producer examples, admin API, configuration |
| Data model and migration      | Yes      | The service's tables, where they live, indexes and unique keys                           |
| Security, privacy, and access | Yes      | Topic permissions, producer authorization, contact data at rest, webhook verification    |
| Performance and scale         | Yes      | The NFR numbers all cost estimates use                                                   |
| Observability                 | Yes      | A delivery platform must show every send's fate                                          |
| Audit trail                   | Yes      | **Added section** — the service is the system of record for "what did we send, to whom, when" |
| Failure modes and rollback    | Yes      | Consumer, registry, sweep, provider, and webhook failure handling; edge cases; rollback  |
| Test strategy                 | **No**   | Design phase; decisive cases are in Failure modes                                        |
| Rollout                       | Yes      | Repository, infrastructure, registry load, first producer, kill switches, v2 roadmap     |

## Approach

A standalone Cloud Run service with its own Spanner database, fed by one Pub/Sub command topic and reporting on one outcome topic. Inside: an idempotent command consumer, a recipient registry, a table-as-queue with a leased sweep, a template engine, a provider adapter, and a webhook receiver. Each step: what we do, why, why not the alternative a reviewer would ask about, and what happens when it fails.

**1. One service, one repository, one database — the boundary is physical.**

- **What:**
  - A new repository (`outreach-service`), a Quarkus application (the platform's language and runtime) deployed as its own Cloud Run service.
  - Its own Spanner database — `outreach-db` — on the existing app-data instance, a sibling of `attestation-db` and the primary database. Migrations are service-owned.
  - No shared tables, no writes to any other database, no reads of any producer's tables. One read-only platform dependency: the tenant's `outreach-config` entry in `tenant_configurations`, read through the platform's existing configuration API and cached (D14). The only ways in are the command topic, the provider webhook, the preference page, and a small admin API.

- **Why:**
  - The platform's microservices direction (v2 D2-32) decides the service boundary; a database per service is the storage half of that decision.
  - The existing engine's coupling is exactly what a shared database produces: its send path resolves `outreach_id` as a credentialing workflow (`SmartOutreachSchedulerService.java:268-284`), and 14 skip rules read workflow state, notes, Jira tickets, CAQH roster data (`OutreachExclusionEngine.java:51-133`). A physical boundary makes that class of coupling impossible, not merely discouraged.

- **Why not a shared library each service embeds instead of a service:**
  - A library gives every service its own SendGrid credential, its own copy of the template files, and no central registry, suppression, or delivery log. The industry writeups name this the anti-pattern: template strings scattered across repositories, no single view of what notifications exist.

- **What if it fails:** Cloud Run instance loss is harmless — every unit of work is a durable row (command inbox, recipient, send row, outbox row) resumed by the next sweep. Full failure handling: *Failure modes and rollback*.

**2. Intake — one command topic, three commands, processed first and acknowledged after.**

- **What:**
  - Producers publish to the Pub/Sub topic **`outreach.commands.v1`**. Three message types: `UPSERT_RECIPIENT`, `SCHEDULE_SENDS`, `CANCEL_SENDS`. Every message carries the envelope in *Contracts*: `commandId`, `producer`, `tenantId`, `correlationId`, `occurredAt`, `payload`.
  - How a producer gets a message there is the producer's business. The Attestation Module uses its transactional outbox plus relay sweep (cycle doc D11); a simpler producer may publish directly and accept the small loss window. The service does not care.
  - The service consumes through one pull subscription. Per message: insert the `commandId` into `outreach_inbox` (unique) — a duplicate is acknowledged and dropped; validate the payload against the schema; apply it in one transaction with the inbox row; **acknowledge only after the transaction commits**.
  - Pub/Sub **ordering key**: `recipientId` for `UPSERT_RECIPIENT`, `cancellationKey` for the two send commands — so a cancel can never overtake the schedule it cancels, and two updates to one person apply in order. Subscription retention ≥ 7 days; dead-letter topic `outreach.commands.v1.dlq` after a bounded attempt count.

- **Why Pub/Sub commands and not a REST endpoint as the producer path:**
  - A producer calling REST from inside its own transaction faces the dual-write problem: commit-then-call can crash between the two and lose the intent, call-then-commit can send an email for a transaction that rolled back. An outbox row plus an asynchronous message is the only shape where the producer's state change and its outreach intent survive together.
  - A topic decouples availability: the service being down for an hour delays emails by an hour; a REST dependency would fail producers' transactions.
  - Pub/Sub supplies redelivery, ordering keys, and a dead-letter topic for free (cycle doc chose it for the same reasons).

- **Why three commands and not more:**
  - Everything a producer needs is expressible as "here is what I know about this person", "schedule these sends under this key", and "cancel whatever is still pending under this key (or these ids)". A reschedule is cancel plus schedule. "Send now" is a schedule whose `scheduledAt` is now. Deactivating a person is an upsert with `status: INACTIVE`. A smaller command set is a smaller surface to keep domain-blind.

- **Why the inbox table rather than trusting Pub/Sub:**
  - Pub/Sub is at-least-once by contract; a redelivered `SCHEDULE_SENDS` would create a second ladder. The inbox row commits in the same transaction as the rows it created, so a duplicate command is refused by a unique key, never by "the code remembers".

- **What if it fails:** a consumer crash mid-transaction rolls back inbox row and effects together; Pub/Sub redelivers; the second attempt finds no inbox row and applies cleanly. A malformed message fails validation deterministically → dead-letter topic after the bounded attempts → alert → audited operator replay or discard.

- **Payloads:** *Contracts → command envelope.*

**3. The recipient registry — producers keep it fresh; the service resolves from it and nowhere else.**

- **What:**
  - `UPSERT_RECIPIENT` creates or replaces one `outreach_recipients` row: `tenant_id`, `recipient_id` (producer-chosen, namespaced like template keys: `practitioner:cert-000123`, `org-contact:admin-42`), `channels.email.address`, `display_name`, `locale`, `timezone`, `status` (`ACTIVE`, `INACTIVE`), `attributes` (free JSON for templates), and `groups[]` (full membership list, replaces the previous one in `outreach_recipient_group_members`).
  - Producers send it whenever a contact is created or changes, and once per existing contact as the initial load (*Rollout*). Keyed on `(tenant_id, recipient_id)`; running it twice has the effect of once. The command carries the producer's `occurredAt`; an older update arriving after a newer one is ignored (last-writer by `occurredAt`), so a replayed backlog cannot revert a fresh address.
  - Two columns on the row are **never written by producers**: `email_status` (`OK`, `HARD_BOUNCED`, `COMPLAINED`, `MANUAL_SUPPRESSED` — written by the webhook and the admin API) and `preferences` (written by the preference page). An upsert that changes the address resets `email_status` to `OK` — a new address has no history.
  - The `producer` that registered a person is stored as `owner_producer`; outcomes about that person (`RECIPIENT_ADDRESS_FAILED`) go to that producer.

- **Why the service keeps its own list instead of receiving addresses in every send:**
  - **People can control their mail.** An unsubscribe choice needs a record of the person that lasts across sends; with addresses inline there is nowhere to attach it.
  - **A bad address is remembered against the person.** One bounce marks the person once; every later send from any producer sees it, and the producer that owns the record is told to fix it. With inline addresses the service knows an address failed, not whose.
  - **Address changes reach already-scheduled sends.** A reminder due in 30 days goes to the address on file *that day*. With inline addresses it goes to the address that was right a month ago, unless the producer notices and reschedules.
  - **Group sends are one command.** The service expands `practitioners` into members; no producer pages its own tables to build a list.
  - **Less personal data in transit.** Send commands carry ids; addresses travel once, in the upsert.
  - **Unknown or inactive people are refused before anything is sent**, not after a bounce.
  - This is how Knock, Courier, Novu, Braze, and Iterable work, and how LinkedIn's and Uber's internal platforms work: the notification platform owns the recipient list; producers send ids and keep the list in sync. *Alternatives* scores the inline-address variant.

- **Why this is still domain-blind:**
  - The registry holds *contact facts* (this id has this address, prefers this language, is active) — platform data of the same kind as sender identity. It holds no business facts (why the producer wants to reach the person, what they owe, what state they are in). The service never reads a producer's tables to fill it; producers push.

- **What if it fails:** a producer's sync lags → a send names an id the service does not have → `SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)` for that send, immediately, so the producer sees its own gap. A stale address in the registry is exactly as wrong as a stale address in a payload would be — and fixable centrally with one upsert instead of one reschedule per pending send.

- **Payloads:** *Contracts → `UPSERT_RECIPIENT`.*

**4. The send row — every scheduled email is one row in `outreach_sends`, naming people by id, with the producer's keys on it.**

- **What:**
  - `SCHEDULE_SENDS` creates one `outreach_sends` row per send in the payload: `tenant_id`, `producer`, `cancellation_key`, `send_key`, `template_key`, `category`, `variables` (JSON), `recipients` (JSON of `recipientId`s for `to`/`cc`/`bcc`), `scheduled_at`, `expires_at` (optional), `correlation_id`, `status = PENDING`.
  - A send may name a group instead of a list (`recipients.toGroup`). The consumer expands it at intake into one row per active member (`send_key = <sendKey>:<recipientId>`), in chunks, tracked by an `outreach_group_expansions` job row so a crash mid-expansion resumes where it stopped. All rows share the command's `cancellation_key`.
  - **`UNIQUE (producer, send_key)`** — the send-level idempotency backbone. `send_key` is producer-supplied and deterministic (for the attestation ladder: `attestation-task:<taskId>:T-30`). A replayed command whose inbox row was somehow lost still cannot create a second row.
  - Intake checks per send: every `recipientId` exists and is `ACTIVE` (else `RECIPIENT_UNRESOLVED`); `templateKey` is active and in the producer's namespace; `variables` match the template's schema; `category` is a known category.
  - Indexes: `(status, scheduled_at)` for the sweep; `(producer, cancellation_key, status)` for cancels; `(tenant_id, correlation_id)` for lookups.

- **Why absolute `scheduledAt` from the producer, never an offset the service computes:**
  - The moment the service knows "T-30 means 30 days before the attestation due date", it knows what an attestation is. Absolute timestamps keep the row meaningless to the service and fully meaningful to the producer.
  - A `scheduledAt` already in the past is sent at the next sweep. Whether a past-dated reminder should be dropped is a producer decision (the cycle doc makes it: drop past tiers, add one kickoff). The service does not second-guess it.

- **Why `expiresAt` exists and is optional:**
  - A domain-blind safety net. The producer can say "this overdue reminder is pointless after 2026-12-01". If the cancel is lost for any reason, the row still cannot send after that instant — it becomes `EXPIRED`, an outcome the producer sees. It is a property of the send, not a rule about attestation.

- **Why a `category` on every send:**
  - Preferences need something to attach to. `compliance-reminder`, `service-notice`, `digest` are service configuration, each flagged mandatory or optional. A person may opt out of optional categories; mandatory ones always send. The producer picks from the list; the service attaches no meaning beyond "may this be opted out of".

- **What if it fails:** an insert violating `UNIQUE (producer, send_key)` inside a `SCHEDULE_SENDS` transaction is treated as "already scheduled" per send, not as a command failure: the transaction inserts nothing for that row and records `SEND_SCHEDULE_DUPLICATE` in audit; the remaining sends in the command proceed. A rejected send (unknown recipient, bad variables) likewise affects only itself.

- **Payloads:** *Contracts → `SCHEDULE_SENDS`.*

**5. Cancellation — flip pending rows under a key; report what could no longer be stopped.**

- **What:**
  - `CANCEL_SENDS` names a `cancellationKey` (whole group) and/or a list of `sendKeys` (individual rows). In one transaction the service flips every matching row whose status is still `PENDING` to `CANCELLED` (`cancelled_at`, `cancel_reason` from the payload) and writes one outcome per row.
  - Rows already `CLAIMED`, `SENT`, `FAILED`, or `EXPIRED` are not touched. For each of them the service emits a `CANCEL_TOO_LATE` outcome carrying the row's actual status, so the producer knows an email went out (or is going out) and can record it.

- **Why every schedule must carry a `cancellationKey` — in plain terms:**
  - Think of it as the **handle** on a bundle of emails. When a producer schedules the three attestation reminders for one task, it ties them together with one handle (`attestation-task:<taskId>`). Later, "stop everything for this task" is one message naming the handle — the producer does not need to remember three send ids.
  - The same handle is what **keeps messages in order**. Pub/Sub only promises "A before B" for messages that share an ordering key. The schedule and its cancel share the handle, so the cancel can never be delivered before the schedule it refers to. Without a shared key, a cancel could arrive first, find nothing, and be dropped — and the schedule would then create rows nobody will ever cancel.
  - It is **required, not optional**, for exactly that reason: a send without a handle would have no ordering guarantee, and a later cancel by individual `sendKeys` could still overtake it. Making it mandatory removes the whole class of "cancel lost because it ran ahead" problems at the cost of one string per command.
  - A producer that will never cancel (a one-off summary email) still sets it — its correlation id is fine. The key costs nothing and the ordering guarantee is still useful (two schedules for the same thing apply in order).
  - The key is **opaque** to the service: it never parses `attestation-task:…`; it only compares it for equality. The producer chooses a value that means something to itself.
  - Same idea, two different jobs: `sendKey` / `commandId` answer "have I seen this exact request before?" (so a retry does not create a duplicate); `cancellationKey` answers "which bundle does this belong to?" (so it can be stopped and kept in order).

- **Why cancellation is the only way to stop a send:**
  - The alternative — the service asking the producer "is this still relevant?" before sending — is the coupling this design exists to remove. It reintroduces a synchronous dependency at send time and a per-producer callback contract. Every reference platform (Knock's `cancellation_key`, Courier's `cancelation_token`) uses the cancel-by-key model instead; the race it leaves open is documented and accepted.

- **The race, stated plainly:** a send claimed by the sweep in the same minute a cancel arrives goes out. Worst case is one extra email per group. `expiresAt` bounds the damage for time-boxed sends; ordering keys guarantee the cancel never precedes its own schedule.

- **What if it fails:** a cancel that fails to commit is redelivered (process-then-ack) and applies idempotently — a row already `CANCELLED` is skipped, its outcome not re-emitted (the inbox row prevents the whole command from re-applying). A cancel for a key with no rows is a no-op, audited (`CANCEL_NO_MATCH`) — a producer relay bug surfaces as that audit row.

- **Payloads:** *Contracts → `CANCEL_SENDS`, outcome `CANCELLED`, `CANCEL_TOO_LATE`.*

**6. The sweep — claim due rows with a lease, resolve people, render, send, record, one row at a time.**

- **What:**
  - Cloud Scheduler calls `POST /internal/outreach/sweep` every minute (shared-secret header, fail-closed, same pattern as `SmartOutreachSchedulerResource.java:140-150`).
  - The sweep **claims** in batches: `UPDATE outreach_sends SET status = 'CLAIMED', claimed_at = now, lease_until = now + 5m, attempt = attempt + 1 WHERE status = 'PENDING' AND scheduled_at <= now AND (expires_at IS NULL OR expires_at > now) LIMIT 200` — a conditional write, so two overlapping sweeps cannot claim the same row. Rows whose `expires_at` has passed flip to `EXPIRED` in the same pass.
  - Per claimed row, in order:
    - Check the tenant is not paused.
    - **Resolve every `recipientId` from `outreach_recipients` now** — the current address, name, locale. Drop a recipient (with a `SUPPRESSED` outcome naming the cause) if `status = INACTIVE`, `email_status` is not `OK`, or the person opted out of this send's category. A send with no remaining `to` recipient becomes `SUPPRESSED`.
    - Load the template version (tenant override, else default); render subject and body with the row's variables plus `recipient.*` and `tenant.*`.
    - Call the provider adapter with `send_key` as the idempotency key.
    - Success: set `status = SENT`, `sent_at`, `provider_message_id`, `resolved_recipients` (the addresses actually used), `rendered_subject`, `rendered_body`, `rendered_body_hash`, `template_version`; write the `SENT` outcome to the outbox.
    - Transient provider error: set `status = PENDING`, `next_attempt_at = now + backoff`; audit `SEND_RETRY_SCHEDULED`.
    - Permanent error, or the fifth attempt: set `status = FAILED` with the reason; write the `FAILED` outcome.
  - A row whose lease expired while still `CLAIMED` (the instance died mid-send) is reclaimed by a later sweep.

- **Why resolve at send time, not at intake:**
  - The address that is right on the due date is the one on file on the due date. Resolving at intake would freeze a month-old address into the row — the inline-address problem, re-created internally. The row stores ids until it is sent, then stores the resolved snapshot so the audit answers "which address received it".

- **Why a table sweep and not one Cloud Task per send with a future `scheduleTime`:**
  - Cloud Tasks caps future scheduling at 30 days; the attestation OVERDUE row (due date + 1 day) is born ~31 days early (cycle doc, step 6). A table has no horizon.
  - A table can be queried ("what is pending for this tenant?"), cancelled by key with one statement, and audited. Named tasks would need a second table mapping keys to task names — the table anyway.
  - The existing engine proves the pattern in production (`SpannerScheduledOutreachRepository.java:117-127`); this design adds the claim step it lacks (verified: no lease today, overlapping runs can double-process — `SmartOutreachSchedulerService.java:456-465`).

- **Why render here and not in SendGrid dynamic templates:**
  - Rendering here gives tenant overrides and versioning under our control, provider independence, and an auditable record of exactly what was sent. SendGrid templates put the copy outside version control and outside the audit trail. *Alternatives* scores it.

- **Why retry at all — the existing engine does not:**
  - Today `FAILED` is terminal on the first provider error (verified: `ScheduledOutreachProcessor.java:165-176`, no retry path). A SendGrid 5xx or a rate limit is transient; five attempts with exponential backoff over roughly an hour turn most of those into `SENT`. Permanent errors (invalid address, rejected sender) fail fast on the first attempt.

- **What if it fails:** the sweep endpoint failing leaves rows `PENDING` — delayed, never lost; liveness alert fires. A single poison row (template rendering error) fails only itself: it is marked `FAILED` with the reason and the batch continues. Provider outage: every attempt is transient → rows cycle through backoff → the `provider.failed` alert fires → sends resume when the provider does.

- **Payloads:** *Contracts → sweep endpoint, outcome `SENT`, `FAILED`, `EXPIRED`, `SUPPRESSED`.*

**7. Templates — owned here, keyed, versioned, tenant-overridable, with a declared variable schema.**

- **What:**
  - `outreach_templates`: one row per `(template_key, tenant_id, version)`; `tenant_id = '*'` is the platform default. Columns: `channel` (`EMAIL`), `subject_template`, `body_template` (Handlebars-style, the platform's existing convention), `variables_schema` (JSON Schema of the producer variables the template expects), `default_category`, `status` (`DRAFT`, `ACTIVE`, `RETIRED`), `created_at`, `activated_at`.
  - Templates may use three variable families: `vars.*` (from the send), `recipient.*` (`displayName`, `locale`, `attributes.*` from the registry), `tenant.*` (branding from the tenant's `outreach-config` entry). Producers therefore never send a recipient's name as a variable.
  - Template keys are namespaced by producer: `attestation.reminder.t30`, `roster.ingestion.summary`. The consumer refuses a `SCHEDULE_SENDS` whose `templateKey` prefix does not match the message's `producer` — a cheap authorization rule that needs no domain knowledge.
  - Resolution at send time: the `ACTIVE` version for `(template_key, tenant_id)`, else the `ACTIVE` default. Locale-specific variants (`body_template` per `locale`) are a v2 field; v1 renders one language.
  - Variables are validated against `variables_schema` **at schedule time**, in the consumer transaction — a send that could never render is refused at intake with a `SCHEDULE_REJECTED` outcome naming the field, not discovered on the due date.
  - Templates are managed through the admin API (*Contracts*), with a preview endpoint that renders a template against sample variables and a sample recipient. Template rows are never deleted; retiring a version keeps every historical send explainable.

- **Why templates here and not with producers:**
  - The industry consensus and the platform decision: the service owns copy and rendering; producers own the facts. Platform teams (Knock, Courier, Novu, Airbnb's rendering service, Contentsquare) all put templates in the notification service so copy changes need no producer deploy and every notification is discoverable in one place.
  - The existing engine already leans this way: it selects an org-settings template and applies variables (`OutreachEmailConstructor.java:25-32,41-63`); this design makes the template store first-class instead of borrowing organization settings.

- **What if it fails:** a template key with no `ACTIVE` version at send time → row `FAILED` with `NO_ACTIVE_TEMPLATE`; the schedule-time check makes this reachable only if a template is retired after scheduling, and the admin API refuses retiring the last `ACTIVE` version while pending sends reference the key.

- **Payloads:** *Contracts → admin API (templates).*

**8. Outcomes — the service reports every fate on its own topic, through its own outbox.**

- **What:**
  - Every state change of a send row, and every change the service learns about a person, writes one `outreach_outbox` row in the same transaction; a post-commit dispatcher publishes it to **`outreach.outcomes.v1`** and marks it `PUBLISHED`; a relay sweep (the same Cloud Scheduler job as step 6, second phase) publishes anything still `PENDING` older than a grace age.
  - Outcome message attributes carry `producer`, `tenantId`, `outcomeType` — so a producer's subscription uses a Pub/Sub filter (`attributes.producer = "attestation-module"`) and receives only its own outcomes.
  - Send outcomes: `SCHEDULED`, `SCHEDULE_REJECTED`, `CANCELLED`, `CANCEL_TOO_LATE`, `SENT`, `FAILED`, `EXPIRED`, `SUPPRESSED`, `DELIVERED`, `BOUNCED`, `COMPLAINED`. Recipient outcomes (to the `owner_producer`): `RECIPIENT_ADDRESS_FAILED`, `RECIPIENT_UNSUBSCRIBED`. Every outcome carries the producer's `correlationId`, `cancellationKey`, and `sendKey` (or `recipientId`) verbatim.

- **Why outcomes are mandatory, not a nice-to-have:**
  - Producers must write their own audit (attestation: `OUTREACH_SENT` / `OUTREACH_BOUNCED` in `attestation_audit_events`, 7-year retention). Without outcome events they would have to read this service's tables — the coupling again, in the other direction.
  - `RECIPIENT_ADDRESS_FAILED` closes the loop that inline addresses can never close: the producer that owns the contact record learns it is broken and can show "please update your email" in its own UI.
  - The service uses the same outbox discipline it asks of producers; an outcome is never lost between commit and publish.

- **Why not let producers query a REST endpoint instead:**
  - Polling scales badly and puts the service on every producer's request path. The read API exists (*Contracts*) for operators and support tooling, not as the integration contract.

- **What if it fails:** dispatcher failure → the outbox row stays `PENDING` → the relay publishes it within minutes. A producer that is down leaves messages waiting on its own subscription (its retention, its dead-letter topic — the producer's concern). Outcome ordering is per `cancellationKey` (ordering key), so `SENT` never arrives after `DELIVERED` for the same send.

- **Payloads:** *Contracts → outcome envelope and per-outcome fields.*

**9. Provider adapter, webhook, address health, and preferences — the only place a vendor name appears.**

- **What:**
  - `EmailProvider` interface: `send(renderedEmail, idempotencyKey) → providerMessageId`. `SendGridEmailProvider` is the v1 implementation; the API key comes from Secret Manager; per-tenant from-address and reply-to come from the tenant's `outreach-config` entry (D14).
  - `POST /webhooks/sendgrid/events` receives SendGrid's signed event webhook; the signature is verified with the configured public key (the pattern already in `SendGridWebhookResource.java:60`).
  - Each event is matched to a send row by `provider_message_id`, appended to `outreach_delivery_events`, and mapped to an outcome:
    - `delivered → DELIVERED`, `bounce → BOUNCED`, `dropped → FAILED` (post-send), `spamreport → COMPLAINED`.
    - Opens and clicks are stored, not published — engagement is not a delivery fact producers need.
  - **A hard bounce or complaint is recorded against the person:** the matching `recipientId` in `resolved_recipients` gets `email_status = HARD_BOUNCED` or `COMPLAINED` in the same transaction, and a `RECIPIENT_ADDRESS_FAILED` outcome goes to the `owner_producer`. Soft bounces are stored only. Operators may clear or set `MANUAL_SUPPRESSED` through the admin API; every change is audited.
  - **Preferences:** every email in an optional category carries an unsubscribe link to `/p/{signedToken}` (token = tenant + recipient + category, signed, expiring). The page writes `preferences.unsubscribedCategories` on the recipient row, audits it, and emits `RECIPIENT_UNSUBSCRIBED` to the owner producer. Mandatory categories carry no link. `List-Unsubscribe` headers are set for mail clients that support one-click.

- **Why an interface even with one provider:**
  - Provider swap or dual-provider failover is then a configuration change, and nothing outside the adapter knows SendGrid's payloads. Non-negotiable 6 (source-agnostic design) applied to egress.

- **Why address health lives on the person, not in a separate suppression list:**
  - A list of failed address strings protects sender reputation but cannot tell anyone whose address it was. On the person's row the mark protects every producer's sends *and* names the record to fix. Sender reputation is still protected: `email_status != OK` blocks the send regardless of who asks.

- **What if it fails:** webhook delivery failures are SendGrid's to retry (the endpoint returns 5xx on a transient error, 2xx once persisted — the existing pattern). An event for an unknown `provider_message_id` is stored as unmatched and alerted at volume; nothing is dropped. A preference page outage means links fail for its duration; no sends are affected.

- **Payloads:** *Contracts → webhook, outcome `DELIVERED`, `BOUNCED`, `COMPLAINED`, `RECIPIENT_ADDRESS_FAILED`, `RECIPIENT_UNSUBSCRIBED`.*

**What the approach deliberately does NOT do:**

- No lookups of any producer's data, ids, or state. No callback to a producer before sending. Producers push contact facts; the service never pulls.
- No date arithmetic on business dates; no default ladders; no per-domain reminder types.
- No channel routing, quiet hours, digesting, or frequency capping in v1 — the registry is where they attach later.
- No changes to the existing `smart_outreach` engine or the `scheduled_outreaches` table.
- No synchronous producer API. The read and admin API is for operators and tooling.
- Minimal new infrastructure: one Cloud Run service, one Spanner database on the existing instance, two Pub/Sub topics with dead-letter topics, one Cloud Scheduler job, four Secret Manager secrets (DevOps request).

### Key decisions

| #   | Question                                   | Resolution                                                                                                                 |
| --- | ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| D1  | Service boundary                           | Standalone Cloud Run service, own repository, own `outreach-db` Spanner database on the app-data instance                    |
| D2  | Producer integration                       | Pub/Sub command topic `outreach.commands.v1`; three commands; producers choose their own reliability mechanism (outbox recommended) |
| D3  | Business logic location                    | **Producers own all of it**: whether, whom (by id), when (absolute), which template key, which variables, when to cancel   |
| D4  | Send-time relevance check                  | **None.** Cancel-by-key is the only way to stop a send; `expiresAt` is the optional domain-blind bound; the race is accepted (one extra email, worst case) |
| D5  | Template ownership                         | Service-owned, keyed, versioned, tenant-overridable, with a variables schema validated at intake                            |
| D6  | Recipient resolution                       | **Recipient registry inside the service** (the model Knock, Courier, Novu, Braze, LinkedIn, Uber use): producers push contact records with `UPSERT_RECIPIENT` and name people by `recipientId`; the service resolves addresses at send time from its own registry; the inline-address variant analyzed and rejected |
| D7  | Scheduling mechanism                       | Table as queue with a leased claim; one-minute Cloud Scheduler sweep; no horizon                                            |
| D8  | Outcome reporting                          | Mandatory outcome topic `outreach.outcomes.v1`, published through the service's own outbox, filtered per producer            |
| D9  | Provider                                   | SendGrid behind an `EmailProvider` interface; provider name confined to the adapter                                          |
| D10 | Idempotency                                | Three layers: `outreach_inbox (command_id)`, `UNIQUE (producer, send_key)`, provider idempotency key; registry upserts are last-writer by `occurredAt` |
| D11 | Retries                                    | Transient provider errors retried with exponential backoff, five attempts; permanent errors fail on the first               |
| D18 | Rendered body                              | Stored on the send row (subject, body, hash) for the full retention period; re-rendering on demand rejected because exact reproduction cannot be guaranteed over years; cheaper storage for old bodies is a later decision |
| D12 | Existing engine                            | Untouched; not migrated; credentialing keeps running on it                                                                  |
| D13 | Channels                                   | Email only in v1; registry `channels` object and command shape channel-neutral                                              |
| D14 | Tenant settings home                       | **The platform's `tenant_configurations` entry, type `outreach-config`** — the place every service keeps tenant configuration today. Read through the platform's existing configuration API (read-only, cached 60 s); never written by this service. No service-owned settings table |
| D15 | Address health and suppression             | On the recipient row (`email_status`), written by the webhook and the admin API only; blocks every producer's sends; owner producer notified |
| D16 | Preferences                                | Per-recipient opt-out of optional categories via signed unsubscribe link; categories are service configuration with a mandatory flag; compliance reminders mandatory |
| D17 | Group sends                                | `recipients.toGroup` expanded at intake into one row per active member, resumable via `outreach_group_expansions` |

## Alternatives considered

Each alternative in full: what it is, its genuine strengths, its costs, and the direct reason it lost to the chosen approach.

### Inline addresses in every send — no registry (the simpler model)

- **What it is:**
  - `SCHEDULE_SENDS` carries resolved email addresses and display names per recipient; the service stores and sends exactly what it was given. No `UPSERT_RECIPIENT`, no recipient tables, no groups.
  - A separate suppression list of failed address strings protects sender reputation.

- **Pros:**
  - Two fewer tables, one fewer command, no initial load, no sync path from producers — a smaller first build.
  - The first producer already holds each practitioner's address at the moment it schedules; nothing needs to be synced for it to work.
  - Nothing to keep fresh: the message is the truth at the time it was sent.

- **Cons — each one is a loop the service can never close:**
  - No unsubscribe or preference is possible — there is no record of the person for a choice to attach to.
  - A bounce marks an address string, not a person: the service cannot tell the owning producer "this practitioner's record is broken", and a re-cased or re-typed address escapes the list.
  - An address changed after scheduling is not used: a 30-day-out reminder goes to last month's address unless the producer notices, cancels, and reschedules — and nothing tells it to.
  - Every producer that wants a group send pages its own tables and publishes batches; the same code is rebuilt in each.
  - Names and addresses travel in every scheduled message and sit in every send row.
  - The service can check only that a string looks like an email; unknown or inactive people cannot be refused before sending.

- **Why rejected:** every reference platform — Knock, Courier, Novu, Braze, Iterable, and LinkedIn's and Uber's internal systems — keeps a recipient registry and takes ids, for the reasons above. Building without one saves roughly a third of the first build and forfeits preferences, address-health feedback, live address resolution, and group sends; retrofitting later means migrating every producer's payloads. The boundary is unaffected either way: contact facts are platform data, not producer business logic.

- **Cost:** infrastructure identical; the saving is engineering time on the first build only.

### Extend the existing `smart_outreach` engine inside `api-layer` (the cycle doc's earlier plan)

- **What it is:**
  - Keep the engine, its `scheduled_outreaches` table and Cloud Scheduler sweep; add a Pub/Sub consumer in `api-layer` that writes reminder rows via the DAL, plus one evaluation branch that resolves attestation ids and checks task state before sending.
  - Producers are served by adding a branch per producer.

- **Pros:**
  - The smallest amount of new code; table, sweep, SendGrid wiring, and webhook already exist and run in production.
  - No new deployable, no new database, no DevOps request beyond a subscription.

- **Cons:**
  - The send path is credentialing-shaped by construction: `outreach_id` is resolved as a credentialing workflow (`SmartOutreachSchedulerService.java:268-284`); every new producer needs a new branch inside the monolith — business logic of other services accumulating in one place.
  - The table has no unique key beyond the UUID primary key and no claim step (verified; race documented in-code at `SmartOutreachSchedulerService.java:456-465`); idempotency is application-level.
  - `FAILED` is terminal on the first provider error; there is no retry, no dead-letter, no outcome event — producers would read the table to learn what happened.
  - Lives in the monolith the microservices decision moves away from; every producer's release is coupled to `api-layer` deploys.

- **Why rejected:** it fails the one criterion this service exists for — domain-blindness — and it keeps the boundary inside the monolith. The chosen design reuses the engine's proven *pattern* (table as queue, scheduled sweep, SendGrid, webhook) without inheriting its coupling.

- **Cost:** ≈ $0 infrastructure; paid for in a permanent per-producer branch inside `api-layer` and a shared table with no uniqueness guarantee.

### Synchronous REST API as the producer integration

- **What it is:**
  - Producers call `POST /sends` and `POST /sends/cancel` on the service, inside or right after their business transaction; the service writes rows synchronously and returns ids.

- **Pros:**
  - Immediate validation feedback to the caller (bad template key, bad variables) in the same request.
  - Familiar shape; no topic provisioning for producers.

- **Cons:**
  - Dual write on the producer side: commit-then-call loses intent on a crash between the two; call-then-commit sends emails for rolled-back transactions. Every producer must build an outbox anyway to be safe — at which point publishing to a topic is simpler than calling an API from the relay.
  - Availability coupling: the service down means producers' requests fail or block.
  - No ordering guarantee between a schedule and its cancel across retries.

- **Why rejected:** loses on reliability of intent and on coupling, the two heaviest criteria; the validation advantage is recovered by the `SCHEDULE_REJECTED` outcome. A REST surface remains for operators and tooling only.

- **Cost:** same infrastructure; the hidden cost is every producer re-implementing reliability.

### The service computes send times from business dates (producer sends a due date and a ladder)

- **What it is:**
  - `SCHEDULE_SENDS` carries `dueDate` and `offsets: [-30, -7, 1]`; the service computes the three timestamps, drops past ones, adds a kickoff — the cycle doc's late-opening rule, moved here.

- **Pros:**
  - Smaller producer payload; ladder logic in one place if many producers use ladders.

- **Cons:**
  - The service now knows what a due date means and encodes one producer's policy (drop past tiers, add kickoff) as everyone's. The next producer's policy differs, and the service grows a per-producer rule set — the exclusion engine again.
  - A change in a producer's ladder policy becomes a change in this service.

- **Why rejected:** violates the boundary rule directly. Absolute timestamps cost the producer three lines of date arithmetic it already performs.

- **Cost:** none in infrastructure; the cost is coupling.

### One Cloud Task per send with a future `scheduleTime` instead of a table sweep

- **What it is:**
  - The consumer creates a named Cloud Task per send at `scheduledAt`; the task's HTTP callback renders and sends; cancel deletes the task by name.

- **Pros:**
  - No sweep job; per-send retry with backoff is queue-native; name-based duplicate refusal.

- **Cons:**
  - Future scheduling is capped at 30 days; the attestation OVERDUE row is created ~31 days ahead and would need a two-hop workaround.
  - No query surface: "what is pending for this tenant" needs a table anyway; cancel-by-key needs a key-to-task-name mapping — a table anyway.
  - No dead-letter queue: an exhausted task is silently deleted.

- **Why rejected:** the horizon cap alone disqualifies it for the first producer; the table it would need anyway is the chosen design.

- **Cost:** Cloud Tasks: first 1M operations/month free, then $0.40/M — ≈ $0 at 71k sends/month; disqualified on capability, not cost.

### SendGrid dynamic templates as the template store

- **What it is:**
  - Templates are created and versioned in SendGrid; the service sends `template_id` + `dynamic_template_data`; rendering happens at SendGrid.

- **Pros:**
  - Visual editor for non-engineers; zero rendering code; the existing send path already passes a SendGrid template id (`CredentialingOutreachService.java:126,134`).

- **Cons:**
  - The rendered content never exists on our side — the audit trail cannot say what was sent, only which template id and variables were passed.
  - Tenant overrides become one SendGrid template per tenant per key, managed in a vendor console outside version control and outside our access model.
  - Provider lock-in: a second provider means re-authoring every template.

- **Why rejected:** fails the audit criterion and the source-agnostic rule; the visual-editor advantage is answered by the admin preview endpoint and, later, an internal editor.

- **Cost:** included in SendGrid plans; disqualified on audit and portability.

### A managed notification platform (Knock, Courier, Novu SaaS)

- **What it is:**
  - Producers trigger workflows on a vendor API; the vendor stores recipients, templates, schedules, cancels by key, sends via SendGrid, and reports outcomes by webhook.

- **Pros:**
  - The exact model this doc adopts, already built: recipient profiles, cancellation keys, idempotency keys, schedules, template versioning, delivery logs, preferences, batching and digests.
  - Fastest time to first email.

- **Cons:**
  - Recipient contact data and variables leave the platform to a third party — a BAA and vendor security review for a healthcare platform; email content is login links only today, but the platform would be one variable away from PHI in a vendor.
  - Producers still need an outbox to reach the vendor reliably; the reliability half of the work remains ours.
  - Per-message pricing at scale, plus a hard dependency on a vendor's availability for compliance reminders.

- **Why rejected:** the data-boundary and vendor-dependency costs outweigh the build savings for a service whose core is a registry, a table, a sweep, and a template engine. Their designs are used here as validation of the model, not as the implementation.

- **Cost (order of magnitude):** vendor list pricing scales per notification — at ~71k emails/month the mid tiers land around $250–$1,000/month before SendGrid, against ≈ $16/month for the chosen design's infrastructure (*Performance and scale*).

### Producers render subject and body; the service only relays

- **What it is:**
  - `SCHEDULE_SENDS` carries a finished subject and HTML body; the service stores, schedules, sends, and reports.

- **Pros:**
  - The service is smaller still; no template store, no variables schema.

- **Cons:**
  - Copy changes need producer deploys; templates scatter across repositories; no single inventory of what the platform sends; per-tenant branding re-implemented by every producer.
  - Rendered bodies in every message payload (larger, and every producer holds branding assets).

- **Why rejected:** the platform decision and the industry consensus both place templates in the notification service; the marginal simplicity is paid for by every producer forever.

- **Cost:** none in infrastructure; the cost is duplicated rendering in every producer.

## Contracts and interfaces

**Where the endpoints live:** the Smart Outreach Service (`outreach-service` Cloud Run). Producer integration is **topic-only**; HTTP endpoints are internal (Cloud Scheduler, SendGrid webhook, the preference page, operator tooling) and protected by a shared-secret header (`X-Outreach-Key`, validated fail-closed, constant-time comparison — the pattern at `SmartOutreachSchedulerResource.java:140-150`), by a signed token (preference page), or by the platform's operator JWT (admin API).

**Reading order:** command envelope → the three commands → outcome envelope and outcome types → three worked producer examples (Attestation Module: registry sync, a ladder with a cancel; roster ingestion: a send-now summary; platform operations: a group send) → internal endpoints → admin API → configuration and flags. Rationale per item: *Approach* (envelope → step 2; `UPSERT_RECIPIENT` → step 3; `SCHEDULE_SENDS` → step 4; `CANCEL_SENDS` → step 5; sweep → step 6; templates → step 7; outcomes → step 8; webhook and preferences → step 9).

### Command envelope — every message on `outreach.commands.v1`

```json
{
  "commandId": "obx-0c1f…",
  "commandType": "SCHEDULE_SENDS",
  "schemaVersion": 1,
  "producer": "attestation-module",
  "tenantId": "org-xyz",
  "correlationId": "task-7f3a…",
  "occurredAt": "2026-09-15T06:00:03Z",
  "payload": { }
}
```

- `commandId` — the producer's unique id for this command (for an outbox producer: the outbox row id). **The dedup key**: a second message with the same `commandId` is acknowledged and ignored.
- `commandType` — `UPSERT_RECIPIENT`, `SCHEDULE_SENDS`, or `CANCEL_SENDS`.
- `schemaVersion` — integer; the consumer rejects unknown versions to the dead-letter topic. The topic name carries the major (`v1`); this field carries additive minors.
- `producer` — the producer's registered name (*Configuration*); must match the publishing service account's registration and the `templateKey` / `recipientId` namespace.
- `tenantId` — always set; the service enforces tenant scoping on every row it creates.
- `correlationId` — opaque to the service; echoed verbatim on every outcome. Producers put their aggregate id here (attestation: the task id; for a recipient upsert: the practitioner id).
- `occurredAt` — producer's timestamp, ISO 8601 UTC. For `UPSERT_RECIPIENT` it is also the last-writer rule: an upsert older than the stored record's `source_occurred_at` is ignored.
- **Pub/Sub message attributes** (duplicated from the body for filtering and ordering): `producer`, `tenantId`, `commandType`; **ordering key** = `payload.recipientId` for `UPSERT_RECIPIENT`, `payload.cancellationKey` for the two send commands.

### `UPSERT_RECIPIENT` — payload

```json
{
  "recipientId": "practitioner:cert-000123",
  "channels": {
    "email": { "address": "dr.smith@clinic.example" }
  },
  "displayName": "Dr. A. Smith",
  "locale": "en-US",
  "timezone": "America/New_York",
  "status": "ACTIVE",
  "groups": ["practitioners", "practitioners:plan-gold"],
  "attributes": { "salutation": "Dr." }
}
```

- `recipientId` — required; producer-chosen, namespaced by the producer's registered recipient namespace (`practitioner:`, `org-contact:`), opaque to the service.
- `channels.email.address` — required in v1 (the only channel); validated syntactically. A changed address resets `email_status` to `OK`.
- `displayName`, `locale` (BCP 47), `timezone` (IANA) — optional; defaults `null`, `en-US`, `UTC`. Available to templates as `recipient.*`.
- `status` — `ACTIVE` or `INACTIVE`. `INACTIVE` = never send; pending sends to this person become `SUPPRESSED` (`cause: INACTIVE`) at their sweep.
- `groups[]` — full membership list; replaces the previous membership. Group keys belong to the producer that maintains membership through its upserts (`practitioners` is maintained by `pdm-platform`, the owner of the `practitioner:` records). Omit the field to leave membership unchanged; send `[]` to clear it.
- `attributes` — free JSON, ≤ 4 KB; stored verbatim; available as `recipient.attributes.*`.
- **Not accepted from producers:** `emailStatus`, `preferences` — those belong to the webhook, the preference page, and operators.

**What the consumer does with it (one transaction):** inbox insert → compare `occurredAt` with the stored `source_occurred_at` (older → audit `RECIPIENT_UPSERT_STALE`, done) → upsert `outreach_recipients` → replace `outreach_recipient_group_members` for this recipient when `groups` is present → audit `RECIPIENT_UPSERTED` → commit → acknowledge. No outcome is published for a plain upsert (the producer already knows); `RECIPIENT_UNRESOLVED` on a later send is the signal that a sync is missing.

### `SCHEDULE_SENDS` — payload

```json
{
  "cancellationKey": "attestation-task:task-7f3a…",
  "category": "compliance-reminder",
  "sends": [
    {
      "sendKey": "attestation-task:task-7f3a…:T-30",
      "templateKey": "attestation.reminder.t30",
      "scheduledAt": "2026-09-15T13:00:00Z",
      "expiresAt": "2026-10-16T00:00:00Z",
      "recipients": {
        "to": [ { "recipientId": "practitioner:cert-000123" } ]
      },
      "variables": {
        "nextAttestationDate": "2026-10-15",
        "portalLink": "https://portal.example/attest/task-7f3a…"
      }
    }
  ]
}
```

- `cancellationKey` — required; groups the sends for cancellation; also the Pub/Sub ordering key. Producer-chosen, opaque here. A producer with no cancel intent sets it to its correlation id.
- `category` — required; one of the configured categories (*Configuration*). Decides whether recipients may opt out.
- `sends[]` — 1 to 50 sends per command (one Spanner transaction; keeps the mutation count small). A `toGroup` send counts as one entry but may expand to many rows.
  - `sendKey` — required, unique per producer (`UNIQUE (producer, send_key)`); deterministic so a replay cannot create a second row. For group sends the service derives `<sendKey>:<recipientId>` per member.
  - `templateKey` — required; must have an `ACTIVE` version and a namespace equal to `producer`'s.
  - `scheduledAt` — required, ISO 8601 UTC. Past values are sent at the next sweep.
  - `expiresAt` — optional; the row becomes `EXPIRED` instead of sending if still pending at this instant. Must be later than `scheduledAt`.
  - `recipients` — exactly one of:
    - `to[]` (1 to 20 `{ recipientId }`), with optional `cc[]`, `bcc[]` of the same shape — every id must exist in this tenant's registry and be `ACTIVE`; or
    - `toGroup` (one group key) — expanded at intake to the group's `ACTIVE` members; `cc`/`bcc` not allowed with a group.
  - `variables` — required object; validated against the template's `variables_schema` at intake. Unknown keys are rejected (a typo must not silently render as an empty field). Recipient-level values (name, locale) are **not** variables — templates read them from `recipient.*`.

**What the consumer does with it** (one transaction per command; group expansion in resumable chunks):

- Insert `outreach_inbox(command_id)`.
- Per send: validate template, namespace, variables, category, and that every recipient exists and is `ACTIVE`.
- Insert the `outreach_sends` rows — or record `SEND_SCHEDULE_DUPLICATE` on a unique-key hit.
- Write one `SCHEDULED` (or `SCHEDULE_REJECTED`) outcome per row to `outreach_outbox`, plus the audit rows; commit; acknowledge.
- For `toGroup`: insert an `outreach_group_expansions` job row in the command transaction and acknowledge; then expand in chunks of 500 members, each chunk its own transaction, the job row tracking the last member processed.

### `CANCEL_SENDS` — payload

```json
{
  "cancellationKey": "attestation-task:task-7f3a…",
  "sendKeys": null,
  "reason": "SUBMITTED"
}
```

- `cancellationKey` — required (ordering key); with `sendKeys = null` cancels every `PENDING` row under the key, including rows produced by a group expansion.
- `sendKeys[]` — optional; restricts the cancel to named rows (must belong to the same `cancellationKey`).
- `reason` — free text ≤ 100 chars, stored on the rows and echoed on outcomes; opaque here.

**What the consumer does with it** (one transaction):

- Inbox insert.
- `UPDATE outreach_sends SET status='CANCELLED', cancelled_at=now, cancel_reason=@reason WHERE producer=@p AND cancellation_key=@k AND status='PENDING'`.
- One `CANCELLED` outcome per flipped row; one `CANCEL_TOO_LATE` outcome per matching row not in `PENDING`, carrying its actual status.
- `CANCEL_NO_MATCH` audit row if nothing matched; commit; acknowledge.
- A cancel arriving while a group expansion is still running also marks the expansion job `CANCELLED`, so members not yet expanded are never created.

### Outcome envelope — every message on `outreach.outcomes.v1`

```json
{
  "outcomeId": "oox-91aa…",
  "outcomeType": "SENT",
  "schemaVersion": 1,
  "producer": "attestation-module",
  "tenantId": "org-xyz",
  "correlationId": "task-7f3a…",
  "cancellationKey": "attestation-task:task-7f3a…",
  "sendKey": "attestation-task:task-7f3a…:T-30",
  "sendId": "snd-5d2e…",
  "recipientId": null,
  "occurredAt": "2026-09-15T13:00:41Z",
  "detail": { }
}
```

- `outcomeId` — the `outreach_outbox` row id; the producer's dedup key.
- `producer`, `tenantId`, `correlationId`, `cancellationKey`, `sendKey` — echoed verbatim from the command; `sendId` is this service's row id (useful in support conversations, never required by producers). Recipient outcomes carry `recipientId` and the registering command's `correlationId`; their send fields are null.
- **Pub/Sub message attributes:** `producer`, `tenantId`, `outcomeType`; **ordering key** = `cancellationKey` (send outcomes) or `recipientId` (recipient outcomes). Producers subscribe with a filter such as `attributes.producer = "attestation-module"`.

| `outcomeType` | Emitted in | `detail` fields |
| --- | --- | --- |
| `SCHEDULED` | the `SCHEDULE_SENDS` transaction (or expansion chunk) | `scheduledAt`, `expiresAt`, `templateKey`, `category`, `recipientIds[]` |
| `SCHEDULE_REJECTED` | the `SCHEDULE_SENDS` transaction | `reasonCode` (`UNKNOWN_TEMPLATE`, `TEMPLATE_NAMESPACE_MISMATCH`, `VARIABLES_INVALID`, `RECIPIENT_UNRESOLVED`, `RECIPIENT_INACTIVE`, `GROUP_UNKNOWN`, `CATEGORY_UNKNOWN`, `SCHEDULE_INVALID`, `TENANT_UNKNOWN`), `message`, `recipientIds[]` (for recipient codes) |
| `CANCELLED` | the `CANCEL_SENDS` transaction | `reason` |
| `CANCEL_TOO_LATE` | the `CANCEL_SENDS` transaction | `currentStatus` (`CLAIMED`, `SENT`, `FAILED`, `EXPIRED`), `sentAt` when `SENT` |
| `SENT` | the sweep's send-success transaction | `providerMessageId`, `templateVersion`, `attempt`, `sentAt`, `resolvedRecipients` (`recipientId` + address per `to`/`cc`/`bcc`) |
| `FAILED` | the sweep's final-failure transaction | `reasonCode` (`PROVIDER_PERMANENT`, `PROVIDER_EXHAUSTED`, `RENDER_ERROR`, `NO_ACTIVE_TEMPLATE`, `TENANT_PAUSED_EXPIRED`), `message`, `attempt` |
| `EXPIRED` | the sweep's claim pass | `expiresAt` |
| `SUPPRESSED` | the sweep, instead of a provider call | `suppressed[]`: `{ recipientId, cause }` with `cause` in `HARD_BOUNCED`, `COMPLAINED`, `MANUAL_SUPPRESSED`, `UNSUBSCRIBED`, `INACTIVE`; `sentToRemaining` (boolean — true when only a `cc`/`bcc` was dropped and the email still went) |
| `DELIVERED` | the webhook transaction | `providerEventAt`, `recipientId` |
| `BOUNCED` | the webhook transaction | `bounceType` (`hard`, `soft`), `providerReason`, `recipientId` |
| `COMPLAINED` | the webhook transaction | `providerEventAt`, `recipientId` |
| `RECIPIENT_ADDRESS_FAILED` | the webhook / admin transaction (to `owner_producer`) | `recipientId`, `emailAddress`, `cause` (`HARD_BOUNCE`, `COMPLAINT`, `MANUAL`), `sourceSendKey` |
| `RECIPIENT_UNSUBSCRIBED` | the preference-page transaction (to `owner_producer`) | `recipientId`, `categories[]` now opted out |

- **Producer obligation:** consume outcomes idempotently on `outcomeId`, process first and acknowledge after, and write their own audit rows from them. The service keeps its own audit regardless (*Audit trail*).

### Worked example 1 — the Attestation Module: registry sync, a three-step reminder ladder, and a cancel

Producer name `attestation-module` (**sender only** — the `practitioner:` registry records are owned and synced by `pdm-platform`, the PDM backend); templates `attestation.reminder.t30`, `attestation.reminder.t7`, `attestation.reminder.overdue`, each with variables schema `{ nextAttestationDate, portalLink }` and default category `compliance-reminder` (mandatory). Every date below is computed by the Attestation Module; this service only stores and obeys them.

0. **Registry sync (once at onboarding, then on every change) — published by `pdm-platform`, not the Attestation Module.** The PDM backend owns practitioner contact data, so it owns the sync: one `UPSERT_RECIPIENT` per active practitioner from the OV's contact fields, republished on every change:

```json
{
  "commandId": "pdm-evt-a1b2…",
  "commandType": "UPSERT_RECIPIENT",
  "schemaVersion": 1,
  "producer": "pdm-platform",
  "tenantId": "org-xyz",
  "correlationId": "cert-000123",
  "occurredAt": "2026-09-10T08:12:00Z",
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

   - The registry row now exists, with `owner_producer = pdm-platform`. The Attestation Module never publishes an upsert — it only sends to the id.

1. **Task opens (cycle doc step 5).** The opening transaction writes an `attestation_outbox` row of type `OUTREACH_SCHEDULE`. The relay publishes it to `outreach.commands.v1` as:

```json
{
  "commandId": "obx-0c1f…",
  "commandType": "SCHEDULE_SENDS",
  "schemaVersion": 1,
  "producer": "attestation-module",
  "tenantId": "org-xyz",
  "correlationId": "task-7f3a…",
  "occurredAt": "2026-09-15T06:00:03Z",
  "payload": {
    "cancellationKey": "attestation-task:task-7f3a…",
    "category": "compliance-reminder",
    "sends": [
      { "sendKey": "attestation-task:task-7f3a…:T-30", "templateKey": "attestation.reminder.t30",
        "scheduledAt": "2026-09-15T13:00:00Z", "expiresAt": "2026-10-16T00:00:00Z",
        "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
        "variables": { "nextAttestationDate": "2026-10-15", "portalLink": "https://…" } },
      { "sendKey": "attestation-task:task-7f3a…:T-7", "templateKey": "attestation.reminder.t7",
        "scheduledAt": "2026-10-08T13:00:00Z", "expiresAt": "2026-10-16T00:00:00Z",
        "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
        "variables": { "nextAttestationDate": "2026-10-15", "portalLink": "https://…" } },
      { "sendKey": "attestation-task:task-7f3a…:OVERDUE", "templateKey": "attestation.reminder.overdue",
        "scheduledAt": "2026-10-16T13:00:00Z", "expiresAt": "2026-11-14T00:00:00Z",
        "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
        "variables": { "nextAttestationDate": "2026-10-15", "portalLink": "https://…" } }
    ]
  }
}
```

   - No address and no name in the message. The Attestation Module already applied its own rules before publishing: tiers whose dates are past were dropped and a kickoff added (cycle doc D5); `expiresAt` is its choice of "no longer worth sending".
   - The service checks the id exists and is `ACTIVE`, creates three `PENDING` rows, and publishes three `SCHEDULED` outcomes. The Attestation Module records `OUTREACH_SCHEDULED` in its audit from the command it sent.

2. **Dr. Smith changes email on 2026-09-12.** The PDM backend publishes another `UPSERT_RECIPIENT` for `practitioner:cert-000123` with the new address. Nothing else happens; the three pending rows still name the id.

3. **Sweep on 2026-09-15 13:00 UTC.** The T-30 row is claimed; the service resolves `practitioner:cert-000123` → **the new address**; renders the tenant's override of `attestation.reminder.t30` (or the default) with `vars.*`, `recipient.displayName`, `tenant.*`; sends; stores `resolved_recipients`; publishes `SENT`. Minutes later SendGrid's webhook posts `delivered` → `DELIVERED` outcome. The Attestation Module's outcome consumer writes `OUTREACH_SENT` into `attestation_audit_events` with `sendKey`, `providerMessageId`, `templateVersion`, and the resolved address.

4. **Practitioner submits on 2026-10-03.** The submission transaction writes an `attestation_outbox` row of type `OUTREACH_CANCEL`; the relay publishes:

```json
{
  "commandId": "obx-77e2…",
  "commandType": "CANCEL_SENDS",
  "schemaVersion": 1,
  "producer": "attestation-module",
  "tenantId": "org-xyz",
  "correlationId": "task-7f3a…",
  "occurredAt": "2026-10-03T15:22:10Z",
  "payload": { "cancellationKey": "attestation-task:task-7f3a…", "sendKeys": null, "reason": "SUBMITTED" }
}
```

   - T-7 and OVERDUE rows are `PENDING` → flipped to `CANCELLED`; two `CANCELLED` outcomes. The T-30 row is `SENT` → one `CANCEL_TOO_LATE` outcome with `currentStatus = SENT` (informational; the Attestation Module already knew).

5. **The race, if the submission had landed at 2026-10-08 13:00:30 UTC:** the T-7 row was claimed at 13:00:05 and sent; the cancel finds it `SENT` → `CANCEL_TOO_LATE`. One extra email; the producer's audit shows exactly why.

6. **A month later the new address hard-bounces on another tenant's send.** The webhook marks `practitioner:cert-000123` `email_status = HARD_BOUNCED` and publishes `RECIPIENT_ADDRESS_FAILED` to `pdm-platform` (the owner), which owns fixing the contact record. Every producer's future sends to that person are `SUPPRESSED` until a new address is upserted — the Attestation Module sees each suppression as a `SUPPRESSED` outcome on its own sends.

7. **The successor obligation** (cycle doc: submission inserts a new `SCHEDULED` row due 2027-01-01) is unrelated to this service until it opens and its own `SCHEDULE_SENDS` arrives with a new `cancellationKey` (`attestation-task:task-9b1c…`).

### Worked example 2 — roster ingestion: an immediate summary email, no cancel

Producer name `roster-ingestion` (hypothetical second producer, illustrating a send-now use with no ladder). It sends to `org-contact:` recipients maintained by that namespace's owner — none is registered today, so this example is illustrative of the extension path; template `roster.ingestion.summary` with variables `{ fileName, rowsAccepted, rowsRejected, reportLink }`; category `service-notice` (optional — contacts may opt out).

```json
{
  "commandId": "ri-evt-4410…",
  "commandType": "SCHEDULE_SENDS",
  "schemaVersion": 1,
  "producer": "roster-ingestion",
  "tenantId": "org-abc",
  "correlationId": "roster-batch-2026-09-15-0007",
  "occurredAt": "2026-09-15T02:14:00Z",
  "payload": {
    "cancellationKey": "roster-batch:roster-batch-2026-09-15-0007",
    "category": "service-notice",
    "sends": [
      { "sendKey": "roster-batch:roster-batch-2026-09-15-0007:summary",
        "templateKey": "roster.ingestion.summary",
        "scheduledAt": "2026-09-15T02:14:00Z",
        "recipients": { "to": [ { "recipientId": "org-contact:ops-11" }, { "recipientId": "org-contact:pm-12" } ] },
        "variables": { "fileName": "roster_20260915.csv", "rowsAccepted": 4812, "rowsRejected": 37, "reportLink": "https://…" } }
    ]
  }
}
```

- `scheduledAt = occurredAt` → sent by the next sweep (within a minute). No `expiresAt`, no cancel expected. If `org-contact:pm-12` opted out of `service-notice`, the email goes to `ops-11` only and the outcome is `SENT` plus a `SUPPRESSED` with `sentToRemaining: true`.
- The service ran the identical code path as example 1: nothing in it knows the difference between a compliance reminder and an ingestion summary.

### Worked example 3 — platform operations: a group send

Producer `platform-ops`; template `platform.maintenance.notice`; category `service-notice`.

```json
{
  "commandId": "ops-2026-09-18-01",
  "commandType": "SCHEDULE_SENDS",
  "schemaVersion": 1,
  "producer": "platform-ops",
  "tenantId": "org-xyz",
  "correlationId": "maint-2026-09-20",
  "occurredAt": "2026-09-18T10:00:00Z",
  "payload": {
    "cancellationKey": "maintenance:2026-09-20",
    "category": "service-notice",
    "sends": [
      { "sendKey": "maintenance:2026-09-20:practitioners",
        "templateKey": "platform.maintenance.notice",
        "scheduledAt": "2026-09-18T14:00:00Z",
        "recipients": { "toGroup": "practitioners" },
        "variables": { "windowStart": "2026-09-20T02:00:00Z", "windowEnd": "2026-09-20T04:00:00Z" } }
    ]
  }
}
```

- The service records the expansion job, acknowledges, and expands `practitioners` in chunks of 500 into rows with `send_key = maintenance:2026-09-20:practitioners:<recipientId>`, all under `cancellationKey = maintenance:2026-09-20`. 70,000 members = 70,000 rows; a crash mid-way resumes from the job row.
- One `CANCEL_SENDS` on `maintenance:2026-09-20` stops all of them, including members not yet expanded.
- Group keys belong to the producer that maintains membership (`pdm-platform` here — it maintains `practitioners` through its upserts); `platform-ops` may send to it because groups are tenant-scoped and readable by any producer — sending to a group is not a privileged act, maintaining it is.

### Producer registration and authorization — how the service decides to trust a message

The service has no login for producers and no per-producer code. Trust is three string comparisons against a registration list, `outreach.producers` (deployment configuration):

```yaml
producers:
  - name: pdm-platform
    serviceAccount: api-layer@certifyos-prod.iam.gserviceaccount.com
    templateNamespace: pdm.          # reserved; sends nothing today — registered as the registry owner
    recipientNamespaces: [ "practitioner:" ]   # the system of record for practitioner contact data
  - name: attestation-module
    serviceAccount: attestation-backend@certifyos-prod.iam.gserviceaccount.com
    templateNamespace: attestation.
    recipientNamespaces: []          # sender only — practitioner: records are pdm-platform's
  - name: roster-ingestion
    serviceAccount: roster-ingestion@certifyos-prod.iam.gserviceaccount.com
    templateNamespace: roster.
    recipientNamespaces: []          # sends to records their owners maintain — sending is not privileged
```

| Check | What is compared | Stops |
| --- | --- | --- |
| 1. **Who published?** | Pub/Sub records the publishing service account on every message; it must be the `serviceAccount` registered for the message's `producer` name | A service claiming to be another producer (`roster-ingestion` publishing `producer: attestation-module`) |
| 2. **Whose template?** | `templateKey` must start with the producer's `templateNamespace` | A producer sending another producer's email templates |
| 3. **Whose contact record?** | On `UPSERT_RECIPIENT`, `recipientId` must start with one of the producer's `recipientNamespaces` | A producer overwriting contact records it does not own |

Plain-language rules that fall out of this:

- **A recipient namespace has exactly one owning producer.** The registration list is validated at load: two producers may never claim the same recipient namespace — otherwise the "whose contact record" check could not name an owner, and `owner_producer` outcome routing (`RECIPIENT_ADDRESS_FAILED`, `RECIPIENT_UNSUBSCRIBED`) would be ambiguous.
- **Sending to a person is not privileged; maintaining the person is.** Any registered producer may name any recipient or group in a tenant in `SCHEDULE_SENDS` — the platform-operations example sends to the `practitioners` group that `pdm-platform` maintains. Only the owning namespace may `UPSERT_RECIPIENT` that person.
- **Publish rights are the outer wall.** Only registered service accounts can publish to `outreach.commands.v1` at all (topic IAM); the three checks are the inner wall that keeps registered producers inside their own lanes.
- **Failures are loud, not silent.** Check 1 failing is a security signal: dead-letter topic plus alert. Checks 2 and 3 failing are contract mistakes: `SCHEDULE_REJECTED (TEMPLATE_NAMESPACE_MISMATCH)` or a dead-lettered upsert, each with an audit row naming the producer.
- **Onboarding a producer is one YAML entry plus topic IAM** — no code, no redeploy of the service beyond configuration.
- **Namespaces are the only "knowledge" the service has about a producer,** and they are prefixes on opaque strings — the boundary rule holds.

### Internal endpoints — Cloud Scheduler, provider, preference page

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/internal/outreach/sweep` | `X-Outreach-Key` | Phase 1: expire, then claim and process due sends (batch 200, lease 5 min). Phase 2: continue any `RUNNING` group expansion. Phase 3: relay `PENDING` outbox rows older than the grace age. Returns `200` (the work runs inline within the call) with `{ claimed, sent, failedTransient, failedFinal, expired, suppressed, expanded, outboxRelayed }`. Every minute. |
| `POST` | `/webhooks/sendgrid/events` | SendGrid signature (public key) | Receives the event webhook batch; persists each event; updates recipient `email_status`; maps to outcomes; `2xx` only after commit, `5xx` on transient failure so SendGrid retries. |
| `GET/POST` | `/p/{signedToken}` | signed token (tenant + recipient + category, 30-day expiry) | Preference page: shows optional categories, saves opt-outs; one-click `List-Unsubscribe-Post` supported. |
| `GET` | `/internal/outreach/ping` | `X-Outreach-Key` | Liveness. |

### Admin and read API — operators and tooling (operator JWT, `outreach:admin` / `outreach:read`)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/admin/sends?tenantId=&correlationId=&cancellationKey=&recipientId=&status=&from=&to=` | Find sends; keyset-paginated; tenant scoping enforced server-side |
| `GET` | `/admin/sends/{sendId}` | One send with its resolved recipients, delivery events, and audit rows, including `rendered_subject` and the stored rendered body |
| `POST` | `/admin/sends/{sendId}/retry` | Operator retry of a `FAILED` send (new attempt, audited, `actor` = user) |
| `GET` | `/admin/recipients?tenantId=&recipientId=&email=&emailStatus=` | Find recipients; "what have we sent this person" via `/admin/sends?recipientId=` |
| `PUT` | `/admin/recipients/{tenantId}/{recipientId}/email-status` | Set `OK` (clear) or `MANUAL_SUPPRESSED`; audited; emits `RECIPIENT_ADDRESS_FAILED` when suppressing |
| `GET/POST` | `/admin/templates`, `/admin/templates/{templateKey}/versions` | List, create a `DRAFT` version (default or per tenant) |
| `POST` | `/admin/templates/{templateKey}/versions/{version}/preview` | Render against sample variables and a sample or real recipient; validates against `variables_schema` |
| `POST` | `/admin/templates/{templateKey}/versions/{version}/activate` | Activate (retires the previous `ACTIVE` for the same tenant scope); refused if it would leave a referenced key with no `ACTIVE` version |
| `GET` | `/admin/tenants/{tenantId}/config` | The tenant's **effective** outreach configuration as this service currently sees it (cached `outreach-config` entry merged with platform defaults) — read-only; edits happen where all tenant configuration is edited |
| `GET` | `/admin/groups?tenantId=` | Group keys and member counts (membership itself is producer-maintained) |
| `POST` | `/admin/dlq/replay` | Republish dead-lettered commands after inspection (audited) |

### Configuration and flags

Two configuration homes, both service-owned. **No feature flags** — the service uses no Flagsmith or other flag system; kill switches are configuration values.

**Deployment configuration** (environment / Secret Manager; not per tenant):

| Key | Lives in | Type | Default | Controls |
| --- | --- | --- | --- | --- |
| `OUTREACH_API_KEY` | Secret Manager | secret string | required | `X-Outreach-Key` for internal endpoints; fail-closed when unset |
| `SENDGRID_API_KEY` | Secret Manager | secret string | required | Provider credential |
| `SENDGRID_WEBHOOK_PUBLIC_KEY` | Secret Manager | string | required | Webhook signature verification |
| `PREFERENCE_TOKEN_KEY` | Secret Manager | secret string | required | Signs preference-page tokens |
| `outreach.sweep.batchSize` | deployment config | integer | 200 | Rows claimed per sweep call |
| `outreach.sweep.leaseSeconds` | deployment config | integer | 300 | Claim lease; expired leases are reclaimed |
| `outreach.send.maxAttempts` | deployment config | integer | 5 | Attempts before `FAILED` (`PROVIDER_EXHAUSTED`) |
| `outreach.send.backoffSeconds` | deployment config | integer array | `[60, 300, 900, 1800]` | Delay before attempts 2–5 |
| `outreach.expansion.chunkSize` | deployment config | integer | 500 | Members per group-expansion transaction |
| `outreach.outbox.relayGraceSeconds` | deployment config | integer | 120 | Age before the relay republishes a `PENDING` outbox row |
| `outreach.globalPaused` | deployment config | boolean | `false` | Platform-wide kill switch: sweep claims nothing; commands still consumed and stored |
| `outreach.producers` | deployment config (YAML) | list | — | Registered producers: `name`, publishing service account, permitted `templateKey` namespace, permitted `recipientId` namespaces |
| `outreach.categories` | deployment config (YAML) | list | `compliance-reminder (mandatory)`, `service-notice (optional)`, `digest (optional)` | The category list and which may be opted out of |
| Pub/Sub: retention, dead-letter attempts | subscription config (DevOps) | — | ≥ 7 days; 5 attempts | Command subscription behavior |

**Tenant configuration** — one `tenant_configurations` entry per tenant, type **`outreach-config`** (the platform's existing pattern; the attestation module keeps its keys in a sibling entry, `attestation-module-config`). This service **reads** it through the platform's existing tenant-configuration API and **never writes** it; edits happen where all tenant configuration is edited today. The consolidated JSON schema for the entry is owned here.

| Key | Lives in | Type | Default | Controls |
| --- | --- | --- | --- | --- |
| `fromAddress`, `fromName` | `outreach-config` | string | platform default sender | Sender identity |
| `replyToAddress` | `outreach-config` | string | none | Reply-To header |
| `brandLogoUrl`, `brandPrimaryColor` | `outreach-config` | string | platform defaults | Rendering variables available to every template as `tenant.*` |
| `outreachPaused` | `outreach-config` | boolean | `false` | Per-tenant kill switch: the sweep skips the tenant's rows (they stay `PENDING`; `expiresAt` still applies — a paused row that expires fails with `TENANT_PAUSED_EXPIRED`) |
| `dailySendCap` | `outreach-config` | integer | none | Optional safety cap per tenant per UTC day; excess rows wait for the next day (audited) |

How the entry is read:

- Fetched per tenant on first use and cached for 60 seconds; the sweep reads the cache, so a configuration change takes effect within a minute.
- A tenant with no `outreach-config` entry inherits every platform default; sends are not refused for a missing entry (`TENANT_UNKNOWN` applies only to tenant ids the platform does not know at all, checked at intake).
- If the configuration API is unreachable and the cache is empty for a tenant, that tenant's due rows are **skipped this sweep** (they stay `PENDING`) and an alert fires — the service never sends with guessed sender identity. Full handling: *Failure modes*.

## Data model and migration

- **Where everything lives:** the service's own Spanner database **`outreach-db`** on the existing app-data instance (no new instance; Spanner bills the instance). Migrations are service-owned (Liquibase, the platform's tool). Nothing is added to any other database.

- **Tables this service creates** (all in `outreach-db`): `outreach_inbox`, `outreach_recipients`, `outreach_recipient_group_members`, `outreach_sends`, `outreach_send_recipients` (pending-send recipient index), `outreach_group_expansions`, `outreach_outbox`, `outreach_templates`, `outreach_delivery_events`, `outreach_audit_events`.

- **Tables it interacts with, never owns:** `tenant_configurations` (platform database) — **read-only, through the platform's existing configuration API**, for the tenant's `outreach-config` entry and for tenant existence at intake; never written, never queried directly. Nothing else: it does not read or write `scheduled_outreaches` or any `smart_outreach_*` table.

- **`outreach_inbox`** — command dedup. `command_id` (PK), `producer`, `command_type`, `tenant_id`, `received_at`, `result` (`APPLIED`, `REJECTED`, `DUPLICATE`, `STALE`). Retained 30 days (a duplicate older than the subscription retention cannot arrive).

- **`outreach_recipients`** — the registry.
  - Columns: `tenant_id`, `recipient_id`, `owner_producer`, `email_address` (normalized lower-case), `email_status` (`OK`, `HARD_BOUNCED`, `COMPLAINED`, `MANUAL_SUPPRESSED`), `email_status_changed_at`, `email_status_source_send_id`, `display_name`, `locale`, `timezone`, `status` (`ACTIVE`, `INACTIVE`), `attributes` (JSON), `preferences` (JSON: `unsubscribedCategories[]`), `source_occurred_at` (the producer's `occurredAt` of the last applied upsert), `created_at`, `updated_at`.
  - PK `(tenant_id, recipient_id)`. Index `(tenant_id, email_address)` — webhook and support lookups.
  - Write rules: producers (via `UPSERT_RECIPIENT`) write everything except `email_status*` and `preferences`; the webhook and admin API write `email_status*`; the preference page writes `preferences`. Enforced in code paths, audited on every change.

- **`outreach_recipient_group_members`** — `tenant_id`, `group_key`, `recipient_id`. PK `(tenant_id, group_key, recipient_id)`; index `(tenant_id, recipient_id)` for membership replacement. Group keys are namespaced by the maintaining producer.

- **`outreach_sends`** — the queue and the record of every send.
  - Columns, by group:
    - Identity and keys: `id` (UUID PK), `tenant_id`, `producer`, `cancellation_key`, `send_key`, `correlation_id`, `expansion_id` (nullable — the group expansion that created the row).
    - What to send: `template_key`, `template_version` (stamped at send), `category`, `channel` (`EMAIL`), `recipients` (JSON of ids: `to`/`cc`/`bcc`), `variables` (JSON).
    - When: `scheduled_at`, `expires_at` (nullable), `next_attempt_at`.
    - Lifecycle: `status`, `attempt`, `claimed_at`, `lease_until`, `sent_at`, `cancelled_at`, `cancel_reason`, `failure_code`, `failure_message`.
    - Provider and audit: `provider`, `provider_message_id`, `resolved_recipients` (JSON: id + address + name actually used, stamped at send), `rendered_subject`, `rendered_body` (the sent body), `rendered_body_hash`.
    - Timestamps: `created_at`, `updated_at`.
  - **`UNIQUE (producer, send_key)`** — the idempotency backbone.
  - Indexes: `(status, scheduled_at)` — the sweep; `(producer, cancellation_key, status)` — cancels; `(tenant_id, correlation_id)` — lookups; `(provider_message_id)` — webhook matching; `(status, lease_until)` — lease reclamation. Sends by recipient are found through `resolved_recipients` for sent rows and a small `outreach_send_recipients (tenant_id, recipient_id, send_id)` index table for pending rows (part of the same migration).
  - **States:** `PENDING → CLAIMED → SENT` (happy path); `PENDING → CANCELLED`; `PENDING → EXPIRED`; `CLAIMED → PENDING` (transient failure, backoff); `CLAIMED → FAILED`; `CLAIMED → SUPPRESSED`. `DELIVERED` / `BOUNCED` are **not** row states — they are delivery events on a `SENT` row. Every transition is a conditional update on the expected prior state.

- **`outreach_group_expansions`** — `id`, `command_id`, `tenant_id`, `producer`, `group_key`, `send_template` (the send entry to clone, JSON), `cancellation_key`, `status` (`RUNNING`, `COMPLETED`, `CANCELLED`), `last_recipient_id` (resume cursor), `members_total`, `members_created`, `created_at`, `updated_at`.

- **`outreach_outbox`** — the service's outcome outbox. `id` (UUID PK; doubles as `outcomeId`), `outcome_type`, `producer`, `tenant_id`, `send_id` (nullable), `recipient_id` (nullable), `ordering_key`, `payload` (JSON), `status` (`PENDING`, `PUBLISHED`), `attempts`, `created_at`, `published_at`. Index `(status, created_at)`. Rows kept for correlation (retention with the audit table).

- **`outreach_templates`** — the template store.
  - Columns: `template_key`, `tenant_id` (`'*'` = default), `version` (integer), `channel`, `subject_template`, `body_template`, `variables_schema` (JSON Schema), `default_category`, `status` (`DRAFT`, `ACTIVE`, `RETIRED`), `created_at`, `activated_at`, `retired_at`, `created_by`.
  - PK `(template_key, tenant_id, version)`. "One `ACTIVE` per `(template_key, tenant_id)`" is enforced by a conditional transaction in the activate endpoint (Spanner has no partial unique index). Rows are never deleted.

- **`outreach_delivery_events`** — append-only provider events. `id`, `send_id`, `recipient_id` (matched from the event's address against `resolved_recipients`), `provider`, `provider_event_id`, `event_type` (`processed`, `delivered`, `deferred`, `bounce`, `dropped`, `open`, `click`, `spamreport`, `unsubscribe`), `provider_event_at`, `raw` (JSON), `received_at`. `UNIQUE (provider, provider_event_id)` — webhook redelivery is a no-op.

- **`outreach_audit_events`** — *Audit trail*.

- **What a send row looks like across time** (T-30 row from example 1):

  Created by the consumer (ids only):

```json
{ "id": "snd-5d2e…", "tenantId": "org-xyz", "producer": "attestation-module",
  "cancellationKey": "attestation-task:task-7f3a…", "sendKey": "attestation-task:task-7f3a…:T-30",
  "correlationId": "task-7f3a…", "templateKey": "attestation.reminder.t30", "templateVersion": null,
  "category": "compliance-reminder",
  "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
  "scheduledAt": "2026-09-15T13:00:00Z", "expiresAt": "2026-10-16T00:00:00Z",
  "status": "PENDING", "attempt": 0, "providerMessageId": null, "resolvedRecipients": null }
```

  After the sweep (ids plus what was actually used):

```json
{ "id": "snd-5d2e…", "status": "SENT", "attempt": 1, "templateVersion": 3,
  "sentAt": "2026-09-15T13:00:41Z", "provider": "sendgrid", "providerMessageId": "sg-14f0…",
  "resolvedRecipients": {
    "to": [ { "recipientId": "practitioner:cert-000123", "email": "dr.smith@newclinic.example", "name": "Dr. A. Smith" } ] },
  "renderedSubject": "Action needed: attest your directory information by 2026-10-15",
  "renderedBodyHash": "sha256:…" }
```

- **Why the rendered body is stored, not re-rendered on demand:**
  - "What exactly did we send this person?" must be a lookup, not a reconstruction. Re-rendering from template version + variables reproduces the email only while the renderer, its helpers, and the branding assets stay byte-identical — a promise no codebase keeps for seven years.
  - Notification platforms store rendered content (Knock, Courier, Novu all do); they keep it for weeks because their customers are not regulated. Attestation is a compliance workflow, so the platform's audit retention applies.
  - Cost is noise: ≈ 3.4 GB/year at $0.30/GB/month ≈ $1/month, growing $1/month per year.
  - The body stays in `outreach-db` for now. Moving bodies older than a year to cheaper storage, with the hash kept on the row as proof, is a later decision that changes no contract.

- **Retention:** `outreach_sends`, `outreach_delivery_events`, `outreach_outbox`, and `outreach_audit_events` are retained 7 years (the platform's audit retention, D2-20, applies to anything a producer's compliance trail depends on); `rendered_body` is kept with the row, with cheaper storage for old bodies as a later option. `outreach_recipients` rows are kept while `ACTIVE` or referenced by a send in retention; an `INACTIVE` row with no references may be purged after 7 years. `outreach_inbox` 30 days.

## Security, privacy, and access

- **Producer authorization — three checks, spelled out in *Contracts → Producer registration and authorization*.** Summary:
- **Topic permissions are the outer wall.** `outreach.commands.v1`: publish rights granted per producer service account; the consumer verifies the message's `producer` field against the publishing account's registration (`outreach.producers`) — a service cannot publish as another producer. `outreach.outcomes.v1`: publish rights only the outreach service; each producer gets its own filtered subscription with subscribe rights on its own service account.
- **Namespace checks:** `templateKey` prefix must equal the producer's template namespace; `recipientId` prefix must be one of the producer's registered recipient namespaces — a producer cannot overwrite another producer's contact records or send another producer's templates. Any producer may *send to* any recipient or group in a tenant (sending is not privileged); only the owning producer may *update* the record.
- **Tenant isolation server-side on every path:** every row carries `tenant_id`; every query, sweep batch, expansion, admin read, and outcome carries and filters by it. Admin reads require the operator's tenant scope or a platform-admin role. A recipient id is scoped to a tenant; the same practitioner under two tenants is two registry rows.
- **Contact data at rest:** names and addresses live in `outreach_recipients` (and, for sent rows, in `resolved_recipients`), encrypted at rest by Spanner; access is through the admin API only, audited per read of contact fields. Send commands carry ids, not addresses; the only command carrying an address is `UPSERT_RECIPIENT`. Pub/Sub topics use Google-managed encryption at rest; retention ≤ 7 days.
- **No PHI in templates, variables, or attributes** is the platform rule; the variables schema and attribute usage are reviewed when a template is registered. The attestation templates carry a login link, a display name, and a date only.
- **Rendered bodies stored** for audit contain the same fields; access through the admin API only, audited.
- **Preference page:** tokens are signed (`PREFERENCE_TOKEN_KEY`), bound to tenant + recipient + category, expire in 30 days, and reveal nothing but the category list; a forged or expired token gets a generic error.
- **Webhook:** SendGrid event signatures verified with the configured public key (existing pattern); unsigned or invalid → `401`, logged.
- **Internal endpoints:** `X-Outreach-Key` from Secret Manager, fail-closed, constant-time comparison; Cloud Scheduler is the only caller. The secret is never a plaintext Terraform literal in any environment, feature environments included.
- **Provider credential** in Secret Manager, read at startup, never logged.
- **No producer data** is ever read: the service holds no credentials to any other service's database or API beyond tenant-existence lookup. The registry is filled only by what producers push.

## Performance and scale

| #   | Item | Value / assumption |
| --- | --- | --- |
| N1  | First producer volume | Attestation: ~780 openings/day × 3 tiers → **~2,300 sends/day ≈ 71k/month**; ~780 schedule + ~780 cancel commands/day |
| N2  | Registry | ~70,000 practitioners + a few thousand organization contacts per platform; initial load 70k upserts once; steady state ≪ 1k upserts/day |
| N3  | Design target | **50,000 sends/day** (≈ 20× N1) with no design change; sweep at 200 rows/minute sustains 288k/day |
| N4  | Group expansion | 70k members at 500/chunk ≈ 140 transactions, a few minutes; never blocks the sweep |
| N5  | Sweep latency | A send due at minute *m* leaves within minute *m+1* under normal load; recipient resolution is one indexed read per id |
| N6  | Command latency | Schedule visible as a `PENDING` row within seconds of publish (outbox relay adds ≤ 2 minutes in the producer's worst case) |
| N7  | Idempotency | Redelivered command, replayed outbox, overlapping sweep, provider retry, out-of-order upsert: zero duplicate emails and no reverted address by construction |
| N8  | Audit | Every event in *Audit trail*, same transaction as the change, 7 years |
| N9  | Tenant isolation | Server-side for every query, sweep, expansion, outcome, admin read |
| N10 | Storage | ~71k send rows/month × ~4 KB ≈ 280 MB/month ≈ 3.4 GB/year; registry ≈ 70k × 1 KB ≈ 70 MB |

**Cost ≈ $16/month at N1, derived:**

- Cloud Run: sweep ≈ 1 call/minute × ~2 s vCPU ≈ 86k vCPU-seconds/month × $0.000024 ≈ **$2**; consumer, webhook, and preference-page traffic add < **$3**.
- Spanner: no new instance; storage 3.5 GB/year × $0.30/GB/month ≈ **$1/month in year one**, growing ≈ $1/month per year.
- Pub/Sub: ~6k messages/day × ~2 KB ≈ 360 MB/month (initial registry load ≈ 70 MB once) — inside the 10 GB free tier → **$0**.
- Cloud Scheduler: 1 job → **$0.10** (3 free per account; counted conservatively).
- Secret Manager: 4 secrets × $0.06 ≈ **$0.25**.
- SendGrid: ~71k sends/month on the platform's existing plan; if headroom runs out the next tier ≈ **$90/month** (the same open item the cycle doc carries).

## Observability

Telemetry is best-effort and never blocks the workflow — metrics and logs are emitted outside database transactions.

**Metrics** (low-cardinality labels: tenant and producer yes, recipient never):

```
outreach.command.received                counter  {producer, commandType, result}
outreach.command.rejected                counter  {producer, reasonCode}
outreach.recipient.upserted              counter  {producer, tenant}
outreach.recipient.stale_ignored         counter  {producer}
outreach.recipient.unresolved            counter  {producer}
outreach.recipients.active               gauge    {tenant}
outreach.recipients.address_failed       gauge    {tenant}
outreach.sends.pending.depth             gauge    {producer}
outreach.sends.oldest_due.age            gauge    {producer}
outreach.expansion.running               gauge
outreach.sweep.run.duration              timer    {outcome}
outreach.send.sent                       counter  {producer, tenant, templateKey}
outreach.send.failed                     counter  {producer, tenant, reasonCode}
outreach.send.retried                    counter  {producer, attempt}
outreach.send.cancelled                  counter  {producer}
outreach.send.cancel_too_late            counter  {producer}
outreach.send.expired                    counter  {producer}
outreach.send.suppressed                 counter  {tenant, cause}
outreach.provider.latency                timer    {provider, outcome}
outreach.delivery.event                  counter  {provider, eventType}
outreach.preference.optout               counter  {tenant, category}
outreach.outbox.pending.depth            gauge
outreach.outbox.oldest_pending.age       gauge
outreach.webhook.unmatched               counter  {provider}
outreach.config.unavailable              counter  {tenant}
outreach.dlq.depth                       gauge    {topic}
```

**Alerts:**

- **Sweep liveness (the important one):** no `SWEEP_COMPLETED` audit event in 5 minutes → page. Absence-based.
- `sends.oldest_due.age` > 10 minutes — due rows not being processed (sweep failing, tenant volume spike, provider outage).
- `send.failed` rate > 5% per producer over 15 minutes; any `PROVIDER_EXHAUSTED` burst.
- `recipient.unresolved` > 0 for a producer in a new deployment window — its registry sync is behind or broken.
- `recipient.stale_ignored` rising — a producer is replaying an old backlog; harmless, but investigate.
- `recipients.address_failed` / `recipients.active` > 2% for a tenant — contact data quality problem at the source.
- `provider.latency` p95 > 5 s or provider 5xx rate > 10% — provider incident.
- `command.rejected` > 0 for a producer in a new deployment window — a contract mismatch shipped.
- `outbox.oldest_pending.age` > 5 minutes — dispatcher and relay both failing.
- `dlq.depth` > 0 on either topic; command subscription oldest-unacked > 30 minutes — consumer down.
- `webhook.unmatched` > 50/hour — webhook misconfiguration or a provider-side id change.
- `config.unavailable` > 0 for 5 minutes — tenant configuration API down; sends for uncached tenants are delayed.
- `cancel_too_late` rate rising for a producer — its cancels lag its sends; a producer-side relay problem surfacing here.
- `expansion.running` > 0 for more than 30 minutes — a stuck group expansion.
- `globalPaused` or any tenant `outreachPaused` engaged > 24 hours.
- **Daily invariant** — must return empty: (a) rows `CLAIMED` with `lease_until` more than 1 hour past; (b) `SENT` rows with no delivery event after 48 hours (webhook gap); (c) outbox rows `PENDING` older than 1 hour; (d) `PENDING` rows whose every `to` recipient is `INACTIVE` or address-failed (they will suppress at due time — flag early).

**Correlation:** `commandId` → `sendId` → `outcomeId` → `providerMessageId`, plus the producer's `correlationId` and `recipientId` on every row, log line, and outcome. A support question ("did Dr. Smith get the T-7?") is one admin query by `recipientId` or `correlationId`.

## Audit trail

*Added section — the service is the system of record for what the platform sent, to whom, and what it knows about each person's address.*

**Audit table: `outreach_audit_events`** — created by this service, in `outreach-db`. Every event below is one row. The service writes to no other audit table; producers write their own audit from outcome events.

Rules: append-only; same transaction as the change; 7-year retention; every event carries `producer`, `tenant_id`, and the correlation ids.

- **`send_id`, `recipient_id`, `command_id`, and `correlation_id` are first-class, indexed columns**, not fields inside a JSON blob.

### The common envelope — every event carries these fields

```json
{
  "id": "oae-2c9d…",
  "type": "SEND_SENT",
  "producer": "attestation-module",
  "tenantId": "org-xyz",
  "commandId": "obx-0c1f…",
  "sendId": "snd-5d2e…",
  "recipientId": null,
  "correlationId": "task-7f3a…",
  "cancellationKey": "attestation-task:task-7f3a…",
  "sendKey": "attestation-task:task-7f3a…:T-30",
  "actor": "system:outreach-sweep",
  "occurredAt": "2026-09-15T13:00:41Z",
  "detail": { }
}
```

- `commandId` — null on events not caused by a command (sweep, webhook, preference page, admin actions).
- `sendId` / `sendKey` / `cancellationKey` — null on recipient-level, run-level (`SWEEP_*`), template, and tenant admin events. `recipientId` — set on recipient-level events and on per-recipient send events (`DELIVERY_EVENT_RECEIVED`, `SEND_SUPPRESSED`).
- `actor` — `system:outreach-consumer`, `system:outreach-sweep`, `system:outreach-webhook`, `recipient:self` (preference page), or an operator user id for admin actions.

### Per-event contracts — the `detail` fields and where each commits

| Event | Committed in | `detail` fields |
| --- | --- | --- |
| `COMMAND_RECEIVED` | the command transaction | `commandType`, `sendCount`, `result` (`APPLIED`, `REJECTED`, `DUPLICATE`, `STALE`) |
| `RECIPIENT_UPSERTED` | the `UPSERT_RECIPIENT` transaction | `created` (boolean), `changedFields[]`, `addressChanged` (boolean — resets `emailStatus`), `groupsReplaced` (boolean) |
| `RECIPIENT_UPSERT_STALE` | the `UPSERT_RECIPIENT` transaction | `commandOccurredAt`, `storedOccurredAt` |
| `RECIPIENT_STATUS_CHANGED` | the `UPSERT_RECIPIENT` transaction | `from`, `to` (`ACTIVE`/`INACTIVE`) |
| `RECIPIENT_EMAIL_STATUS_CHANGED` | the webhook / admin transaction | `from`, `to`, `cause`, `sourceSendId`, `emailAddress` |
| `RECIPIENT_PREFERENCE_CHANGED` | the preference-page transaction | `categoriesBefore[]`, `categoriesAfter[]`, `viaOneClick` |
| `SEND_SCHEDULED` | the `SCHEDULE_SENDS` transaction / expansion chunk | `templateKey`, `category`, `scheduledAt`, `expiresAt`, `recipientIds[]`, `expansionId` |
| `SEND_SCHEDULE_REJECTED` | the `SCHEDULE_SENDS` transaction | `reasonCode`, `message`, `field`, `recipientIds[]` |
| `SEND_SCHEDULE_DUPLICATE` | the `SCHEDULE_SENDS` transaction | `existingSendId`, `existingStatus` |
| `GROUP_EXPANSION_STARTED` / `PROGRESSED` / `COMPLETED` / `CANCELLED` | the command transaction / each chunk / the final chunk / the cancel | `expansionId`, `groupKey`, `membersTotal`, `membersCreated`, `lastRecipientId` |
| `SEND_CANCELLED` | the `CANCEL_SENDS` transaction | `reason` |
| `CANCEL_TOO_LATE` | the `CANCEL_SENDS` transaction | `currentStatus` |
| `CANCEL_NO_MATCH` | the `CANCEL_SENDS` transaction | `cancellationKey`, `sendKeys` |
| `SWEEP_STARTED` / `SWEEP_COMPLETED` | their own writes | `claimed`, `sent`, `failedTransient`, `failedFinal`, `expired`, `suppressed`, `expanded`, `outboxRelayed`, `durationMs` |
| `SEND_CLAIMED` | the claim transaction | `attempt`, `leaseUntil` |
| `SEND_SENT` | the send-success transaction | `templateVersion`, `provider`, `providerMessageId`, `resolvedRecipients`, `renderedSubject`, `renderedBodyHash`, `attempt` |
| `SEND_RETRY_SCHEDULED` | the transient-failure transaction | `attempt`, `nextAttemptAt`, `providerError` |
| `SEND_FAILED` | the final-failure transaction | `reasonCode`, `message`, `attempt` |
| `SEND_EXPIRED` | the claim pass | `expiresAt` |
| `SEND_SUPPRESSED` | the sweep, per dropped recipient | `recipientId`, `cause`, `sentToRemaining` |
| `DELIVERY_EVENT_RECEIVED` | the webhook transaction | `eventType`, `providerEventId`, `providerEventAt`, `recipientId`, `mappedOutcome` (nullable) |
| `OUTCOME_PUBLISHED` | the dispatcher's mark transaction | `outcomeId`, `outcomeType`, `attempts` |
| `TEMPLATE_VERSION_CREATED` / `ACTIVATED` / `RETIRED` | the admin transaction | `templateKey`, `tenantScope`, `version`, `previousActiveVersion` |
| `OPERATOR_RETRY` | the admin transaction | `sendId`, `previousStatus` |
| `DLQ_REPLAYED` | the admin transaction | `topic`, `messageIds[]` |
| `CONTACT_DATA_READ` | its own write | `recipientId` or `sendId`, fields read, operator id |

- "Committed in" is a contract: events listed with a transaction are written inside it — if the change rolls back, so does its event.
- An engineer implementing from this table should never have to invent an audit field. A missing field is a gap in this doc — fix it here first.

Questions this answers directly: "what exactly did we send Dr. Smith on Sept 15, and to which address?" (`SEND_SENT` with `resolvedRecipients` + stored body) · "why did no T-7 go out?" (`SEND_CANCELLED` with `reason`, or `SEND_EXPIRED`, or `SEND_SUPPRESSED` with `cause`) · "did the producer's cancel arrive in time?" (`CANCEL_TOO_LATE`) · "when did this address start failing, and who was told?" (`RECIPIENT_EMAIL_STATUS_CHANGED` + `OUTCOME_PUBLISHED`) · "who changed the template and when?" (`TEMPLATE_VERSION_ACTIVATED`).

## Failure modes and rollback

**Command path — three failure points, three answers:**

1. **Producer's message never arrives.** The producer's problem by design (its outbox and relay); this service cannot know about intent it never received. Mitigation on our side: none needed; on the producer's side: the attestation module proves gaps from its own intent-vs-confirmation audit events and re-emits (an audited ops action), and this service's `UNIQUE (producer, send_key)` and last-writer upserts make re-emission safe.
2. **Consumer crashes mid-transaction.** Inbox row and effects roll back together; Pub/Sub redelivers; the retry applies cleanly. Process-then-ack is the rule.
3. **Message is malformed or unknown-version.** Deterministic rejection → dead-letter topic after the bounded attempts → `dlq.depth` alert → operator inspects and replays or discards (audited).

**Registry path:**

1. **Producer's sync lags or is missing.** A send naming an unknown id is rejected per send (`RECIPIENT_UNRESOLVED`), immediately visible to the producer as an outcome and to operators as a metric. The rest of the command proceeds.
2. **Out-of-order upserts** (a replayed backlog after a fresh change). Last-writer by `occurredAt`: the older command is recorded `STALE` and ignored; the fresh address stands.
3. **Registry address is wrong.** Exactly as wrong as a wrong address in a payload would have been — but fixable with one upsert for every pending send at once, and detected by the bounce loop (`RECIPIENT_ADDRESS_FAILED`) rather than silently.
4. **Initial load partially fails.** Upserts are independent and idempotent; rerun the load; `RECIPIENT_UPSERTED` counts show progress; unresolved sends in the meantime are rejected, not lost.

**Sweep path:**

1. **Sweep endpoint down.** Rows stay `PENDING`; `oldest_due.age` and liveness alerts fire; sends resume when the sweep does. Delay, not loss.
2. **Instance dies after claim, before send.** Lease expires (5 min) → reclaimed → sent. Nothing recorded, nothing sent — safe.
3. **Instance dies after the provider accepted, before the row update.** Lease expires → reclaimed → provider called again with the same idempotency key (`send_key`): SendGrid does not offer a native idempotency key today, so the second call could produce a second email — the one genuine duplicate window. Mitigation: the send transaction commits the `SENT` state immediately after the provider returns (milliseconds); the window is a process kill inside that gap. The webhook shows two `delivered` events for one `send_key`, alerted as `duplicate_delivery`. Accepted as rare and flagged openly here. **Closing it fully needs a provider whose send API accepts an idempotency key** — SendGrid's does not today; Resend's does (`Idempotency-Key` header, deduplicated for 24 hours), and a second adapter is a configuration change under the `EmailProvider` interface. Tracked as an open question.
4. **Poison row** (render error). Fails only itself: `FAILED` with `RENDER_ERROR` on the first attempt (render errors are permanent), outcome published, batch continues.
5. **Provider outage.** Every attempt transient → backoff → `provider.failed` alert → resumes with the provider. Rows exceeding `maxAttempts` during a long outage become `FAILED` (`PROVIDER_EXHAUSTED`); the operator retry endpoint re-queues them in bulk once the provider is back (audited).
6. **Group expansion crashes mid-way.** The job row holds the resume cursor; the next sweep continues; already-created rows are protected by the unique send key.
7. **Tenant configuration API unreachable.** The 60-second cache serves recent tenants; a tenant with no cached entry has its due rows skipped this sweep (still `PENDING`, delayed one minute per retry) and `config.unavailable` alerts. The service never sends with a guessed sender identity.

**Outcome path:** dispatcher failure → relay republishes within the grace age; producer down → its subscription holds messages; duplicate outcome (crash between publish and mark) → the producer's `outcomeId` dedup absorbs it.

**Edge cases:**

| Case | Handling |
| --- | --- |
| Same `SCHEDULE_SENDS` delivered twice | Inbox key → acknowledged, no effect; audited `DUPLICATE` |
| Same `sendKey` in two different commands | Unique key → second is `SEND_SCHEDULE_DUPLICATE`, other sends in the command proceed |
| Send names an unknown `recipientId` | `SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)` for that send only; producer fixes its sync and re-sends |
| Send names an `INACTIVE` recipient | `SCHEDULE_REJECTED (RECIPIENT_INACTIVE)` at intake; if deactivated after scheduling → `SUPPRESSED (INACTIVE)` at the sweep |
| Two producers upsert the same `recipientId` | Refused by namespace: only the owning producer's namespace may write it; the other is dead-lettered with a security alert |
| Upsert older than the stored record | Ignored, `STALE`; no address reverted |
| Address changes between schedule and send | New address used — resolution is at send time |
| Recipient hard-bounced or complained | `SUPPRESSED` for that recipient on every producer's future sends; owner producer told via `RECIPIENT_ADDRESS_FAILED`; cleared by a new address (upsert) or by an operator |
| Recipient opted out of an optional category | `SUPPRESSED (UNSUBSCRIBED)` for sends in that category; mandatory categories unaffected |
| Only a `cc` recipient is suppressed | Email still goes to `to`; `SUPPRESSED` outcome carries `sentToRemaining: true` |
| Cancel arrives before its schedule | Impossible under the ordering key; if seen (producer bug), `CANCEL_NO_MATCH` audited; the later schedule creates rows normally |
| Cancel and sweep race on the same minute | Claimed row cannot be cancelled → `CANCEL_TOO_LATE`; one extra email, worst case |
| Cancel during a running group expansion | Expansion job marked `CANCELLED`; created rows cancelled; uncreated members never created |
| `scheduledAt` in the past | Sent at the next sweep; the producer decided to send it |
| `expiresAt` earlier than `scheduledAt` | Rejected at intake (`SCHEDULE_INVALID`) |
| Template retired while rows reference it | Refused by the activate/retire endpoint; if forced (platform default retired), rows fail with `NO_ACTIVE_TEMPLATE` and the producer sees `FAILED` |
| Variables valid at intake, template changed before send | The `ACTIVE` version at send time renders; a new version must keep its `variables_schema` backward compatible (activate endpoint diff-checks required fields) |
| Tenant paused for weeks | Rows wait; those with `expiresAt` become `FAILED` (`TENANT_PAUSED_EXPIRED`) so the producer learns they never went; alert on pause > 24 h |
| Tenant daily cap reached | Remaining rows wait until the next UTC day; audited; alert |
| Tenant configuration API down | Cached `outreach-config` used; uncached tenants skipped this sweep (rows stay `PENDING`); alert |
| Webhook event for unknown message id | Stored unmatched; alert at volume; never dropped |
| Webhook redelivery | `UNIQUE (provider, provider_event_id)` → no-op |
| 50k sends due in one minute | Sweep claims 200/minute per call; Cloud Scheduler can be raised to several calls/minute or the batch size raised — both configuration; backlog visible in `oldest_due.age` |
| Producer publishes with another producer's name | Publishing service account does not match registration → rejected to the dead-letter topic; security alert |
| Producer sends a `templateKey` outside its namespace | `SCHEDULE_REJECTED (TEMPLATE_NAMESPACE_MISMATCH)` |

**Rollback:**

- **Per-tenant kill switch** (`outreachPaused`): the sweep skips the tenant immediately; commands are still consumed and stored (registry updates included), so nothing is lost while paused. No deployment.
- **Platform kill switch** (`globalPaused`): the sweep claims nothing; commands still land as rows.
- **Full rollback** (service removed): stop the sweep job; pending rows and the registry stay in `outreach-db`; the command subscription retains messages ≥ 7 days; producers' outboxes retain intent indefinitely. Re-enabling drains in order. Emails already sent cannot be recalled.
- Schema is additive-only; schema rollback never required.
- The existing `smart_outreach` engine is unaffected in every scenario — it was never touched.

## Rollout

1. **Repository and service skeleton:** `outreach-service` repository (Quarkus, Cloud Run), CI, and the `outreach-db` database on the app-data instance; service-owned Liquibase migrations create the tables and indexes.
2. **DevOps request:** Cloud Run service; Pub/Sub topics `outreach.commands.v1` (+ `.dlq`) and `outreach.outcomes.v1` (+ `.dlq`); the command pull subscription (retention ≥ 7 days, 5 delivery attempts, ordering enabled); Cloud Scheduler job `outreach-sweep` (every minute, `X-Outreach-Key`); Secret Manager entries for the API key, the SendGrid key, the webhook public key, and the preference-token key; a SendGrid event-webhook pointed at `/webhooks/sendgrid/events`; a public hostname for the preference page.
3. **Producer registration:** add `pdm-platform` (the PDM backend's service account, recipient namespace `practitioner:`) as the registry owner, and `attestation-module` (template namespace `attestation.`, no recipient namespaces — sender only; the attestation cycle doc's D14) as the first sender; grant publish on the command topic to both; create each producer's filtered outcome subscription. Configure `outreach.categories`. Register the `outreach-config` entry type and its JSON schema in `tenant_configurations`; seed the pilot tenant's sender identity.
4. **Templates:** register the three attestation templates as platform defaults (`tenant_id = '*'`) with their variables schemas and default category; preview against a sample recipient; activate; add pilot-tenant overrides if branding requires.
5. **Registry load for the pilot tenant:** `pdm-platform` publishes one `UPSERT_RECIPIENT` per active practitioner — its initial-load job, a PDM deliverable that must complete **before** the Attestation Module's backfill; verify `recipients.active` matches the expected population and `recipient.unresolved` stays at zero through a dry-run day.
6. **Dry run in a non-production project:** replay a day of synthetic upserts, `SCHEDULE_SENDS`, and `CANCEL_SENDS`; verify the outcome stream, the audit table, the preference page, and the alert set end to end. **The attestation doc amendments** (cycle doc D12, step 6, and the engine-branch items in its Rollout; doc 5's recipient sync and outcome consumer) are made at this point, by the attestation module's own review process — nothing in this service depends on them.
7. **Enable for the pilot tenant** together with the Attestation Module pilot; watch the alerts through one full reminder ladder (≥ 31 days). Kill switches are the brake at every step.
8. **Second producer** onboarding is steps 3–5 only — no service change.
9. **v2 roadmap (not in this design):** additional channels behind the same registry (`channels.sms`, in-app) and command shape; locale-specific template variants; quiet hours and per-recipient frequency caps on the registry row; an internal template editor; optional migration of the credentialing engine as a producer.
