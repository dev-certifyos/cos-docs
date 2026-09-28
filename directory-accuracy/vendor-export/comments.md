# Confluence review comments — CP-39602 vendor export design

Page: https://certifyos.atlassian.net/wiki/x/RIBGhg
Status: replies drafted here first, posted to Confluence only after review.

---

## Comment 1 — overwrite race between retry attempts (inline, on "Keeps the batch id, adds one to attempt")

**Comment (2026-09-27):**

> The new attempt gets a new egress correlation ID but appears to retain the same destination filename. If the earlier attempt places its file late, how do we prevent either attempt from overwriting the other? Please specify an object creation precondition and the existing-object behavior.

**Agreed draft reply:**

Good catch — marking the batch `FAILED` doesn't cancel the egress job, so an earlier attempt can finish late and race the retry. Fix, four parts; will update the doc:

1. **Cancel first:** egress already has a cancel endpoint (CP-35570, `POST /jobs/{type}/{tenantId}/{correlationId}/cancel`). On a retry, `RequestEgressJob` cancels the stored prior `egress.correlationId` before requesting the new attempt; the 48-hour deadline check also cancels before marking the batch `FAILED`. Cancellation is cooperative (checkpoint-based), so it shrinks the window but can't stop a pipeline already past its last checkpoint — hence 2.
2. **Precondition:** the egress copy uses `ifGenerationMatch: 0` (create-only). First writer wins; a late writer gets `412` and treats it as a no-op — the file is already in place.
3. **Existing object:** egress stamps `correlationId` in the object metadata; `FinishJob` accepts the file from whichever attempt won (same NPI set, same template) and records which.
4. **Retry preflight:** if cancel returns `409` (prior attempt already terminal), the job checks the destination; a complete object means the old attempt landed late — short-circuit to `EGRESS_COMPLETED` instead of re-exporting.

One filename, one file, retries invisible to the vendor.

**Doc changes:** step 3 (cancel-first bullet, destination ask, why-bullet on cooperative cancel), step 5 (cancel before `EGRESS_DID_NOT_FINISH`), step 6 (overwrite mechanism), step 8 (retry note), rollout ask #6, D11, contracts metadata.

---

## Comment 2 — template provisioning and revision pinning (inline, on "templateId — the tenant's vendor template")

**Comment (2026-09-27):**

> Does each participating tenant need its own Candor template? Who provisions and keeps those mappings consistent? Please record an immutable template revision and mapping-file generation on each batch so a retry uses the same definition.

**Agreed draft reply:**

Yes — the template row is tenant-scoped and egress requires the mappings path to contain the tenant id, so each tenant gets its own template. But nobody hand-makes them; will update the doc with all three parts:

1. **One master definition, provisioned by this module.** The Candor mappings CSV lives in this module's repo, versioned in git. The enable-tenant call (step 0) creates the tenant's template through api-layer's existing upload flow (`POST /egress-templates`), which validates every mapping against the field catalog before anything is stored. Twenty tenants are twenty machine-made copies of one reviewed file; re-running provisioning rolls out a new revision. Consistency by construction, and the owner is this module.

2. **Immutable revision per batch — using what already exists.** Template updates through api-layer never overwrite: each edit writes a new `v<N>/mappings.csv` and bumps the row's `version`, so every CSV revision is already immutable. And egress captures the request's `mappingsCsvUrl`, `separator`, `outputFormat`, and `rowExpansionKeys` per job at request time and never re-reads the template row mid-run. The only unpinned hop was between our attempts.

3. **The pin:** on attempt 1, `RequestEgressJob` reads the template row once and records `templateVersion`, `mappingsCsvUrl`, `separator`, `outputFormat`, `rowExpansionKeys` on the batch row *before* calling egress. Every attempt — first or retry — sends the pinned values on the export request (all existing request fields; zero egress changes). A retry is built from a byte-identical definition even if the template moved in between; the row change takes effect from the next batch. If the live row has moved when a retry runs, we log it as information — the pin makes drift harmless.

Operational rule added alongside: a template with non-terminal batches must not be deleted (its pinned CSV revision must stay readable).

**Doc changes:** step 0 (provisioning from master CSV, why-bullet, delete rule), step 3 (pin bullet, pinned values on the request, why-bullet on pinning, failure cases), batch-row contract (pin fields), step 0 contract (`egressTemplateId` provisioned not supplied), step 3 contract (request carries pinned fields; `templateId` is display-only for egress), outbound-file-contract section (authorship), D14.

---

## Comment 3 — Skipped NPIs exclusion (footer comment)

**Comment (2026-09-27):**

> @Dev Pandey How are NPIs intentionally marked 'Skipped' excluded from vendor exports?

**Agreed draft reply:**

Today they aren't — and that's deliberate, now stated in the doc rather than implied. Selection is criteria-only: every NPI matching the schedule's filter goes out each cadence. "Skipped" is a review action on the vendor's response (phase 2), not a flag on the practitioner record, so the export has nothing to filter on. Whether a skipped NPI should sit out later cadences is a product decision not yet taken — it trades reviewer workload against per-NPI vendor cost.

When it is taken, two mechanisms were weighed:

- **A flag on the practitioner record** ("skipped"), which selection criteria would filter out. Set aside: it stores an export-relationship concern in master data, needs DAL/api-layer changes plus an unskip path, and a flag can't carry per-entry reason, audit, or expiry.
- **A module-owned exclusion list** (`tenantId|vendor|npi`, with reason, source, added-by, optional expiry) that `SelectJob` consults after the criteria match, dropping listed NPIs and recording the dropped count on the batch. This is the standard suppression-list pattern: operators can write it by hand, the review module can write it automatically once skip semantics are decided, unskip is deleting a row, and neither api-layer nor egress changes.

The doc now records this as an explicit non-goal with the exclusion-list hook named, so the design lands either way without rework. The mechanism itself will be specced when the product decision is made.

**Doc changes:** one paragraph added to *What the approach deliberately does NOT do* (explicit non-goal, hook named). No mechanism designed in — deferred until the product decision.
