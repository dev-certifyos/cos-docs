# Companion — Smart Outreach Service (concepts + supplementary material)

> Two parts. **Part 1 — Concepts:** plain-language explanations of every service, tool, and concept named in `smart-outreach-service.md`, written for a reader new to the stack. **Part 2 — Supplementary material:** the deep-dive analysis behind the design (baseline evidence, full approach scoring with cost derivations, traceability, design rationale, open questions, ticket impact, and the industry research that validated the model). The design doc stands alone as the shareable artifact; this file is the preparation and defense material.

## GCP services

### Cloud Run

- Managed container runtime: give it an image; it runs copies ("instances"), scales them with traffic (down to zero), and fronts them with an HTTPS URL.
- Scale-to-zero means nothing of ours runs between requests — why the sweep is driven by Cloud Scheduler, never by an in-process timer.
- Requests have a time limit (up to 60 minutes, configured); the sweep processes a bounded batch per call so it always finishes well inside it.
- Billing per vCPU-second and memory-second while handling requests (≈ $0.000024 per vCPU-second).

### Cloud Scheduler

- Managed cron: "call this URL on this schedule" with an auth header. Google fires it; no server of ours has to stay up.
- Retries on non-2xx per its own retry config; can alert on repeated failure.
- Triggers work only — carries no payload logic.
- Pricing: 3 jobs free per account, then $0.10 per job per month. The service uses one job (`outreach-sweep`, every minute).

### Pub/Sub

- Managed publish/subscribe messaging: publishers write to a *topic*; every *subscription* on that topic gets its own copy.
- **At-least-once delivery** — a message may arrive twice (slow ack, redelivery after timeout). Consumers must tolerate duplicates; this service does it with the inbox table.
- **Ordering keys** — messages sharing a key are delivered in publish order (requires ordering enabled on the subscription and a single publishing region). Used so a `CANCEL_SENDS` can never overtake the `SCHEDULE_SENDS` it cancels (key = `cancellationKey`).
- **Subscription filters** — a subscription can accept only messages whose *attributes* match an expression (`attributes.producer = "attestation-module"`). Filtering is on attributes, not the body — why `producer`, `tenantId`, and `commandType`/`outcomeType` are duplicated as attributes.
- **Dead-letter topic** — after N failed delivery attempts a message moves to a separate topic instead of retrying forever.
- **Retention** — unacked messages are kept up to 7 days (configurable; the service pins ≥ 7).
- **Process first, acknowledge after** — do the work (commit) before acking; an ack before processing lets a crash discard the message with no retry left.
- Pricing: first 10 GB/month free, then ≈ $40/TiB. The service's ~5k messages/day are far inside the free tier.

### Dead-letter queue (DLQ)

- A parking lot for messages that failed every delivery attempt. Nothing is lost; an operator inspects and replays or discards. Depth > 0 is an alert.

### Cloud Spanner

- Google's distributed relational database: SQL, strong consistency, horizontal scale.
- **Transaction** — reads/writes that commit atomically. The command inbox row, the send rows, the outcome outbox rows, and the audit rows commit together.
- **Unique index** — the database refuses a second row with the same values; stronger than any application check. `UNIQUE (producer, send_key)` is the send-level idempotency guarantee.
- **Conditional update** — `UPDATE … WHERE status = 'PENDING'` inside a read-write transaction: two overlapping sweeps cannot both flip the same row to `CLAIMED`; the loser sees zero rows affected.
- **No partial unique index** — "one `ACTIVE` version per template key" cannot be a constraint; it is enforced by a conditional transaction in the activate endpoint.
- Instance billing — databases on an existing instance cost only their storage (≈ $0.30/GB/month). `outreach-db` adds no instance cost.

### Secret Manager

- Stores secrets (API keys, webhook public keys) with versioning and IAM; services read them at startup. Never a plaintext literal in Terraform or code.

## Messaging and reliability concepts

### Command vs event

- An **event** says "something happened" (a fact); a **command** says "do this" (a request). Producers send this service *commands* (`SCHEDULE_SENDS`, `CANCEL_SENDS`); the service publishes *events* back (outcomes). Naming them differently keeps the direction of authority clear.

### Table as queue

- Rows with a due time and a status; a periodic sweep picks due rows, processes, and updates status. Simple, queryable, cancellable, no horizon.
- Needs a **claim** step (lease) so two sweeps cannot process the same row — the existing engine lacks it; this design adds it.

### Lease / claim

- Marking a row `CLAIMED` with a `lease_until` timestamp. If the worker dies, the lease expires and another sweep reclaims the row. Turns "a crashed worker loses work" into "a crashed worker delays work".

### Idempotency

- Running an operation twice produces the effect once. Here in three layers: `outreach_inbox (command_id)` for commands, `UNIQUE (producer, send_key)` for sends, an idempotency key on the provider call (where the provider supports it).

### Idempotency key vs cancellation key

- An **idempotency key** answers "have I seen this exact request before?" — `commandId` for a whole command, `sendKey` for one send. A retry or a redelivery with the same key produces nothing new.
- A **cancellation key** answers "which bundle does this belong to?" — `cancellationKey`. All the sends a producer may later want to stop together carry the same one (the three attestation reminders for one task).
- The cancellation key is also the **Pub/Sub ordering key**. Pub/Sub keeps order only between messages that share a key, so a cancel sharing the schedule's key can never be delivered before it. That is why the key is required on every schedule, even one that will never be cancelled — a producer with nothing to cancel uses its correlation id.
- Both are opaque strings the producer chooses; the service compares them for equality and never parses them. Knock (`cancellation_key`, `Idempotency-Key`) and Courier (`cancelation_token`, idempotency keys) use exactly these two concepts under the same names.

### Outbox pattern (transactional outbox)

- Write the message you intend to publish into a table in the same transaction as the business change; a dispatcher publishes it afterwards and marks it; a relay sweep catches anything the dispatcher missed. Solves the dual-write problem (a database commit and a publish can never be atomic). Producers use it to reach this service; the service uses it to publish outcomes.

### Inbox pattern

- The consumer-side twin of the outbox: record every processed message id in a table inside the processing transaction; a redelivered message hits the unique key and is dropped. Guarantees exactly-once *effect* on top of at-least-once *delivery*.

### Exponential backoff

- Retry delays that grow (1 min, 5, 15, 30) so a struggling provider is not hammered. Bounded by a maximum attempt count.

### Kill switch

- A configuration value that stops an action without a deployment. Two here: `outreachPaused` (per tenant) and `globalPaused` (platform).

### Dual-write problem

- Writing to two systems (a database and a message broker) that cannot share a transaction: crash between the two and they disagree forever. The outbox pattern is the standard fix; a synchronous REST integration has the problem by construction.

### Database per service

- Each service owns its database; no other service reads or writes it. Cross-service effects travel as messages. The physical form of a service boundary.

## Email and provider concepts

### SendGrid

- The platform's email delivery provider. Accepts a rendered email (or a template id + data) via API, returns a message id, and reports delivery events (processed, delivered, deferred, bounce, dropped, open, click, spam report, unsubscribe) to a webhook we host.
- **Event webhook signing** — SendGrid signs each webhook batch; the receiver verifies with a public key. The platform already does this (`SendGridWebhookResource.java:60`).
- **No native idempotency key** on the send API — the one duplicate window the design documents (*Failure modes*, sweep case 3).

