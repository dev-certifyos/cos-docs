# Product clarifications received 2026-09-08 — analysis and blocker record

*Reference input, not source of truth. Nothing here is folded into the v2 decision register (§7) or the open register (§9.1) until Dev reviews and Product ratifies the items marked "confirm". Field-mapping question sent to Product (Madhunika Sivasankar) on Slack; Product replied same day. Two further design questions (association-level attestation, termination flow) raised in the same thread; no answer yet.*

Jira: blocked subtasks CP-38092 (Doc 01 Cycle), CP-38538 (Doc 02 Portal lane), CP-38539 (Doc 03 SFTP), CP-38540 (Doc 04 Ingestion), parent CP-38793.

---

## 1. Field mapping — what Product said vs. what we asked

Legend: **Confirmed** = mapping can go into the GET contract as-is. **Partial** = level decided, source column still ambiguous. **Open** = no usable answer yet.

| # | Field | Product answer (verbatim gist) | Status | Resolved mapping | What is still needed |
|---|---|---|---|---|---|
| 1 | Provider name | no comment | **Confirmed by silence** | `Practitioner.prefix/firstName/middleName/lastName/suffix` → `core_practitioners_ov` | Nothing. Treat silence as acceptance; state that in the ratification message. |
| 2 | Group affiliation | no comment | **Confirmed by silence** — but see §2 | link row `tenant_group_practitioners` | Directly entangled with the association-level question (§2): if a provider admin scoped to Group A opens the form, do they see and attest the Group B link row? |
| 3 | Street address(es) | "only practice address is being attested" | **Confirmed** | `core_entity_addresses_ov` rows reached via `location_entity_addresses` where address type = practice, for each of the practitioner's locations (`group_practitioner_locations` → `tenant_group_locations` → `group_locations` → `core_locations_ov`). Mailing/billing excluded. `Practitioner.addresses[]` on `core_practitioners_ov` excluded. | Confirm add/remove semantics: can the attester remove a practice location entirely, or only edit the address of an existing one? (Removing a location is a terminations-engine change, v2 §6 "Terminations", not an OV edit.) |
| 4 | Telephone number(s) | "practitioner's own phone number from core_practitioner, as well as the practice location phone number (agnostic of network)" | **Partial** | Two sources: (a) `Practitioner.phoneNumbers[]` → `core_practitioners_ov`; (b) one phone per practice location on `core_locations_ov`. Per-network `appointmentPhone` on `tenant_group_location_practitioner_networks` excluded. | `core_locations_ov` has four phones: `phone`, `appointmentPhone`, `afterHoursPhone`, `callCoveragePhone`. "The practice location phone number" names none of them. Assume `phone`; ask Product to confirm whether the other three are shown, attested, or hidden. |
| 5 | Website URL | "we might end up adding website at practitioner level, this is an open discussion point" | **Open** | Today: `Location.website` → `core_locations_ov` only. No practitioner-level website field exists on `Practitioner`. | Decision: (a) MVP attests `Location.website` per practice location and a practitioner-level field is a later additive change; or (b) MVP waits for a new `Practitioner.website` field (schema + Liquibase + survivorship change, cross-team with MDM). Recommend (a). Product must pick. |
| 6 | Specialty | "tenant practitioner specialty is what it refers to" | **Confirmed, with a routing consequence** | `tenant_practitioner_specialty` (tenant-level link), not `Practitioner.specialties[]` on the OV and not the location specialties. | The attested value is a tenant link table, not an OV attribute. Release path for an edited specialty therefore does not go through survivorship/`contribution_map`; it writes a tenant link row. Flag to doc 5 (backend) and doc 6 (data layer): specialty is the one mandated field whose release target is not the Golden record. Confirm with Product that "specialty" edits mean add/remove tenant-specialty links. |
| 7 | Accepting new patients | "group practitioner location level, might extend to group practitioner location network level in the future. Will be good to build in the flexibility" | **Confirmed level; contradicted by F-2 answer (see below)** | `TenantGroupPractitionerLocation.acceptingNewPatients` → `group_practitioner_locations` ("this doctor takes new patients at this office"). Network level excluded for MVP. Our F-2 recommendation (most specific = network) is **rejected**. | Flexibility requirement: prefill item identity must carry a level discriminator so a network-level row can be added later without a contract break. Already covered by F-3's per-item identity; note it explicitly in the portal-lane contract. |
| 8 | Cultural / linguistic | "practitioner record level attestation" | **Confirmed** | `Practitioner.languages[]` (+ `culturalCompetency`) → `core_practitioners_ov`. `Location.languagesSpokenAtLocation` and network `languageSpoken[]` excluded. | Confirm whether `culturalCompetency` is displayed alongside `languages[]` or only languages. Minor. |
| 9 | Disability accommodations | no comment | **Confirmed by silence** | `Location.adaCompliance` → `core_locations_ov`, per practice location | `adaCompliance` is a structured object. Form needs a field-by-field rendering decision (which sub-attributes are attestable). Engineering, not Product, but flag so nobody assumes it is a single boolean. |
| 10 | Telehealth | F-1: "telehealth is at a practitioner level, not related to location" | **Confirmed — closes O-12** | `Practitioner.telemedicineAvailable` + `telemedicineURL` → `core_practitioners_ov`. No new field. | Nothing. Record as D2-xx on ratification. |
| 11 | NPI | no comment | **Confirmed** | `Practitioner.npi`, display-only (D2-19) | Nothing. |

