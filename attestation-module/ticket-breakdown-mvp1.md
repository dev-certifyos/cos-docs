# Directory Accuracy & Attestations — MVP 1 Ticket Breakdown (CP-37409 Spike Output)

**Prepared:** 2026-08-24 · **Author:** Dev Pandey · **Jira:** [CP-37409](https://certifyos.atlassian.net/browse/CP-37409)
**Companion document:** `source-of-truth-v2.md` (the "SoT doc"). *(This breakdown predates v2 and the per-module design docs — tickets are updated in Jira as each module doc is finalized (O-10); WD-xx references are to the retired v1's working decisions, superseded by v2's D2-xx register.)*
**Status:** Proposed ticket list for five-team lead review. **No ticket below may be formally created or groomed until each has lead sign-off** (S3 R5). Per the spike's own rules: every ticket has exactly one owning team, flagged dependencies are listed on the ticket rather than assumed away, and out-of-scope items have no tickets.

> **At a glance:** 32 proposed tickets across five teams — PDM (17), Shared Services (4), Portal (4), Roster/Integration (4), MDM (3) — organized into seven build phases. Every MVP1-in-scope requirement of the Product Spec (§1, §2 minus §2.2, §3 plus §3.6.1) traces to at least one ticket (traceability matrix in §6). Eight sign-off/contract prerequisites are listed first because specific tickets are blocked on them.

---

## 1. Prerequisites — decisions and contracts that block tickets (not engineering tickets)

Per acceptance criterion 3 of CP-37409, these are flagged separately, not resolved by assumption. Each blocked ticket lists its prerequisite by ID.

| ID | Prerequisite | Owner | Blocks |
|---|---|---|---|
| P1 | **Signed external-source schema** (one versioned Candor schema + canonical ordered confidence-tier mapping; resolves CT-011) | Madhunika + Candor technical team (+ Anmol/Ansar) | DA-15, DA-16 |
| P2 | **Candor transport confirmation** (SFTP per WD-03) | Madhunika + Anmol + Ansar | DA-14 |
| P3 | **WD register sign-off** — Product/eng leads approve WD-01…WD-20 (SoT §13.1), notably WD-01 release semantics, WD-09 prefill source, WD-15 expiry semantics | Five team leads + Product | DA-05, DA-09, DA-13, DA-20, DA-23 |
| P4 | **Compliance: partial approval** (whole-record restart is the working rule — v2 O-5). ~~Clock reset on rejection~~ **RESOLVED 2026-09-01 (Product):** a rejection does not reset the clock; the rejection + 30 rule (WD-02) is removed — see v2 D2-28 | Madhunika + Compliance | DA-19 |
| P5 | **Compliance: two-business-day boundary** — confirm the clock attaches to attestations, not third-party recommendations (S1 §5.2), and where CertifyOS responsibility ends | Madhunika + Compliance | DA-30 (alert thresholds) |
| P6 | **Audit retention** — confirm 7-year working policy (WD-11) | Madhunika + Compliance | DA-29 (config value only) |
| P7 | **Provider-admin association depth** — is tenant-level scoping sufficient, or is group-level association required (WD-07 nuance)? | Product + Portal lead | DA-08 (visibility query) |
| P8 | **Reviewer action-level permission split** — final list of permissions (view / decide / bulk / rollback / sync / report / replay) | Madhunika + Platform team | DA-22 |

---

## 2. Proposed tickets

Legend per ticket: **Team** (exactly one) · **Traces** (spec/requirement IDs) · **Depends on** (build-order dependencies, noted on both sides) · **Flags** (prerequisites/sign-offs). Architecture references are to SoT §6A modules M1–M8.

### Phase A — Foundation (workflow state and identifiers)

**DA-01 — Directory-accuracy database and core schema** · **Team: PDM**
Create the new Spanner database (own DB in `app-data`, roster-staging precedent) with the M4 tables: `attestation_cycles`, `attestation_tasks`, `attestation_submissions`, `directory_change_items`, `source_batches`, `source_recommendations`, `review_actions`, `sync_releases`, `sync_release_items`, `outbox`, `processed_messages` — including the uniqueness constraints that carry the idempotency design (cycle identity, accepted-submission-per-task, batch checksum, release-item triple) and integer `version` columns. Tenant-first composite indexes.
*Traces:* S2 §1.5.4, §2.3.1; DR-001–DR-010; SoT §6A.7. *Depends on:* —. *Depended on by:* nearly everything. *Flags:* none.

**DA-02 — Outbox relay and consumer dedup infrastructure** · **Team: PDM**
Transactional outbox writer + relay publishing to Pub/Sub; `processed_messages` dedup guard for every DA subscription (none exists platform-wide — SoT §19.3). DLQ + depth alert on every new subscription.
*Traces:* SoT §14.1, §6B.6. *Depends on:* DA-01. *Flags:* none.

**DA-03 — Identifier and fingerprint library** · **Team: PDM**
Deterministic key builders for every stage (SoT §19.3 table) and the recommendation fingerprint: SHA-256 over canonical tuple with versioned normalization rules (phone → E.164, casing, address canonicalization reusing the cleansing layer's rules). Includes the demotion comparison rules (identical + same/lower tier → demote; higher tier → re-queue; different value → new item; **external-source lane only**).
*Traces:* S2 §3.3.2; SoT §6.3 item 8, §19.3. *Depends on:* —. *Depended on by:* DA-10, DA-11, DA-16, DA-23. *Flags:* normalization-rule alignment consult with MDM (cleansing layer).

### Phase B — Cycle and outreach (M1, M2)

**DA-04 — Module database and tables** · **Team: Shared Services**
**Rescoped (2026-09-01, cycle doc D8/D2-32; supersedes the OV denorm columns and the app-data tables):** create the `attestation-db` Spanner database on the app-data instance + `attestation_tasks`, `attestation_audit_events`, `attestation_outbox` tables with module-owned migrations; unique index `(tenant_id, certify_practitioner_id, due_period)`; scan index `(tenant_id, state, next_attestation_date)`; states `SCHEDULED / OPEN / SUBMITTED / CLOSED` (CLOSED = terminations, cycle doc D18).
*Traces:* cycle doc *Data model*. *Depends on:* —. *Flags:* none.

**DA-05 — 90-day cycle scheduler** · **Team: Shared Services**
Cloud Scheduler → `POST /internal/attestation-cycles/trigger` (202) → Cloud Tasks **chunks** (~500/task; AutoRecred pattern). Opening = atomic transaction (state flip + audit + `OUTREACH_SCHEDULE` outbox row) with the termination re-check (`TASK_OPEN_SKIPPED_TERMINATED`, cycle doc D18); post-commit dispatch + relay sweep (D11); clock rules (submission + 90, submission-anchored; a rejection never resets the clock — Product 2026-09-01, v2 D2-28; never a second task while one is open). Tenant `leadDays` config (no `lookbackDays` — structural). Includes the weekly reconcile run (same endpoint as DA-06, both checks — cycle doc D19).
*Traces:* cycle doc *Approach* 3–5, *Contracts* 1–4b. *Depends on:* DA-04. *Flags:* none.

**DA-06 — Reconcile endpoint: backfill + weekly check** · **Team: Shared Services**
`POST /internal/attestation-cycles/reconcile` (cycle doc D19 — one endpoint, two callers): detector + Cloud Tasks chunk handlers for two checks — `population` (insert missing `SCHEDULED` rows; date = `run_date + hash(certify_id) mod staggerDays`; terminated excluded; `source BACKFILL/SEEDED`) and `terminated` (close live rows of terminated practitioners, D18). Backfill = the supervised first run (per tenant, dry-run → report → execute); weekly Cloud Scheduler run covers seeding + termination net. Audited `RECONCILE_RUN_STARTED/COMPLETED`.
*Traces:* cycle doc *Contracts* step 0, *Observability → weekly reconcile*. *Depends on:* DA-04, DA-05. *Flags:* registry load (PDM-1) must precede the first backfill.

**DA-07 — Outreach integration via the Smart Outreach Service** · **Team: Shared Services**
**Rescoped (2026-09-07, cycle doc D12; supersedes the `smart_outreach` engine extension):** outbox relay publishing `SCHEDULE_SENDS`/`CANCEL_SENDS` on `outreach.commands.v1` (ordering key `attestation-task:<taskId>`; tiers as `sendKey` suffixes T-30/T-7/OVERDUE/KICKOFF; `expiresAt` per tier); outcome consumer on `outreach.outcomes.v1` (filter `producer = attestation-module`, `outcomeId` dedup, outcome → audit mapping incl. `OUTREACH_SCHEDULE_CONFIRMED`/`OUTREACH_CANCEL_CONFIRMED`); register the three `attestation.reminder.*` templates (variables `{ nextAttestationDate, portalLink }`, category `compliance-reminder`). No send-time task check (D16). Deep link is tokenless — `<portalBaseUrl>/attest/<taskId>` (D17); OVERDUE is the final automated email.
*Traces:* cycle doc *Approach* 5–6, *Contracts* 4b–6; Smart Outreach Service doc. *Depends on:* DA-05; Smart Outreach Service live; PDM-1 (registry). *Cross-team:* entry-route contract with Portal (DA-13). *Flags:* none.

**NEW-1 — DevOps: cycle infrastructure** · **Team: DevOps**
Three Cloud Scheduler jobs (daily scan, relay sweep, weekly reconcile); `attestation-cycle-queue` Cloud Tasks queue; publish rights on `outreach.commands.v1` for the attestation backend's service account; filtered subscription + DLQ on `outreach.outcomes.v1` (retention ≥ 7 days).
*Traces:* cycle doc *Rollout* 3. *Depends on:* Smart Outreach Service topics existing.

**PDM-1 — Recipient registry sync (NEW, cross-team)** · **Team: PDM**
`api-layer` registers with the Smart Outreach Service as producer `pdm-platform`, owner of the `practitioner:` recipient namespace (cycle doc D14 / v2 D2-34): initial `UPSERT_RECIPIENT` load per tenant **before** the attestation backfill, republish on every contact change, `status: INACTIVE` on termination. Receives `RECIPIENT_ADDRESS_FAILED` / `RECIPIENT_UNSUBSCRIBED` outcomes (contact-data fixes are PDM's).
*Traces:* cycle doc step 0b; Smart Outreach Service doc. *Depends on:* Smart Outreach Service live. *Flags:* needs PDM team sign-off.

**PDM-2 — Practitioner-terminated event (NEW, cross-team)** · **Team: PDM**
Publish a practitioner-terminated event (topic + payload TBD — cycle doc D18 / v2 D2-36); the attestation backend's consumer closes obligations (`CLOSED`) and cancels outreach. Until it exists, the weekly reconcile `terminated` check is the closer.
*Traces:* cycle doc *Practitioner termination (D18)*. *Depends on:* —. *Flags:* Product timing questions open (v2 O-16).

### Phase C — Portal attestation (M3)

**DA-08 — Pending tasks API and dashboard entry** · **Team: Portal**
`GET pending-attestations` resolving eligible practitioner/admin identities server-side (`portal_user`/`portal_user_tenant`/`portal_entity`); "Pending Tasks" dashboard section + attestation timeline (last attested, Complete/Expiring/Overdue).
*Traces:* S2 §1.3.1–1.3.2, §1.4.2–1.4.3, §1.5.1; SoT §6A.6. *Depends on:* DA-01, DA-11 (task read API — noted both sides). *Flags:* P7 (association depth changes the visibility query).

**DA-09 — Prefilled attestation form** · **Team: Portal**
Render current OV (WD-09) with `snapshot_version` = content hash of displayed values (not OV `updated_at` — SoT §6A.6); mandated field set per S2 §1.5.9 (no fax/office hours — WD-17); NPI display-only; tenant-config form modes (edit / flag / both / view-and-attest — v2 D2-31: plan = tenant); accessible validation.
*Traces:* S2 §1.3.3–1.3.7, §1.5.9; SoT §6A.6. *Depends on:* DA-11 (prefill endpoint). *Flags:* P3 (WD-09).

**DA-10 — Submission flow and envelope** · **Team: Portal**
"You are attesting as [Name] (Practitioner/Admin)" confirmation + required checkbox; single-pass submit (no drafts) with `Idempotency-Key`; structured deltas (add/edit/delete) carrying `listKey` aligned to survivorship grouping keys; envelope v1 (SoT §19.4); stale-snapshot 409 → refresh UX; first-writer-lost UX ("Already attested by…"); success copy = "submission recorded," never "directory updated."
*Traces:* S2 §1.3.8, §1.4.3–1.4.4, §1.5.5–1.5.6, §1.5.8; SoT §6A.6, §6B.2. *Depends on:* DA-03, DA-11 (envelope contract — noted both sides). *Flags:* P3.

**DA-11 — Attestation intake API (PDM side)** · **Team: PDM**
Accept the envelope: one-transaction validation (task open, snapshot fresh, first-writer unique index), store submission + snapshots + change items (PENDING), NO_CHANGE completes with zero items and **annotates pending recommendations** ("provider confirmed as-is on [date]"); future effective dates stored for the release gate; task read/prefill endpoints for DA-08/DA-09; RFC 9457 errors, idempotent replay.
*Traces:* S2 §1.5.2–1.5.6; SoT §6A.6 step 5, §6A.7, §6B.2. *Depends on:* DA-01, DA-02, DA-03. *Cross-team:* envelope contract with Portal (DA-10). *Flags:* none.

**DA-12 — Non-PDM routing via webhook dispatcher** · **Team: Roster/Integration**
New `attestation.submitted` event type on the existing webhook dispatcher; per-tenant endpoint config, OAuth, retries, DLQ, partner-2xx acknowledgment; delivers the identical envelope (`data_flow_context` NON_PDM/CLIENT).
*Traces:* S2 §1.5.7; S5 §8; SoT §19.4, WD-12. *Depends on:* DA-11 (envelope). *Flags:* none.

**DA-13 — Attestation entry route** · **Team: Portal**
**Rescoped (2026-09-07, cycle doc D17):** serve the stable, tokenless entry route `/attest/<taskId>` — unauthenticated visitors go through the portal's normal login, then land on the task; the portal stack owns all routing behind the entry point and may layer magic-link tokens later without changing the contract. Nothing expires; **overdue tasks remain submittable** (D2-23).
*Traces:* portal doc step 0; cycle doc D17. *Depends on:* DA-08. *Cross-team:* entry-route contract with Shared Services (DA-07). *Flags:* none.

### Phase D — External-source ingestion (M5, source-agnostic)

**DA-14 — Source SFTP onboarding and pickup** · **Team: Roster/Integration**
Per-source dedicated SFTP identity (key auth, rotation — origin proven by depositing account), pickup job to immutable GCS archive, `accuracy-source.file.received` Pub/Sub notification.
*Traces:* WD-03; SoT §6A.8 steps 1–2, §6B.3. *Depends on:* —. *Flags:* P2.

**DA-15 — Source-agnostic adapter framework** · **Team: Roster/Integration**
Batch registration (`tenant + source + checksum` dedup), streaming parser, per-source schema-version mapping config in `tenant_configurations` (columns, enums, **canonical ordered confidence tiers**), whole-batch rejection on unknown schema. No source names in code paths — adapters are configuration.
*Traces:* S2 §2.1.1–2.1.4, §2.4.1–2.4.2; SoT §6A.8, principle 10. *Depends on:* DA-01. *Flags:* **P1 (hard blocker — signed schema + tier mapping).**

**DA-16 — Row disposition engine** · **Team: Roster/Integration**
Per-row dispositions with reconciling counts: no-op vs current PDM value, duplicate, **rejection-memory demotion** (fingerprint vs rejection history), invalid/NPPES-fail, already-terminated, stale-superseded (including re-issued batches), quarantine on dependency outage, `FACILITY_DROPPED` (WD-05); same-attribute conflict rule (highest tier wins, tie → quarantine both); effective date = evidence verification date else end-of-month (WD-13, config); actionable rows → change items.
*Traces:* S2 §2.1.5–2.1.6, §2.3.2–2.3.3, §2.5.1–2.5.5; SoT §6A.8 step 5–6, §6B.3. *Depends on:* DA-01, DA-03, DA-15. *Flags:* P1.

**DA-17 — Batch operations tooling (quarantine, replay, reconciliation)** · **Team: PDM**
Operator surface for quarantined rows/batches: audited replay with original identity, batch count reconciliation view, reprocess-from-GCS-archive path. No direct DB edits.
*Traces:* OPS-004; SoT §14.3, §6B.3/§6B.6. *Depends on:* DA-16. *Flags:* none.

### Phase E — Review (M4 review surface)

**DA-18 — Review queue APIs** · **Team: PDM**
Tenant-scoped list mixing all sources; filters (NPI, group, TIN, specialty, source, status, changed field, aging); item detail (current vs proposed, operation, effective date, confidence **tier badge**, evidence as copyable string — WD-19); **cross-lane grouping** of same provider + field/list-key; cursor pagination.
*Traces:* S2 §3.1.1–3.1.4; SoT §6A.7, §6B.4. *Depends on:* DA-01, DA-11, DA-16. *Flags:* none.

**DA-19 — Decision APIs (accept / reject / save / bulk)** · **Team: PDM**
Field/attribute-level decisions as conditional version transitions; reject requires reason (picklist + free text — WD-16) and feeds rejection memory; save/skip with visible aging; bulk with per-item results (never hidden partial failure). No clock-reset trigger — a rejection never resets the attestation clock (Product 2026-09-01, v2 D2-28).
*Traces:* S2 §3.2.1–3.2.2, §3.3.1–3.3.4; SoT §6A.7, §6B.4. *Depends on:* DA-18. *Flags:* **P4 (Compliance — partial approval/clock).**

**DA-20 — Rollback, Sync Latest, and release creation** · **Team: PDM**
Truthful pre-release rollback (conditional, loses cleanly to a racing claim); Sync Latest with no-rollback confirmation creating an immediate release; post-approval "auto-released by next batch" notice data; release snapshot of exact item versions.
*Traces:* S2 §3.3.5, §3.4.1; SoT WD-01, §6A.7, §6B.4. *Depends on:* DA-19. *Flags:* P3 (WD-01).

**DA-21 — Review queue UI** · **Team: PDM**
New `apps/web` feature copying the monitoring-flags pattern: grid + filters, generic bulk-actions-bar, bulk confirmation modal **with operation-type breakdown (updates vs removals)**, tier badges, evidence strings, aging, pre-sync vs claimed status per row, Sync Latest warning dialog, post-approval notice.
*Traces:* S2 §3.1, §3.3; SoT §6A.7, §19.6. *Depends on:* DA-18, DA-19, DA-20, DA-22. *Flags:* none.

**DA-22 — Directory Accuracy Reviewer RBAC** · **Team: PDM**
New role + permissions via existing role/permission APIs; new `PermissionTypes` constants + frontend permission mirrors; endpoint annotations across DA APIs; portal actors never hold platform permissions.
*Traces:* S2 §3.5.3; SoT WD-06, §6A.11, §19.6. *Depends on:* DA-01 (endpoints exist to annotate). *Flags:* P8.

### Phase F — Release and merge (M6, MDM boundary)

**DA-23 — Release worker** · **Team: PDM**
Scheduled batch sweep (2–3×-daily) + Sync Latest fan-out through Cloud Tasks; per-item conditional claim; recheck (identity, termination, supersession → SUPERSEDED, **future-effective-date hold**, ranking-config guard — refuse tenants missing new-source ranks); slice upsert under the item's source id; **verification by OV `contribution_map` read-back** with 15-minute timeout → RETRYABLE → QUARANTINED (never trusts survivorship's HTTP response); poison isolation; audit records the actual survivorship outcome.
*Traces:* S2 §3.4.2–3.4.3; SoT §6A.9, §6B.5, §19.2–19.3. *Depends on:* DA-20, DA-24, DA-25. *Cross-team:* MDM (DA-24/25 — noted both sides). *Flags:* P3 (WD-01).

**DA-24 — Source registration and cleansing coverage** · **Team: MDM**
Per-tenant `core_sources` rows (`portal-attestation:{tenantId}`, `candor:{tenantId}`) as part of tenant onboarding; confirm cleansing layer handles the new source types (emitting `certify-cleanser:candor:*` etc.).
*Traces:* SoT §19.1–19.2. *Depends on:* —. *Cross-team:* PDM (DA-23 blocked without it). *Flags:* none.

**DA-25 — Survivorship ranking config for new sources** · **Team: MDM**
Seed per-tenant `source-ranking-rule` rows: default `portal-attestation` > `candor` > roster/UI, wildcards covering cleansed variants; onboarding checklist entry; document the rank-997 unknown-source trap and the guard contract with DA-23.
*Traces:* S2 §2.4.3, §3.4.3; SoT WD-08, §19.2, §6B.5. *Depends on:* DA-24. *Cross-team:* PDM (DA-23). *Flags:* P3 (WD-08 default order).

**DA-26 — Survivorship lineage extension** · **Team: MDM**
Stamp `configVersion`/`configSource` + applied rule id into `contribution_map` entries; append-only versioning of survivorship config rows; carry `configVersion` in `mdm.slice.ov_generated`. Resolves CT-010; the audit tickets (DA-29) depend on it for "why did this value win."
*Traces:* S2 §3.5.2; SoT §19.7, CT-010. *Depends on:* —. *Cross-team:* PDM (DA-29 — noted both sides). *Flags:* none.

**DA-27 — Termination translation** · **Team: PDM**
Map removal/disassociation items to the existing termination API: resolve `list_key` → relationship IDs (zero/multiple matches → QUARANTINED, no change); single-scope `terminationType` + narrowers; future effective dates passed through natively; replay 400 "already terminated" recorded as terminal success.
*Traces:* S2 §3.4.4; SoT §6A.9 step 3, §6B.5, §19.3/§19.6. *Depends on:* DA-23. *Cross-team:* Terminations owners (api-layer) — contract review. *Flags:* none.

### Phase G — Reporting, audit, operations, rollout

**DA-28 — Non-attested report (API + grid)** · **Team: PDM**
`GET /directory-accuracy/non-attested` (cursor-paged, `as_of`-stamped, tenant from auth) + `apps/web` grid on the same query path; columns per WD-18 (incl. last reminder tier + delivery status from outreach tables); terminated practitioners excluded from the denominator; `(tenant_id, status, due_date)` index; permission `directory-accuracy-report.read`.
*Traces:* S2 §3.6.1; SoT §6A.10, §19.8. *Depends on:* DA-01, DA-05, DA-07 (reminder status), DA-22. *Flags:* none.

**DA-29 — Audit event stream** · **Team: PDM**
Append-only audit events for every state change (actor/service, tenant, object, before/after, source, correlation, config versions when available) streamed to BigQuery; 7-year working retention as a policy knob.
*Traces:* S2 §3.5.1–3.5.2; SoT §6A.11, WD-11. *Depends on:* DA-01, DA-02. *Cross-team:* MDM (DA-26 supplies OV-side lineage — noted both sides). *Flags:* P6.

**DA-30 — Observability, alerts, and runbooks** · **Team: PDM**
End-to-end correlation ids; metrics per SoT §14.3; aging alerts **split by source** (attestation items: 12 h warn / 24 h page; recommendation items: operational SLA); DLQ depth alerts; kill-switch-engaged-24h alert; runbooks for every §14.3 operator action.
*Traces:* OPS-003–OPS-007; SoT §19.5, §6A.11. *Depends on:* DA-23 (states to observe). *Flags:* P5 (alert thresholds if Compliance rules differently).

**DA-31 — Tenant flags and rollout tooling** · **Team: PDM**
Per-tenant, per-module feature flags and kill switches; pilot-tenant configuration; shadow-period reconciliation job (expected tasks vs created; staged vs merged vs audit) per SoT §15.
*Traces:* ROL-001–ROL-003; SoT §15, §6A.3 principle 8. *Depends on:* DA-01…DA-30 progressively. *Flags:* none.

**DA-32 — End-to-end acceptance and failure-mode test suite** · **Team: PDM**
Automated coverage of SoT §15.2 minimum acceptance and the §14.2/§6B failure catalog: concurrency (dual submit, dual reviewer, rollback-vs-claim race), idempotency (double scheduler, double file, double Sync Latest, worker replay), tenant-isolation negative matrix, timing under load and dependency failure. Each feature ticket carries its own tests; this ticket owns the cross-module scenarios.
*Traces:* TST-001–TST-006; SoT §14.2, §15.2, §6B. *Depends on:* all phases. *Flags:* none.

---

## 3. Ticket count by team

| Team | Tickets |
|---|---|
| PDM | DA-01, DA-02, DA-03, DA-11, DA-17, DA-18, DA-19, DA-20, DA-21, DA-22, DA-23, DA-27, DA-28, DA-29, DA-30, DA-31, DA-32 (17) |
| Shared Services | DA-04, DA-05, DA-06, DA-07 (4) |
| Portal | DA-08, DA-09, DA-10, DA-13 (4) |
| Roster/Integration | DA-12, DA-14, DA-15, DA-16 (4) |
| MDM | DA-24, DA-25, DA-26 (3) |

## 4. Build order (dependency spine)

```
P1–P8 decisions/contracts
  → Phase A: DA-01 → DA-02, DA-03
  → Phase B: DA-04 → DA-05 → DA-06, DA-07          (parallel with C/D once DA-01 lands)
  → Phase C: DA-11 → DA-08/DA-09/DA-10 → DA-12, DA-13
  → Phase D: DA-14 → DA-15 → DA-16 → DA-17          (DA-15/16 gated on P1)
  → Phase E: DA-18 → DA-19 → DA-20; DA-22 → DA-21
  → Phase F: DA-24 → DA-25 → DA-23 → DA-27; DA-26 in parallel
  → Phase G: DA-28, DA-29, DA-30 → DA-31 → DA-32 → pilot rollout (SoT §15)
```

Phases B, C, and D can run largely in parallel after Phase A. Phase F is the critical path's tail: DA-23 cannot start before MDM's DA-24/DA-25 and cannot finish safely before DA-26 exists for audit completeness (DA-29 may ship with a flagged lineage gap if DA-26 slips — call it out at sign-off, don't hide it).

## 5. Explicitly NOT ticketed

**Out of scope for MVP 1 (S3 R6 — creating tickets against these fails the spike's acceptance criteria):** facility attestations; critical/non-critical, field-level, or timed auto-approval; post-OV-sync rollback/reversal; provider-facing outcome/status sync to the Portal; bulk client-collected attestation uploads; shared-credit/multi-plan propagation; survivorship configuration UI; source feedback-loop writes (reject reasons back to Candor); completion-funnel/discrepancy/trend analytics; report exports; reimplementation of survivorship, termination cascades, or downstream directory engines.

**Future functionality (S3 R7 — "not now," distinct from "not ever"):** post-sync compensating rollback ("Revert"); tier-based auto-accept (VERY_HIGH, non-critical); source feedback loop; attestation campaign management; OV-change reflection back to the Portal; per-field 90-day clocks (schema already supports); facility workflow; report exports.

## 6. Traceability matrix (Product Spec → tickets)

| Spec section | Requirement area | Tickets |
|---|---|---|
| §1.1.1–1.1.4 | Cycle computation, scheduling, practitioner-only trigger | DA-04, DA-05, DA-06 |
| §1.2.1–1.2.5 | Reusable outreach, T-30/T-7/T+1, link-back pattern | DA-07, DA-13 |
| §1.3.1–1.3.9 | Portal UX: entry, timeline, prefill, confirm/edit/flag, config, NPI protection | DA-08, DA-09, DA-10 |
| §1.4.1–1.4.4 | Practitioner vs admin, tenant policy (v2 D2-31: plan = tenant), first-writer, no drafts | DA-08, DA-10, DA-11 |
| §1.5.1–1.5.9 | Pending API, direct PDM writes, staging tables, deltas, envelope, routing, effective date, mandated fields | DA-01, DA-08, DA-09, DA-10, DA-11, DA-12 |
| §2.1.1–2.1.6 | Source file ingestion, parsing, no-op filtering, staging | DA-14, DA-15, DA-16 |
| §2.2 | Critical/non-critical routing | **Out of scope MVP1 — no ticket (by design)** |
| §2.3.1–2.3.4 | Shared staging tables, effective dating, tenant-specificity | DA-01, DA-16 |
| §2.4.1–2.4.3 | Source pluggability, config-driven mapping, cross-source ranking | DA-15, DA-25 |
| §2.5.1–2.5.5 | Ingestion edge cases | DA-16 |
| §3.1.1–3.1.4 | Unified queue, status, filters, evidence display | DA-18, DA-21 |
| §3.2.1–3.2.2 | Field-level review, partial approval | DA-19 (flag P4) |
| §3.3.1–3.3.5 | Accept/reject/save/bulk, Sync Latest, rollback | DA-19, DA-20, DA-21 |
| §3.4.1–3.4.4 | Manual sync, single write path, survivorship, termination handoff | DA-20, DA-23, DA-24, DA-25, DA-27 |
| §3.5.1–3.5.3 | Audit trail, OV lineage, reviewer RBAC | DA-22, DA-26, DA-29 |
| §3.6.1 | Non-attested reporting (only in-scope reporting item) | DA-28 |
| Cross-cutting (SoT §8: SEC/NFR/OPS/TST/MIG/ROL) | Isolation, reliability, operations, testing, migration, rollout | DA-02, DA-03, DA-17, DA-30, DA-31, DA-32 |

Every MVP1-in-scope spec item maps to ≥1 ticket; §2.2 is the single deliberate no-ticket row. (R1 ✓, R6 ✓)

## 7. Sign-off checklist (S3 R5 — required before any ticket is created)

- [ ] Shared Services lead — Phases B tickets + DA-04 DAL coordination
- [ ] Portal lead — Phase C tickets + P7 answer
- [ ] PDM lead — Phases A/E/F/G tickets (largest share — confirm capacity or re-split)
- [ ] MDM lead — DA-24/25/26 + normalization consult on DA-03
- [ ] Roster/Integration lead — DA-12/14/15/16 + P1/P2 ownership
- [ ] Product (Madhunika) — WD register (P3), out-of-scope confirmation (§5)
- [ ] Compliance — P4, P5, P6