### Dynamic templates (SendGrid)

- Templates stored and rendered at SendGrid; the caller sends a template id and a JSON of variables. Convenient, but the rendered content never exists on our side. Rejected as the template store (*Alternatives*).

### Handlebars templating

- `{{variable}}` substitution with helpers (`{{#each}}`, `{{#if}}`) — the syntax SendGrid dynamic templates use and the platform's existing outreach composer already renders. The service renders Handlebars-style templates itself.

### Hard bounce / soft bounce / complaint

- **Hard bounce** — permanent failure (address does not exist). **Soft bounce** — temporary (mailbox full, greylisting). **Complaint** — the recipient marked the mail as spam. Hard bounces and complaints go on the suppression list; soft bounces do not.

### Suppression list

- Addresses the platform will not email again (per tenant) until an operator clears them. Protects sender reputation for every tenant sharing the sending domain.

### Sender reputation

- Mailbox providers score sending domains by bounce and complaint rates; a bad score lands every tenant's mail in spam. Why suppression is a platform concern, not a producer concern.

### Webhook

- An HTTP callback a third party makes to our endpoint when something happens on their side. SendGrid posts delivery events to `/webhooks/sendgrid/events`.

## Platform-specific concepts

### `smart_outreach` engine (existing)

- The platform's current automated follow-up system for credentialing: `scheduled_outreaches` table + Cloud Scheduler sweep + a 14-rule exclusion engine + SendGrid. Lives inside `api-layer`. Untouched by this design; research in *Supplementary*.

### `api-layer`

- The platform's main Quarkus monolith (Cloud Run service `api-service`). Hosts the existing engine. This service is deliberately *not* inside it.

### DAL (data access layer)

- The platform's REST service in front of the primary Spanner databases; the existing engine writes `scheduled_outreaches` through it. This service has its own database and does not call the DAL.

### Tenant / tenant isolation

- A tenant is a client organization. Every row, query, sweep batch, and outcome carries `tenant_id` and is filtered by it server-side. UI filtering is never a boundary.

### Operator JWT and permissions

- The platform's existing authenticated operator access; the admin API reuses it with two permissions, `outreach:read` and `outreach:admin`.

### `tenant_configurations` / `outreach-config`

- The platform's existing per-tenant configuration store: one entry per tenant per configuration type, read through an existing API. The attestation module keeps its keys in an entry of type `attestation-module-config`; this service reads an entry of type `outreach-config` (sender identity, branding, pause switch, daily cap).
- The service reads it (cached 60 seconds) and never writes it — edits happen where all tenant configuration is edited today. It is the service's one read-only platform dependency besides tenant existence.

### Producer authorization — the three checks

- The service has no producer logins and no per-producer code. A producer is a YAML entry: name, publishing service account, template namespace, recipient namespaces.
- On every message: (1) the account Pub/Sub says published it must match the registered account for the claimed `producer` name; (2) `templateKey` must start with the producer's template namespace; (3) an `UPSERT_RECIPIENT` id must start with one of its recipient namespaces.
- Sending to anyone in a tenant is allowed for every registered producer; maintaining a person's record is allowed only for the namespace owner.
- Check 1 failing is a security signal (dead-letter plus alert); checks 2 and 3 failing are contract mistakes surfaced as `SCHEDULE_REJECTED` or a dead-lettered upsert, audited by producer.

### Shared-secret header auth

- Internal endpoints protected by a header whose value must match a configured secret; fail-closed when the secret is unset; constant-time comparison.

## Domain-neutral vocabulary used by the service

### Producer

- Any service that publishes commands to `outreach.commands.v1`. Registered by name with its publishing service account and template namespace. The service treats every producer identically.

### Send

- One email to one recipient set at one time, under one template key — one `outreach_sends` row.

### Template key / template version / tenant override

- **Key** — the stable name a producer uses (`attestation.reminder.t30`). **Version** — an immutable revision of the template's subject/body/schema. **Override** — a tenant-specific version of the same key; resolution prefers the tenant's `ACTIVE` version, else the platform default (`tenant_id = '*'`).

### Variables schema

- A JSON Schema declaring what variables a template expects, with types and required fields. Validated at intake so a send that could never render is refused immediately.

### Recipient registry

- The notification platform's own list of people it can contact: id, current address, name, language, preferences, per tenant.
- Producers keep it fresh by publishing a change whenever a contact's details change; sends then name people by id and the platform looks up the address at send time.
- The model used by Knock (users), Courier (profiles), Novu (subscribers), Braze and Iterable (users), and by LinkedIn's and Uber's internal platforms.
- Part of the v1 design: `UPSERT_RECIPIENT` fills it; `SCHEDULE_SENDS` names people by `recipientId`; the sweep resolves addresses from it at send time.

### Recipient group

- A named set of recipients the platform maintains (`tenant-admins`, `practitioners-of:org-xyz`). A send that names a group is expanded by the platform into one send per member.
- Knock calls these objects with subscriptions, Novu calls them topics, Courier lists or audiences, Braze segments.

### Correlation id

- An opaque string the producer attaches; echoed on every outcome and audit row. The service never interprets it.

### Outcome

- An event the service publishes about a send's fate (`SCHEDULED`, `SENT`, `FAILED`, `CANCELLED`, `CANCEL_TOO_LATE`, `EXPIRED`, `SUPPRESSED`, `DELIVERED`, `BOUNCED`, `COMPLAINED`).

# Supplementary material

Deep-dive material behind the design doc (`smart-outreach-service.md`): baseline evidence, the full approach analysis with weightage and cost derivations, traceability, context from earlier docs, design rationale, open questions, ticket impact, and the industry research. Vocabulary is in *Concepts*, above.

## Baseline — verified current behavior