### F-1 / F-2 / F-3 and the directory-display flag

| Finding | Product answer | Reading | Status |
|---|---|---|---|
| F-1 telehealth level | practitioner level | Closes O-12. | **Resolved** |
| F-2 attest level | "accepting patients, office hours will be at location level for now, not location network level. Network participation attestation is not in scope currently... Languages and cultural competencies will be at practitioner level." | Languages: practitioner level, consistent with #8, closes that half of O-13. **Accepting patients: "location level" here vs "group practitioner location level" in #7 are two different tables** (`core_locations_ov.acceptsNewPatients` = the office; `group_practitioner_locations.acceptingNewPatients` = this doctor at this office). Almost certainly #7's answer is the intended one (the practitioner attests their own fact, not the office's). **Office hours: Product names it as an attested field; D2-25 excludes office hours from the MVP form.** Either Product is answering our question generically (we asked about office hours in F-2) or D2-25 is being reopened. | **Partially resolved — two confirmations needed:** (1) accepting patients = `group_practitioner_locations`, not `core_locations_ov`; (2) office hours stays excluded per D2-25. |
| F-3 composed view | not addressed | Engineering-owned; no Product input needed. | n/a |
| `includePractitionerLocationInNetworkDirectory` | "Directory display is not applicable because the universe of providers who need to submit attestations is only those who have directory display set to yes." | Closes O-14 as **not attestable, not displayed**. But it introduces a **new population rule** nobody has: the attestation universe = practitioners with directory display = yes. Cycle doc defines in-scope as *active practitioners only* (cycle.md "In-scope = active practitioners only", D18). The flag lives on `tenant_group_location_practitioner_networks`, i.e. per practitioner-location-network row, so "practitioner has directory display = yes" needs a definition: any row Y? all rows Y? And a practitioner whose only Y row flips to N mid-cycle behaves like a soft termination (close task? cancel outreach?). | **Resolved as a field question; opens a new cycle-doc question.** Proposed amendment to doc 1 (backfill, seeding, weekly `population` reconcile check, and the task-open re-check) and a fifth sub-question for O-16. |

### Not answered at all

