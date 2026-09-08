# Companion — Portal lane module (concepts + supplementary material)

Vocabulary first, deep-dive material second. Concepts entries carry no design decisions — the module doc (`portal-lane.md`) owns the design.

## Web and auth concepts

### Auth0

- A hosted identity provider: it runs the login screens, password storage, and token issuance so the application does not.
- The portal frontend uses it for logged-in users (`@auth0/nextjs-auth0`); after login the app receives a session it can check on every request.
- The design leans on one property: logged-in identity is established by the portal stack before any call reaches our endpoints.

### Magic link

- A login mechanism where the emailed link itself *is* the credential — clicking it signs the user in without a password.
- **Not used by this design's emails (doc 1's D17):** the attestation deep link is tokenless (`<portalBaseUrl>/attest/<taskId>`); authentication is the portal's normal login. Magic links remain a portal-stack capability it may layer onto the same entry route later — a portal-side enhancement, no contract change here.
- Limits worth knowing if the portal adds them: links expire (configurable validity days) and can be re-sent a capped number of times per day.

### JWT (JSON Web Token)

- A signed, self-contained token: a JSON payload (who, what for, until when) plus a cryptographic signature.
- The receiver verifies the signature and trusts the payload without a database lookup.
- Here: how the portal's magic-link machinery works when used — a JWT in the link's `?token=` parameter, verified once, then switched to a session cookie. (The attestation email link itself carries no token, D17.)

### Session cookie (HttpOnly, opaque)

- After the JWT is verified once, the server issues a random session id stored in a cookie.
- `HttpOnly` means page JavaScript cannot read the cookie — it only travels with requests, which blocks token theft via injected scripts.
- "Opaque" means the id carries no data — the server looks the session up; revoking it is a server-side delete.
- Here: the portal's `attest-session` cookie, 24-hour TTL.

### Next.js (Pages Router)

- The React framework the portal frontend is built on; "Pages Router" is its file-based routing style (a file under `pages/` is a URL).
- Relevant only as context: the frontend is an existing app with its own middleware and API-calling conventions — this module changes none of it.

## API and reliability concepts

### Backend-for-frontend (the proxy role)

- A server that sits between a frontend and the platform's services: it owns the frontend's auth and sessions, and calls internal services on its behalf.
- provider-portal-api plays this role: the browser talks only to it; it talks server-to-server to everything else.
- The property the design leans on: identity is resolved in the proxy, so internal services receive *resolved* facts, never browser claims.

### Server-to-server call

- An HTTP call between two backend services, authenticated service-to-service (no browser, no cookies).
- Trust model: the caller vouches for facts it resolved (here: "this user acts for these practitioners"); the callee still re-checks what it can (tenant scope).

### OpenAPI spec

- A machine-readable description of an HTTP API: endpoints, parameters, request/response schemas, error shapes.
- Both teams code against the same file; drift between "what the API does" and "what the client expects" becomes a diff, not a surprise.

### Idempotency key (and why this design doesn't use one)

- The classic pattern: a client-generated unique id sent with a write request; the server stores it and replays the stored result on a retry — network retries become safe.
- Its cost: every client must generate, persist, and correctly reuse the key — a real burden for machine clients, and often done wrong.
- This design gets the same safety server-side instead (**replay detection**): a retried submission is recognized by comparing the stored `attestation_submissions` row's `attester_id` + `snapshot_version` against the incoming request — the client sends nothing extra.

### HTTP 409 Conflict

- The status code for "your request was well-formed, but it conflicts with the current state of the resource."
- Here it carries both guard rejections: stale snapshot (the data changed) and already-attested (someone else won).

### Content hash / SHA-256

- A hash function turns any input bytes into a fixed-length code; SHA-256 produces 256 bits, shown as 64 hex characters.
- Same input → same code, any change → a completely different code; the input cannot be recovered from the code.
- Here: the `snapshot_version` is a SHA-256 of the displayed field values — a fingerprint of "what the attester saw".

### Canonicalization

- Putting data into one unambiguous byte form before hashing: fixed field order, fixed formats, fixed encoding.
- Needed because JSON key order and whitespace are not stable — the same data can serialize to different bytes, which would break hash comparison.

### Optimistic concurrency (the stale-snapshot guard's pattern)

- Instead of locking data while a user works on it, the server gives out a version and checks it at write time; a mismatch means someone else changed the data first.
- Fits long-lived human interactions (a form open for minutes) where holding a database lock is impossible.
- Here the "version" is the content hash — so the check fires only on changes the user could actually see.

### Unique index / first-writer-wins

- A database rule refusing a second row with the same key value(s); enforcement happens inside the database, atomically.
- "First-writer-wins" built on it: whichever transaction inserts first succeeds; the second insert fails and is turned into a friendly 409.
- The design leans on this being database-enforced — no UI state, no application memory, no race window.

### Staging area

- A holding zone where incoming changes wait for review before touching the system of record.
- Here: staged change items are the reviewer's queue; nothing an attester submits reaches the Golden record without the review + Sync Latest path (D2-01).

### Webhook (and why the design avoids it)

- Push delivery: the producer HTTP-POSTs events to a URL the consumer registered.
- Costs: the consumer must run a receiving endpoint, the producer must run retry/backoff machinery, both must exchange secrets and reconcile missed deliveries.
- The design's alternative: the consumer reads state back from the same API it submitted to — pull, not push, with nothing to reconcile.

## Platform concepts

### api-layer

- The platform's main backend service (the monolith): external and internal HTTP APIs, credential validation, tenant-context resolution, request filters, and error conventions live here, deployed on Cloud Run.
- The design's only dependency on it: the **practitioner read endpoints** the backend uses for OV reads (prefill, submit-time comparison) — the module never reads a platform database directly (doc 1's rule).
- It was the proposed home of the Attestation Module Backend until D2-32 (2026-09-01) decided the module is its own service; that placement is now a rejected alternative in the module doc.

### OV (operational value) / Golden record

- The merged, authoritative practitioner record the MDM pipeline produces from all sources — the platform's system of record for provider facts.
- One row per practitioner; the row's stable key is `certify_id`.
- This module reads it (prefill values) and never writes it.