What the platform runs today for automated outreach, with receipts. A light pass — enough to justify the design, not a full engine audit (the cycle module's companion already carries the deeper engine research).

1. **One engine, credentialing-only, inside the monolith.** Package `com.certifyos.api_layer.smart_outreach` in `api-layer` (22 source files, ~4,100 lines). Trigger: `POST /internal/smart-outreach/process` and `GET /internal/smart-outreach/ping`, header `X-Smart-Outreach-Key`, fail-closed (`SmartOutreachSchedulerResource.java:40,57-58,72,125-126,143-148`). No other endpoints exist — there is no write API.
2. **The send path resolves `outreach_id` as a credentialing workflow.** `SmartOutreachSchedulerService.java:268-284` builds `WorkflowData` from the resolved outreach context; every downstream step is typed on `WorkflowData`. An id that is not a credentialing outreach cannot be processed.
3. **Fourteen ordered skip rules, all credentialing business logic.** `OutreachExclusionEngine.java:51-133`: "Workflow not in outreach in progress", "Has replies", "Has external notes", "Jira Ticket Present", "Wrong client referenced", "CAQH roster update present", "Document uploaded since last edit", "Not tagged", "No reason for outreach", "No template for given reason", "No email to send to", "Max outreach attempts", "Time since last email", "Recred cadence". A live CAQH status check follows (`ScheduledOutreachProcessor.java:204-238`). This is the domain coupling the new service is designed to exclude.
4. **Scheduling math is inside the engine.** After a send it computes the next attempt itself with a business-hours calculator and recredentialing cadence (`SmartOutreachSchedulerService.java:430-433`) — the "service computes offsets" alternative, in production.
5. **No claim step; race documented in code.** Rows stay `PENDING` until after processing; the duplicate-`PENDING` race is described in a comment recommending a uniqueness constraint that does not exist (`SmartOutreachSchedulerService.java:456-465`).
6. **Table has no unique key beyond the PK.** `scheduled_outreaches` changeset 002 dropped `idempotency_key`, `evaluation_cycle`, `workflow_id`, `is_active`, `cancel_reason`, `exclusion_reasons` and every index including the unique one (`core-data-access-layer/.../scheduled-outreach/002-simplify-scheduled-outreach-schema.yaml:21-40`). Current columns: `id`, `outreach_id`, `entity_type`, `reason`, `attempt_number`, `scheduled_at`, `status`, `tenant_id`, `email_id`, timestamps (`ScheduledOutreachRecord.java:8-19`).
7. **All reads and writes go through the DAL over HTTP** with a Google ID token and `tenant-id: no-tenant` (`ScheduledOutreachDalService.java:53-60,92-121,154-173,271-280`). A cross-tenant sweep read, then per-row status updates.
8. **Templates come from organization settings and previously sent emails.** `OutreachEmailConstructor.java:25-32,41-63` applies the org template subject with variables; the body is inherited verbatim from the parent email (`:96-100`). Sending goes through `CredentialingOutreachService.sendNewOutreach` (`ScheduledOutreachProcessor.java:147-151`), which uses SendGrid dynamic template ids from config (`CredentialingOutreachService.java:126,134`).
9. **`FAILED` is terminal.** No retry path exists after a provider error; the outcome is logged (`ScheduledOutreachProcessor.java:165-176`) and the row marked.
10. **Delivery events arrive on an existing signed webhook** (`SendGridWebhookResource.java:49,60,297-298`) and update the outreach email record — not a generic send table.
11. **Tenant settings** for the engine live in `SmartOutreachSettingsDto` (`enabled`, `businessDaysBetweenAttempts`, `maxAttemptsPerReason`, `recredCadenceDays`, `dryRunEnabled`, `useCalendarDays`, `caqhValidStatuses` — `SmartOutreachSettingsDto.java:13-49`): every key is a credentialing policy.
12. **No Pub/Sub, no Cloud Tasks, no DLQ, no outcome events** anywhere in the package. The table is the queue; the log is the only telemetry.

⚠ **Corrections to earlier assumptions:**

- The runbook (`api-layer/docs/smart-outreach-runbook.md`) describes tables and endpoints (`smart_outreach_idempotency_keys`, `smart_outreach_evaluation_logs`, `/logs`, `/dry-run-results`) that do not exist in the code or the changelog; the code receipts above are authoritative.
- The cycle module doc (step 6, D12) planned a consumer plus an "attestation branch" inside this engine. That plan is superseded by this service; the cycle doc is amended separately (*Open questions*).

**Consequence:** the existing engine is a credentialing feature, not a platform capability. Reusing it would mean adding a per-producer branch to its evaluation path and inheriting a table with no idempotency guarantee. The pattern (table as queue, scheduled sweep, SendGrid, webhook) is sound and is reused; the implementation is not.

## Approaches analyzed in depth

**Decision criteria (stated before the approaches):**

1. **Domain-blindness** — the service can be implemented and operated with zero knowledge of any producer's business objects; onboarding a producer changes no service code. Highest weight: the reason the service exists.
2. **Reliability of intent** — a producer's decision to send or cancel survives every crash on either side; nothing is silently lost.
3. **Idempotency by construction** — duplicates blocked by database uniqueness, never by application memory.
4. **Operational burden** — new deployables, on-call surfaces, vendor relationships.
5. **Team familiarity** — does the platform already run this pattern?
6. **Audit and observability fit** — same-transaction audit, "what exactly was sent", per-send fate visible to the producer.
7. **Cost** — with derivation.
8. **Future extension** — more producers, more channels, tenant branding, recipient preferences — without redesign.

**Volume inputs for every estimate:** ~2,300 sends/day ≈ 71k/month from the first producer; ~1,560 commands/day; ~5k Pub/Sub messages/day including outcomes; design target 50k sends/day.

Weightage scale: **5 = fully satisfies · 3 = workable with caveats · 1 = fails or requires hand-building.**

### A. Standalone service, Pub/Sub commands, recipient registry, table-as-queue sweep, service-owned templates — **ADOPTED**

- **How it works:** *Approach* steps 1–9 of the design doc, the recipient registry (step 3) included.

- **Pros:**
  - Physical boundary; no code path can reach producer data — the registry is filled only by what producers push.
  - Per-person address health, preferences, live address resolution, and group sends come from the registry; every reference platform has one.
  - Every reliability property comes from a table plus a unique key plus a conditional update — nothing novel.
  - Reuses the proven engine pattern and the platform's Pub/Sub, Cloud Scheduler, Cloud Run, Spanner, SendGrid idioms.
  - Outcomes give producers facts for their own audit.

- **Cons:**
  - A new deployable and repository to own (build, CI, on-call).
  - Producers need an outbox to integrate safely (they need one anyway under the microservices direction) and a sync path for contact records plus a one-time load.
  - Two tables and one command more than the inline-address variant — roughly a third more initial engineering.
  - One documented duplicate window (crash between provider accept and row commit) because SendGrid has no idempotency key.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | No producer knowledge anywhere; namespace check is the only producer-specific rule |
| 2 Reliability of intent | 5 | Outbox → topic → inbox → row; leases; retries; DLQ |
| 3 Idempotency | 5 | Inbox key, unique send key, conditional transitions |
| 4 Ops burden | 3 | One new service to run; a sync path to watch |
| 5 Familiarity | 3 | Every component already in production; no registry exists today; the combination is new |
| 6 Audit fit | 5 | Rendered content stored; outcomes published; same-transaction audit |
| 7 Cost | 5 | ≈ $15/month |
| 8 Future extension | 5 | Producers by config; channels by adapter |

- **Failure modes:** *Failure modes and rollback* in the design doc.

- **Cost ≈ $15/month, derived:** Cloud Run ≈ $5 (86k vCPU-s sweep + consumer/webhook); Spanner storage ≈ $1 (3.4 GB/year × $0.30); Pub/Sub $0 (300 MB/month inside 10 GB free); Cloud Scheduler $0.10; Secret Manager $0.20; SendGrid on the existing plan (else ≈ $90 next tier, shared open item).

### B. Extend the existing engine inside `api-layer`

- **How it works:** a Pub/Sub consumer in `api-layer` writes `scheduled_outreaches` rows via the DAL; the sweep gets an `ATTESTATION_*` branch that resolves an attestation task and checks `OPEN`; each further producer adds a branch.

- **Pros:** least new code; existing table, sweep, SendGrid wiring, webhook; no new deployable.

- **Cons:** producer business logic accumulates inside the engine; credentialing-typed send path (`WorkflowData`); no unique key, no claim step (Baseline 5–6); `FAILED` terminal, no outcomes; lives in the monolith; every producer release couples to `api-layer` deploys.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 1 | A branch per producer, by design |
| 2 Reliability of intent | 3 | Pub/Sub in; no outcomes out; terminal failures |
| 3 Idempotency | 2 | Application-level `FIND_OR_CREATE`; no unique key |
| 4 Ops burden | 5 | Nothing new |
| 5 Familiarity | 5 | It exists |
| 6 Audit fit | 2 | No rendered-content record; producers read the table |
| 7 Cost | 5 | ≈ $0 |
| 8 Future extension | 1 | Each producer = engine change |

- **Why it loses:** fails criterion 1 outright and scores lowest on 8; the monolith placement contradicts the platform direction.

- **Failure modes:** overlapping sweeps double-process; a producer's bad branch breaks every producer's sweep.

- **Cost:** ≈ $0 infrastructure.

### C. Synchronous REST API as the producer path

- **How it works:** `POST /sends`, `POST /sends/cancel` called by producers; service writes rows and returns ids.

- **Pros:** immediate validation feedback; familiar; no topic for producers.

- **Cons:** dual write on the producer; availability coupling; no ordering between schedule and cancel across retries; every producer re-implements reliability.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | Same contract, different transport |
| 2 Reliability of intent | 2 | Dual-write on every producer |
| 3 Idempotency | 4 | Idempotency-Key header + unique send key |
| 4 Ops burden | 3 | Same service |
| 5 Familiarity | 5 | REST |
| 6 Audit fit | 4 | Same tables |
| 7 Cost | 5 | Same |
| 8 Future extension | 3 | Fine; coupling grows with producers |

- **Why it loses:** criterion 2 — the heaviest after 1. Kept as the operator/read surface only.

- **Failure modes:** service outage fails producer transactions or loses intent depending on call ordering.

- **Cost:** same as A.

### D. Service computes send times from business dates

- **How it works:** payload carries `dueDate`, `offsets`, a late-opening policy; the service derives timestamps.

- **Pros:** smaller producer payload; ladder logic in one place.

- **Cons:** the service now encodes one producer's policy for all; every policy change is a service change; the next producer's ladder differs.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 1 | Knows what a due date means |
| 2 Reliability of intent | 5 | Same transport |
| 3 Idempotency | 5 | Same keys |
| 4 Ops burden | 3 | Same service |
| 5 Familiarity | 4 | The existing engine does this |
| 6 Audit fit | 4 | Fine |
| 7 Cost | 5 | Same |
| 8 Future extension | 2 | Per-producer policy set grows |

- **Why it loses:** criterion 1 — directly. Three lines of date arithmetic in the producer buy a permanently clean boundary.

- **Cost:** same as A.

### E. One Cloud Task per send with a future `scheduleTime`

- **How it works:** consumer creates a named Cloud Task at `scheduledAt`; callback renders and sends; cancel deletes by name.

- **Pros:** no sweep; native retry/backoff; name-based dedup.

- **Cons:** 30-day future cap (T+1 born ~31 days early); no query surface; cancel-by-key needs a mapping table; no DLQ (exhausted tasks silently deleted).

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | Neutral |
| 2 Reliability of intent | 3 | No DLQ; horizon workaround |
| 3 Idempotency | 4 | Task names |
| 4 Ops burden | 4 | One queue |
| 5 Familiarity | 5 | AutoRecred pattern |
| 6 Audit fit | 3 | Needs the table anyway for records |
| 7 Cost | 5 | ≈ $0 |
| 8 Future extension | 2 | Horizon cap is structural |

- **Why it loses:** the horizon cap disqualifies it for the first producer; it needs the table it tries to avoid.

- **Cost:** Cloud Tasks first 1M ops free, $0.40/M after → ≈ $0 at 71k sends/month.

### F. SendGrid dynamic templates as the template store

- **How it works:** templates authored and versioned in SendGrid; the service sends `template_id` + `dynamic_template_data`.

- **Pros:** visual editor; zero rendering code; existing send path already passes template ids.

- **Cons:** rendered content never on our side; per-tenant overrides become vendor-console objects; provider lock-in for every template.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | Neutral |
| 2 Reliability of intent | 5 | Neutral |
| 3 Idempotency | 5 | Neutral |
| 4 Ops burden | 4 | Templates managed in a vendor console |
| 5 Familiarity | 5 | In use today |
| 6 Audit fit | 1 | Cannot say what was sent |
| 7 Cost | 5 | Included |
| 8 Future extension | 2 | Second provider = re-author everything |

- **Why it loses:** criterion 6 (the audit rule) and the source-agnostic rule.

- **Cost:** included in SendGrid plans.

### G. Managed notification platform (Knock / Courier / Novu SaaS)

- **How it works:** producers trigger vendor workflows; the vendor stores templates, schedules, cancels by key, sends via SendGrid, posts outcome webhooks.

- **Pros:** the adopted model, already built and battle-tested; batching/digests/preferences included; fastest to first email.

- **Cons:** recipient PII and variables leave the platform (BAA, security review); producers still need an outbox; per-message pricing; hard external dependency for compliance reminders.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | By design |
| 2 Reliability of intent | 4 | Vendor-side reliability strong; outbox still ours |
| 3 Idempotency | 5 | Idempotency keys native |
| 4 Ops burden | 2 | Vendor management, BAA, data-flow review |
| 5 Familiarity | 1 | None |
| 6 Audit fit | 3 | Vendor logs; export needed for 7-year retention |
| 7 Cost | 2 | $250–$1,000+/month at volume |
| 8 Future extension | 5 | Channels, preferences, digests included |

- **Why it loses:** criteria 4, 5, 7 and the data boundary; the core we would buy is a table, a sweep, and a template engine.

- **Cost (order of magnitude):** vendor tiers are per notification and change often; at ~71k/month expect the $250–$1,000/month band before SendGrid. Self-hosted Novu removes license cost but adds a stack (MongoDB, Redis, workers) to operate — heavier than A.

### H. Producers render subject and body; service relays

- **How it works:** `SCHEDULE_SENDS` carries finished subject and HTML.

- **Pros:** no template store or schema in the service.

- **Cons:** copy changes need producer deploys; templates scattered; branding re-implemented per producer; big payloads.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | Maximally blind |
| 2 Reliability of intent | 5 | Same |
| 3 Idempotency | 5 | Same |
| 4 Ops burden | 3 | Same service, less code |
| 5 Familiarity | 3 | Nothing renders this way today |
| 6 Audit fit | 4 | Body stored; provenance of copy unclear |
| 7 Cost | 5 | Same |
| 8 Future extension | 1 | No central copy inventory, no tenant branding, channels impossible without per-producer work |

- **Why it loses:** criterion 8 and the explicit platform decision to own templates centrally.

- **Cost:** same as A.

### I. Shared library instead of a service

- **How it works:** each producer embeds a library that writes its own send table, runs its own sweep, and calls SendGrid.

- **Pros:** no new deployable; each team owns its schedule.

- **Cons:** N SendGrid credentials, N suppression lists (or none), N webhook endpoints, N copies of templates; no platform view of what was sent; library upgrades roll across every service.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | Trivially |
| 2 Reliability of intent | 4 | Local table |
| 3 Idempotency | 4 | Local keys |
| 4 Ops burden | 1 | Duplicated everywhere |
| 5 Familiarity | 3 | No such library exists |
| 6 Audit fit | 2 | Fragmented |
| 7 Cost | 4 | N webhooks, N secrets |
| 8 Future extension | 1 | Every change × N |

- **Why it loses:** criteria 4, 6, 8 — the named anti-pattern.

- **Cost:** same compute, multiplied by producers.

### J. Inline addresses in every send — no registry (the simpler variant of A) — **REJECTED**

- **How it works:** A without step 3: `SCHEDULE_SENDS` carries `{ email, name }` per recipient; no `UPSERT_RECIPIENT`, no recipient tables, no groups; a separate `outreach_suppressions` list of failed address strings protects sender reputation.

- **Pros:** two fewer tables, one fewer command, no initial load, no sync path — a smaller first build; the first producer already holds each address when it schedules; nothing to keep fresh.

- **Cons:** no unsubscribe or preferences (nothing to attach them to); a bounce marks a string, not a person, so the owning producer is never told; an address changed after scheduling is not used; every producer rebuilds group sends by paging its own tables; names and addresses in every message and every send row; unknown or inactive people cannot be refused before sending.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Domain-blindness | 5 | Neutral |
| 2 Reliability of intent | 5 | Same transport |
| 3 Idempotency | 5 | Same keys |
| 4 Ops burden | 4 | Same service, nothing to sync |
| 5 Familiarity | 4 | The existing engine passes addresses around |
| 6 Audit fit | 3 | "What was sent" yes; "to whom, as a person" no; no address-health history |
| 7 Cost | 5 | ≈ $15/month |
| 8 Future extension | 1 | Preferences, group sends, channels all need a person record; retrofitting migrates every producer's payload |

- **Why it loses:** criterion 8 decisively and criterion 6 — the loops it can never close (preferences, address-health feedback, live resolution, group sends) are exactly what every reference platform built a registry for. The saving is one-time engineering; the loss is permanent.

- **Failure modes:** stale address in a payload → wrong recipient with no central fix; bounce → string suppressed, producer never learns which record is broken.

- **Cost:** infrastructure as A; roughly a third less initial engineering.

### Comparison

| Criterion (weight) | A adopted | B extend engine | C REST | D offsets | E Cloud Tasks | F SendGrid tpl | G SaaS | H producers render | I library | J inline addresses |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Domain-blindness (×3) | 5 | 1 | 5 | 1 | 5 | 5 | 5 | 5 | 5 | 5 |
| 2 Reliability (×3) | 5 | 3 | 2 | 5 | 3 | 5 | 4 | 5 | 4 | 5 |
| 3 Idempotency (×2) | 5 | 2 | 4 | 5 | 4 | 5 | 5 | 5 | 4 | 5 |
| 4 Ops burden (×2) | 3 | 5 | 3 | 3 | 4 | 4 | 2 | 3 | 1 | 4 |
| 5 Familiarity (×1) | 3 | 5 | 5 | 4 | 5 | 5 | 1 | 3 | 3 | 4 |
| 6 Audit fit (×2) | 5 | 2 | 4 | 4 | 3 | 1 | 3 | 4 | 2 | 3 |
| 7 Cost (×1) | 5 | 5 | 5 | 5 | 5 | 5 | 2 | 5 | 4 | 5 |
| 8 Extension (×2) | 5 | 1 | 3 | 2 | 2 | 2 | 5 | 1 | 1 | 1 |
| **Weighted total (max 80)** | **74** | 42 | 58 | 56 | 60 | 66 | 60 | 66 | 52 | 66 |
| Cost / month | ≈ $15 | ≈ $0 | ≈ $15 | ≈ $15 | ≈ $15 | ≈ $15 | $250–1,000 | ≈ $15 | ≈ $15 × N | ≈ $15 |

- J is A minus the registry. It wins two points on ops burden and familiarity today and loses eight on audit fit and extension for the life of the service — the arithmetic of a one-time saving against a permanent gap.
- F and H score close on arithmetic because they are *variants* of A that each fail one criterion hard (audit; extension). They are not competing architectures — A with a different template decision. The design doc treats them as alternatives for the template decision specifically.

## Requirements traceability

**Functional:**

| # | Requirement | Source | Where satisfied |
| --- | --- | --- | --- |
| F1 | Any service can schedule emails for future times without a service change | Platform direction (D2-32), producer-registration model | Approach 1–3; Configuration `outreach.producers` |
| F2 | Any service can cancel pending emails as a group or individually | Attestation submission flow (v2 §6.2, D2-23) | Approach 4; `CANCEL_SENDS` |
| F3 | Producers learn the fate of every send | Non-negotiable 2 (producers' audit), v2 §6.2 | Approach 7; outcome topic |
| F4 | Templates owned centrally, per-tenant overridable | Platform decision | Approach 6 |
| F5 | Email only in v1; channel-neutral contract | Platform decision | `channel` field; `EmailProvider` interface |
| F6 | Existing credentialing engine untouched | Platform decision | Key decision D12 |
| F7 | Attestation T-30 / T-7 / T+1 ladder deliverable, with cancel on submission | v2 §6.2, D2-23 | Worked example 1 |
| F8 | Producers name people by id; the service resolves addresses from its own registry; producers keep it in sync | Platform decision (industry model) | Approach 3, 6; `UPSERT_RECIPIENT` |
| F9 | Address health and opt-outs recorded per person and enforced for every producer | Sender reputation; platform decision | Approach 9; D15, D16 |

**Non-functional:**

| # | Requirement | Source | Where satisfied |
| --- | --- | --- | --- |
| N1 | Idempotency by uniqueness, never memory | Non-negotiable 1 | Inbox key; `UNIQUE (producer, send_key)`; conditional transitions |
| N2 | Same-transaction, append-only audit, 7 years | Non-negotiable 2, D2-20 | *Audit trail* |
| N3 | Correlation end to end; telemetry never blocks | Non-negotiable 3 | *Observability* |
| N4 | Tenant isolation server-side on every path | Non-negotiable 4 | *Security* |
| N5 | Source/vendor-agnostic naming and adapters | Non-negotiable 6 | `EmailProvider`; no vendor name outside the adapter and webhook path |
| N6 | Quarantine over guessing; nothing silently discarded | Non-negotiables 8–9 | DLQ; `SCHEDULE_REJECTED`; unmatched webhook storage; `CANCEL_NO_MATCH` |
| N7 | ISO 8601 everywhere | Non-negotiable 11 | All contracts |
| N8 | 20× headroom without redesign | Design target | *Performance and scale* N2 |

## Context from previous module docs

Inherited from the cycle module doc (ticked 2026-08-27, amended 2026-09-01):

- **Transactional outbox + relay** as the producer's mechanism (D11) — adopted unchanged as the recommended producer pattern; the command envelope's `commandId` is the producer's outbox row id.
- **Pub/Sub with ordering keys and process-then-ack** (step 5) — adopted; ordering key changes from `taskId` to the generic `cancellationKey` (the attestation producer sets `cancellationKey = attestation-task:<taskId>`, so the effect is identical).
- **Reminder ladder semantics** (D5 late-opening rule, D2-23 final email, reminder offsets stamped on the task row) — remain the producer's; the service receives absolute timestamps.
- **Audit envelope discipline** (first-class correlation columns, "committed in" contract) — mirrored in this service's own audit table.

Exports to later docs and to the cycle doc amendment:

- The command and outcome contracts (this doc's *Contracts*) replace the cycle doc's `OUTREACH_SCHEDULE` / `OUTREACH_CANCEL` payload shapes (which were written for the `scheduled_outreaches` consumer). Field mapping for the amendment: `eventId → commandId`; `taskId → correlationId` and `cancellationKey`; the three tiers become three `sends[]` with `sendKey = attestation-task:<taskId>:<tier>`, absolute `scheduledAt`, and `expiresAt`.
- Attestation audit events `OUTREACH_SENT / BOUNCED / FAILED` are written by the attestation module's **outcome consumer**, from `SENT / BOUNCED / FAILED` outcomes, carrying `sendKey`, `providerMessageId`, `templateVersion` in place of `scheduledOutreachId` / `emailId`.
- Doc 5 (backend) hosts the attestation outcome consumer and the recipient resolution (which addresses receive reminders) — the producer-side work that replaces the "engine branch".

## Design rationale — anticipated questions

**Why is the service not allowed to check whether an attestation task is still open before sending? That would prevent the one-extra-email race.**
Because the check requires the service to know what an attestation task is, where it lives, and what "open" means — and then the same for every producer. That per-producer knowledge is exactly what the existing engine accumulated (14 rules) and what made it unusable as a platform service. The race is bounded (one email, only when a cancel and a due time coincide within a minute), visible (`CANCEL_TOO_LATE`), and further bounded by `expiresAt`. Every reference platform makes the same trade.

**Why not at least a generic "pre-send check URL" the producer supplies?**
It adds a synchronous producer dependency to the send path (their outage delays every send), a per-producer callback contract to version, and latency per email. It also invites producers to skip cancellation. Deferred to v2 if a real need appears; the contract can add an optional field without breaking anyone.

**Why does the service keep its own list of people instead of taking addresses in each send?**
Because that is what every notification platform of note does, for reasons that only show up after launch: a person can unsubscribe and it sticks; a bounce is remembered against the person and the owning producer is told; an address changed today reaches a reminder scheduled last month; "everyone in this group" is one command; ids rather than addresses travel through the topic; unknown or inactive people are refused before anything is sent. The inline-address variant saves roughly a third of the first build and forfeits all of that permanently.

**Doesn't a registry break the "no producer knowledge" rule?**
No. The registry holds contact facts — this id has this address, prefers this language, is active — the same kind of platform data as the tenant's sender identity. It holds no business facts: not why the producer wants to reach the person, not what they owe, not what state they are in. And it is filled only by what producers push; the service never reads a producer's tables.

**Who is allowed to write a recipient record?**
Only the producer whose registered recipient namespace matches the id (`practitioner:` belongs to `pdm-platform`, the PDM backend — the system of record for practitioner contact data; a namespace has exactly one owner). Any producer may send to any recipient or group in a tenant — sending is not privileged; maintaining the record is. Two columns are never written by producers: `email_status` (webhook and operators) and `preferences` (the person, through the preference page).

**What keeps the registry correct?**
Producers republish on every change (they own the source of truth and already handle those changes); upserts are idempotent and last-writer-wins by the producer's timestamp so a replayed backlog cannot revert a fresh address; the bounce loop surfaces bad addresses to the owner; `RECIPIENT_UNRESOLVED` at intake surfaces a missing sync immediately. A wrong address in the registry is exactly as wrong as a wrong address in a payload — the difference is that it is fixed once, centrally.

**Why does the service read `tenant_configurations` if it is supposed to read only its own database?**
Because that entry is where every service on the platform keeps tenant configuration today, and it is read through the platform's own API — not by querying another service's tables. The boundary rule is about producer *business* data; sender identity and a pause switch are platform configuration. Keeping them in the usual place means operators have one place to edit, and the service still writes nothing there.

**Why is `cancellationKey` mandatory when most sends are never cancelled?**
Because it is also the ordering key. Pub/Sub keeps order only between messages that share a key; a cancel that shares the schedule's key cannot arrive first. If the key were optional, a send without one would have no ordering guarantee and a later cancel by individual ids could overtake it. One string per command buys away a whole class of "the cancel ran ahead and was dropped" failures.

**How does the service know a message really came from the attestation module?**
Pub/Sub records which service account published every message. The registration list says which account belongs to which producer name. If they do not match, the message is dead-lettered and an alert fires. Two more prefix checks keep a genuine producer inside its own template and recipient namespaces. No passwords, no per-producer code — three string comparisons.

**The registry, in one breath?**
A person can unsubscribe or ask for fewer reminders, and the choice sticks. A bounced address is remembered against the person, so every producer benefits. A changed address reaches already-scheduled reminders. "Notify everyone in this group" is one command. Less personal data travels through the topic. Unknown or inactive people are refused before anything is sent. "What have we sent this person" is one lookup.

**Why does the service store the rendered body? Isn't the template version plus variables enough?**
The template version plus variables reconstruct the body only if the renderer is deterministic forever and no helper changes. Storing the body (a few KB) makes "what did we send" a lookup, not a reconstruction — the audit standard the platform holds itself to. Cold storage after a year keeps cost flat.

**Why a one-minute sweep rather than an event-driven send?**
Sends are time-scheduled future work; nothing "happens" at the due time to react to. A sweep is the simplest correct mechanism; one minute is the accepted latency for reminder emails.

**Why not put tenant sender settings in the platform's `tenant_configurations` entry like the attestation module does?**
A service does not read another service's database. The admin API is the single write path, so settings are auditable in the same table as everything else. If a platform-wide configuration service appears, this service can consume it.

**How does a second producer onboard?**
Registration (name, service account, namespace) in `outreach.producers`; publish grant on the command topic; a filtered outcome subscription; templates registered via the admin API. Zero service code.

**Why require a `cancellationKey` on every schedule, even for sends that will never be cancelled?**
It is the Pub/Sub ordering key, so it must exist. Producers with no cancel intent set it to their correlation id.

**What stops a producer from flooding a tenant?**
`dailySendCap` per tenant (optional), the sweep's bounded batch, and per-producer command metrics with alerts. Per-recipient rate caps are v2.

**Why five attempts and about an hour of backoff?**
Long enough to ride out a typical provider incident; short enough that a compliance reminder is still timely. Configuration, not code.

**What happens to the cycle doc's "outreach consumer in api-layer" and "attestation branch"?**
Superseded. The attestation module's own review amends the cycle doc (D12, step 6, Rollout item 4, audit fields). This service does not depend on the amendment; the amendment depends on this doc's contracts.

**Is this how large platforms do it?**
Yes. Knock, Courier, and Novu expose exactly this contract (workflow key + recipients + data + idempotency key + cancellation key; templates and schedules platform-side; outcome webhooks). Airbnb separates a rendering service from a delivery service; Contentsquare's notification service owns templates and consumes Kafka events from domain services that decide what and when. Where big platforms add intelligence (LinkedIn's Air Traffic Controller, Uber's communication gateway, Netflix's planner/executor) it is recipient-centric — channel, timing, frequency capping — never producer domain logic. Sources at the end of this file.

## Worked walkthrough — the recipient registry, in plain terms

> Presentation material for the registry decision (D6). One tenant (`org-xyz`), two producers in their real v1 roles — `pdm-platform` (the PDM backend) maintains the `practitioner:` registry records, the Attestation Module only sends to them (cycle doc D14) — six steps. Each step shows the message or the table row exactly as it would look. Read it once and the model is: **the producer keeps "person P has address A" fresh separately, then says "email person P with template X at time T." The service joins the two at send time and remembers everything it learns about P.** The rejected variant — "email this address with template X at time T" — is what the service would have done without a registry.

### Step 1 — the registry is filled: the `UPSERT_RECIPIENT` command

The **registry owner** — `pdm-platform`, the PDM backend, which owns practitioner contact data (cycle doc D14) — publishes this on `outreach.commands.v1` whenever a contact is created or changes. The first-time load is the same command, once per existing practitioner. The Attestation Module never publishes an upsert: it holds no recipient namespace, and an upsert from it would fail authorization check 3.

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
    "groups": ["practitioners"],
    "attributes": { "salutation": "Dr." }
  }
}
```

- `recipientId` is chosen by the owning producer and namespaced like template keys (`practitioner:…` belongs to `pdm-platform`; an `org-contact:` namespace is deferred until a real send needs an owner — cycle doc D14). The service never interprets it.
- The command replaces the fields it carries. Running it twice has the same effect as once — keyed on `(tenantId, recipientId)`. Ordering key = `recipientId`.
- `groups` is the full membership list; sending a changed list replaces membership.
- `attributes` is free JSON the producer may want in templates (`{{recipient.attributes.npi}}`); the service stores it and never reads its meaning.
- `status: INACTIVE` (or a later `DELETE_RECIPIENT`) means "never email this person again"; pending sends to them become `SUPPRESSED`.

### Step 2 — what the registry tables hold

`outreach_recipients`

| tenant_id | recipient_id | email | email_status | display_name | locale | status | preferences | updated_at |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| org-xyz | practitioner:cert-000123 | dr.smith@clinic.example | OK | Dr. A. Smith | en-US | ACTIVE | `{"unsubscribedCategories":[]}` | 2026-09-10 |
| org-xyz | practitioner:cert-000987 | j.doe@old-domain.example | HARD_BOUNCED | Dr. J. Doe | en-US | ACTIVE | `{}` | 2026-08-02 |
| org-xyz | org-contact:admin-42 | credentialing@clinic.example | OK | Clinic Credentialing | en-US | ACTIVE | `{"unsubscribedCategories":["digest"]}` | 2026-07-19 |

- Primary key `(tenant_id, recipient_id)`.
- `email_status` is written by the **webhook** when a bounce arrives — never by producers.
- `preferences` is written by the unsubscribe link or preference page — never by producers.
- The `org-contact:admin-42` row is **illustrative of the extension path only** — no `org-contact:` owner is registered in v1 (deferred, cycle doc D14).

`outreach_recipient_group_members`

| tenant_id | group_key | recipient_id |
| --- | --- | --- |
| org-xyz | practitioners | practitioner:cert-000123 |
| org-xyz | practitioners | practitioner:cert-000987 |
| org-xyz | tenant-admins | org-contact:admin-42 |

### Step 3 — scheduling by id: the same `SCHEDULE_SENDS`, different recipient entries

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
      {
        "sendKey": "attestation-task:task-7f3a…:T-30",
        "templateKey": "attestation.reminder.t30",
        "scheduledAt": "2026-09-15T13:00:00Z",
        "expiresAt": "2026-10-16T00:00:00Z",
        "recipients": {
          "to": [ { "recipientId": "practitioner:cert-000123" } ]
        },
        "variables": { "nextAttestationDate": "2026-10-15", "portalLink": "https://…" }
      }
    ]
  }
}
```