- Per-field editability ("editable per tenant config") — no comment. Assume the per-field editability map in `attestation-module-config` stands (portal-lane.md).
- Which practice locations are in scope: all rows in `group_practitioner_locations` for the practitioner in the tenant, or only those with directory display = yes? Follows from the directory-display answer above; needs an explicit yes.
- Address / location add-remove semantics (#3).

### Net effect on the GET contract

Resolvable today with stated assumptions: 1, 2, 3, 6, 8, 9, 10, 11 (eight of eleven). Blocked on a one-line Product confirmation: 4 (which location phone), 7 (which table). Genuinely open: 5 (website level). Plus the population rule from the directory-display answer, which is a doc 1 change, not a doc 2 change.

---

## 2. Association-level attestation — Product reopening D2-17

**Decided state.** D2-17 (v2 §7, decided 2026-08-27, closed O-7): provider admin authorization is tenant-scoped only; a provider admin attests for any practitioner in their tenant; group-level narrowing "explicitly not wanted". Listed in `instructions.md` §6 as a constraint module docs must not reopen. Built into: portal-lane.md (identity resolution via `portal_entity` resolves an admin to every practitioner in the tenant), portal-lane-concepts F9, architecture-brief ("no new association model to build"), audit brief (`PROVIDER_ADMIN` authorization basis = tenant scope). Ticket breakdown P7 still lists this as open and blocking DA-08; that line is stale relative to D2-17.

**What Product is now saying.** A provider admin associated with Group A should attest only Group A's data for a practitioner who is also in Group B. That is group-level association: the thing D2-17 rejected.

**Why it is not a small change.** With the field mapping above, the attested set splits into two kinds:

- *Practitioner-level facts* (name, specialty, languages, telehealth, NPI, own phone): one value per practitioner per tenant. Cannot be partitioned by group. Any admin who can attest for the practitioner attests these for every group at once.
- *Location-level facts* (practice address, location phone, website, accepting patients, ADA): one row per practitioner-location, and every location is reached through a group (`tenant_group_practitioners` → `group_practitioner_locations`). These *can* be filtered by the admin's groups.

So "attest only Group A data" means: practitioner-level fields plus Group A's location rows. Group B's location rows are unattested. That breaks:

1. **One task per practitioner, one attestation per practitioner** (D2-28, D2-31, cycle F5 "no second task while any task is open"; the retired "one task per plan" alternative was rejected for exactly this duplication). Either the task grain becomes practitioner × group, or the task stays practitioner-grained and gets a per-row attestation status.
2. **Task states.** `SCHEDULED → OPEN → SUBMITTED (→ CLOSED)` has no partial state. A per-row or per-group "attested / pending" sub-status is new schema on `attestation_tasks` or a new child table.
3. **`snapshot_version`.** It is a hash of the full displayed field set. A per-group submission hashes a subset; stale-snapshot detection needs redefining.
4. **Outreach.** T-30 / T-7 / OVERDUE is per task, cancelled on submission via `cancellationKey = attestation-task:<taskId>`. A partially attested task must not cancel; the ladder must keep running until the last group is done, or be re-targeted to Group B's admin.
5. **Identity resolution.** `portal_entity` resolution in provider-portal-api goes from "tenant" to "tenant + group set", and the practitioner's own scope stays "everything". Two authorization shapes for one endpoint.
6. **The non-attested report** (DA-28) and the 90-day compliance clock: is a practitioner with Group A attested and Group B pending compliant?

**Scenarios that must be answered before design continues** (already put to Product 2026-09-08, unanswered):

- Group A attested, Group B not: is the practitioner "partially attested"? What is stored?
- Does Group B stay pending until someone else attests? Who is notified, and how (a second outreach ladder to Group B's admin?)
- No provider admin exists for Group B: can the practitioner attest the remainder themselves? Does the practitioner always see the full set?
- UI and backend representation of partial status; effect on the compliance clock and on the non-attested denominator.

**Recommendation.** Hold D2-17 unless Product states the group-scoped requirement in writing as a compliance or client commitment. If they do, it is a v2 amendment (new D2-xx superseding D2-17) with rework in docs 1 and 2 and the ticket breakdown (DA-04, DA-05, DA-07, DA-08, DA-09, DA-10, DA-11, DA-13, DA-28), not a note in doc 2.

---

## 3. Termination and attestation — O-16 still open, plus new sub-questions

**Decided state.** D2-36 / cycle D18 (2026-09-07): terminated practitioners excluded at backfill and seeding; task-open re-check skips them (`TASK_OPEN_SKIPPED_TERMINATED`, row stays `SCHEDULED`); a PDM practitioner-terminated event (does not exist today, PDM-2) closes rows to `CLOSED` and writes `OUTREACH_CANCEL`; weekly reconcile `terminated` check is the net until the event exists. `CLOSED` and `SCHEDULED` tasks 404 on the portal detail and submit endpoints (portal-lane D6). Emailed deep link is tokenless and never expires (D17, D2-23).

**Open (O-16, four-question ask drafted 2026-09-07, unanswered):**

1. Terminated right after open, before any email: cancel + close? (proposed yes)
2. Terminated after submission, review pending: does review continue? (proposed yes)
3. Termination rolled back: revive `CLOSED` task or fresh obligation? (proposed: `CLOSED` stays closed, fresh obligation from reinstatement via the seeding check)
4. 90-day clock continues across the gap or restarts? (proposed: fresh clock)

**Additional sub-questions surfaced 2026-09-08, mapped to the scenarios sent to Product:**

- *Scenario 1 (terminated before any outreach)*: covered by D18 layers 2 and 3 and O-16 q1. Design answer exists; needs Product ratification only.
- *Scenario 2 (terminated after an outreach was sent, practitioner opens the link)*: today's design = 404 on both view and submit. Product asked whether they should be able to *view but not submit*. That is a **UX decision the current design does not offer**: `CLOSED` is never consumer-visible (D6). Supporting view-only for `CLOSED` means D6 changes (return `CLOSED` with `submittable: false`) and the portal renders a "no longer required" state instead of a 404. Add as O-16 q5.
- *Scenario 3 (rollback)*: O-16 q3 and q4 cover revive-vs-fresh and the clock. Not covered: **if outreach was partially sent before termination, does the reinstated obligation restart the full ladder or continue from the remaining tiers?** Under the proposed answer (fresh obligation, fresh clock) the ladder restarts by construction; state that explicitly. Add as O-16 q6.
- *Reinstatement path*: today it is the weekly `population` seeding check, so re-entry lags up to seven days and there is no PDM "reinstated" event. Confirm the lag is acceptable or extend PDM-2 to a status-change event (terminated and reinstated).
- *Directory-display flip to N* (from §1): behaves like a soft termination for the attestation universe. Same closer, same questions. Add as O-16 q7 or a new O-17.

**Also blocked on:** PDM-2 event topic and payload (Q12, PDM team). Until it exists, the weekly reconcile is the only closer and a terminated practitioner can receive up to a week of reminders plus one race-window email (`CANCEL_TOO_LATE`).

---

## 4. Blocker map — which ticket waits on what

| Ticket | Doc | Blocked on | Owner |
|---|---|---|---|
| CP-38092 | Doc 01 Cycle | O-16 termination timing (4 + 3 new sub-questions); PDM-2 event contract (Q12); **new** population rule from directory-display = yes; D2-17 reopening changes task grain and outreach cancellation | Product + Compliance; PDM team |
| CP-38538 | Doc 02 Portal lane | D2-17 reopening (identity resolution, task grain, snapshot hash); field mapping items 4, 5, 7 and the office-hours / D2-25 contradiction; view-only `CLOSED` UX (Scenario 2) | Product; Portal lead |
| CP-38539 | Doc 03 SFTP | *not these three reasons*: O-1 Candor file contract, P2 transport confirmation | Product + Candor + Anmol/Ansar |
| CP-38540 | Doc 04 Ingestion | *not these three reasons*: P1 signed vendor schema + confidence-tier mapping (O-1) | Product + Candor |

Stale line to fix when tickets are updated: `ticket-breakdown-mvp1.md` P7 still says association depth is open and blocks DA-08; either mark it resolved by D2-17 or, if Product's reopening stands, widen its "Blocks" list to the tickets in §2.

---

## 5. Jira comment drafts

### 5.1 Parent CP-38793 (Spike: Directory Accuracy and Portal Attestation)

```
Moving the design-doc subtasks (CP-38092, CP-38538, CP-38539, CP-38540) to Blocked. The engineering side of the docs is done, but we still need clarity from Product on three things before the design can be frozen.

1. Who can attest what
   Our current design assumes both the practitioner and a Provider Admin can attest all of the practitioner's data, regardless of which groups the admin is associated with. Product has indicated a Provider Admin should only attest data for the groups they are associated with. If that is the expected behavior, we need answers to:
   - If the admin attests only Group A, what happens to Group B?
   - Is the practitioner then "partially attested", and how is that shown?
   - Does Group B stay pending until someone else attests it? Who is notified?
   - If Group B has no Provider Admin, can the practitioner attest the rest themselves?
   This changes the data model, status handling, API contracts and the Portal flow, so we need to align on it now.

2. Termination flow
   We need the expected behavior when a practitioner is terminated during an attestation cycle:
   - Terminated before any outreach is sent: cancel scheduled reminders and close the attestation?
   - Terminated after a reminder was sent and the practitioner opens the link: can they still view the data, view but not submit, or is the page blocked?
   - Termination is later rolled back: does the practitioner re-enter the cycle automatically, do cancelled reminders get rescheduled, and does the schedule restart or continue from where it stopped?

3. Field mapping for the attestation form
   Product replied on the field list (thanks). Most fields are now clear. Still need a decision on:
   - Phone number: which practice location phone is shown (main phone only, or also appointment / after-hours / call-coverage)?
   - Accepting new patients: "doctor at this location" or "location as a whole"? The two answers we got point to different fields.
   - Website: keep at location level for now, or wait for a practitioner-level website field?
   - Office hours: still out of the MVP form as previously agreed?
   - Directory display = yes as the attestation universe: does one location with display = yes qualify the practitioner, or all locations? And if it later flips to no, treat like termination?

CP-38539 (SFTP) and CP-38540 (Ingestion) are blocked separately on the Candor file contract and vendor schema, not on the above.

Detailed analysis: cos-docs/attestation-module/reference/product-clarifications-2026-09-08.md
```

### 5.2 Short per-subtask comments

**CP-38092 (Doc 01 Cycle):**
```
Blocked. Doc finalized and re-approved 2026-09-07, but three inputs change its contracts: (1) O-16 termination timing rules with Product + Compliance, four questions from 2026-09-07 plus two added 2026-09-08 (view-only CLOSED link; ladder restart vs continue after reinstatement); (2) PDM-2 practitioner-terminated event topic/payload with the PDM team, until then the weekly reconcile is the only closer; (3) new population rule from Product 2026-09-08: universe = practitioners with directory display = yes, which changes backfill, seeding, the weekly population check and the task-open re-check. If Product's reopening of D2-17 (group-scoped provider admins) stands, task grain and outreach cancellation change too. Details on CP-38793.
```

**CP-38538 (Doc 02 Portal lane):**
```
Blocked. Doc finalized 2026-08-27 on D2-17 (tenant-scoped provider admin); Product now indicates group-scoped attestation, which changes identity resolution, task grain, snapshot_version and adds a partial-attestation state that does not exist. Field-mapping answers from Product 2026-09-08 resolve 8 of 11 fields; still need: which core_locations_ov phone (#4), accepting-new-patients table (group_practitioner_locations vs core_locations_ov, #7 vs F-2 answers conflict), website level (#5), and whether office hours stays excluded (D2-25). GET contract cannot freeze until these land. Details on CP-38793.
```

**CP-38539 (Doc 03 SFTP):**
```
Blocked on the Candor side only: O-1 file contract (columns, enums, manifest fields, schema versioning) and P2 transport confirmation. Doc itself finalized and approved 2026-08-27. Not affected by the D2-17 / termination / field-mapping items on CP-38793.
```

**CP-38540 (Doc 04 Ingestion):**
```
Blocked on P1: signed vendor schema and canonical ordered confidence-tier mapping (resolves CT-011, O-1). Doc drafted; review and template retrofit can proceed, sign-off cannot. Not affected by the D2-17 / termination / field-mapping items on CP-38793.
```

---

## 6. Ratification message back to Product (Slack, to send after Dev review)

```
Thanks — recording these. Reading back so we can freeze the contract; please correct anything wrong:

Confirmed: #1 name, #2 group affiliation, #3 practice address only (mailing/billing out), #6 tenant practitioner specialty, #8 languages/cultural at practitioner level, #9 ADA per practice location, #10 telehealth at practitioner level, #11 NPI display-only. Directory display flag: not shown, not attested; it defines who gets an attestation at all.

Need one more line each:
a) #4 phone — core_locations_ov has phone / appointmentPhone / afterHoursPhone / callCoveragePhone. We will show `phone` only unless you say otherwise.
b) #7 accepting new patients — "group practitioner location level" (this doctor at this office) and "location level" (this office) are two different fields. We are taking the first. OK?
c) Office hours — your F-2 note lists it; D2-25 has it out of the MVP form. Still out?
d) #5 website — we propose MVP attests the location website per practice location; a practitioner-level website is a later add. OK?
e) Directory display = yes as the universe: practitioner qualifies if ANY location-network row is Y, or only if ALL are? And if the last Y flips to N mid-cycle, treat like termination (close task, stop reminders)?

Separately, still waiting on: the provider-admin group-scope question (this reopens the 2026-08-27 decision that admins are tenant-scoped) and the termination questions from 2026-09-07.
```

---

## 7. Portal UI field list — what we show, where we read it (record for reference)

Basis: Product answers 2026-09-08 + v2 §6.7.1 mapping (column names verified against `schemas/entities/*.schema.json` and the DAL Liquibase tables on 2026-09-01). Product's 11 mandated names become **16 technical fields** in **two blocks**: one practitioner block, then one block per practice location. Where Product named one field but the data lives in two tables, it is split into two UI fields and marked **(split)**.

Grain: **P** = one value set per practitioner per tenant. **L** = one value set per practice location, repeated for every row in `group_practitioner_locations` for this practitioner in this tenant (location resolved `group_practitioner_locations` → `tenant_group_locations` → `group_locations` → `core_locations_ov`).

### Block A — Practitioner (grain P)

| # | Product's name | Portal UI field | Read from | Status |
|---|---|---|---|---|
| A1 | Provider name | Name (prefix, first, middle, last, suffix) | `core_practitioners_ov`: `prefix`, `firstName`, `middleName`, `lastName`, `suffix` | Confirmed (by silence). Five sub-fields or one composed display; editability per tenant config. |
| A2 | NPI | NPI | `core_practitioners_ov`: `npi` | Confirmed. Display-only, never editable (D2-19). |
| A3 | Telephone number(s) **(split 1/2)** | Practitioner phone number(s) | `core_practitioners_ov`: `phoneNumbers[]` | Confirmed ("practitioner's own phone number from core_practitioner"). List field. |
| A4 | Specialty | Specialty(ies) | `tenant_practitioner_specialty` (tenant-level link rows) | Confirmed ("tenant practitioner specialty"). **Not** `Practitioner.specialties[]`, **not** location specialties. Release target is a tenant link table, not the Golden record — flag for docs 5/6. |
| A5 | Cultural / linguistic capabilities | Languages spoken | `core_practitioners_ov`: `languages[]` | Confirmed ("practitioner record level"). |
| A6 | Cultural / linguistic capabilities | Cultural competency | `core_practitioners_ov`: `culturalCompetency` | Same answer covers it; confirm whether shown as a separate field or dropped. Minor. |
| A7 | Telehealth availability | Telehealth offered | `core_practitioners_ov`: `telemedicineAvailable` | Confirmed (F-1: practitioner level). |
| A8 | Telehealth availability | Telehealth URL | `core_practitioners_ov`: `telemedicineURL` | Same answer. Shown alongside A7. |
| A9 | Group affiliation | Group(s) | link rows `tenant_group_practitioners` (`tenant_group_id` → `tenant_groups`, `tenant_practitioner_id` → `tenant_practitioners`); group display name via the `tenant_groups` → group record join | Confirmed (by silence). List field; add/remove = terminations engine, not an OV edit. **Entangled with the association-scope question (§2).** |

### Block B — Per practice location (grain L, repeated per location)

| # | Product's name | Portal UI field | Read from | Status |
|---|---|---|---|---|
| B0 | — (header) | Location name / identifier | `core_locations_ov` (location name column, pin in doc 5) | Needed to label each block; not attested itself. |
| B1 | Street address(es) | Practice address (line 1, line 2, city, state, zip) | `core_entity_addresses_ov`: `addressLine1`, `addressLine2`, `city`, `state`, `zip`, reached via `location_entity_addresses` where `addressType` = the practice type | Confirmed ("only practice address"). Mailing/billing excluded. **Correction 2026-09-08:** `AddressType` enum has no `practice` value (`billing`, `mailing`, `MRStorageAddress`, `office`, `service`, `w9Address`); `office` assumed, Product to confirm. `Practitioner.addresses[]` excluded. |
| B2 | Telephone number(s) **(split 2/2)** | Practice location phone | `core_locations_ov`: `phone` | Level confirmed ("practice location phone number, agnostic of network"). **Column pending**: Product to say whether `appointmentPhone`, `afterHoursPhone`, `callCoveragePhone` are also shown. Per-network `appointmentPhone` on `tenant_group_location_practitioner_networks` excluded. |
| B3 | Website URL | Practice website | `core_locations_ov`: `website` | **Open**: Product may move website to practitioner level (no such field exists today). Working assumption: location level for MVP. |
| B4 | Accepting new patients | Accepting new patients at this location | `group_practitioner_locations`: `acceptingNewPatients` (this doctor, this office) | Level confirmed per Product's #7 answer. **Confirm** it is not `core_locations_ov.acceptsNewPatients` (the office as a whole), which Product's F-2 wording could also mean. Network level (`tenant_group_location_practitioner_networks.acceptingNewPatients`) excluded for MVP; item identity must allow adding it later. |
| B5 | Disability accommodations | Disability / ADA accommodations | `core_locations_ov`: `adaCompliance` (structured object) | Confirmed (by silence). Sub-attribute rendering is an engineering decision; not a single boolean. |

### Explicitly excluded from the form

| Item | Why |
|---|---|
| Office hours (`core_locations_ov.officeHours`, network `officeHours`) | D2-25. Product's F-2 wording mentions it; confirm D2-25 still stands. |
| Fax (`core_locations_ov.fax`, `providerFax`) | D2-25. |
| Anything on `tenant_group_location_practitioner_networks` (`acceptingNewPatients`, `appointmentPhone`, `languageSpoken[]`, `officeHours`) | Product: network participation not in scope for MVP. |
| `includePractitionerLocationInNetworkDirectory` | Product: not attested; defines the attestation universe instead (population rule, doc 1). |
| Mailing / billing address types | Product: practice address only. |
| `core_practitioners_ov.addresses[]` | Superseded by practice-location addresses. |
| `core_practitioners_ov.specialties[]`, `specialty`, `cmsSpecialties`; `core_locations_ov.locationPrimarySpecialty`, `locationHsdSpecialty` | Product: tenant practitioner specialty is the attested one. |
| `core_locations_ov.languagesSpokenAtLocation` | Product: languages at practitioner level. |
| SSN, DOB, sanctions, disclosures, malpractice, NPDB, credentialing dates, documents | v2 §6.7.1 "what the GET must not return". |

### Count

- 9 practitioner-level UI fields (A1–A9), read from `core_practitioners_ov` (7), `tenant_practitioner_specialty` (1), `tenant_group_practitioners` (1).
- 5 attested location-level UI fields per location (B1–B5) plus a header, read from `core_entity_addresses_ov` (1), `core_locations_ov` (3), `group_practitioner_locations` (1).
- Pending Product: B2 column set, B3 level, B4 table, office-hours exclusion, A6 display.

---

## 8. Final copy-paste list — portal field, source, linkage (verified 2026-09-08)

Linkage: how a practitioner reaches its locations (from `PractitionerLocationLookupRepository`, all hops tenant-scoped)

- core_practitioners_ov
  - practitioner Golden record
  - contributing_crosswalks[] contains the tenant's certify_practitioner_id
- tenant_practitioners
  - certify_practitioner_id IN core_practitioners_ov.contributing_crosswalks
- tenant_group_practitioners
  - tenant_practitioner_id = tenant_practitioners.id
  - tenant_group_id = tenant_groups.id (this is the group affiliation row)
- group_practitioner_locations
  - tenant_group_practitioner_id = tenant_group_practitioners.id
  - one row = this practitioner at this location
- tenant_group_locations
  - id = group_practitioner_locations.tenant_group_location_id
- group_locations
  - id = tenant_group_locations.group_location_id
  - gives location_id
- core_locations_ov
  - group_locations.location_id IN core_locations_ov.contributing_crosswalks
  - location Golden record
- location_entity_addresses
  - location_id = group_locations.location_id
  - gives entity_address_id; data JSON holds addressType
- core_entity_addresses_ov
  - location_entity_addresses.entity_address_id IN core_entity_addresses_ov.contributing_crosswalks
  - address Golden record
- Note
  - OV tables have no foreign key; the link table's crosswalk id must be inside the OV row's contributing_crosswalks array

Practitioner-level fields (one value set per practitioner per tenant)

- Name (prefix, first, middle, last, suffix)
  - read from core_practitioners_ov: prefix, firstName, middleName, lastName, suffix
  - confirmed
- NPI
  - read from core_practitioners_ov: npi
  - confirmed; display-only, never editable
- Practitioner phone number(s)
  - read from core_practitioners_ov: phoneNumbers[]
  - confirmed
  - half of Product's "telephone number"; other half is the location phone below
- Specialty(ies)
  - read from tenant_practitioner_specialty (tenant_practitioner_id = tenant_practitioners.id)
  - name via tenant_specialty_id → tenant_specialties
  - confirmed; not OV specialties[], not location specialties
  - release path writes a tenant link row, not the Golden record
- Languages spoken
  - read from core_practitioners_ov: languages[]
  - confirmed
- Cultural competency
  - read from core_practitioners_ov: culturalCompetency
  - same Product answer; confirm whether shown as its own field
- Telehealth offered
  - read from core_practitioners_ov: telemedicineAvailable
  - confirmed
- Telehealth URL
  - read from core_practitioners_ov: telemedicineURL
  - confirmed
- Group affiliation(s)
  - read from tenant_group_practitioners rows for the practitioner
  - display name via tenant_group_id → tenant_groups → group record
  - confirmed; tied to the open provider-admin scope question

Location-level fields (repeated once per group_practitioner_locations row; each block is one practice location)

- Location name (block header, not attested)
  - read from core_locations_ov: locationName
  - linkage: group_locations.location_id IN core_locations_ov.contributing_crosswalks
- Practice address (line 1, line 2, city, state, zip)
  - read from core_entity_addresses_ov: addressLine1, addressLine2, city, state, zip
  - linkage: location_entity_addresses.location_id = group_locations.location_id → entity_address_id IN core_entity_addresses_ov.contributing_crosswalks
  - filtered to the practice address type
  - level confirmed: practice only; mailing/billing excluded
  - open: AddressType enum has no "practice" value (billing, mailing, MRStorageAddress, office, service, w9Address); assuming "office"
- Practice location phone
  - read from core_locations_ov: phone
  - linkage: group_locations.location_id IN core_locations_ov.contributing_crosswalks
  - level confirmed: location phone, network-agnostic
  - open: also show appointmentPhone, afterHoursPhone, callCoveragePhone?
  - other half of Product's "telephone number"
- Practice website
  - read from core_locations_ov: website
  - linkage: same as above
  - open: Product may move website to practitioner level (no such field today); location level assumed for MVP
- Accepting new patients at this location
  - read from group_practitioner_locations: data.acceptingNewPatients (the row itself)
  - level confirmed per Product's #7 answer: doctor at this location
  - open: confirm it is not core_locations_ov.acceptsNewPatients (office as a whole); F-2 wording could mean either
  - network level excluded for MVP
- Disability / ADA accommodations
  - read from core_locations_ov: adaCompliance (structured object)
  - linkage: same as above
  - confirmed; sub-attribute rendering is an engineering decision

Excluded from the form

- Office hours and fax
  - D2-25; confirm Product's F-2 mention of office hours does not reopen it
- Everything on tenant_group_location_practitioner_networks
  - network-level acceptingNewPatients, appointmentPhone, languageSpoken[], officeHours
  - Product: network participation out of scope for MVP
- includePractitionerLocationInNetworkDirectory
  - not attested; defines who gets an attestation at all
- Mailing, billing and other non-practice address types
- core_practitioners_ov.addresses[]
  - superseded by practice-location addresses
- core_practitioners_ov.specialties[], specialty, cmsSpecialties; core_locations_ov.locationPrimarySpecialty, locationHsdSpecialty
  - Product: tenant practitioner specialty is the attested one
- core_locations_ov.languagesSpokenAtLocation
  - Product: languages at practitioner level

Still pending Product

- location phone columns
- website level
- accepting-new-patients table
- practice address type value
- office-hours exclusion
- cultural competency display