### DAL (data access layer)

- The platform service that owns database access: every table has a resource with REST endpoints; services call the DAL instead of the database.
- Migrations (Liquibase changesets) and query operators live with it.

### The DAL `/batch` endpoint

- One HTTP call carrying a list of operations (CREATE / UPDATE / FIND_OR_CREATE …) across multiple resources; with `transaction: true` they commit or fail as one Spanner transaction.
- Not in this module's write path (D2-32 — the module writes its own database through its own data layer). It appears in the design only historically: the superseded same-database alternative rode it, and the withdrawn api-layer outreach-consumer plan would have used it for `scheduled_outreaches` writes (both superseded — reminders are now Smart Outreach Service commands, doc 1's D12).

### Transactional outbox (and the relay sweep)

- The pattern for "commit a change AND tell another service" when the two cannot share a transaction: the intent is written as a row in an `attestation_outbox` table **inside** the business transaction, then published to Pub/Sub after commit.
- A crash between commit and publish loses nothing — the row is durable proof of unfinished intent; a scheduled **relay sweep** re-publishes any row still `PENDING` past a grace age.
- Here: the closing transaction writes one `OUTREACH_CANCEL` outbox row, published as a `CANCEL_SENDS` command; the Smart Outreach Service flips its own remaining `PENDING` sends to `CANCELLED` (doc 1's D12). Worst case is delay measured in minutes, never a lost cancellation.
- Pub/Sub **ordering key** = the `cancellationKey` (`attestation-task:<taskId>`): the cancel command can never overtake the task's earlier `SCHEDULE_SENDS`.

### `portal_application` (table)

- The portal stack's existing store for application-style submissions (Spanner, via the DAL), with a `pdm_ref` JSON column as a hand-off hook.
- An application store: no change items, no per-field states, no review model — which is why extending it was rejected (Supplementary, approach C).
- Its link-expiry columns (`attestation_expires_at` and siblings) are rails the portal stack has if it ever layers magic links onto the entry route — unused by this design's tokenless links (D17), never written by us.

### `portal_user` / `portal_user_tenant` / `portal_entity` (tables)

- The portal stack's identity model in the DAL: users, their tenant memberships, and `portal_entity` — the user → practitioner link.
- "Which practitioners can this user act for" is answered from these tables by provider-portal-api's resolution logic.

### DynamicFormRenderer

- The portal frontend's form engine: it renders forms from configuration, with per-field `prefill.editable` flags and a whole-form `readOnly` toggle.
- The design maps prefill editability + form mode onto these existing flags — no new renderer capability.

### Smart Outreach Service (and the legacy `scheduled_outreaches`)

- Reminders are sent by the platform's **Smart Outreach Service** (its own design doc): a recipient registry, an `outreach_sends` table-as-queue in its own `outreach-db`, service-owned templates, SendGrid behind an adapter, and an outcome topic.
- This lane never touches its tables: on submission it commits an `OUTREACH_CANCEL` outbox row, published as a `CANCEL_SENDS` command; the service flips its remaining `PENDING` sends to `CANCELLED` and reports each outcome.
- A tier already claimed by the service's sweep can send before the cancel lands — one extra email worst case, bounded by `expiresAt`; there is deliberately **no send-time task-state check** (doc 1's D16).
- The legacy `scheduled_outreaches` table (the api-layer credentialing engine) is not involved anywhere — the earlier plan that used it was withdrawn 2026-09-07.

### Tenant / tenant isolation

- A tenant is one customer organization; all data is tenant-scoped.
- Isolation is enforced server-side on every query and endpoint — UI filtering is never a boundary (v2 §8).

## Domain concepts

### Attestation

- A provider (or their admin) formally confirming that their directory information is accurate — the regulatory heart of the feature.
- The confirmation is recorded with who, when, what they saw, and what they changed.

### Attester types (PROVIDER / ADMIN)

- The practitioner themselves, or a provider admin acting on their behalf.
- Admin scope is tenant-level (any practitioner in their tenant, D2-17); the record always shows on-whose-behalf.

### PDM / non-PDM client

- PDM = the Provider Data Management product. PDM clients' practitioners use the portal.
- Non-PDM clients have no portal relationship — they consume the same attestation APIs directly, machine-to-machine (D2-21).

### Prefill / the mandated field set

- Prefill: the current Golden-record values shown to the attester so they confirm or correct real data instead of typing from memory.
- The field set is fixed by the product spec (S2 §1.5.9): name, group affiliation, addresses, phones, website, specialty, accepting-new-patients, cultural/linguistic capabilities, disability accommodations, telehealth, NPI (display-only). Fax and office hours excluded (D2-25).

### snapshot_version