What is different from the inline-address variant:

- No address and no name in the message. The template uses `{{recipient.displayName}}`, filled in by the service when it renders.
- `category` (new, optional) makes preferences work: a person who opted out of `digest` still receives `compliance-reminder`. Categories come from a short list the service publishes; some are marked mandatory (compliance reminders cannot be opted out of). Still no business meaning inside the service.
- The intake check is now "does this `recipientId` exist and is it `ACTIVE`?" If not → `SCHEDULE_REJECTED (RECIPIENT_UNRESOLVED)` for that send, and the producer knows immediately that its sync is behind.

### Step 4 — the send row, and what happens on the due date

The row created at intake stores **ids**, not addresses:

```json
{ "id": "snd-5d2e…", "sendKey": "attestation-task:task-7f3a…:T-30",
  "recipients": { "to": [ { "recipientId": "practitioner:cert-000123" } ] },
  "status": "PENDING", "scheduledAt": "2026-09-15T13:00:00Z" }
```

The sweep on 2026-09-15 at 13:00 does, for this row:

1. Loads each recipient row **now**. If Dr. Smith changed address on 2026-09-12, the new address is used. Nobody had to cancel and reschedule.
2. Checks `status = ACTIVE`, `email_status` not `HARD_BOUNCED`, and the category not in `unsubscribedCategories`. Any failure → a `SUPPRESSED` outcome naming the recipient and the cause; no email is attempted.
3. Renders the template with the producer's `variables` plus `recipient.*` and `tenant.*`.
4. Sends. After the send, the row stores the **resolved snapshot** for audit:

