# Design: Portal lane — attestation task APIs

## Purpose and scope

The Portal UI is built and owned by a different team. This module is therefore **not** a UI design. It is the design of the **attestation task API surface**: which service exposes it, what the endpoints and payloads are, what guards the server enforces, and where submissions land.

The endpoints are deliberately **not portal-specific**: the Portal team consumes them through provider-portal-api, and **non-PDM clients consume the very same endpoints directly** (get their tasks, submit, read back SUBMITTED status). One contract, two consumers.

**This module owns:**

- The **API contract** for attestation work: task discovery ("what does this user owe?"), prefill ("what data do they review?"), and submission ("record what they attested").
- The **server-side guards**: stale-snapshot rejection, first-submission-wins, NPI read-only enforcement, tenant-scoped identity checks.
- The **submission envelope** (snapshots + delta + metadata + routing context, per D2-21) and where it lands — source-tagged staging in the module's own tables.
- The **submission transaction**: task → SUBMITTED, staged change items, reminder cancellation, and the successor obligation row — committed together.
- The **outcome semantics**: NO_CHANGE vs edits/flags; task SUBMITTED ≠ workflow complete (D2-09).
- **Direct non-PDM consumption** of the same endpoints.

**This module does NOT own:**

- The Portal UI itself — screens, form rendering, UX (Portal team).
- Login and session mechanics — these exist and stay in the portal stack (provider-portal-api + portal frontend); any future magic-link tokens are theirs too (the emailed link itself is tokenless, doc 1's D17).
- Resolving "which practitioners can this user act for" — the portal stack's `portal_entity` model does this today; this module consumes the resolved identity and never trusts the browser.
- Review, release, or anything downstream of staging (docs 5–7).

**Neighbors:** Cycle (doc 1) creates and opens the tasks this module serves, and defines the identity, states, and successor-row clock rules; the Attestation Module Backend (doc 5) hosts these endpoints; the Database (doc 6) owns the final DDL for what they write; the UI doc (7) consumes the staged items on the reviewer side.

## Section triage

| Section                       | Included | Reason                                                                     |
| ----------------------------- | -------- | ---------------------------------------------------------------------------- |
| Approach                      | Yes      | The chosen design as numbered steps                                        |
| Alternatives considered       | Yes      | Four rejected designs (data path, guard delegation, monolith placement, shared-database placement), each self-contained |
| Components and files touched  | **No**   | Pre-implementation design doc — no file lists yet                          |
| Contracts and interfaces      | Yes      | The three endpoints with full payloads — the core of this module           |
| Data model and migration      | Yes      | The submission + staged-item row shapes this module writes                 |
| Security, privacy, and access | Yes      | Identity resolution, tenant isolation, NPI policy                          |
| Performance and scale         | Yes      | The NFR numbers the cost estimates use                                     |
| Observability                 | Yes      | Attesters being turned away must be seen immediately                       |
| Audit trail                   | Yes      | **Added section** — "what did the attester see" is compliance evidence     |
| Failure modes and rollback    | Yes      | Concurrency, staleness, retry, and outage behavior                         |
| Test strategy                 | **No**   | Design phase; decisive failure cases are in Failure modes                  |
| Rollout                       | Yes      | Cross-team sequencing with the Portal team, pilot ordering, kill switch    |

## Approach

The Attestation Module Backend owns three generic task endpoints; provider-portal-api keeps auth, sessions, and identity resolution and fronts them server-to-server; non-PDM clients call the same endpoints directly. Each step: what we do, why, and why not the alternative a reviewer would ask about.

**1. The backend serves three generic task endpoints — where the task data lives.**

- **What:**
  - `GET /attestation-tasks` — the tasks for a set of resolved practitioner ids; defaults to actionable tasks (OPEN, overdue included — OVERDUE is derived, doc 1's D9), with a `status` filter for history.
  - `GET /attestation-tasks/{taskId}` — one task plus its prefill and `snapshot_version` in one call.
  - `POST /attestation-tasks/{taskId}/submission` — the envelope, behind all guards.

- **Why the backend and nowhere else:**
  - Open tasks exist only in the module's `attestation_tasks` table (doc 1) — the portal stack has no open-task concept, and the OV holds provider facts, not workflow state.
  - The guards (staleness, first-writer-wins) and the submission's writes need the task row, the staging tables, and the audit log in **one database transaction** — only the service owning those tables can give that.

- **Why generic names, not portal-prefixed:** the portal is one consumer among others — non-PDM clients use the identical surface (step 5). One contract to version, document, and test.

- **Where the backend runs — the module's own service (D2-32; doc 5 owns the decomposition):**
  - The Attestation Module is built as its own service(s): these endpoints, their guards, and the submission transaction run in the module's own deployable, against the module's own database (`attestation-db`, doc 1's D8).
  - Authentication is the module's own layer: portal traffic arrives server-to-server from provider-portal-api with resolved identity; non-PDM callers present tenant-scoped API credentials validated by the backend itself. This doc states the requirement only — the authentication layer's design is doc 5's.
  - Why not inside api-layer: *Alternatives considered* (superseded by D2-32).

- **What if it fails:** the backend down means the portal shows an explicit "attestation temporarily unavailable" — never a silent empty list; tasks are rows, nothing is lost, links keep working when it returns.

- **Payloads:** *Contracts → lifecycle steps 1–3*.

**2. provider-portal-api stays the portal's single door.**

- **What happens, click to render:**
  - The user arrives — through the emailed tokenless deep link (`<portalBaseUrl>/attest/<taskId>`, doc 1's D17) or by logging in directly; either way authentication is the portal's normal login (Auth0) — the link carries no credential and no data.
  - provider-portal-api resolves who the user may act for from `portal_entity`: a practitioner resolves to themselves; a provider admin resolves to every practitioner in their tenant (D2-17).
  - It calls our task endpoints server-to-server, passing the **resolved** practitioner ids and tenant — never anything browser-supplied.
  - It enriches our lean responses for display (practitioner names from its own OV access, D10) and hands the result to the portal frontend — whose calling pattern (everything through provider-portal-api) is unchanged.

- **Why:**
  - The session machinery exists and works — rebuilding any of it buys nothing. (Magic-link tokens, if the portal team layers them onto the same entry route later, are a portal-side enhancement that changes nothing in this contract.)
  - Identity must be resolved server-side (F-requirement: never trust the browser); their stack already owns that resolution.

- **Why not have the frontend call our backend directly:** it would put auth, session, and practitioner resolution — the portal stack's job — on our side, duplicate their machinery, and break their verified calling pattern.

- **What if it fails:** portal-api down means the whole portal is down — an existing, monitored failure mode owned by that team; our tasks and links are unaffected.

**3. Task detail = prefill + `snapshot_version`, one call.**

- **What happens on the call, in order:**
  1. provider-portal-api calls `GET /attestation-tasks/{taskId}` with the resolved identity.
  2. The backend loads the task row from `attestation_tasks`, re-checks tenant scope, and branches by state: `SCHEDULED` or `CLOSED` → 404, `SUBMITTED` → attestation summary only, `OPEN` → continue.
  3. It reads the tenant's form mode (`portalMode`) and per-field editability from `attestation-module-config` — one mode per tenant (D13).
  4. It reads the mandated fields' current values from the OV — through the existing api-layer practitioner read endpoints (the module never reads a platform database directly, doc 1's rule; the exact endpoint is pinned in doc 5) — canonicalizes them, and hashes them into the `snapshot_version`.
  5. It writes the `FORM_PREFILLED` audit event (values included, once per distinct snapshot — D11) and returns task + prefill + `snapshot_version` in one response.

- **What:** no separate prefill endpoint — the task detail *is* the prefill; one request renders (or programmatically fills) the attestation.

- **Why a content hash and not a timestamp:** the OV row's `updated_at` changes whenever the merge pipeline runs, **even when no value changed** — a timestamp guard would reject valid submissions for invisible reasons. Hashing the displayed values fires the guard only when something the attester actually saw has changed.

- **What if it fails:** the read is stateless — a failed call is retried by the client; the audit event `FORM_PREFILLED` is written with the response, so "what was shown" is never reconstructed.

- **Payloads and hash mechanics:** *Contracts → lifecycle step 2 and the `snapshot_version` subsection*.

**4. Submission behind guards — one transaction closes the whole cycle.**

- **What the submit endpoint enforces, in order:**
  - task exists (else 404) → **tenant/practitioner match the resolved identity (else 404 — scope is verified before any task state, attester name, or timestamp is revealed; a wrong-tenant caller learns nothing, not even that the task exists)** → state check: `SCHEDULED` or `CLOSED` → 404; **`SUBMITTED` → the replay check** (the sequential-retry path — see *Contracts → step 4*); `OPEN` → continue (an overdue task is still `OPEN` — OVERDUE is derived, never stored) → `snapshot_version` matches current (else 409 stale) → NPI untouched (a delta naming NPI → 400 `NPI_EDIT_REJECTED`, never a silent strip) → accept; the first-writer unique index catches only the concurrent race two requests can create.

- **What one transaction then commits (the module's own database, `attestation-db` — doc 1's D8):**
  - the submission row + staged change items (source-tagged),
  - task → `SUBMITTED`,
  - one `attestation_outbox` row (type `OUTREACH_CANCEL`) — published post-commit as a `CANCEL_SENDS` command to the Smart Outreach Service, which flips its own remaining `PENDING` sends to `CANCELLED` and reports each outcome (doc 1's D11/D12),
  - the **successor obligation**: a new `SCHEDULED` row, next attestation date = submission + 90 days (doc 1's clock rule; submission-anchored, confirmed by Product 2026-09-01),
  - the audit rows for all of it.

- **Where those writes land — two new tables plus doc 1's `attestation_tasks` and `attestation_outbox`. Each stores a different kind of fact:**

- **`attestation_submissions` — the evidence. One row per act of attesting.**
  - What it stores: who attested (`attester_id`, type, display name), when (`attested_at`), against which `snapshot_version`, with which outcome — written once at accept, never updated.
  - Why it must exist: `UNIQUE (task_id)` on this table **is** the first-submission-wins guard, and the stored `attester_id` + `snapshot_version` are the replay-detection keys (*Contracts → step 4*).
  - Why it can't be inferred from staged items: a `NO_CHANGE` attestation writes **zero** staged items — the submission row is the only record the act happened at all (D2-09). "Staged items exist → submitted" holds; the converse doesn't.
  - Why the task row can't do this: a unique index fires on INSERT — two racing submits, one insert wins, the database decides. The task row is UPDATEd in place: both racers read `OPEN`, both flip it, nobody loses — the guard would move from the database into application code.
  - The task row is also mutable workflow state; attestation facts are frozen point-in-time evidence. Mixing the two puts rewritable columns under facts that must never change.

- **`attestation_staged_items` — the proposed changes. One row per changed field.**
  - Each row says one thing: "change this field from X to Y" — which attribute, what kind of change (`ADD | UPDATE | REMOVE`), the old value, the new value, and who proposed it (the `source` tag survivorship ranks at release). Full column list: *Data model*.
  - Why one row per change: the reviewer decides each change on its own — approve the phone fix, reject the address removal. A row is the smallest unit that can carry its own approve/reject state, its own release, its own audit trail.
  - Why not columns on the task row: one submission can propose up to ~11 changes, and a single row holds a single state — it can say "approved" or "rejected", never "phone approved, address rejected".
  - Why not one JSON blob on the task: the same single-state problem — plus the review screen must list, filter, and count individual changes, and values buried inside a blob can't be indexed or queried the way rows can.

- **`attestation_tasks` (doc 1's) — the schedule. Touched, not extended:**
  - This step writes the `SUBMITTED` flip on the current row and inserts the successor `SCHEDULED` row; no new columns.
  - The three tables count three different things — obligations, acts of attesting, proposed changes — and no fact is stored twice, so nothing must be kept in sync. Worked example: *Data model → why three tables*.

- **`attestation_outbox` (doc 1's) — the cross-service intent. One row inserted here:**
  - The `OUTREACH_CANCEL` row commits with the submission and is published post-commit (doc 1's dispatch + relay sweep); the reminder cancellation itself happens consumer-side.

- **Why one transaction:** a partial module-side result (submitted but no successor row — the practitioner silently drops off the schedule; submitted but no evidence row) is impossible by construction, not by compensation logic. The one cross-service effect — reminder cancellation — commits *as intent* in the same transaction (the outbox row), so it survives every crash; a tier already claimed by the outreach sweep can send before the cancel lands — one extra email worst case, bounded by `expiresAt`; there is deliberately no send-time task-state check (doc 1's D16).

- **What if it fails:** the transaction rolls back whole — the task stays OPEN, the attester retries; a retry after a lost response is recognized server-side (replay detection, *Contracts → step 4*) and gets the original result back instead of double-writing.

- **Payloads:** *Contracts → lifecycle steps 3–4*.

**5. Non-PDM clients call the same endpoints directly.**

- **What:** clients without a PDM relationship use the identical three endpoints with their own tenant-scoped API credentials: list tasks → submit the same envelope → read back `SUBMITTED` status as their confirmation.

- **Why direct consumption and not webhooks:** push delivery means retry machinery, client-side receiving endpoints, and secret exchange — for information the client can read back from the same API in one call. This refines D2-21 ("same envelope over REST") from webhook-push to direct consumption.

- **What if it fails:** a client that cannot reach the API simply retries; the same guards and idempotency apply — there is no separate delivery state to reconcile.

**What the approach deliberately does NOT do:**

- No portal-specific endpoints, no second API surface for non-PDM clients.
- No writes to `portal_application` — it keeps serving the portal's existing non-directory flows untouched.
- No writes to the OV or any core practitioner table — prefill reads it, nothing here writes it.
- No writes to any other service's database — the one cross-service effect (reminder cancellation) leaves as a `CANCEL_SENDS` command through the outbox (doc 1's D11/D12); the Smart Outreach Service's tables are never touched.
- No webhook dispatcher, no push delivery machinery.
- No portal auth, session, or link mechanics of our own — the portal stack's verified machinery is reused as-is. (The backend's own authentication layer for direct API callers is required here and designed in doc 5.)

### Key decisions

| #   | Question                        | Resolution                                                                                              |
| --- | ------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| D1  | Who serves the attestation APIs | The Attestation Module Backend; provider-portal-api fronts them server-to-server with resolved identity |
| D2  | Prefill delivery                | Inside the task-detail response with its `snapshot_version` — no separate prefill endpoint              |
| D3  | Staleness guard                 | `snapshot_version` = content hash of the displayed values — never a timestamp                            |
| D4  | First submission wins           | `UNIQUE (task_id)` on the submissions table; the loser gets 409 with who attested and when              |
| D5  | Non-PDM delivery                | Direct consumption of the same endpoints (D2-21 refined) — no webhook machinery                          |
| D6  | Consumer-visible states         | `SCHEDULED` and `CLOSED` rows are never returned — list filters exclude them, and the detail endpoint answers **404** for them (`CLOSED` = terminated practitioner, doc 1's D18) |
| D7  | Submission transaction scope    | Submission + staged items + SUBMITTED flip + successor row + `OUTREACH_CANCEL` outbox row + audit — one transaction in `attestation-db`; the reminder sends themselves are flipped to `CANCELLED` by the **Smart Outreach Service** consuming the `CANCEL_SENDS` command (doc 1's D11/D12) |
| D8  | NPI                             | Display-only, enforced server-side regardless of what the UI sends                                       |
| D9  | OVERDUE in the API              | A derived value (doc 1's D9): `status=OVERDUE` and the `overdue` flag translate to `state = OPEN AND nextAttestationDate < today`, computed server-side |
| D10 | Lean responses — no OV data     | Task responses carry `certifyPractitionerId` and task fields only; **provider-portal-api enriches display data (names) from its own OV access**. The one exception: the prefill's mandated fields, which must come from us so the `snapshot_version` guard works |
| D11 | Prefill evidence                | `FORM_PREFILLED` stores the shown **values**, not just the hash (the OV keeps no history — the hash alone can't reconstruct what was shown); written once per distinct `(taskId, snapshotVersion)`, never per render — dedup enforced by a deterministic event `id` (derived from taskId + snapshotVersion): a duplicate insert hits the primary key and is skipped; the audit table is never *read* as lookup state |
| D12 | Backend placement               | The module's **own service** (D2-32, 2026-09-01 — supersedes the earlier api-layer package placement, now a rejected alternative). Service decomposition, runtime, and the module's own authentication layer are doc 5's |
| D13 | Plans and form modes            | **Plan = tenant for this module (Product, 2026-09-01).** One task per practitioner (identity unchanged, D2-12); one attestation covers the practitioner's Golden-record facts; form mode comes from **tenant-level** `attestation-module-config`; `appliedMode` recorded as evidence at submission. The module is always tenant-scoped — review is done by one tenant's users. Practitioner↔plan linking and per-payor field variation belong to NSCP solutioning, not this module |
| D14 | Prefill/guard ownership         | Stays in the backend — delegating prefill, staleness, and delta to the portal stack analyzed and rejected (*Alternatives considered*); non-PDM direct consumers force the backend copy to exist regardless |
| D15 | List-entry deltas               | `oldValue`/`newValue` carry the **full entry object**; `entryKey` is a per-conversation routing id; the survivorship grouping key is derived from the entry's values at release (doc 5, `ARRAY_FIELD_GROUPING_KEYS`) — never stored as a copied column |

## Alternatives considered

Each alternative in full: what it is, its genuine strengths, its costs, and the direct reason it lost to the chosen approach.

### The portal reads practitioner data from the platform surface directly

- **What it is:**
  - provider-portal-api (or the frontend via it) reads practitioner OV data the way it already reads other platform data — directly from the DAL — renders the attestation form from that, and submits either to the DAL or to our backend.
  - No task endpoints on our side for the read path.

- **Pros:**
  - Zero new read endpoints — the portal stack already reads practitioner data from the DAL today.
  - The Portal team is fully autonomous on the read side.

- **Cons:**
  - **Wrong scope of data:** the DAL read surface exposes the whole practitioner OV — far more than the mandated field set, with no notion of an open task; task filtering would be re-implemented client-side.
  - **No staleness control:** the guard needs the server that accepts the submission to have issued the `snapshot_version` at prefill time; with reads in one system and writes in another, version checking spans two systems that share no state.
  - **Two write paths risk:** if submissions keep landing in `portal_application` while directory attestations need staging + review states, the workflow store splits across two models — a permanent sync problem.

- **Why rejected:** it fails the module's three highest criteria at once — least data exposed, single write path, guards in one transaction. The first compliance question ("prove what the attester was shown") needs `FORM_PREFILLED` + `snapshot_version` recorded by the system of record; split read/write paths make that a reconstruction, which the audit rules forbid.

- **Cost:** ≈ $0/month (existing reads, no new infrastructure) — and irrelevant, because the approach is disqualified on the criteria above.

### Delegate prefill, staleness, and delta to the portal stack

- **What it is:**
  - provider-portal-api owns the whole read side: we hand it the form-mode + field-editability configuration, it reads the OV directly (its existing DAL access), builds the prefilled form, enforces modes, runs its own staleness check, and computes the delta.
  - Our backend keeps only the submission endpoint: it receives the finished delta envelope and stages it.

- **Pros:**
  - Maximum portal-team autonomy — form, prefill, and guard iterate without touching our backend.
  - Reuses the portal stack's existing direct DAL reads; our surface shrinks to one endpoint.
  - Config-driven: form modes travel as configuration; no new portal-api capability beyond reading it.

- **Cons:**
  - **The guard splits from its enforcement point:** `snapshot_version` must be issued by the system that later validates it. With the portal issuing it, we accept submissions against a snapshot we never saw — issue and acceptance live in two systems sharing no state.
  - **The compliance evidence moves out of the audit store:** "what did the attester see" (`FORM_PREFILLED`) would be recorded in the portal stack — outside the module's append-only, same-transaction, 7-year store.
  - **The delta arrives unverifiable:** computed across the trust boundary; the backend cannot confirm `oldValue` was really the displayed value without re-reading the OV and re-deriving — the exact work being delegated.
  - **Non-PDM clients bypass the portal entirely** (D2-21): they need prefill, snapshot, and guards from us directly — so the backend must implement the same logic anyway, and delegation leaves two drifting copies.

- **Why rejected:** it trades the module's compliance spine — guard issued where it is enforced, evidence in the audit store, one copy of the logic serving both consumer types — for portal-team autonomy. The non-PDM requirement alone forces the backend implementation to exist; the portal's copy would be a second one.

- **Cost:** ≈ $0/month infrastructure either way — the real cost is the duplicated guard/prefill logic and the split evidence, both permanent.

### Inside api-layer, as an `attestation` package (the superseded placement)

- **What it is:**
  - The three endpoints and the submission transaction live in the platform's api-layer monolith as a new `attestation` package, with a stated boundary rule (the package owns its tables and exposes interfaces).
  - Credential validation, tenant-context resolution, request filters, error conventions, deploy pipeline, and monitoring are inherited from the host service.

- **Pros:**
  - Everything the endpoints need exists there today — non-PDM key validation in particular becomes registering a new key type on existing rails instead of building an authentication layer.
  - Zero new deployables: no new pipeline, dashboard set, alert set, or on-call surface.
  - ≈ $0 incremental infrastructure.

- **Cons:**
  - Contradicts D2-32: the module's compute lives inside the monolith — its release cadence, scaling profile, and outage blast radius are the monolith's, permanently.
  - The package boundary is a convention, not a wall: nothing physically stops another package reaching into the module's tables, and extracting the package later is a live migration.
  - The module's availability couples to every unrelated api-layer deploy.

- **Why rejected:** D2-32 (2026-09-01) decided the service boundary — the Attestation Module is its own service, and building inside the monolith is no longer a candidate approach. The inherited-rails economy is real, and it is the cost D2-32 accepted paying: the authentication layer is built as the module's own (doc 5).

- **Cost:** ≈ $0/month infrastructure — the option's whole appeal, paid for with permanent monolith coupling.

### Same-database placement (module tables inside the platform's primary database)

- **What it is:**
  - `attestation_submissions`, `attestation_staged_items`, and doc 1's tables live as new tables inside the platform's primary app-data database; writes go through the DAL's transactional `/batch` endpoint.
  - The closing transaction spans the module's tables *and* the reminder rows: submission + flip + reminder cancellation + successor commit as one transaction — no outbox, no command.

- **Pros:**
  - The strongest possible consistency: a submitted task with reminders still firing is impossible by construction, not eventually repaired.
  - Rides existing rails: DAL repositories, Liquibase migrations, `/batch` — the least new code of any option.

- **Cons:**
  - Contradicts D2-32: the module's state lives inside the monolith's database — schema, migrations, backups, and every DDL change coordinate with the platform team forever.
  - The database boundary becomes a naming convention (`attestation_*` prefixes), not a real one; extracting it later is a data migration under load.
  - Couples the module's write path to the DAL service's availability and release cadence.

- **Why rejected:** doc 1 decided this for the module as a whole (D8, revised 2026-09-01): the module owns `attestation-db`, and every cross-service effect leaves through the transactional outbox. The one real loss — reminder cancellation inside the closing transaction — is bounded: the cancel intent commits *with* the submission and survives every crash; worst case is one extra email, bounded by cancellation ordering and `expiresAt` (doc 1's D16 — there is no send-time task-state check).

- **Cost:** infrastructure the same either way (Spanner bills the instance, not the database count); the saved outbox/consumer code is the option's only economy, paid with permanent shared-database coupling.

*No AWS variant is analyzed: wherever the backend runs, it is GCP compute next to the Spanner data — the placement question is service boundary, not which cloud.*

## Contracts and interfaces

**Where the endpoints live:** the **Attestation Module Backend** — the module's own service (D12/D2-32; decomposition and runtime are doc 5's). Portal traffic arrives server-to-server from provider-portal-api; non-PDM traffic arrives with tenant-scoped API credentials validated by the backend's own authentication layer — required here, designed in doc 5. Published as an OpenAPI spec (a machine-readable API description both teams code against).

### Configuration and flags

Every tenant-level knob this module reads or adds, in one place. All keys live in the shared `attestation-module-config` entry (doc 1's config, extended — doc 1's carry-forward rule; doc 5 owns the consolidated schema). **No feature flags** — no Flagsmith or other flag system; the kill switch is a config key.

| Key | Lives in | Type | Default | Controls |
| --- | --- | --- | --- | --- |
| `portalMode` | `attestation-module-config` | enum `CONFIRM` \| `FLAG` \| `EDIT` | none — set per tenant at onboarding | The form mode the prefill returns; one mode per tenant (D13); recorded as `appliedMode` at submission |
| per-field editability map (key name pinned in doc 5's consolidated schema) | `attestation-module-config` | per-field map | none — set per tenant at onboarding | Which mandated fields the attester may edit; the Portal maps it onto its renderer flags |
| `portalPaused` | `attestation-module-config` | boolean | `false` | Per-tenant kill switch: endpoints return an explicit "attestation paused" — never a silent empty list |

### Responsibility split (the team contract)

| Concern                                                        | Owner                                       | Notes                                                                                     |
| -------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Screens, form rendering, UX                                    | **Portal team** (frontend)                  | Their renderer already supports per-field editability and read-only — reused for NPI and modes |
| Auth, sessions, login (and any future magic-link tokens)       | **Portal stack** (provider-portal-api + FE) | Existing login machinery reused untouched; the emailed link is tokenless (doc 1's D17) |
| "Which practitioners can this user act for"                    | **provider-portal-api**                     | `portal_entity` resolution, tenant-scoped per D2-17; passes resolved ids to us            |
| Display enrichment (names and any OV data beyond the prefill)  | **provider-portal-api**                     | Our task responses carry `certifyPractitionerId` only (D10); portal-api resolves display data from its own OV access |
| Task list, prefill + snapshot, submission + guards, staging, audit | **Attestation Module Backend**          | The three endpoints below                                                                 |
| Non-PDM client access                                          | **Attestation Module Backend**              | Same three endpoints, consumed directly with tenant-scoped credentials                    |

### The lifecycle, end to end — every call with its request and response

> The full chain in execution order: session → task list → task detail (prefill) → submission → the closing transaction → read-back. Field names are the contract for docs 5–6. Rationale per step: *Approach* (session → step 2, list/detail → steps 1 and 3, submission → step 4, read-back → step 5).

**Step 0 — the user lands (portal stack, existing machinery).**

- The emailed deep link is **tokenless** (doc 1's D17): `<portalBaseUrl>/attest/<taskId>` — a stable entry route the portal stack owns and may redirect internally. It carries no credential and no data; an unauthenticated visitor goes through the portal's normal login (Auth0), then lands on the task.
- provider-portal-api resolves the user's practitioners from `portal_entity` (provider admins resolve to every practitioner in their tenant, D2-17).
- The portal stack may later layer its magic-link machinery (tokened links, attest-session cookie) onto the same entry route — a portal-side enhancement that changes nothing in this contract. Nothing in the link expires, because it carries nothing; the task itself stays submittable indefinitely (D2-23).
- **Audit written in this step:** none — session establishment is the portal stack's concern, recorded in its own logs; this module's trail starts where its own state is read or written (`FORM_PREFILLED` at task detail, step 2).

**Step 1 — task discovery.**

- `GET /attestation-tasks?tenantId=…&practitionerIds=…&status=…`
- Identity inputs are **server-resolved**: from portal-api for portal users; from the API credential's tenant scope for direct non-PDM callers — never from a browser.
- `status`: `OPEN | OVERDUE | SUBMITTED | ALL`; default **OPEN + OVERDUE** (the "what needs attesting" view). `SUBMITTED` lets a non-PDM client confirm receipts and see history.
- **OVERDUE is derived, never stored** (doc 1's D9): the server translates `status=OVERDUE` to `state = 'OPEN' AND nextAttestationDate < today` — one rule, one clock, computed here so no consumer reimplements it.
- `SCHEDULED` and `CLOSED` rows are **never returned** (D6; doc 1's D18): future obligations and terminated practitioners' closed obligations are internal state, not consumer-visible work; `ALL` means OPEN + OVERDUE + SUBMITTED.

Response `200`:

```json
{
  "tasks": [
    {
      "taskId": "task-7f3a…",
      "tenantId": "org-xyz",
      "certifyPractitionerId": "cert-000123",
      "duePeriod": "2026-10-15",
      "nextAttestationDate": "2026-10-15",
      "lastAttestationDate": null,
      "state": "OPEN",
      "overdue": false,
      "openedAt": "2026-09-15T06:00:03Z"
    }
  ],
  "nextPageToken": null
}
```

- Task fields only — never the practitioner's OV surface, and **no OV data copied into the response** (D10): no names, no NPI. `certifyPractitionerId` is the join key; provider-portal-api resolves display data (the practitioner's name for the list screen) from its own OV access — that enrichment is its job, keeping this backend lean.
- `overdue` is derived server-side at response time (D9) — `state` stays the stored state.
- `nextAttestationDate` is the date the whole ladder hangs on (doc 1); `lastAttestationDate` fills on successor rows.
- **Audit written in this step:** none — a list read changes no state; the serving is counted in metrics, not audit.

**Step 2 — task detail = the prefill.**

- `GET /attestation-tasks/{taskId}`
- By state: `OPEN` → the full response below; **`SCHEDULED` or `CLOSED` → 404** — an unopened future obligation, or a terminated practitioner's closed one (doc 1's D18), is not revealed to any caller (D6).
- `SUBMITTED` → task fields **plus the attestation summary, and no prefill**: `{ attestedBy, attesterType, attestedAt, outcome }`, read from the `attestation_submissions` row. A refresh after submitting therefore shows "already attested by X on Y" — never a fresh form, never a new `snapshot_version`. This is the structural reason a submitted task can never be re-attested from a refreshed page: the refresh has no snapshot to echo.
- The prefill's mandated fields are the one place this backend returns OV values — they must come from us, because the `snapshot_version` is a hash of exactly what we returned, issued here and checked at submit (D10).

Response `200`:

```json
{
  "task": {
    "taskId": "task-7f3a…",
    "tenantId": "org-xyz",
    "certifyPractitionerId": "cert-000123",
    "duePeriod": "2026-10-15",
    "nextAttestationDate": "2026-10-15",
    "state": "OPEN"
  },
  "prefill": {
    "snapshotVersion": "v1:9f3ac2…",
    "mode": "EDIT",
    "fields": [
      { "field": "providerName",         "value": "Dr. Asha Patel",  "editable": true  },
      { "field": "npi",                  "value": "1234567890",      "editable": false },
      { "field": "telephoneNumbers",     "value": [ { "entryKey": "office-1", "number": "555-0100" } ], "editable": true },
      { "field": "acceptingNewPatients", "value": true,              "editable": true  }
    ]
  }
}
```

- **The field set is fixed** (Product Spec S2 §1.5.9, restated in v2 §6.7; per-field source map in *Prefill field map* below): provider name · group affiliation · street address(es) · telephone number(s) · website URL · specialty · accepting-new-patients · cultural/linguistic capabilities · disability accommodations · telehealth availability · NPI (display-only). Fax and office hours excluded (D2-25).
- `mode` comes from the tenant's `attestation-module-config` entry (`portalMode`) — **one mode per tenant** (D13; *Form modes are tenant-level*, below). Per-field `editable` comes from the same config; the Portal team maps both onto their existing renderer flags.
- Nothing plan-related is read or stored anywhere; the mode that was *applied* becomes evidence at submission (`appliedMode`).
- **Audit written in this step:** `FORM_PREFILLED` — the snapshot version, the OV read time, and the **field values themselves**; the evidence of *what the attester was shown*. Written once per distinct `(taskId, snapshotVersion)` — repeat renders of an unchanged form are not re-logged (details in the `snapshot_version` subsection).

**Step 3 — submission.**

- `POST /attestation-tasks/{taskId}/submission` — no idempotency header, deliberately: retry safety is server-side (replay detection, step 4), so clients carry zero idempotency machinery.

Request:

```json
{
  "snapshotVersion": "v1:9f3ac2…",
  "outcome": "UPDATED",
  "deltas": [
    {
      "field": "telephoneNumbers",
      "operation": "UPDATE",
      "entryKey": "office-1",
      "oldValue": { "number": "555-0100" },
      "newValue": { "number": "555-0199" }
    }
  ],
  "attester": { "id": "pu-8842…", "type": "PROVIDER", "displayName": "Asha Patel" },
  "routingContext": { "channel": "PORTAL" }
}
```

- `outcome`: `NO_CHANGE | UPDATED | FLAGGED`. NO_CHANGE sends no `deltas`; the submission record itself is reviewer-visible (D2-09).
- `attester.type`: `PROVIDER | ADMIN` — an admin submission records on-behalf-of.
- Deltas carry old and new values — the reviewer sees what changed without re-deriving it. `entryKey` (which entry inside a list-valued field, e.g. which of three phone numbers) is null for scalar fields.
- **For list-valued fields, `oldValue`/`newValue` carry the full entry object** — the whole address or phone entry, not just the changed field. `entryKey` is a routing id, valid only within one prefill/submit conversation; the release path needs the merge engine's grouping key (`ARRAY_FIELD_GROUPING_KEYS`, e.g. `[address1, city, state, zipcode]` for addresses), which the sync worker derives **from the entry's values** at release — doc 5 owns that derivation (v2 §6.7.1 F-2; the attested level per attribute is O-13's decision, and this rule survives either answer).
- `routingContext.channel` (`PORTAL` | `DIRECT_API` — which door the submission came through, for audit and reporting; never the source tag, which is derived server-side from the caller's registration).
- **`attestedAt` is stamped by the server at accept time** — the request carries no timestamp; client clocks are never evidence. The stored value doubles as the task's `submittedAt`.
- **The request carries no plan information** — plan = tenant for this module (D13). The mode that governed the form (`appliedMode`) is derived server-side from the tenant config at accept time and stored as evidence; a caller can no more claim its mode than its tenant.

Response `200` (accepted):

```json
{
  "submissionId": "sub-5c2e…",
  "taskId": "task-7f3a…",
  "state": "SUBMITTED",
  "stagedItems": 1,
  "successorTask": { "taskId": "task-9c1d…", "nextAttestationDate": "2027-01-08" },
  "appliedMode": "EDIT",
  "message": "submission recorded"
}
```

- The message is always "submission recorded" — never "directory updated" (v2 §3.5 boundary: the workflow finishes in review).

Response `409` (stale snapshot):

```json
{
  "error": "STALE_SNAPSHOT",
  "presentedVersion": "v1:9f3ac2…",
  "currentVersion": "v1:b81f77…",
  "action": "refresh the task detail and re-attest the latest data"
}
```

Response `409` (already attested):

```json
{
  "error": "ALREADY_ATTESTED",
  "attestedBy": "Asha Patel",
  "attesterType": "PROVIDER",
  "attestedAt": "2026-10-10T17:21:04Z"
}
```

- Guard order: task exists (404) → **identity match (tenant/practitioner scope, 404 on mismatch — checked before the state branch, so no attester name, timestamp, or state ever crosses a tenant boundary)** → state (`SCHEDULED` or `CLOSED` 404 / **`SUBMITTED` → replay check** / `OPEN` continue) → snapshot match (409 stale) → NPI check (400 `NPI_EDIT_REJECTED`) → accept. The unique index is the last line, for the concurrent race only.
- **Audit written in this step:** `SUBMIT_REJECTED_STALE_SNAPSHOT` or `SUBMIT_BLOCKED_ALREADY_ATTESTED` on the 409 paths (standalone writes — nothing else changed); the accept path's events all commit inside step 4's transaction.

**Step 4 — the closing transaction (one read-write transaction in `attestation-db`, through the module's own data layer).**

All writes commit or fail **together** — the module's own database, same shape as doc 1's opening transaction (its step 4b):

```json
{
  "transaction": [
    { "table": "attestation_submissions", "operation": "INSERT",
      "row": { "taskId": "task-7f3a…", "tenantId": "org-xyz", "certifyPractitionerId": "cert-000123",
               "duePeriod": "2026-10-15", "outcome": "UPDATED", "snapshotVersion": "v1:9f3ac2…",
               "attesterId": "pu-8842…", "attesterType": "PROVIDER", "attestedAt": "2026-10-10T17:21:04Z",
               "source": "portal-attestation" } },
    { "table": "attestation_staged_items", "operation": "INSERT",
      "row": { "tenantId": "org-xyz", "taskId": "task-7f3a…", "attribute": "telephoneNumbers",
               "operation": "UPDATE", "entryKey": "office-1", "oldValue": { "number": "555-0100" },
               "newValue": { "number": "555-0199" }, "source": "portal-attestation" } },
    { "table": "attestation_tasks", "operation": "UPDATE",
      "row": { "id": "task-7f3a…", "state": "SUBMITTED", "submittedAt": "2026-10-10T17:21:04Z" } },
    { "table": "attestation_tasks", "operation": "INSERT",
      "row": { "tenantId": "org-xyz", "certifyPractitionerId": "cert-000123", "duePeriod": "2027-01-08",
               "nextAttestationDate": "2027-01-08", "lastAttestationDate": "2026-10-10",
               "state": "SCHEDULED", "source": "SUBMISSION_ADVANCE", "reminderOffsets": [-30, -7, 1] } },
    { "table": "attestation_outbox", "operation": "INSERT",
      "row": { "id": "obx-77bd…", "eventType": "OUTREACH_CANCEL", "aggregateId": "task-7f3a…",
               "orderingKey": "attestation-task:task-7f3a…", "tenantId": "org-xyz", "status": "PENDING",
               "payload": { "cancellationKey": "attestation-task:task-7f3a…", "sendKeys": null, "reason": "SUBMITTED" } } },
    { "table": "attestation_audit_events", "operation": "INSERT",
      "row": { "type": "ATTESTATION_SUBMITTED", "taskId": "task-7f3a…", "tenantId": "org-xyz" } }
  ]
}
```

- **The reminder cancellation leaves as a command, not a write:** the `OUTREACH_CANCEL` outbox row is published post-commit (doc 1's dispatch + relay sweep) as a `CANCEL_SENDS` command on `outreach.commands.v1`; the **Smart Outreach Service** flips its remaining `PENDING` sends under `cancellationKey = attestation-task:<taskId>` to `CANCELLED` in its own database and reports one `CANCELLED` outcome per row — a tier no longer `PENDING` (already claimed, sent, failed, or expired) comes back as `CANCEL_TOO_LATE` with its `currentStatus`. The Pub/Sub ordering key (the `cancellationKey`) keeps the cancel behind its schedule command; a tier already claimed by the sweep can send before the cancel lands — one extra email worst case, bounded by `expiresAt`; there is no send-time task-state check (doc 1's D16).
- **The replay-vs-conflict decision** — one rule, applied at two trigger points, always by reading the existing **`attestation_submissions`** row (never the audit table — audit is evidence, not lookup state). The rule: existing row's `attester_id` **and** `snapshot_version` both match the incoming request → the **same logical submission arriving again** → return the original `200`, rebuilt from that row, writing nothing; either differs → `409 ALREADY_ATTESTED` with who/when from the row.
  - **Trigger 1 — the sequential retry (the common case):** the original request committed (task now `SUBMITTED`), the response was lost, the client resends. The state guard finds `SUBMITTED` and runs the rule *before rejecting* — the retry matches and gets its `200`. Without this, every lost-response retry would see a confusing "already attested by you."
  - **Trigger 2 — the concurrent race (rare):** two requests both read `OPEN` and both pass the guards before either commits (a double-click racing itself, or two actors submitting in the same seconds). The first `CREATE` into `attestation_submissions` commits; the second hits `UNIQUE (task_id)`, the transaction aborts, and the rule runs. Note both racers passed the stale guard against the same OV, so a same-attester racer matches on snapshot too → replay; a different actor → 409.
  - A **stale tab can never reach either trigger:** its echoed `snapshot_version` no longer matches the current OV, so the snapshot guard rejects it with 409 STALE before any insert is attempted. The "same attester, different snapshot" 409 inside the rule is therefore a defense-in-depth backstop, not an expected user path.
  - First-writer-wins is the database's decision, never UI state — and retry safety costs the client nothing: no idempotency header, no client-generated key, no client state.
- The successor row is doc 1's contract (source `SUBMISSION_ADVANCE`, next attestation date = submission + 90 days, `lastAttestationDate` = the submission date); the next scan finds it when its window arrives. The cycle is closed — no OV write, no platform-database write, anywhere. *(A later rejection in review does not touch this successor — a rejection never resets the clock (Product, 2026-09-01); one future obligation per practitioner, never two.)*
- **Audit written in this step (same transaction):** `ATTESTATION_SUBMITTED`, `STAGED_ITEM_CREATED` per delta, `TASK_STATUS_CHANGED`, `OUTREACH_CANCELLED`, `OBLIGATION_SCHEDULED` for the successor.

**Step 5 — read-back confirmation (non-PDM clients).**

- `GET /attestation-tasks?status=SUBMITTED&practitionerIds=…` — the client confirms its submission landed by reading the task's state back. No webhook, no delivery state to reconcile.
- A naive retry of step 3 (same request, resent after a lost response) gets the original `200` back via the replay detection above — the client sends nothing special and keeps no state.

### `snapshot_version` — what it is, how it is created, and how it is compared

- **What:** a short fingerprint (a content hash — a fixed-length code computed from the field values; same values → same code, any changed value → a different code) of exactly the field values the task-detail call returned.

- **How it is created (server-side, at task-detail time):**
  1. Take the mandated field set's values as returned in the prefill — only those fields, nothing else.
  2. Canonicalize them (put them in one unambiguous byte form): fixed field order, fixed value formats (dates ISO, lists in stored order), UTF-8. JSON key order and formatting are not stable — hashing raw JSON would produce different codes for identical data.
  3. SHA-256 the canonical bytes; prefix with a rules version — `v1:9f3ac2…` — so a future change to the field set or the canonicalization is recognized by prefix instead of silently failing to match.
  4. Return it in the response and record it in the `FORM_PREFILLED` audit event.

- **How it is compared (server-side, at submit time):** the server re-reads the **current** Golden-record values for the same field set, canonicalizes and hashes with the same rules version, and compares to the echoed version. Equal → what the attester saw is still what is on record → accept. Different → 409 stale; a fresh task-detail call issues a new version and the user re-attests the latest data.

- The client never computes the hash — it only echoes it; both computations happen on our server with our rules.

- **Why this guard is kept, and why the hash is the lean version of it:**
  - Some staleness guard is mandatory (D2-19: never a silent overwrite) — the failure it prevents is quiet and compliance-shaped: a NO_CHANGE confirmation recorded against values the attester never saw.
  - A **timestamp check** was rejected: the merge pipeline touches the OV row's `updated_at` even when no displayed value changed — constant false rejections.
  - **Echoing all displayed values back at submit** is the same guard with a fatter payload — the server must canonicalize both sides to compare either way; the hash just compresses "compare 11 fields" into "compare two strings."
  - Total cost: one canonicalization function, one string in two payloads, one comparison. The two OV reads it requires are single-row lookups through the existing api-layer read endpoints — milliseconds, no state held between them.

- **The prefill values are recorded, not just their fingerprint:** the `FORM_PREFILLED` event's `detail` stores the mandated field **values** as returned. The OV keeps no history, so a hash alone cannot reconstruct what was shown years later — the stored values make "show me exactly what the attester confirmed" answerable from the audit trail directly (7-year retention applies).

- **Written once per distinct snapshot, not per render:** `FORM_PREFILLED` is deduplicated on `(taskId, snapshotVersion)` — the first render of a given version logs it; re-opening the same unchanged form adds nothing new and is not re-logged. When the OV changes, the next render produces a new version and therefore a new event. The trail is exactly "every distinct thing ever shown," with zero repetition noise.
- **How the dedup is enforced — a deterministic event id, no lookup and no extra table:** the `FORM_PREFILLED` event's `id` is derived, not random — a name-based UUID computed from `(taskId, snapshotVersion)`. The write is a blind insert: a repeat render computes the same id, hits the audit table's primary key, and is skipped. Nothing ever *reads* the audit table to decide behavior — the "evidence, not lookup state" rule holds, because a write-time uniqueness guard is not a lookup (it is the same discipline as the outreach service's `commandId` inbox and `UNIQUE (producer, send_key)`).

### Form modes are tenant-level

Form modes (`CONFIRM`, `FLAG`, `EDIT`) are configured **per tenant** — "plan" in the spec's mode wording refers to the tenant (Product, 2026-09-01; the v2 in-scope wording is amended accordingly).

- One `portalMode` + per-field editability per tenant, in `attestation-module-config` (doc 1's config, extended — Q4).
- Prefill returns that mode; the Portal team maps it onto their existing renderer flags (verified: the portal has per-field editability and a whole-form read-only toggle today, no mode concept — no new renderer capability needed).
- At submission, the server records `appliedMode` on the `attestation_submissions` row and in the `ATTESTATION_SUBMITTED` audit detail — evidence of which policy governed the form.
- One attestation covers everything about the practitioner: the mandated fields are practitioner facts in the Golden record — one value set per practitioner per tenant.
- Practitioner↔plan linking, and attestation fields that vary per payor, are NSCP-solutioning scope — nothing plan-related exists in this module.

### Prefill field map — source per field

Mirrors v2 §6.7.2 (Product answers of 2026-09-08, ratification pending). This is the field-by-field resolution of *The field set is fixed* above: what the prefill GET shows, where the backend reads it, and how the practitioner's rows are reached. The composed read (v2 F-3) is exactly these hops; each returned item carries the identity of the row it came from so a submitted edit routes back to it. Open rows block the OpenAPI freeze for the GET.

**Linkage — how a practitioner reaches its locations.** From `PractitionerLocationLookupRepository.CHAIN_QUERY` (core-data-access-layer), verified 2026-09-08. Every hop is tenant-scoped. OV tables have no foreign key: the link table's crosswalk id must be contained in the OV row's `contributing_crosswalks` array.

| Hop | Table | Join | Gives |
|---|---|---|---|
| 1 | `core_practitioners_ov` | `tenant_id` = tenant; `contributing_crosswalks` contains the tenant's `certify_practitioner_id` | practitioner Golden record |
| 2 | `tenant_practitioners` | `certify_practitioner_id` IN `core_practitioners_ov.contributing_crosswalks` | `id` |
| 3 | `tenant_group_practitioners` | `tenant_practitioner_id` = `tenant_practitioners.id`; `tenant_group_id` = `tenant_groups.id` | the group-affiliation row |
| 4 | `group_practitioner_locations` | `tenant_group_practitioner_id` = `tenant_group_practitioners.id` | one row per practitioner-at-location; `data.acceptingNewPatients` |
| 5 | `tenant_group_locations` | `id` = `group_practitioner_locations.tenant_group_location_id` | `group_location_id` |
| 6 | `group_locations` | `id` = `tenant_group_locations.group_location_id` | `location_id` |
| 7 | `core_locations_ov` | `group_locations.location_id` IN `core_locations_ov.contributing_crosswalks` | location Golden record |
| 8 | `location_entity_addresses` | `location_id` = `group_locations.location_id` | `entity_address_id`; `data.addressType` |
| 9 | `core_entity_addresses_ov` | `location_entity_addresses.entity_address_id` IN `core_entity_addresses_ov.contributing_crosswalks` | address Golden record |

**Practitioner-level fields — one value set per practitioner per tenant.**

| Portal field | Read from | Status (2026-09-08) |
|---|---|---|
| Name (prefix, first, middle, last, suffix) | `core_practitioners_ov`: `prefix`, `firstName`, `middleName`, `lastName`, `suffix` | Confirmed |
| NPI | `core_practitioners_ov`: `npi` | Confirmed; display-only, never editable (D2-19) |
| Practitioner phone number(s) | `core_practitioners_ov`: `phoneNumbers[]` | Confirmed. Half of Product's "telephone number"; the other half is the location phone below |
| Specialty(ies) | `tenant_practitioner_specialty` (`tenant_practitioner_id` = `tenant_practitioners.id`); name via `tenant_specialty_id` → `tenant_specialties` | Confirmed. Not `Practitioner.specialties[]`, not location specialties. Release path writes a tenant link row, not the Golden record |
| Languages spoken | `core_practitioners_ov`: `languages[]` | Confirmed |
| Cultural competency | `core_practitioners_ov`: `culturalCompetency` | Same Product answer; confirm whether shown as its own field |
| Telehealth offered | `core_practitioners_ov`: `telemedicineAvailable` | Confirmed (F-1) |
| Telehealth URL | `core_practitioners_ov`: `telemedicineURL` | Confirmed (F-1) |
| Group affiliation(s) | `tenant_group_practitioners` rows for the practitioner; display name via `tenant_group_id` → `tenant_groups` → group record | Confirmed. Tied to the open provider-admin scope question (D2-17 reopened by Product 2026-09-08) |

**Location-level fields — repeated once per `group_practitioner_locations` row; each block is one practice location.**

| Portal field | Read from | Linkage | Status (2026-09-08) |
|---|---|---|---|
| Location name (block header, not attested) | `core_locations_ov`: `locationName` | hop 7 | Label only |
| Practice address (line 1, line 2, city, state, zip) | `core_entity_addresses_ov`: `addressLine1`, `addressLine2`, `city`, `state`, `zip` | hops 8–9, filtered to the practice address type | Level confirmed: practice only; mailing/billing excluded. **Open:** `AddressType` enum has no `practice` value (`billing`, `mailing`, `MRStorageAddress`, `office`, `service`, `w9Address`); `office` assumed |
| Practice location phone | `core_locations_ov`: `phone` | hop 7 | Level confirmed: location phone, network-agnostic. **Open:** also `appointmentPhone`, `afterHoursPhone`, `callCoveragePhone`? Other half of Product's "telephone number" |
| Practice website | `core_locations_ov`: `website` | hop 7 | **Open:** Product may move website to practitioner level (no such field today); location level assumed for MVP |
| Accepting new patients at this location | `group_practitioner_locations`: `data.acceptingNewPatients` | hop 4 (the row itself) | Level confirmed per Product's #7 answer (doctor at this location). **Open:** confirm it is not `core_locations_ov.acceptsNewPatients` (office as a whole); F-2 wording could mean either. Network level excluded for MVP |
| Disability / ADA accommodations | `core_locations_ov`: `adaCompliance` (structured object) | hop 7 | Confirmed; sub-attribute rendering is an engineering decision |

**Excluded from the form.**

| Item | Why |
|---|---|
| Office hours, fax | D2-25; confirm Product's F-2 mention of office hours does not reopen it |
| Everything on `tenant_group_location_practitioner_networks` (`acceptingNewPatients`, `appointmentPhone`, `languageSpoken[]`, `officeHours`) | Product: network participation out of scope for MVP |
| `includePractitionerLocationInNetworkDirectory` | Product: not attested; it defines who gets an attestation at all (population rule → doc 1) |
| Mailing, billing and other non-practice address types | Product: practice address only |
| `core_practitioners_ov.addresses[]` | Superseded by practice-location addresses |
| `core_practitioners_ov.specialties[]`, `specialty`, `cmsSpecialties`; `core_locations_ov.locationPrimarySpecialty`, `locationHsdSpecialty` | Product: tenant practitioner specialty is the attested one |
| `core_locations_ov.languagesSpokenAtLocation` | Product: languages at practitioner level |

**Still pending Product:** location phone columns · website level · accepting-new-patients table · practice address type value · office-hours exclusion · cultural competency display.

## Data model and migration

- **Where everything lives:** the module's own Spanner database — **`attestation-db`** (doc 1's D8) — alongside `attestation_tasks`; migrations are module-owned (tooling pinned in doc 6); final DDL is doc 6's, these shapes are this doc's contract.

- **Tables this module creates** (in `attestation-db`; final DDL doc 6): `attestation_submissions`, `attestation_staged_items`.

- **Tables it interacts with, never owns:** `attestation_tasks` and `attestation_outbox` (doc 1's — the closing transaction flips the task, inserts the successor row, and writes the `OUTREACH_CANCEL` outbox row); `attestation_audit_events` (doc 1's — this lane writes its events into it); the OV read model — read-only, through the existing api-layer endpoints (prefill and the submit-time comparison); the Smart Outreach Service's tables (`outreach_sends`, `outreach_recipients`, …) — never touched: its sends are cancelled by that service consuming our `CANCEL_SENDS` command (doc 1's D12).

- **`attestation_submissions`** — one row per accepted submission:
  - `id`, `task_id`, `tenant_id`, `certify_practitioner_id`, `due_period`, `outcome`, `snapshot_version`, `attester_id`, `attester_type`, `attester_display_name`, `attested_at`, `source`, `applied_mode`, `created_at`.
  - `applied_mode` is **point-in-time evidence** (which form policy governed the submission), derived server-side from the tenant config at accept time — never client-supplied.
  - `UNIQUE (task_id)` — first-submission-wins, enforced by the database.
  - Retry replay needs no column of its own: the stored `attester_id` + `snapshot_version` are the replay-detection comparison keys (*Contracts → step 4*).
  - `attester_display_name` here is **point-in-time evidence**, not a cache: it records who the attester was at the moment of submission and is *supposed* to stay as written even if the OV changes later. (Convenience copies of live OV data are banned on our tables — doc 1's D10; historical facts recorded where they happen are the deliberate exception.)

- **`attestation_staged_items`** — one row per delta, the reviewer's input (review-side states are docs 5/7; this doc defines the birth shape):
  - `id`, `submission_id`, `task_id`, `tenant_id`, `certify_practitioner_id`, `attribute`, `operation` (`ADD | UPDATE | REMOVE`), `entry_key`, `old_value`, `new_value`, `source`, `created_at`.
  - `source` is the tag survivorship ranking reads later: `portal-attestation` for portal submissions, the caller's registered source for direct non-PDM submissions (D2-11, D2-18).
  - The stored value is the bare registry reference; at release the sync worker maps it to the tenant-scoped slice source id (`portal-attestation:{tenantId}` — one source per lane, v2 D2-37) — that mapping is doc 5's.

- **How the three tables connect — keys:**
  - `attestation_tasks` (doc 1): primary key `id` (UUID, minted at backfill); business identity `(tenant_id, certify_practitioner_id, due_period)` held unique by index. This `id` is the `task_id` every other row points at.
  - `attestation_submissions`: primary key `id`; `task_id` points at `attestation_tasks.id` — and `UNIQUE (task_id)` makes the link one-to-at-most-one: an obligation has zero or one act of attesting.
  - `attestation_staged_items`: primary key `id`; **two pointers** — `submission_id` at the parent submission (which act proposed this change) and `task_id` at the obligation (task → items reads in one hop, no join through the submission).
  - The two pointers on an item always agree: both are written in the same transaction, from the same submission.
  - Whether the links are declared Spanner `FOREIGN KEY` constraints or interleaved tables is doc 6's DDL call; this doc's contract is the pointer shape above.

- **Worked example — one attestation, end to end:**
  - The form opens against task `task-7f3a…` (`OPEN`, due 2026-10-15). Prefill shows the 11 mandated fields read live from the OV; their hash is `snapshot_version` `v1:9f3ac2…`.
  - The attester corrects the office phone (`555-0100` → `555-0199`), removes a stale address, and confirms the other nine fields unchanged.
  - Submit passes the guards, and **one transaction** writes:
    - 1 row in `attestation_submissions` — outcome `UPDATED`, the attester's identity, the `snapshot_version` attested against;
    - 2 rows in `attestation_staged_items` — the phone `UPDATE` and the address `REMOVE`, one row per change;
    - the task flips to `SUBMITTED`, the `OUTREACH_CANCEL` outbox row lands (the Smart Outreach Service flips the pending reminder sends to `CANCELLED` on our `CANCEL_SENDS` command), and the successor `SCHEDULED` row lands (next attestation date = submission + 90 days);
    - the audit rows for all of it.
  - The nine confirmed-but-untouched fields write **no rows anywhere**: confirming them is recorded by the submission row itself — outcome plus `snapshot_version` prove exactly what the attester saw and accepted. Staged items are deltas only.
  - Had nothing changed: outcome `NO_CHANGE`, one submission row, zero staged items — that is the whole record.
  - What the reviewer does with the two items — including their split fate — continues at *Why three tables*, below.

- **Two of those rows as stored — the submission and the phone item:**

```json
{
  "id": "sub-5c2e…",
  "taskId": "task-7f3a…",
  "tenantId": "org-xyz",
  "certifyPractitionerId": "cert-000123",
  "duePeriod": "2026-10-15",
  "outcome": "UPDATED",
  "snapshotVersion": "v1:9f3ac2…",
  "attesterId": "pu-8842…",
  "attesterType": "PROVIDER",
  "attestedAt": "2026-10-10T17:21:04Z",
  "source": "portal-attestation"
}
```

```json
{
  "id": "si-20fa…",
  "submissionId": "sub-5c2e…",
  "taskId": "task-7f3a…",
  "attribute": "telephoneNumbers",
  "operation": "UPDATE",
  "entryKey": "office-1",
  "oldValue": { "number": "555-0100" },
  "newValue": { "number": "555-0199" },
  "source": "portal-attestation"
}
```

### Why three tables — the grain, shown on one submission

The three tables count three different things; no fact appears twice:

| Table | One row per… | Count |
| --- | --- | --- |
| `attestation_tasks` | obligation (one 90-day cycle) | 1 per practitioner per cycle |
| `attestation_submissions` | act of attesting | 0 or 1 per task |
| `attestation_staged_items` | one field change | 0 to ~11 per submission |

Walk one submission through it — the attester corrects a phone number and removes a stale address:

- `attestation_tasks`: **1 row** flips to `SUBMITTED` — the obligation is answered.
- `attestation_submissions`: **1 row** — who attested, when, against which `snapshot_version`. Immutable evidence.
- `attestation_staged_items`: **2 rows** — the phone change and the address removal, each with its own life.

Now the reviewer (doc 7) **accepts the phone change and rejects the address removal** — maybe a vendor recommendation says the address is still valid:

- Row 1 moves to its approved state and is released to a slice via Sync Latest.
- Row 2 moves to rejected and goes nowhere.
- **Two fates for two changes from one submission** — impossible if the changes were columns or a JSON blob on the task row: one row cannot hold two review states, two release outcomes, two audit trails.

Why this creates no drift:

- Drift comes from the **same fact copied into two places** (the reason the OV cache was removed) — a copy must be kept in sync, and one day it isn't.
- Here each fact lives exactly once: the task owns the schedule and state; the submission owns the attestation facts; each item owns one proposed change. They connect by ids (`task_id`, `submission_id`) — pointers, not copies.
- When the reviewer accepts an item, exactly one row changes; nothing anywhere else must be kept in agreement. No duplication → nothing to sync → no drift, by construction.

- A NO_CHANGE submission writes the submission row only — no staged items; the row itself is the reviewer-visible record next to any pending vendor recommendation (D2-09).

- **Not written by this module:** `portal_application` (keeps serving the portal's existing flows; its link-expiry columns are read at their verify step, never written by us), the OV (prefill reads it; nothing here writes it), and the Smart Outreach Service's tables (that service cancels its own sends on our command — never us).

- **Migration:** additive only — two new tables + indexes in `attestation-db`, via the module's own migrations. No shared-table changes, no platform-database changes, no backfill (submissions accrue from go-live).

## Security, privacy, and access

- **Identity is resolved server-side, twice:** portal-api resolves the user's practitioners from `portal_entity` and passes them server-to-server; our backend re-checks tenant scope on every call. Browser-supplied tenant/practitioner/NPI values are never trusted.
- **Non-PDM callers** authenticate with tenant-scoped API credentials, validated by the backend's **own authentication layer** (D12/D2-32 — the module is its own service, so it authenticates its own callers); their identity resolution is the credential's tenant scope, since no portal front door sits in between. This doc states the requirement; the layer's design (credential type, issuance, validation) is doc 5's (Q6).
- **NPI is display-only** — a submission whose deltas name NPI is rejected whole with `400 NPI_EDIT_REJECTED`, regardless of what the UI sends; never silently stripped, so the attester always knows what was recorded (F8).
- **Tenant isolation server-side on every endpoint** (v2 §8) — including audit reads.
- **No provider data and no credentials in URLs** — task ids are opaque UUIDs; the emailed deep link is tokenless (doc 1's D17): authentication happens at the portal, never in the link.
- Session and login security stay in the portal stack's existing, verified mechanism.

## Performance and scale

| #   | Item               | Value / assumption                                                                              |
| --- | ------------------ | -------------------------------------------------------------------------------------------------- |
| N1  | Traffic            | ~780 open tasks/day → a few thousand API calls/day (list + detail + submit per attester, plus retries) ≈ 90k/month. Trivial load |
| N2  | Prefill freshness  | Current OV read at form-open; staleness handled by the guard, never by polling                  |
| N3  | Idempotency        | Server-side: `UNIQUE (task_id)` blocks duplicates; replay detection returns the original result to a retried request — no client-supplied key |
| N4  | Audit              | Every event in *Audit trail*, same transaction as its state change, 7-year retention (D2-20)    |
| N5  | Tenant isolation   | Server-side on every endpoint; nothing browser-supplied is trusted                              |
| N6  | Growth headroom    | 10× (~7,800 tasks/day ≈ 900k calls/month) — still trivial for stateless endpoints               |

**Cost (monthly, derived):** endpoints on the module's own service runtime (doc 5 owns the deployment and its baseline cost) + proxy code in the existing provider-portal-api service. This lane's marginal compute: ~90k requests × ~200 ms × 1 vCPU ≈ 5 vCPU-hours ≈ **< $1/month** of the service's request time; no infrastructure of its own beyond doc 1's (database, outbox topic). Storage is counted in doc 6.

## Observability

Telemetry outside the transaction; never blocks a submission.

**Metrics** (low-cardinality labels: tenant yes, practitioner never):

```
attestation.portal.tasks.served              counter  {tenant}
attestation.portal.prefill.served            counter  {tenant}
attestation.portal.submission                counter  {tenant, outcome}   # NO_CHANGE | UPDATED | FLAGGED
attestation.portal.stale_snapshot_rejected   counter  {tenant}
attestation.portal.already_attested_blocked  counter  {tenant}
attestation.portal.submit.duration           timer    {outcome}
```

**Alerts:**

- Submit error rate > 2% over 5 minutes → page — attesters are being turned away right now.
- `stale_snapshot_rejected` spiking → something is churning the OV underneath open forms, or the hash is misbuilt — investigate.
- Zero submissions in a business day for a tenant with open tasks → liveness check on the whole lane.
- Backend unreachable from portal-api (their health check on us) → page both teams.

**Correlation:** the identity tuple + `taskId` + `submissionId` flow from portal-api's request headers through our logs to the staged items — one grep tells the story.

## Audit trail

*Added section — "what did the attester see, and what did they change" is compliance evidence.*

**Audit table: `attestation_audit_events`** — the shared table doc 1 creates (final DDL doc 6); this lane writes its events into it and owns no audit table of its own.

Rules: append-only; same transaction as the change it records; 7-year retention (D2-20); every event carries the identity plus task/submission correlation ids.

- **`task_id` and `submission_id` are first-class, indexed columns on the audit table** (doc 6 DDL) — not fields buried in a JSON blob.
- The `submissionId` returned by the submit endpoint is a **lookup key**: `WHERE submission_id = @x` returns everything that submission did; `WHERE task_id = @y` returns one obligation's whole life, this lane's events included.

### The common envelope — every event carries these fields

```json
{
  "id": "ae-88c1…",
  "type": "ATTESTATION_SUBMITTED",
  "tenantId": "org-xyz",
  "certifyPractitionerId": "cert-000123",
  "duePeriod": "2026-10-15",
  "taskId": "task-7f3a…",
  "submissionId": "sub-5c2e…",
  "actor": "user:pu-8842…",
  "occurredAt": "2026-10-10T17:21:04Z",
  "detail": { }
}
```

- **Envelope carried forward from doc 1, plus one addition:** this lane adds `submissionId` (nullable, additive — no inherited field changes meaning) and fills it where doc 1's scan events fill `runId`. Later module docs extend the same way; doc 6 owns the consolidated shape.
- `submissionId` — null on read-path and rejection events (no submission row exists yet).
- `actor` — the attesting user (`user:{id}`), or the API credential's principal for non-PDM calls.

### Per-event contracts — the `detail` fields and where each commits

| Event                             | Committed in                                        | `detail` fields                                                                        |
| --------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `FORM_PREFILLED`                  | its own write — once per distinct `(taskId, snapshotVersion)`: the event `id` is deterministic (derived from taskId + snapshotVersion), so a repeat render's insert hits the primary key and is skipped | `snapshotVersion`, `ovReadAt`, `mode`, `fields` (the mandated field values as shown — the reconstructable evidence) |
| `SUBMIT_REJECTED_STALE_SNAPSHOT`  | its own write (nothing else changed)                | `presentedVersion`, `currentVersion`                                                   |
| `SUBMIT_BLOCKED_ALREADY_ATTESTED` | its own write (nothing else changed)                | `existingSubmissionId`, `attesterDisplayName`, `attestedAt`                            |
| `ATTESTATION_SUBMITTED`           | the submission transaction                          | `outcome`, `attester` (id, type, displayName), `snapshotVersion`, `deltaCount`, `channel`, `appliedMode` |
| `STAGED_ITEM_CREATED`             | the submission transaction (one per delta)          | `stagedItemId`, `attribute`, `operation`, `entryKey`, `oldValue`, `newValue`, `source`  |
| `TASK_STATUS_CHANGED`             | the submission transaction                          | `from` (`OPEN`), `to` (`SUBMITTED`), `wasOverdue` (derived at submit time)             |
| `OUTREACH_CANCELLED`              | the submission transaction                          | `cancellationKey`, `outboxEventId`, `cause` (`SUBMITTED`) — records the cancel *intent*; the send flips happen in the Smart Outreach Service (doc 1) |
| `OBLIGATION_SCHEDULED`            | the submission transaction (the successor row)      | `nextAttestationDate`, `rule` (`SUBMISSION_PLUS_90`), `predecessorTaskId`, `reminderOffsets` |

- "Committed in" is a contract, not a note: events listed with the transaction are written **inside** it — if the submission rolls back, so do its events; standalone events record facts with no accompanying state change.
- An engineer implementing from this table should never have to invent an audit field. A field found missing during implementation is a gap in this doc — fix it here first.

Questions this answers directly: "what exactly did the attester see when they attested?" (`FORM_PREFILLED` + snapshot) · "why was the second submission rejected?" (`SUBMIT_BLOCKED_ALREADY_ATTESTED`) · "who attested on whose behalf?" (`ATTESTATION_SUBMITTED.attester`).

## Failure modes and rollback

**Edge cases:**

| Case                                                  | Handling                                                                                              |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| OV changes while the form is open                      | 409 stale + refresh; the guard fires only on displayed-value changes (*snapshot_version* subsection)  |
| Practitioner and admin submit simultaneously           | `UNIQUE (task_id)` — exactly one wins; the loser sees who and when                                    |
| Client retries a submit after a lost response          | Replay detection: the existing submission matches on attester + snapshot version → original result replayed — no double effect |
| Emailed link visited unauthenticated                   | The link is tokenless (doc 1's D17) — the portal's normal login gates it; nothing expires; the task stays submittable indefinitely (D2-23) |
| UI sends an NPI edit                                   | Whole submission rejected with `400 NPI_EDIT_REJECTED` regardless of UI state — never a silent strip  |
| Browser supplies a foreign practitioner/tenant id      | Ids come from server-side resolution; the backend re-checks tenant scope anyway — two layers          |
| NO_CHANGE while a vendor recommendation is pending     | Submission record is reviewer-visible next to the recommendation (D2-09); nothing automatic           |
| Backend down at submit time                            | Explicit "temporarily unavailable" from portal-api; no silent success, nothing lost; user retries     |
| Submission transaction fails mid-commit                | Impossible partially — one transaction rolls back whole; task stays OPEN; the attester retries        |
| Cancel command published but the outreach service is down/lagging | Pub/Sub holds and redelivers the command until acked; pending tiers are cancelled when it is consumed; a tier already claimed sends — one extra email worst case (`CANCEL_TOO_LATE` outcome); `expiresAt` bounds anything staler (doc 1's D16 — no send-time task-state check); the relay sweep re-publishes a crash-stranded outbox row (doc 1) |
| Successor row insert conflicts (duplicate `due_period`) | The unique index refuses it; the transaction aborts — surfaced, never silently swallowed (doc 5 handles the resolution) |
| Non-PDM client cannot confirm delivery                 | It reads back `status=SUBMITTED` from the same API — there is no delivery state to lose               |

**Rollback:**

- Per-tenant kill switch (`portalPaused` in the same `attestation-module-config` entry): the endpoints return an explicit "attestation paused for this tenant" — never a silent empty list. No deployment.
- Accepted submissions, staged items, and successor rows explicitly **stay** — they are recorded business facts; review (docs 5/7) decides what proceeds.
- Re-enabling resumes cleanly: open tasks were untouched, links keep working.
- Schema is additive-only (new tables) — schema rollback never required.

## Rollout

1. Contract first: publish the OpenAPI spec for the three endpoints; agree the proxy + identity pass-through work with the Portal team (their ticket); agree the field-to-Golden-record mapping table with Product before the spec freezes (Q3).
2. Backend endpoints ship behind the per-tenant config on the module's own service (doc 5 runtime); no tenant serves traffic until its config entry exists.
3. Depends on doc 1's rollout: `attestation-db`, the outbox dispatch + relay sweep, the outreach consumer, and live tasks (backfill + scan) must exist before the list endpoint has anything to serve or the closing transaction anywhere to land.
4. Pilot tenant: portal flow end to end (email link → login → prefill → submit → SUBMITTED read-back), watching the submit error rate and stale-rejection alerts.
5. Non-PDM credentials issued after doc 5's authentication-layer design (Q6); first direct consumer onboards against the same spec.
6. Tenant waves; the kill switch is the brake at every step.
