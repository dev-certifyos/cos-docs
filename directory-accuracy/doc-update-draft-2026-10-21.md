# Draft — doc updates for saved filter id + directory accuracy service rename (2026-10-21)

## Facts the draft rests on (verified)

- Egress design CP-41069 (Confluence 2282979391, In-Review): export request gets optional `selection` object `{ source: "SAVED_FILTER", filterId, expectedCount?, listUsed? }`; retry form `{ reuseSelectionFrom: <earlier correlationId> }`. Egress fetches filter, freezes list in `egress_selection_items` (keyed by `certifyId`, 7 days), slices of 10,000, status gets a `selection` block (`state`, `requested`, `distinct`, `matched`, `unmatched`, `parts`, `listUsedUri`, `reusedFrom`, `failureReason`). Failure reasons: `NO_MATCHING_PRACTITIONERS`, `FILTER_NOT_USABLE`, `SELECTION_TOO_LARGE`, `SELECTION_SOURCE_UNAVAILABLE`, `SELECTION_COUNT_MISMATCH`, `RESOLUTION_ABANDONED`; `403 SELECTION_SOURCE_DISABLED`; `429 SELECTION_QUOTA_EXCEEDED` with `Retry-After`. Egress design asks us (its ask A3) to drop our selection.
- Saved-filter store is in api-layer `main` (CP-44214, commit `5826766a95`): `POST /filters` (platform token, `{ entityType, filter }`, `201` new / `200` existing, `400` one error per broken rule), `GET /filters/{id}` for egress only (shared key `X-Saved-Filter-Api-Key`; we cannot read it). Filters are tenant-less. Switched off by default (`api-layer.saved-filter.enabled`). Rules: fields and operators per entity type, no OR, no `tenantId`, ≤ 20 fields, ≤ 200 values per list, ≤ 8 KB.
- Repo today: `vendor-exchange-service`; groups, service accounts, image and Makefile use `vendor-exchange-api` / `vendor-exchange-worker` (not the doc's `vendor-export-*`). Code paths are `/v1/vendor-exports/…`, `/v1/vendor-review/…`; both docs write them without `/v1`.
- Phase 1 Confluence page 2252767300 last edited 2026-09-28 = local `vendor-export/vendor-export.md`. Phase 2 page already uses `directory-accuracy-api` / `directory-accuracy-worker` and already states module 1 keeps no per-NPI registry.

## Decisions (settled 2026-10-21)

1. Schedule `PUT` takes and stores `filterId` only. Our service never calls the filter store and never sees criteria. A wrong id surfaces at export time as egress `FAILED: FILTER_NOT_USABLE` → batch `FAILED`, alert E4.
2. Request sends `listUsed: true`; batch stores `listUsedUri` only; operator uses it on a count mismatch (egress keeps it 7 days).
3. Preview endpoint removed. A filter matching nobody surfaces on the first run as `EMPTY` + E8; the filter's author checks its count before handing over the id.
4. Step numbers kept; step 2 becomes "Selection — egress resolves the saved filter".
5. Every operator and review endpoint path gets the `/v1` prefix the code uses (`/internal/…` push paths have none in code and stay as they are).
6. Companion docs are local only; not part of this pass.

---

## Phase 1 doc — CP-39602 vendor export (page 2252767300)

### Rename to directory accuracy service

- **Purpose and scope:** add the same "One service, three modules" block phase 2 has: service = directory accuracy service; repository `directory-accuracy-service` (renamed from `vendor-exchange-service`); vendor export (this module), vendor ingestion, later attestation are modules in one repo, one deployment, one database; only service and deployables carry the `directory-accuracy` name.
- `vendor-export-api` → `directory-accuracy-api`; `vendor-export-worker` → `directory-accuracy-worker`. Spots: Approach intro, flowchart (two subgraph titles + subscription filter box), decision D1 (two instance groups), Contracts intro ("Where the endpoints live"), step 3 identity line, request `userId`, step 4 subscription filter `attributes.initiator`, Security (two identity bullets).
- "the `vendor-export` service's own MongoDB database" → "the directory accuracy service's MongoDB database". Audit actor `system:vendor-export` and schedule `updatedBy` stay: actors are module-level (phase 2 uses `system:vendor-ingestion`; code uses both).
- Stay as they are (module-level names): collections `vendor_export_*`, permissions `vendor-export:manage|read`, path segment `vendor-exports` (under `/v1`), subscription `vendor-export-egress-events` and its dead-letter topic, recurring job `vendor-export-tick`, metrics `vendor_export.*`.

### `/v1` prefix

- `/vendor-exports/…` → `/v1/vendor-exports/…` everywhere (17 occurrences: Contracts intro, step 0 table and calls, tick trigger, retry, supersede, batch reads, events, flowchart `PUT` label). `/internal/vendor-exports/egress-events` unchanged (no `/v1` in code).

### Saved filter id instead of own selection

**Title**
- "scheduled selection, … and the export registry" → "Design: Vendor export — scheduled export by saved filter, file build through the egress service, event-driven completion, and the export batch record".

**Purpose and scope (owns list)**
- Schedule bullet: "selection filter" → "saved filter id".
- Selection bullet → "Selection by reference — the schedule holds an api-layer saved filter id; egress resolves it to practitioners at export time. This module pages no practitioners."
- Export registry bullet → "one batch row per tenant, vendor, and period, with egress's selection counts. No per-NPI rows."
- Markers line: unchanged. api-layer is still called for template provisioning (step 0) and the template pin (step 3); only the practitioner list call goes away.

**Flowchart**
- Remove `SelectJob`, `vendor_export_npis`, api-layer "practitioner list" edge, edges 4–8 and 18.
- `0b` stays "validate template" only; `0c` write schedule label adds `filterId`.
- Tick `3 enqueue` → `RequestEgressJob`.
- `9 POST export` label → "selection.filterId (retry: reuseSelectionFrom), destination, correlationId batchId-rN".
- `FinishJob` step → "compare egress selection count with file count"; renumber edge labels.

**Approach**
- Step 0: operator sends `filterId` (an api-layer saved filter id, created beforehand in the filter management module) with cadence and timezone; the service stores it as given. Remove "checks the selection criteria against our schema". Delete the preview bullet. Add one bullet: the id is not checked here (this service cannot read saved filters); a wrong id fails the first export as `FILTER_NOT_USABLE`.
- Step 1: "enqueues `SelectJob`" → "enqueues `RequestEgressJob`".
- Step 2 replaced: **"2. Selection — egress resolves the saved filter."** Bullets: egress fetches the filter by id, pages DAL, freezes the list once per export, reports counts in status and event; zero matches comes back as egress `FAILED: NO_MATCHING_PRACTITIONERS` and the batch goes `EMPTY`; why: one evaluation, filter checked once at save, no 100-NPI cap, no 700-call job here.
- Step 3:
  - State check `NPIS_SELECTED` → `SCHEDULED`.
  - Delete "Read the distinct NPIs from `vendor_export_npis`" and the 100-cap "Needs egress change".
  - Request carries `selection: { source: SAVED_FILTER, filterId, listUsed }` — **Needs egress change** (CP-41069).
  - Retry (`attempt > 1`): send `selection.reuseSelectionFrom: <previous correlationId>` so every attempt exports the same frozen list; if egress refuses reuse (`400`: unfinished, expired after 7 days), send `filterId` again and record `selection.reEvaluated = true`.
  - Rewrite "Whichever attempt's file wins is valid: the NPI set was fixed in `vendor_export_npis`…" → "…the practitioner set is egress's frozen list, reused across attempts…". Same edit in the pin rationale ("same reasoning that freezes the NPI set").
  - New failure bullets: `429 SELECTION_QUOTA_EXCEEDED` → job throws, JobRunr retries after `Retry-After`; `403 SELECTION_SOURCE_DISABLED` → `FAILED`, alert E4.
- Step 4/5: on `COMPLETED`, record egress's `selection` counts on the batch (event if it carries them, else the status call); on `FAILED` with `NO_MATCHING_PRACTITIONERS` → `EMPTY` + E8 instead of `FAILED`.
- Step 6: reconcile `totalRecords` (file metadata) against egress `selection.distinct`; rename result keys `registered` → `selected`; store `listUsedUri`. Rewrite "What a count match does not prove" — the duplicate-NPI case is gone (egress keys the list by `certifyId`); state what remains: records without an NPI are exported and counted as `unmatched`. Mismatch: operator compares the file with the list-used file within 7 days.
- Step 7: final-failure filter "`SCHEDULED` or `NPIS_SELECTED`" → "`SCHEDULED` or `EGRESS_REQUESTED`".
- Step 8: retry "A selection that already succeeded is not redone" → "retry reuses egress's frozen list"; supersede "enqueues its `SelectJob`" → "`RequestEgressJob`; the filter is evaluated afresh".
- "Why four jobs": three job types; delete the `SelectJob` row and the whole "Why select and request-egress are not one job" block.
- "Does NOT do": point-in-time bullet → egress's list is "as of the reading window" until the DAL cursor list with `asOf` exists; exclusion bullet → hook moves from `SelectJob` to the saved filter (or an egress-side exclusion), still an open product decision.

**Decisions table**
- D1 (two instance groups): rename groups.
- D5 (selection through api-layer list endpoint) → "Selection by saved filter id: the schedule stores the api-layer saved-filter id; egress resolves it per export and freezes the list" | instead of "Own paged selection through api-layer and NPIs in `npiFilter`; criteria stored on the schedule".
- D9 (registry, one document per NPI) → "Batch row stores ids, egress counts, and file facts; no per-NPI rows; reconciliation is egress selection count versus file count" | instead of "One document per NPI per batch; per-NPI reconciliation from the CSV".
- D12 (seven asks to egress): update count and wording to match Rollout.
- New D16: "The schedule stores a saved filter id only; the filter is checked once by api-layer at save, and this service never reads or validates criteria" | instead of "Criteria on the schedule, checked against our own JSON schema; a preview endpoint".

**Alternatives considered**
- New subsection "Own selection through api-layer, NPIs in the request (the earlier design)": good — we hold the list, no dependency on the filter store; cost — 700 calls per large tenant, egress still needs the 100 cap lifted, two evaluations of one filter, nothing validates the filter (DAL drops unknown operators and matches the whole tenant); why lost — egress design CP-41069.
- Tenant-configuration alternative: "selection filter" → "filter id".
- Cloud Run alternative: drop the "ten-minute selection" arguments (lines on SIGTERM during selection and `api` sized for a ten-minute job); remaining reasons stand.

**Contracts**
- Step 0 table: delete the preview row. `PUT` validation order → token and permission → tenant exists (internal; tenant id contains no underscore, kept from today) → `filterId` present and uses only letters, digits, `_`, `-` (api-layer's id format, at most 255 characters) → template → write. Request sample: `"selection": {…}` → `"filterId": "flt_0f8fad5b-d9cb-469f-a165-70867728950e"`. Change table: "Filter, template id" → "`filterId`, template id"; "it copied the selection at creation" → "it copied `filterId` at creation".
- Step 1: `SelectJob` → `RequestEgressJob`.
- Step 2 replaced by **"The saved filter"** (reference only, we never call it): who creates it (filter management module in api-layer, `POST /filters`), the id format, the rules api-layer enforces at save (fields and operators per entity type, no OR, no `tenantId`, 20 fields, 200 values, 8 KB), tenant-less, same criteria → same id, only egress reads it. Delete our field/operator table, page size, offset paging, `NPIS_REGISTERED`.
- Step 3: request sample — remove `npiFilter`, add `"selection": { "source": "SAVED_FILTER", "filterId": "flt_…", "listUsed": true }`, `userId: directory-accuracy-worker`; add retry sample with `reuseSelectionFrom`; add response table for `400`, `403`, `429`. Remove `filterMaxListSize` bullet.
- Step 4: subscription filter `initiator = "directory-accuracy-worker"`.
- Step 6: `NPIS_RECONCILED` → `EXPORT_RECONCILED`; delete `GET /vendor-exports/{exportBatchId}/npis`; add `listUsedUri` on the batch read.
- Retry table: delete `SELECT` row; `EGRESS` row back-to state `NPIS_SELECTED` → `SCHEDULED`; "the registered NPIs are reused" → "egress's frozen list is reused".
- Supersede: enqueues `RequestEgressJob`.

**Configuration**
- `selection` row → `filterId` (string, api-layer saved filter id, written by `PUT`, copied onto each batch).
- Rules: delete "validated against our JSON schema"; add "format-checked only; content checked by api-layer when the filter was saved".
- Egress-side settings: replace `filterMaxListSize` with `egress.selection.saved-filter.enabled` on, `egress.selection.max-size` ≥ tenant count, one large export per tenant at a time.
- api-layer side: `api-layer.saved-filter.enabled` on.

**Data model**
- Collections table: remove `vendor_export_npis`. Delete the whole `vendor_export_npis` section.
- Schedule sample: `"selection": {…}` → `"filterId": "flt_0f8fad5b-…"`.
- Batch sample: `selection` → `{ "filterId", "requested", "distinct", "unmatched", "parts", "reusedFrom", "listUsedUri", "completedAt" }` (copied from egress); `reconciliation.registered` → `selected`.
- Notes: delete `selection.page` line; states `SCHEDULED → EGRESS_REQUESTED → EGRESS_COMPLETED → DELIVERED`, side states unchanged.
- "the ingestion module … reads the registry" → "reads batch rows".
- Retention: drop "NPIs".

**Security**
- Identities: worker "api-layer read for the practitioner list" → "api-layer read for the template pin"; api group unchanged (template check).
- Tenant isolation: remove "every NPI row"; add "a saved filter carries no tenant; the tenant travels in the egress header".
- Selection safety → "this service holds no criteria, only an id; api-layer checks the filter at save and egress applies it".

**Performance**
- N4 → selection runs in egress (about 70 pages of 1,000 with the DAL cursor list; 700 of 100 interim), outside this service.
- N5: add egress end-to-end estimate 1.7–2.8 h for 70,000 (CP-41069), inside the 6 h first deadline.
- N7: remove the 333,000 NPI upserts. N8: remove NPI row storage.

**Observability**
- Metrics: delete `selection.pages`; `selection.practitioners` fed from egress `distinct`.
- E2 states: drop `NPIS_SELECTED`, add `EGRESS_REQUESTED` past deadline only if not already covered by E3. E6 wording: "egress file count differs from egress selection count". E8: `EMPTY` now from egress `NO_MATCHING_PRACTITIONERS`.

**Audit trail**
- `SCHEDULE_CREATED`: `selection` → `filterId`.
- Delete `NPIS_REGISTERED`, `EXPORT_SELECTION_COMPLETED`.
- `EXPORT_BATCH_EMPTY`: `criteria` → `filterId`, egress reason.
- `EXPORT_EGRESS_REQUESTED`: `npiCount` → `filterId`, `reusedFrom`.
- `NPIS_RECONCILED` → `EXPORT_RECONCILED` (`selected`, `inFile`, `match`).
- "Questions this answers" line: update the reconcile question.

**Failure modes**
- Phase 1 "Before egress (tick, selection, registration)" → "(tick)".
- Delete: api-layer down during selection; tick crash text "enqueues `SelectJob`" → `RequestEgressJob`.
- Change: zero match → egress `NO_MATCHING_PRACTITIONERS` → `EMPTY`, E8. Misspelt field or operator → refused by api-layer when the filter is saved, never reaches us; misspelt value → matches nothing → `EMPTY`, E8.
- Add: wrong or deleted `filterId` → egress `FILTER_NOT_USABLE` → `FAILED`, E4, operator fixes the schedule and retries; egress `403 SELECTION_SOURCE_DISABLED`; `429` quota; reuse target expired → re-evaluate.
- Delete "Preview before enabling is how this is caught early".
- "Egress practitioner count differs from registered" → "file count differs from egress selection count"; operator diffs against list-used file.
- Rollback: "registry rows, NPI rows" → "batch rows".

**Rollout**
- Egress ask 1: "Accept a large NPI list" → "Accept a saved filter id (`selection`) and resolve it in egress, with counts in the status, reuse on retry, list-used file — CP-41069, design 2282979391, In-Review".
- api-layer asks: replace with "Saved-filter store switched on per environment (`api-layer.saved-filter.enabled`) — CP-44214, built, off by default". Operators create filters in the filter module once it is built; not an ask. Location-level filters ask: retarget to the DAL filter language (egress design ask A2), not the api-layer list endpoint.


### Added by the verification pass

- **Step 3 / Step 6 text that still says "registered NPI set":** "a file from an earlier attempt is equally valid, because every attempt exports the same registered NPI set" → "…the same frozen list".
- **Step 6 "Why reconcile by count and not per NPI":** reword — egress reports counts; the list-used file exists for a mismatch; this module never reads the CSV or the list.
- **Jobs table:** `FinishJob` "Talks to: GCS and our registry" → "GCS".
- **Tenant-configuration alternative:** "The JSON schema on the type still checks the selection filter" → "The type would hold the filter id".
- **Cloud Run alternative, Cloud Tasks bullet:** "a log search across the tick, the select task, and the egress task" → "across the tick and the egress task".
- **Contracts "Order:" line:** "schedule (once) → tick → select job → egress request …" → "schedule (once) → tick → egress request …"; rationale map "2→step 2" kept (step 2 now describes egress's selection).
- **Step 5 status response sample:** add the `selection` block (`state`, `distinct`, `unmatched`, `listUsedUri`, `failureReason`).
- **Failure modes intro item 3:** "idempotent by object path and NPI id" → "idempotent by object path".
- **Rollout note under the egress table:** "asks 1 to 4 are small" → "asks 2 to 4 are small; ask 1 is the CP-41069 design".
- **Acknowledgement fields (mismatch with phase 2):**
  - Step 8 "Acknowledge: … sets `acknowledgedAt`" → "the ingestion module appends to `inboundFiles[]`; the first claim writes `EXPORT_BATCH_ACKNOWLEDGED`".
  - Batch notes "`acknowledgedAt`, `inboundBatchId` (written later by the ingestion module)" → "`inboundFiles[]` (written by the ingestion module)".
  - Audit row `EXPORT_BATCH_ACKNOWLEDGED`: `inboundBatchId, vendorBatchId, daysToResponse` → `uploadId, vendorBatchId, daysToResponse`, trigger "the first claim on a batch" (phase 2 wording).
- **Completion event carries selection data (settled 2026-10-21): extend egress ask 5 (CP-41070).**
  - Event sample gains `failureReason` (egress's reason code, null on `COMPLETED`) and the `selection` block (`state`, `requested`, `distinct`, `matched`, `unmatched`, `parts`, `listUsedUri`, `reusedFrom`); `failureReason` also as a message attribute.
  - Handler table: phase `FAILED` with `failureReason = NO_MATCHING_PRACTITIONERS` → batch `EMPTY`, `EXPORT_BATCH_EMPTY`, E8; other `FAILED` unchanged. Phase `COMPLETED` → `selection` counts and `listUsedUri` written onto the batch in the same transition.
  - Deadline check reads the same two from the status response.
  - `FinishJob` reconciles against counts already on the batch row; talks to GCS only. "Does not poll" bullet unchanged.
  - Rollout ask 5 text: add "failure reason code and the selection block (counts, list-used file location)" with why: tells an empty selection from a failure and gives reconciliation its expected count without a status call.
- **Egress sign-in (flagged by the egress design, not part of this change):** CP-41069 says egress has no verified caller identity today, while phase 1 Security says egress authenticates through IAP. Left as is; tracked outside this pass.

---

## Phase 2 doc — CP-40437 vendor ingestion (page 2302345225)

- **"One service, three modules" block:** add "Repository `directory-accuracy-service` (renamed from `vendor-exchange-service`)". Rewrite the instance-group bullet: drop "before the service name was settled … (module 1 open question Q10)" → "Module 1's doc uses the same names."
- **Identity lookup at staging (Alternatives):** "(or reads module 1's registry)" → delete; "Module 1 no longer keeps a per-NPI export registry (egress filters by practitioner id)" → "Module 1 keeps no per-NPI registry: egress resolves the saved filter and keeps its frozen list 7 days only".
- **Data model "Where":** "so the export registry is read and written without a network hop" → "so export batch rows are read and written without a network hop".
- **Rejected values, later readers (two spots):** "an exclusion input for module 1's export" / "module 1's export selection (exclusion by practitioner, location, field)" → "an exclusion input for module 1's saved filter" (selection now lives in the filter, not in a module 1 job).
- No other change: phase 2 already uses `directory-accuracy-api` / `directory-accuracy-worker`, never references `SelectJob`, `vendor_export_npis`, or `npiFilter`.

### Phase 2 `/v1` prefix

- `/vendor-review/…` → `/v1/vendor-review/…` everywhere (about 20 occurrences: Contracts endpoint list, "Where the endpoints live", Security, Approach steps 5–8). `/internal/vendor-ingestion/…` unchanged. File-ingestion paths (`/v1/ingestion…`, `/v1/consumers`, `/v1/mappings`) already carry it.

## Cross-doc consistency checks after edit

- Both docs: repository `directory-accuracy-service`; groups `directory-accuracy-api` / `directory-accuracy-worker`; no `vendor-export-api`, `vendor-export-worker`, `vendor-exchange` left.
- Both docs: no `vendor_export_npis`, `SelectJob`, `NPIS_SELECTED`, `npiFilter` (phase 1 Alternatives keeps them only inside the "earlier design" subsection).
- No path without `/v1` except `/internal/…`.