```json
{ "status": "SENT", "sentAt": "2026-09-15T13:00:41Z",
  "resolvedRecipients": {
    "to": [ { "recipientId": "practitioner:cert-000123", "email": "dr.smith@newclinic.example", "name": "Dr. A. Smith" } ] },
  "providerMessageId": "sg-14f0…", "templateVersion": 3 }
```

The row now holds both what the producer asked for (ids) and what was actually sent (the addresses at that moment). "Which address received the T-30?" is answered from the row.

### Step 5 — a group send: one command, many rows

A platform operations service wants to tell every practitioner of the tenant about a maintenance window:

```json
{
  "commandType": "SCHEDULE_SENDS",
  "producer": "platform-ops",
  "tenantId": "org-xyz",
  "correlationId": "maint-2026-09-20",
  "payload": {
    "cancellationKey": "maintenance:2026-09-20",
    "category": "service-notice",
    "sends": [
      {
        "sendKey": "maintenance:2026-09-20:practitioners",
        "templateKey": "platform.maintenance.notice",
        "scheduledAt": "2026-09-18T14:00:00Z",
        "recipients": { "toGroup": "practitioners" },
        "variables": { "windowStart": "2026-09-20T02:00:00Z", "windowEnd": "2026-09-20T04:00:00Z" }
      }
    ]
  }
}
```

