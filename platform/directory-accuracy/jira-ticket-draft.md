# Jira draft — Directory Accuracy service (Candor lane) — PUBLISHED 2026-09-09

**Status:** published to Jira 2026-09-09. Parent **CP-39602** (Story, epic CP-35879, Pod PDM, Sprint 119, 35 SP total). Subtasks: **CP-39603** spike 8 SP · **CP-39604** scaffolding 4 SP · **CP-39605** selection/export/inbound 5 SP · **CP-39606** ingestion 7 SP · **CP-39607** review APIs + Sync Latest 6 SP · **CP-39608** observability + audit 5 SP. Subtasks inherit the parent's sprint; estimates on subtasks live in "Story point estimate" (Story Points field not on the sub-task screen).
**Convention:** 1 story point = 1 hour. Implementation estimates are provisional — the spike (subtask 1) re-baselines them.

---

## Parent story

**Summary:** Directory Accuracy — external source ingestion and review service (Candor)

**Description (proposed):**

Following the 2026-09-08 product call, the Candor recommendation workflow is decoupled from the attestation module (decision D2-38 in the source-of-truth doc). Candor is not an input lane into attestation review — it is an independent directory-accuracy program with its own population, cadence, staging, review, and release path.

Scope of the program end to end:

- Monthly (configurable) export per tenant: NPI selection via a tenant-configured query filter, file delivered to the vendor over the existing shared SFTP platform (Candor account `candor-health` is already provisioned — TS-111546).
- Ingestion of the vendor's response file: batch registry for duplicate shielding, chunked streaming parse through a vendor adapter, one disposition per row, quarantine over guessing, previously-rejected suppression.
- Review APIs: endpoints to query ingested recommendations and mark them approved / rejected (with reason) / skipped. The reviewer UI lives in the Attestation Module UI surface and consumes these endpoints — this story delivers APIs only.
- Sync Latest release: endpoints to query approved rows and release them to the Golden record under the tenant-scoped source `candor:{tenantId}` (decision D2-37 — rebuild the full slice from a released-items ledger on every sync, never sparse-write).

Out of scope: any coupling to attestation tasks or cycles; Golden-record engine changes (extend-only); auto-approval (manual review only in MVP); any reviewer UI (built in the Attestation Module UI surface, not here).

---

## Subtasks

### 1. Spike — design doc for the directory-accuracy Candor lane

- **Summary:** Spike: design doc — directory accuracy Candor lane (export, ingestion, review APIs, release)
- **What:** author the design doc per the standard design-doc template and house style. Rework the retired SFTP-exchange and ingestion drafts into the decoupled shape.
- **Must decide inside the doc:** whether this is its own service or part of an existing module's backend; selection-query configuration format/storage/injection safety (open item O-17); skip semantics and cross-cadence re-send (O-18); release path — own vs shared Sync Latest machinery (O-19); inbound delivery mechanism (e.g. manifest JSON vs filename identity); vendor contract proposal for the Candor call (O-1) including large-file clauses (CSV/NDJSON above ~500k rows, gzip, declared row counts).
- **Exit criteria:** doc reviewed and approved by two people; open questions either closed or assigned an owner; implementation subtask estimates re-baselined.
- **Estimate:** 8 SP

### 2. Service scaffolding and infrastructure

- **Summary:** Scaffolding and infrastructure — tables, config, wiring
- **What:** set up the database tables and migrations, tenant configuration entries, messaging/queue wiring, and deployment plumbing for the lane. Concrete shape (own service vs part of an existing backend, deployables, service accounts) is decided by the design doc — this subtask executes whatever it fixes.
- **Depends on:** subtask 1.
- **Estimate:** 4 SP — provisional

### 3. Selection query, export, and inbound receipt

- **Summary:** Tenant-configured NPI selection, export file to vendor bucket, inbound file detection
- **What:** run the tenant-configured selection query, extract the NPIs, build the export file, write it to the vendor bucket `from/` folder; detect and register the vendor's inbound file per the mechanism the design doc fixes (e.g. manifest JSON declaring what was uploaded and when parsing may start), with duplicate shielding and archive-before-parse.
- **Depends on:** subtasks 1, 2; vendor contract (O-1) fixes the export column set.
- **Estimate:** 5 SP — provisional

### 4. Ingestion pipeline — adapter, chunked parse, dispositions

- **Summary:** Parse vendor file to staged recommendations with one disposition per row
- **What:** Candor adapter (schema-versioned; unknown version rejects the whole batch); chunked streaming parse as retryable tasks with row-fingerprint idempotency; disposition cascade (actionable / no-op / duplicate-stale / excluded / quarantined / previously-rejected) with count reconciliation against the batch; quarantine store and replay; `rejected_recommendations` suppression lookup; same-transaction audit throughout. Candor schema and the outbound/inbound mechanism are already settled by the design doc before this starts.
- **Depends on:** subtasks 2, 3.
- **Estimate:** 7 SP — provisional

### 5. Review APIs and Sync Latest release

- **Summary:** Review/approval endpoints and release of approved rows to the Golden record
- **What:**
  - Review APIs: query endpoints over ingested recommendations (filters, evidence fields); approve / reject-with-mandatory-reason / skip actions, one decision per row enforced by constraint; rejected rows feed the suppression table; server-side tenant isolation. Consumed by the Attestation Module UI surface.
  - Sync Latest release: endpoint to query approved rows and release them — released-items ledger, rebuild the full slice per (source, practitioner) and write via DAL UPSERT under `candor:{tenantId}` (never sparse-write); per-tenant survivorship ranking entry for the new source (unranked sources fall to rank 997 and lose to roster); release status reporting.
- **Depends on:** subtasks 1, 4.
- **Estimate:** 6 SP — provisional

### 6. Observability and audit

- **Summary:** Metrics, alerts, ops tooling, runbook, audit trail
- **What:**
  - Observability and ops: batch metrics, reconciliation and suspected-partial-file alerts, DLQ depth alert, quarantine/batch replay tooling, runbook.
  - Audit trail: same-transaction audit events across the lane (export, ingestion dispositions, review decisions, release) with 7-year retention; audit written in the same transaction as the state change, never after.
- **Depends on:** subtasks 3, 4, 5.
- **Estimate:** 5 SP — provisional

---

## Totals and notes for review

- **Total:** 35 SP (35 h) — spike is 8 SP; implementation estimates re-baseline after the design doc.
- Existing ticket-breakdown rows re-homed here (Jira updated once the design doc is final, per standing rule): DA-14 (SFTP onboarding/pickup), DA-15 (adapter framework), DA-16 (disposition engine), DA-17 (batch ops tooling), DA-18 (review queue APIs), and the export job that never had a ticket.

## Publishing decisions (answered 2026-09-09)

1. Epic: CP-35879 (same epic as the attestation work).
2. Issue types: parent Story CP-39602 + six Sub-tasks.
3. Ownership: PDM pod.
4. All six subtasks created up front; estimates re-baseline after the spike.
5. Sprint 119 on the parent (subtasks inherit); parent carries total 35 SP in both Story Points and Story point estimate; subtasks carry individual estimates in Story point estimate.