- The content hash of the prefill values, issued at read time and echoed at submit time — the staleness guard's version.
- The evidence lives next to it: `FORM_PREFILLED` records the hash **and the field values as shown** (the OV keeps no history, so the hash alone can't reconstruct the screen later), once per distinct `(taskId, snapshotVersion)`.

### Form mode (and where "plan" went)

- A **form mode** is the attestation form behavior: `CONFIRM` (attest as-is), `FLAG` (mark wrong), `EDIT` (correct fields) — Product Spec S2 §1.3.7.
- The mode is set **per tenant** in `attestation-module-config`. The spec's "per plan configuration" wording meant the tenant — **plan = tenant for this module** (Product, 2026-09-01, D13); the attestation module is always tenant-scoped because review is done by one tenant's users.
- Practitioner↔plan linking (the schema path `tenant_group_practitioner_network_plans` → `network_plans` → `plans`) belongs to NSCP solutioning, not this module. In production that data does not exist anyway (verified 2026-08-31): the link table is missing from `data-production-b` and empty in `data-production`; `plans` / `network_plans` hold one test row each.
- The mode that governed the form is recorded at submission (`appliedMode`) — evidence, derived server-side, never client-claimed.

### Outcomes: NO_CHANGE / UPDATED / FLAGGED

- NO_CHANGE: everything is accurate — the attester's job is done, no data changes, and the record is still reviewer-visible (D2-09).
- UPDATED: the attester corrected fields — each correction is a delta.
- FLAGGED: the attester marked something wrong without providing the correction.

### Delta / staged change item

- A delta: one field-level change in the envelope — attribute, operation (ADD / UPDATE / REMOVE), old value, new value.
- A staged change item: the delta persisted as a row, source-tagged, waiting for review — the unit the reviewer accepts or rejects.

### Task states (and why nothing expires)

- Stored states: `SCHEDULED → OPEN → SUBMITTED` (doc 1's contract). `SCHEDULED` is internal — a future obligation, never shown to consumers.
- **OVERDUE is derived, never stored:** `state = OPEN AND next attestation date < today`, computed server-side — a fact about a date, not a state something has to flip.
- Tasks never expire — an overdue task stays `OPEN` and submittable forever (D2-23). The emailed link never expires either: it is tokenless (doc 1's D17), so there is nothing in it *to* expire. Token expiry would only exist if the portal team later layers its magic-link machinery onto the same entry route — a portal-side concern, outside this contract.

### Deep link

- The URL in the reminder email that lands the user directly on the attest flow for their task: the tokenless, stable entry URL `<portalBaseUrl>/attest/<taskId>` (doc 1's D17). It carries no credential and no provider data; authentication is the portal's normal login.

### Successor obligation (advancing the clock)

- After a submission, a new `SCHEDULED` row is inserted for the same practitioner: next attestation date = submission + 90 days, `lastAttestationDate` = the submission date.
- "Advancing the clock" is this insert — the schedule is rows, not a counter anywhere else.

---

# Supplementary material

Deep-dive material behind the module doc (`portal-lane.md`): baseline evidence, the full approach analysis with weightage and cost derivations, traceability, context, design rationale, open questions, ticket impact. Vocabulary is in the *Concepts* half of this file, above.

## Baseline — verified current behavior

One caveat upfront: **provider-portal-api is a separate repository, not present on this machine.** Its behavior is verified through its client contract (the calls the portal frontend makes to it) and the database tables it uses; claims about its internals are marked accordingly.

1. **Portal frontend:** `frontend/apps/provider-portal` — Next.js 15 (Pages Router), React 19, Auth0 for logged-in users (`package.json:12` `@auth0/nextjs-auth0`; `middleware.ts:15`). Public attest pages (`/attest`) bypass Auth0: the emailed link carries a JWT in `?token=` (`pages/attest/index.tsx:402`), exchanged for an opaque session token in an HttpOnly cookie (`attest-session`, 24-hour TTL, max 3 link re-sends/day — `constants/attestation.ts`).

2. **The attest API contract exists** — the frontend calls exactly four endpoints on provider-portal-api: `POST /api/v1/attest/verify`, `POST /api/v1/attest/validate-npi`, `GET /api/v1/attest/session-data`, `POST /api/v1/attest/submit` (`services/attestation-api-client.ts:51,97,99,108`).

3. **Link expiry is first-class**: `attestation_expires_at`, `attestation_sent_at`, `attestation_link_validity_days`, `attestation_invalidated_at` on the Spanner `portal_application` table (`portal-application/001:81-90`) — "links expire, tasks don't" (D2-23) has existing rails.

4. **Identity tables exist in the DAL (Spanner):** `portal_user`, `portal_user_tenant`, `portal_entity` (user → practitioner via `user_id` + `type` — `portal-entity/001`). The resolution logic lives in provider-portal-api's `EntityService` (referenced in `STATUS.md:6443`; not locally verifiable).

5. ⚠ **The portal never calls api-layer today.** The frontend calls only provider-portal-api (`services/api-client.ts` — every URL targets `NEXT_PUBLIC_API_URL`), and provider-portal-api reads the **DAL directly** (its filters use the DAL's own query operators). The earlier assumption (in the retired v1 SoT) that portal traffic flows through api-layer is wrong — decisive for approach B below.

6. **Submissions today land in `portal_application`** (Spanner, via the DAL), with a `pdm_ref` JSON column as the PDM hand-off hook (`portal-application/001:59`). No change items, no per-field states, no review model — an application store, not a workflow store.

7. **No open-task model exists anywhere in the portal stack**: nothing in provider-portal-api's visible contract or the DAL's portal tables represents "this practitioner owes an attestation now." That concept is born in doc 1's scan and lives only in the module's `attestation_tasks` table.

8. ⚠ **The form modes (confirm / edit / flag / view-and-attest) do not exist as configuration.** What exists: per-field `prefill.editable` in the dynamic form config (`DynamicFormRenderer.tsx:809,1330`), a whole-form `readOnly` toggle (`:884`), and per-tenant field config without any editability flag (`api-client.ts:203-217`). Modes are new work, not a reuse.

**Consequence:** the portal stack contributes exactly what it is good at — auth, sessions, links, identity resolution, form rendering — and contributes nothing toward tasks, staleness, staging, or review. Every approach is judged on where it puts *those*.

## Approaches analyzed in depth

**Decision criteria (stated before the approaches):**

1. **Least data exposed** — the portal sees exactly what its job needs: open tasks + the mandated prefill fields, never the full OV. The security default. Highest weight.
2. **Single write path** — submissions land in module staging exactly once, through one owner (v2 §5: the backend owns workflow state).
3. **Guard enforceability** — staleness, first-writer-wins, the successor row, and the audit rows need the task row, staging tables, and audit log in one database transaction; the reminder-cancellation *intent* (the `OUTREACH_CANCEL` outbox row) must commit in that same transaction (doc 1's D11 — the row flips themselves are consumer-side).
4. **Minimal disruption to the Portal team** — they keep their frontend, auth/session stack, and calling patterns.
5. **Ops burden / familiarity** — new moving parts, cross-repo machinery, who is on call.
6. **Cost** — with derivation; every candidate is API code on existing runtimes, so cost decides nothing here. Reported per the template.

**Volume inputs for every estimate:** ~780 open tasks/day; list + detail + submit per attester plus retries ≈ 2,500–3,000 calls/day ≈ **90k calls/month**; ~23,400 submissions/month; one staged item per changed field (most submissions are NO_CHANGE or small).

Weightage scale: **5 = fully satisfies · 3 = workable with caveats · 1 = fails or requires hand-building.**

### A. The chosen approach — the backend serves the APIs; provider-portal-api fronts them

- **How it works:** see the module doc's *Approach* (steps 1–5).

- **Pros:**
  - Task-scoped by construction — the portal surface is "tasks needing attestation"; the full OV never crosses to the portal stack.
  - All guards and the whole closing transaction commit where the data lives — one service, one database, one transaction.
  - One write path; `portal_application` untouched for directory work.
  - Portal team keeps everything they own; their new work is a thin proxy + identity pass-through.
  - The same endpoints serve non-PDM clients directly — one contract to maintain.

- **Cons:**
  - One extra server-to-server hop (portal-api → backend); negligible at this traffic, noted for honesty.
  - Requires provider-portal-api changes in a repository another team owns — coordination and access needed (Q1).
  - Two services now participate in the attest flow — the failure story must be explicit (it is: explicit unavailability, never a silent empty list).

- **Weightage:**

| Criterion              | Score | Note                                                      |
| ---------------------- | ----- | ---------------------------------------------------------- |
| 1 Least data exposed   | 5     | Tasks + mandated fields only, by construction             |
| 2 Single write path    | 5     | One owner; the database enforces it                        |
| 3 Guard enforceability | 5     | All guards + closing transaction native                    |
| 4 Portal disruption    | 4     | Proxy + pass-through work, but nothing they own changes    |
| 5 Ops / familiarity    | 5     | Endpoints on the module's own service (D2-32) — the service surface exists for the whole module regardless; this lane adds none |
| 6 Cost                 | 5     | ≈ $0–5/mo                                                  |
| **Total**              | **29** |                                                           |

- **Failure modes:** backend down → explicit unavailability, tasks are rows, nothing lost · double submit → unique index, one winner · retry after lost response → server-side replay detection · portal-api bug passes a foreign practitioner → backend re-checks tenant scope.

- **Cost ≈ $0–5/month, derived:** Cloud Run request time ~90k requests × ~200 ms × 1 vCPU ≈ 5 vCPU-hours × ~$0.086/vCPU-hour ≈ **< $1**; proxy code adds no measurable load to provider-portal-api; no new infrastructure. Storage counted in doc 6.

### B. The portal reads platform data directly

- **How it works:** provider-portal-api (or the frontend via it) reads practitioner OV data from the DAL as it does for its other features, renders the form from that, and submits to the DAL or to our backend. No task read surface on our side.

- **Pros:**
  - Zero new read endpoints; the read path exists today.
  - Portal team fully autonomous on the read side.

- **Cons:**
  - The DAL read surface is the whole practitioner OV — no mandated-field scoping, no open-task concept; task filtering rebuilt client-side.
  - Prefill issued by one system, submission accepted by another — the `snapshot_version` guard spans two systems that share no state.
  - The assumed rail (portal → api-layer) does not exist — building it is new plumbing with none of A's control.
  - Submissions landing in `portal_application` while workflow state lives in module staging = two write paths, permanent sync burden.

- **Weightage:**

| Criterion              | Score | Note                                                     |
| ---------------------- | ----- | ---------------------------------------------------------- |
| 1 Least data exposed   | 1     | Full OV exposed; scoping rebuilt client-side              |
| 2 Single write path    | 1     | Split read/write; two stores                              |
| 3 Guard enforceability | 1     | Staleness check between systems sharing no state          |
| 4 Portal disruption    | 5     | Lowest — they change nothing                              |
| 5 Ops / familiarity    | 3     | Existing reads, but the cross-system guard is hand-built  |
| 6 Cost                 | 5     | ≈ $0                                                      |
| **Total**              | **16** |                                                          |

- **Why it loses:** fails criteria 1–3 — the module's three highest — simultaneously. "Prove what the attester was shown" becomes a cross-system reconstruction, which the audit rules forbid.

- **Failure modes / what would break it:** the first compliance dispute; any OV churn during an open form (no reliable stale detection); every future consumer inheriting the full OV surface.

- **Cost ≈ $0/month** — existing reads; irrelevant given the disqualification.

### C. Extend the existing `portal_application` flow

- **How it works:** keep the portal's four attest endpoints and `portal_application` persistence; add columns/JSON for directory outcomes; forward accepted submissions to the Attestation Module afterwards (via `pdm_ref` or an event).

- **Pros:**
  - Smallest change for the Portal team — their submit path already exists.
  - Link-expiry columns already live on this table.

- **Cons:**
  - An application store carrying workflow state: no change items, no per-field operations, no review model — all bolted on.
  - The forward step is a two-store consistency problem: "submitted" shown to the attester with staging never (or twice) receiving it; outbox-style machinery inside a repo another team owns.
  - First-writer index, reminder cancellation, and the successor row live in our database; the accepting endpoint in theirs — the one-transaction property is unachievable.

- **Weightage:**

| Criterion              | Score | Note                                                       |
| ---------------------- | ----- | ------------------------------------------------------------ |
| 1 Least data exposed   | 3     | Task scoping possible, store wrong                          |
| 2 Single write path    | 1     | Two stores + forwarding                                     |
| 3 Guard enforceability | 1     | Guards split across services                                |
| 4 Portal disruption    | 4     | Their endpoints change too (columns, forwarding)            |
| 5 Ops / familiarity    | 1     | Permanent consistency machinery in a repo we don't own      |
| 6 Cost                 | 5     | ≈ $0 infrastructure                                         |
| **Total**              | **15** |                                                            |

- **Why it loses:** it trades A's one-time proxy work for a permanent cross-repo consistency burden, and no amount of forwarding machinery restores the one-transaction guarantee A gets natively.

- **Failure modes / what would break it:** any dispute requiring task + submission + staged items + audit to be provably consistent; a forwarding outage silently queueing "submitted" attestations.

- **Cost ≈ $0/month infrastructure**; the real cost is building and operating the forwarding machinery forever.

### D. Delegate prefill, staleness, and delta to the portal stack (config-driven)

- **How it works:** we hand provider-portal-api the form-mode + field-editability configuration; it reads the OV directly (its existing DAL access), builds the prefill, enforces modes, runs its own staleness check, and computes the delta. Our backend keeps only the submission endpoint and stages the finished envelope.

- **Pros:**
  - Maximum portal-team autonomy; our surface shrinks to one endpoint.
  - Reuses their existing direct DAL reads; modes travel as configuration.

- **Cons:**
  - `snapshot_version` issued and checked outside the system that accepts the submission — issue and acceptance share no state.
  - `FORM_PREFILLED` ("what did the attester see") recorded outside the append-only, same-transaction, 7-year audit store.
  - The delta crosses the trust boundary unverifiable — confirming `oldValue` means re-reading and re-deriving, the delegated work itself.
  - Non-PDM clients bypass the portal (D2-21) — the backend needs the identical logic anyway; two copies drift.

- **Weightage:**

| Criterion              | Score | Note                                                          |
| ---------------------- | ----- | ---------------------------------------------------------------- |
| 1 Least data exposed   | 2     | Full-OV read surface; field scoping is config discipline on their side |
| 2 Single write path    | 4     | Submissions still land once in staging — but as pre-computed deltas |
| 3 Guard enforceability | 1     | Snapshot issued and checked outside the accepting system       |
| 4 Portal disruption    | 2     | They build prefill, hashing, mode enforcement, delta computation |
| 5 Ops / familiarity    | 2     | Guard logic duplicated for non-PDM; two copies to keep aligned |
| 6 Cost                 | 5     | ≈ $0                                                           |
| **Total**              | **16** |                                                               |

- **Why it loses:** criterion 3 structurally (the guard's issuer must be its enforcer), and the non-PDM requirement forces the backend copy to exist regardless — delegation only adds a second, drifting one.

- **Failure modes / what would break it:** a compliance dispute needing prefill evidence from the portal stack; any divergence between the portal's guard copy and the backend's non-PDM copy.

- **Cost ≈ $0/month infrastructure** — the real cost is the permanent duplicated guard/prefill logic and the split evidence.

### E. Same-database placement (module tables inside app-data — the superseded 2026-08-29 design)

- **How it works:** the module's tables live as new tables inside the platform's primary app-data database; writes ride the DAL's transactional `/batch`; the closing transaction spans the module's tables *and* the reminder rows — submission, flip, reminder cancellation, and successor commit as one transaction, no outbox, no command. A third axis, orthogonal to the data path (A–D) and compute placement (below): where the module's *storage* lives.

- **Pros:**
  - The strongest possible consistency: a submitted task with reminders still firing is impossible by construction, not eventually repaired.
  - Rides existing rails (DAL repositories, Liquibase, `/batch`) — the least new code of any option.
  - Doc 1's opening transaction gets the same native atomicity (task flip + reminder ladder in one commit).

- **Cons:**
  - **Contradicts D2-32** — a decided constraint (instruction §6.1), which disqualifies the option regardless of its arithmetic below.
  - Shared-database coupling forever: schema, migrations, backups, and every DDL change coordinate with the platform team; the write path couples to the DAL service's availability and release cadence.
  - The module boundary becomes a naming convention (`attestation_*` prefixes); extracting later is a data migration under load.

- **Weightage:**

| Criterion | Score | Note |
| --- | --- | --- |
| 1 Least data exposed | 5 | Endpoints unchanged |
| 2 Single write path | 5 | One transaction, one owner |
| 3 Guard enforceability | 5 | Native — the option's whole appeal |
| 4 Portal disruption | 5 | Invisible to the portal |
| 5 Ops / familiarity | 3 | Rides rails, but permanent cross-team schema coordination |
| 6 Cost | 5 | ≈ $0 |

- **Why it loses:** it scores 28 and still loses — D2-32 decided the service boundary, and a module-owned database (`attestation-db`, doc 1's D8) is the storage half of that decision. The consistency it uniquely offered is recovered by the transactional outbox: the cancel intent commits with the submission and survives every crash; worst case one extra email, bounded by cancellation ordering and `expiresAt` (doc 1's D16 — no send-time check). Delay in minutes, never loss.

- **Cost:** infrastructure the same either way (Spanner bills the instance, not the database count); the saved outbox/consumer code was the option's only economy.

### Placement analysis — inside api-layer vs the module's own service

A separate decision axis from A/B/C (which are about the data path): where the chosen backend runs. **Decided by D2-32 (2026-09-01): the module is its own service** — building inside the monolith is no longer a candidate. The table stays as the record of what the decision costs and buys:

| Consideration | Inside api-layer (package) | Own service (D2-32) |
| --- | --- | --- |
| Auth for non-PDM callers | Existing key machinery — register a new key type | The module's own authentication layer, designed in doc 5 |
| Tenant resolution, filters, error conventions | Inherited | The module's own (doc 5) |
| Deploy pipeline, monitoring, on-call | Existing surfaces | The module's own — one surface for the whole module, not per lane |
| Isolation | Package boundary (a stated rule) — a bad unrelated deploy pauses the endpoints | Physical — unrelated deploys cannot touch it; release cadence and scaling are the module's own |
| Outage consequence | Tolerated by design: tasks are rows, SCHEDULED waits, links keep working after recovery | Same tolerance; the module's blast radius is its own |
| Cost (monthly) | ≈ $0 incremental | service runtime + monitoring owned by doc 5 (module-wide, not this lane's) |

**Verdict:** D2-32 accepted the own-service costs (the authentication layer and the operational surface are built once, in doc 5, for the whole module) in exchange for a real boundary: own deploys, own scaling, own blast radius, no monolith coupling. This lane's contribution is unchanged either way — the three endpoints, the guards, and the closing transaction are placement-independent.

### Comparison

| Criterion              | A Backend serves | B Direct reads | C Extend portal_application | D Delegate to portal | E Same-DB placement |
| ---------------------- | ---------------- | -------------- | --------------------------- | -------------------- | ------------------- |
| 1 Least data exposed   | 5                | 1              | 3                           | 2                    | 5                   |
| 2 Single write path    | 5                | 1              | 1                           | 4                    | 5                   |
| 3 Guard enforceability | 5                | 1              | 1                           | 1                    | 5                   |
| 4 Portal disruption    | 4                | 5              | 4                           | 2                    | 5                   |
| 5 Ops / familiarity    | 5                | 3              | 1                           | 2                    | 3                   |
| 6 Cost                 | 5                | 5              | 5                           | 5                    | 5                   |
| **Total (unweighted)** | **29**           | 16             | 15                          | 16                   | 28 — disqualified (D2-32) |
| **Cost (monthly)**     | ≈ $0–5           | ≈ $0           | ≈ $0 + permanent sync burden | ≈ $0 + duplicated guard logic | ≈ $0 + monolith coupling |

**Recommendation rationale:** A wins every criterion that carries weight and concedes only "portal disruption," where its cost is one proxy ticket. B and C each fail the three highest criteria structurally (split state), not fixably; D fails the guard criterion the same way and is doubly disqualified by the non-PDM requirement, which forces the backend implementation to exist no matter what the portal does. E scores nearly as high and is disqualified anyway — D2-32 is a decided constraint, and the outbox recovers the one property E uniquely offered.

*(No AWS variant: this module is an API contract on the module's own service (doc 5) — no separate infrastructure choice exists at this lane's level.)*

## Requirements traceability

| #   | Requirement                                                                                                                        | Trace              |
| --- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------ |
| F1  | Return the open/overdue attestation tasks for the resolved practitioners — and only those; never the full OV surface               | v2 §6.7            |
| F2  | Serve the prefill: current Golden-record values for the mandated field set, plus `snapshot_version`                                | v2 §6.7, D2-19     |
| F3  | Accept one submission envelope per task: outcome, per-field deltas, attester identity/type, timestamp                              | v2 §6.7, D2-21     |
| F4  | Stale-snapshot guard: reject a mismatched `snapshot_version`; the user refreshes and re-attests — never a silent overwrite         | v2 §6.7, D2-19     |
| F5  | First submission wins: enforced by a database uniqueness rule, not UI state; the loser sees who and when                            | v2 §6.7            |
| F6  | NO_CHANGE completes the attester's job (SUBMITTED + successor row, no data mutation) and stays reviewer-visible                     | D2-09              |
| F7  | The emailed link is tokenless and never expires (doc 1's D17); the task stays open and submittable indefinitely                      | D2-23, D17         |
| F8  | NPI is display-only — enforced server-side regardless of what the UI sends                                                          | v2 §6.7            |
| F9  | Provider admin scope = tenant-level, resolved server-side                                                                            | D2-17              |
| F10 | Non-PDM clients use the same endpoints directly: list (with status filters), submit, read back SUBMITTED — no webhook machinery     | D2-21 (refined)    |

Non-functional numbers: module doc, *Performance and scale*.

## Context from previous module docs

From **doc 1 — Cycle**, this doc inherits and does not re-decide:

- **The deterministic identity** `(tenant_id, practitioner_id, due_period)` (stored physically with `certify_practitioner_id`) — the endpoints return and accept it, never recompute it.
- **Task states** — stored: `SCHEDULED → OPEN → SUBMITTED`; OVERDUE derived server-side (`state = OPEN AND next_attestation_date < today`, doc 1's D9); no EXPIRED state; overdue tasks stay submittable (D2-23).
- **No OV data on task rows** (doc 1's D10): `certify_practitioner_id` is the only practitioner link — the list endpoint returns no names; provider-portal-api enriches display data from its own OV access.
- **Row-per-obligation in the module's own `attestation_tasks` table, in the module's own `attestation-db` database** (doc 1 D7/D8, revised 2026-09-01) — which is exactly why the task endpoints must live on our backend: the portal stack has no open-task concept, and the OV holds dates on no row at all (the schedule lives only in module rows).
- **The successor-row clock rule** — submission + 90 days, `source = SUBMISSION_ADVANCE`, `lastAttestationDate` = the submission date; this doc's closing transaction performs that insert.
- **The transactional outbox and the reminder-cancellation contract** (doc 1 D11/D12) — the submission transaction writes one `OUTREACH_CANCEL` outbox row (`OUTREACH_CANCELLED` audited with its `outboxEventId`), published as a `CANCEL_SENDS` command; the Smart Outreach Service flips its own remaining `PENDING` sends to `CANCELLED` and reports each outcome. This module never writes another service's table.
- **The audit envelope and table** — one shape, one table; this lane's events fill `submissionId` where the scan's fill `runId`.
- **Outreach deep links** — the reminder emails land on the portal attest flow; the link format is a shared contract (DA-07 ↔ DA-13, Q5).

Exports to later docs:

| Export                                                                     | Used by                             |
| --------------------------------------------------------------------------- | ------------------------------------ |
| The three-endpoint contract + OpenAPI spec (*Contracts*)                    | Backend (5) hosts, UI (7) mirrors patterns |
| `attestation_submissions` + `attestation_staged_items` shapes (*Data model*) | Backend (5), Database (6), UI (7)   |
| The closing-transaction operation list (*Contracts step 4*)                 | Backend (5) wiring                   |
| `snapshot_version` mechanics (hash + rules version)                         | Backend (5), UI (7, Sync Latest view) |
| Lane A audit events (*Audit trail*)                                          | Database (6), Operations             |

## Design rationale — anticipated questions

**"Why can't the portal just read the OV like it reads everything else?"**

- Its job here is "show what needs attesting" — a task list plus eleven mandated fields. The DAL read surface is the whole practitioner record with no task concept.
- The staleness guard needs the submission-accepting server to have issued the version at prefill time; split read/write makes the guard span systems that share no state.
- Least-privilege is the default for a surface reachable from a public attest flow.

**"Why a content hash and not the row's `updated_at`?"**

- The merge pipeline rewrites the OV row on every run, updating `updated_at` **even when no displayed value changed** — a timestamp guard would reject valid submissions for invisible reasons.
- Hashing exactly the displayed values means the guard fires only when something the attester actually saw is different.
- The rules-version prefix (`v1:`) keeps old versions recognizable if the field set or canonicalization ever changes.

**"Is the snapshot guard worth its complexity at all?"**

- The failure it prevents is quiet and compliance-shaped: a roster load changes an address while the form is open; the practitioner clicks NO_CHANGE; we record a confirmation of values they never saw. Nothing errors, nobody notices.
- Some staleness guard is mandatory (D2-19, v2 §6.7: never a silent overwrite) — dropping it reopens a v2 decision with Compliance, to save ~50 lines.
- The alternatives are not simpler: a timestamp check false-alarms (merge-pipeline churn); echoing all values back is the same comparison with a fatter payload — canonicalization is needed either way, and the hash just compresses the transport.
- Runtime cost: two single-row point reads of the OV (prefill, submit-compare), milliseconds each, nothing held in memory between them — the second read *is* the guard working.

**"Why store the prefill values when the hash already fingerprints them?"**

- A hash proves "unchanged since time T"; it cannot reproduce content — and the OV keeps no history, so three years later nobody can reconstruct the screen from a fingerprint.
- Storing the 11 values in `FORM_PREFILLED.detail` makes "show me exactly what the attester confirmed" answerable straight from the audit trail, under the same 7-year retention.
- Volume stays sane because the event is deduplicated on `(taskId, snapshotVersion)`: re-opening an unchanged form logs nothing; only a genuinely different screen (new version) produces a new event — the trail is "every distinct thing ever shown," with zero repetition. Mechanism: the event `id` is deterministic (a name-based UUID from taskId + snapshotVersion) — a repeat insert hits the primary key and is skipped; no lookup, no extra table, and the audit table is never *read* to decide.

**"Why is there no separate prefill endpoint?"**

- The prefill is meaningless without the task (which fields, which mode) and the task is unrenderable without the prefill — every consumer needs both, always, together.
- One call means one `snapshot_version` per render, one `FORM_PREFILLED` event, no window where the two disagree.

**"Why not webhooks for non-PDM clients?"** (D2-21 refined)

- Push delivery needs client-side receiving endpoints, producer-side retry/backoff, secret exchange, and missed-delivery reconciliation — permanent machinery.
- The information in question ("did my submission land?") is one `GET` with `status=SUBMITTED` on the API the client already uses.
- If a future client genuinely needs push, it is additive — nothing in the contract blocks adding it later.

**"What stops a browser from submitting for someone else's practitioner?"**

- The browser never supplies identity: portal-api resolves the user's practitioners server-side from `portal_entity` and passes resolved ids server-to-server.
- Our backend re-checks tenant scope on every call regardless — a portal-api bug still cannot cross tenants. Two layers.

**"Why does `ALL` not include SCHEDULED rows?"**

- A `SCHEDULED` row is internal schedule state — an obligation whose window has not arrived. Showing it invites premature submissions and confuses "what do I owe now."
- Non-PDM automation acting on unopened obligations would break the lifecycle (reminders not yet written, task not openable by submission).
- The scan (doc 1) is the only thing that turns SCHEDULED into consumer-visible work.

**"Why does the successor-row insert live in this module's transaction?"**

- Doc 1's contract: advancing the clock *is* inserting the successor row, and the flows that end a task own the insert.
- Outside the transaction, a crash between "SUBMITTED" and "successor inserted" silently drops the practitioner off the schedule — the exact class of gap the single-transaction rule exists to remove. Both rows live in the module's own tables, so no database boundary is in the way.

**"Reminder cancellation used to be inside the transaction — why is an event good enough now?"**

- It cannot be inside anymore: the reminder sends live in the Smart Outreach Service's own `outreach-db`, the module's tables live in `attestation-db` (D2-32 / doc 1 D8/D12), and Spanner has no cross-database transaction — let alone a cross-service one.
- What *is* in the transaction is the intent: the `OUTREACH_CANCEL` outbox row commits with the submission, so no crash anywhere can lose the cancellation — publish and consumption are retryable bookkeeping (dispatch, relay sweep, Pub/Sub redelivery).
- The residual window is bounded and cheap: a tier already claimed by the outreach sweep can send before the cancel lands — one extra email, reported back as `CANCEL_TOO_LATE`; `expiresAt` bounds anything staler. There is deliberately no send-time task-state check (doc 1's D16).
- Compare the risks: an extra email is noise; a lost cancellation (the pre-outbox risk of a post-commit call failing silently) would be a compliance-visible annoyance repeating three times. The outbox trades a small, self-healing delay for structural loss-impossibility.

**"Why is there no session-start audit event from portal-api?"**

- Sessions are the portal stack's state, recorded in its own logs; nothing in the module's data changes when a user logs in.
- The module's audit trail answers questions about *its* facts — what was shown (`FORM_PREFILLED`), what was submitted, what was rejected. "Who logged in when" is answerable from the portal stack if ever needed.
- Structurally, a portal-api-emitted event would also be a cross-service write into `attestation-db` — a foreign writer inside the module's boundary for evidence the module does not need.

**"How do replay detection and first-writer-wins interact — and why no idempotency header?"**

- Every duplicate submit hits `UNIQUE (task_id)` on `attestation_submissions`; the handler then reads the existing submission row to decide which case it is.
- Existing row matches the incoming request on `attester_id` + `snapshot_version` → the same logical submission retried → original 200 replayed from the stored row.
- Either differs → a genuinely different submission (in practice: another actor in the concurrent race) → 409 already-attested with who/when.
- Note a refreshed or stale tab never reaches this check: a submitted task's detail response carries no prefill and no snapshot (nothing to echo), and a stale snapshot is rejected by the staleness guard before any insert. The check runs at two trigger points only: the state guard finding SUBMITTED (sequential retry), and the unique-index abort (concurrent race).
- The header alternative (client-generated `Idempotency-Key`) buys nothing this doesn't, and costs every non-PDM client key-generation machinery they will get wrong; dropped deliberately.
- Everything is database-enforced (`UNIQUE (task_id)`) plus one indexed read; no application memory anywhere.

**"A practitioner is in two plans — don't they owe two attestations? And what if the plans' data differs?"** *(Closed by Product, 2026-09-01 — see Q8.)*

- Product confirmed: the practitioner **attests once**; "plan" in the spec's mode wording refers to the tenant; the module is always tenant-level because review is done by one tenant's users.
- The technical reasoning still stands: the mandated fields are practitioner facts in the OV — **one value set per practitioner per tenant**; there is no "phone number for Gold" to attest separately.
- Per-plan tasks would attest identical data twice, and worse: correcting a value in one plan's task makes the other plan's open prefill stale — the design's own staleness guard would fight the practitioner. (This alternative was analyzed in full and removed from the module doc 2026-09-01 — record below.)
- Practitioner↔plan linking is NSCP-solutioning scope; the plan tables carry no production data anyway (link table missing from `data-production-b`, verified 2026-08-31).

**Record — the removed "one task per plan" alternative (was in the module doc's Alternatives until 2026-09-01):**

- Identity would become `(tenant, practitioner, plan, due_period)` — a practitioner in Gold and Silver gets two tasks per cycle, two reminder ladders, two submissions. Pros: no mode-resolution rule; per-plan clocks and reporting.
- Rejected because: it attests identical practitioner facts twice (duplicate outreach/attester work/review); the two attestations conflict (an edit in one makes the other's open prefill stale); row volume multiplies by plan count through every downstream module; it reopens D2-12's fixed identity. Product's 2026-09-01 answer (attest once, plan = tenant) settles it — the variant is retired, kept here only as the analysis record.

**"Why is a NO_CHANGE submission still reviewer-visible?"**

- D2-09: task SUBMITTED ≠ workflow complete. The reviewer may hold vendor recommendations for the same practitioner; "the provider says nothing changed" is evidence they weigh against those, not a reason to skip review.

**"What happens if provider-portal-api itself is compromised?"**

- Blast radius is bounded: our backend re-checks tenant scope, NPI edits are rejected server-side, and every write is audited with actor and snapshot.
- The attack surface it adds (resolved-identity spoofing within one tenant) is the same one every portal feature already carries — no new class of exposure.

## Open questions and sign-offs

| #   | Question                                                                                                                                                    | Owner                          |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| Q1  | provider-portal-api repo access + that team's buy-in for the proxy endpoints and identity pass-through (internals unverified locally)                        | Module owner + Portal team lead |
| Q2  | ✅ CLOSED (2026-09-01). `PORTAL_SESSION_STARTED` **removed** — sessions are the portal stack's state (its own logs); the module's trail starts at `FORM_PREFILLED`; a portal-api-emitted event would also be a cross-service write into `attestation-db`. No emission asked of the portal team | —                               |
| Q3  | The field list is settled (S2 §1.5.9); remaining: the field-by-field technical mapping to Golden-record paths (which OV attribute/list, which list keys) — agreed before the OpenAPI spec freezes | Product + Eng                   |
| Q4  | Form-mode config shape: `portalMode` + per-field editability living in `attestation-module-config`                                                           | Eng, doc 5 review               |
| Q5  | ✅ CLOSED (2026-09-07, doc 1's D17): the emailed deep link is **tokenless** — `<portalBaseUrl (tenant config)>/attest/<taskId>`, a stable entry route the portal stack owns (free to redirect internally); authentication is the portal's normal login; magic-link tokens are an optional portal-side layer later, no contract change | —                               |
| Q6  | The backend's own authentication layer (D2-32 — the module authenticates its own callers): credential type, issuance, and validation for direct non-PDM consumers, and the service-to-service trust with provider-portal-api. Requirement stated in this doc; design owned by doc 5 | Eng (doc 5) + Security          |
| Q7  | ✅ CLOSED by D2-32 (2026-09-01): the module is its **own service** — the api-layer package placement is a rejected alternative in the module doc. Service decomposition (one service vs several) remains doc 5's | —                               |
| Q8  | ✅ CLOSED (Product, 2026-09-01). Answers: tenant-level config approved — "plan" in the spec meant the tenant (v2 wording amended); the practitioner **attests once** — one attestation, common fields (NPI, specialty) plus payor-varying fields (accepting patients, locations) noted as future NSCP concern; most-permissive (`EDIT > FLAG > CONFIRM`) endorsed for a common attestation should modes ever differ; practitioner↔plan record linking handled during **NSCP solutioning** — this module stays tenant-level permanently, so no per-plan machinery is planned here at all | Product + module owner          |

## Ticket impact

| Ticket | Impact                                                                                                                                                                                       |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DA-08  | **Reframed:** the Portal team consumes `GET /attestation-tasks` + task detail from our backend via the portal-api proxy — not a direct OV read. OpenAPI spec added as a deliverable.        |
| DA-09  | **Reframed:** submit goes portal-api → backend `POST /attestation-tasks/{id}/submission` behind the full guard chain; `portal_application` is never written for directory attestations. The closing transaction includes the `OUTREACH_CANCEL` outbox row (reminder rows flipped consumer-side, doc 1 D11/D12) and the successor SCHEDULED row (doc 1 contract). |
| DA-10  | Confirmed; specify `snapshot_version` = content hash of displayed values with a rules-version prefix (never `updated_at`).                                                                   |
| DA-13  | Confirmed; add the link-format contract (Q5) and the reuse of the existing attest-session mechanics.                                                                                         |
| New    | provider-portal-api ticket (their backlog): proxy endpoints + resolved-identity pass-through (Q1). No audit emission asked of them (Q2 closed — event removed).                              |
| Corrections to record | Portal does not call api-layer today (reads the DAL); form modes don't exist as config (new work); plan tables carry no production data — `tenant_group_practitioner_network_plans` absent from `data-production-b`, empty in `data-production`, `plans`/`network_plans` one test row each (verified 2026-08-31; moot for this module since plan = tenant, Product 2026-09-01); provider-portal-api internals unverified locally; non-PDM delivery = direct API consumption, not webhooks (D2-21 refined — v2 wording touched when doc 5 lands the auth model). |