- The service expands the group at intake: one `outreach_sends` row per member, `sendKey = maintenance:2026-09-20:practitioners:<recipientId>`, all under the same `cancellationKey`.
- 70,000 members = 70,000 rows, inserted in chunks; a small `GROUP_EXPANSION` job row records progress so a crash in the middle resumes where it stopped instead of starting over.
- One `CANCEL_SENDS` on `maintenance:2026-09-20` stops all of them. The producer wrote one message and read no tables.

### Step 6 — the feedback loops that exist only with a registry

**A hard bounce becomes a fact about the person.** The webhook receives a bounce for `sg-14f0…` → finds the send row → finds `recipientId` → sets `email_status = HARD_BOUNCED` on `practitioner:cert-000123` → publishes a new outcome type to the producer that registered that person — the **owner**, `pdm-platform`, never the sender (cycle doc D14):

```json
{ "outcomeType": "RECIPIENT_ADDRESS_FAILED", "producer": "pdm-platform",
  "tenantId": "org-xyz", "correlationId": "cert-000123",
  "detail": { "recipientId": "practitioner:cert-000123",
              "email": "dr.smith@newclinic.example", "cause": "HARD_BOUNCE" } }
```

`pdm-platform` owns fixing the contact record (it can surface "email address invalid — please update" in its own UI); the Attestation Module simply sees its later sends to that person come back as `SUPPRESSED` outcomes, with the cause. Without a registry this loop cannot exist: the service would know an address failed, not whose it was.

**Unsubscribe sticks to the person.** The unsubscribe link in an email opens a preference page hosted by the service; it writes `preferences` on the recipient row. The next send in an optional category is `SUPPRESSED` with cause `UNSUBSCRIBED`. Mandatory categories (compliance reminders) ignore the preference by configuration.

### What this costs, and what it does not (against the inline-address variant)

- Two more tables, one more command type (`UPSERT_RECIPIENT`), group expansion, and a first-time load of existing contacts. Roughly a third more engineering than the inline-address build.
- No change to the service boundary: the registry holds contact data (who has which address), never business data (why the producer wants to reach them).
- No change to the service boundary or to the command envelope: the registry adds one command type and changes how `recipients` is expressed, nothing else.

## Open questions and sign-offs

| # | Question | Owner | Status |
| --- | --- | --- | --- |
| Q1 | Confirm Quarkus/Java for the new repository (platform default) vs another runtime | Platform engineering | Open |
| Q2 | SendGrid plan headroom for +71k/month; next-tier approval if needed (shared with the cycle doc) | Platform / Finance | Open |
| Q3 | Operator permissions `outreach:read` / `outreach:admin` — add to the platform permission registry | Platform engineering | Open |
| Q4 | Tenant registry lookup at intake — which existing API confirms a tenant id exists | Platform engineering | Open |
| Q5 | Rendered body stays in `outreach-db` (decided). Later option: move bodies older than 1 year to cheaper storage with the hash kept on the row — decide when storage growth warrants it | Data / DevOps | Deferred, not blocking |
| Q6 | Cycle doc amendment: D12, step 6, Rollout 4, audit `detail` fields — schedule the review | Attestation module owner | Open (out of this doc's scope) |
| Q7 | Doc 5 (backend): recipient resolution for attestation reminders and the outcome consumer | Attestation module owner | Open (producer-side) |
| Q8 | Duplicate window on crash between provider accept and row commit — accept as documented for v1. Closing it needs a provider whose send API takes an idempotency key: SendGrid's does not; Resend's does (`Idempotency-Key` header, 24-hour dedup). Evaluate a second adapter when provider choice is next reviewed | Platform engineering | Open, recommended: accept for v1, flag openly |
| Q9 | Handlebars helper set allowed in templates (security: no arbitrary code; whitelist of helpers) | Platform engineering | Open |
| Q10 | Pub/Sub ordering requires single-region publishing — confirm producers publish from one region | DevOps | Open |
| Q11 | ✅ CLOSED (2026-09-07, attestation cycle doc D14): the **PDM backend (`api-layer`) owns the `practitioner:` namespace** as producer `pdm-platform` — initial load (before the attestation backfill) + change publisher + `INACTIVE` on termination; the Attestation Module is a sender only. `org-contact:` is **deferred** — no owner until a real send needs one (a later registration, no service change) | — | Closed |
| Q12 | Preference page hostname and branding; one-click `List-Unsubscribe-Post` support confirmed with SendGrid headers | Platform / DevOps | Open |
| Q13 | Register the `outreach-config` type and JSON schema in `tenant_configurations`; confirm the read API and its caching behavior | Platform engineering | Open |

## Ticket impact

- **New epic:** Smart Outreach Service — repository, database and migrations, command consumer, recipient registry and group expansion, sweep, template store and admin API, SendGrid adapter and webhook, preference page, outcome outbox, observability, DevOps request.
- **Attestation tickets to update (cycle doc amended 2026-09-07):** the outreach consumer ticket (an outcome consumer in the attestation backend — recipient sync is NOT the attestation module's: `pdm-platform` publishes `UPSERT_RECIPIENT`, a new PDM deliverable); the cycle-doc audit-event ticket (`OUTREACH_SENT` fields + the `OUTREACH_SCHEDULE_CONFIRMED` / `OUTREACH_CANCEL_CONFIRMED` confirmation events); the DevOps ticket (publish rights on `outreach.commands.v1` + filtered outcome subscription — no module-owned topic).
- **No changes** to credentialing `smart_outreach` tickets.

## Industry research — how large platforms split notification responsibilities

Summary of the research behind the design's boundary rule (2026-09-05).

| Platform / product | Producers decide | Platform decides | Templates live | Cancellation model |
| --- | --- | --- | --- | --- |
| Knock | Trigger: workflow key, recipients, data, tenant, `cancellation_key`, `Idempotency-Key` | Channels, templates, delays, batching, preferences, delivery logs | Platform | `cancel(workflow_key, cancellation_key)`; only pausable runs cancellable; async |
| Courier | Automation invoke with `cancelation_token`, data | Routing, templates, delays | Platform | Cancel by token; dynamic tokens per entity recommended |
| Novu | Trigger with payload, subscriber | Workflow, templates, digests, providers | Platform | Cancel by transaction id |
| Airbnb comms platform | Campaign/targeting rules | Rendering Service (per channel) + Delivery Service (SendGrid, Twilio, FCM) | Platform | n/a (published detail) |
| Contentsquare | Domain service emits Kafka event (what, when) | Channel, template rendering (EJS, Slack blocks, Teams cards) | Platform | n/a |
| LinkedIn ATC | Upstream apps send communication requests via Kafka | Channel, timing, dedup, rate-limit upstreams, aggregation | Platform (content templates) | n/a |
| Uber real-time push | Fireball decides *when* from events; API gateway decides *what* payload | Delivery (RAMEN) | Gateway | n/a |
| SendGrid dynamic templates | Caller sends template id + data | Rendering | Provider | n/a |

Common pattern: domain services own the *decision* (whether, whom, when, which template key, variables, cancel); the notification platform owns *delivery mechanics and content* (templates, rendering, providers, retries, suppression, logs) plus, at consumer scale, *recipient-centric* policy (channel, timing, capping). No reference platform queries producer state at send time.

Sources:

- Knock — Canceling workflows: https://docs.knock.app/send-notifications/canceling-workflows
- Knock — Trigger workflow API: https://docs.knock.app/api-reference/workflows/trigger
- Knock — Schedules: https://docs.knock.app/concepts/schedules
- Courier — Cancelling an automation: https://www.courier.com/docs/platform/automations/cancel
- Uber — Real-time push platform: https://www.uber.com/blog/real-time-push-platform/
- LinkedIn — Air Traffic Controller: https://www.linkedin.com/blog/engineering/messaging-notifications/air-traffic-controller-member-first-notifications-at-linkedin
- Airbnb — Promotions and communications platform: https://medium.com/airbnb-engineering/airbnbs-promotions-and-communications-platform-6266f1ffe2bd
- Contentsquare — Building a reliable notification system: https://engineering.contentsquare.com/2023/building-a-reliable-notification-system/
- Netflix — Thinking fast and slow for a personalized notification system: https://netflixtechblog.medium.com/thinking-fast-slow-for-a-personalized-notification-system-4d89b26525cd
- SendGrid — Dynamic templates: https://www.twilio.com/docs/sendgrid/ui/sending-email/how-to-send-an-email-with-dynamic-templates
- AWS Prescriptive Guidance — Transactional outbox pattern: https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html
