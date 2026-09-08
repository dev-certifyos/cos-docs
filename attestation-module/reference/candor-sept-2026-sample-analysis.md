# Candor September 2026 dictionary + sample file — analysis and contract impact

**Inputs (received 2026-09-08, downloaded from Candor):**
- `Data Dictionary - Accuracy Scorecard (MVP) - Sept 2026.xlsx` — revision of the July 2026 dictionary (S13).
- `MVP_09022026_Sample (1).xlsx` — a sample of the file Candor intends to send us. 2 sheets: *Physicians Directory Accuracy* (31 rows, 57 columns, 11 NPIs) and *Physicians Additional Locations* (16 rows, 31 columns, 12 NPIs). New York practitioners; appears to be a real-data excerpt, not synthetic.

**Verdict in one paragraph.** The September files are a cleanup, not a redesign: same wide NPI×address grain, same five-column pattern per attribute, tightened types and enums, the MMO drift (S12/CT-011) mostly gone. Nothing in them invalidates our proposal (`candor-exchange-contract-proposal.md`); its transport, naming, batch-identity and change-control sections are untouched because Candor's sample has **no filename convention, no batch reference, no tenant, no schema version, and is XLSX with two sheets** — every one of those is still our ask. What changes: the §8 divergence table must be rebased on September, the reading guide (`candor-data-dictionary-explained.md`) needs a September delta, and the sample exposes **eight semantic problems** we should raise before signing — the biggest being what the *Additional Locations* tab actually means when a provider has no valid location left.

---

## 1. What changed July → September (dictionary)

### 1.1 Main sheet (*Physicians Directory Accuracy*): 60 → 57 fields

| Change | Detail | Effect on us |
| --- | --- | --- |
| **Removed** `candor_additional_location` | The boolean flag that said "look in the other sheet" is gone | Only way to know a provider has discovered locations is to join the second sheet by NPI. The *Summary Metric Definitions* sheet still references `candor_additional_location = true` — dictionary is internally inconsistent |
| **Removed** `active_state_license_candor_value` | License flag dropped | We never used it |
| **Removed** `fax_deactivated_status_candor_value` | Folded into `fax_verification_reason = deactivated` | Fine |
| **Types fixed** | `zip`, `phone`, `fax` integer → string (leading zeros preserved: sample has `06360`) | Good; matches our CSV assumption |
| **Confidence enum** | `INCONCLUSIVE` removed from every `_confidence` values list → `VERY HIGH / HIGH / MEDIUM` | **But the sample uses `INCONCLUSIVE` as a confidence on 18+ rows** (every attribute under an INVALID location). Dictionary and sample disagree — see §3.4 |
| **NPI reasons** | `deactivated_nppes` → `nppes_deactivated`; added `active_provider`, `inactive` to field values. Enum sheet says `inactive_direct_outreach`; field values say `inactive` | Still two spellings for the same reason inside one workbook — freeze one (agenda item 11) |
| **Location reasons** | Typo `no_eveidence_health_system` fixed; `health_system_verified`, `po_box_address` added to the field's value list | Now consistent with the Location enum sheet |
| **Phone reason** | `alternative_phone` → `alternative_number` | Matches enum sheet |
| **ANP reason** | `alternative_anp_status` → `invalid_anp_status` | Matches enum sheet |

### 1.2 *Physicians Additional Locations*: 33 → 31 fields

| Change | Detail |
| --- | --- |
| Column names aligned with main sheet | `address_line_1_candor_value` → `address_line1_candor_value`; `pcp_candor_value` → `pcp_at_location_candor_value` |
| **Removed** `county_candor_value`, `active_state_license_candor_value` | County gone (it was in the MMO file but never in the main-sheet dictionary) |
| Confidence enum normalised | `High/Medium/Low` → `VERY HIGH, HIGH, MEDIUM` |
| Reason lists narrowed | Only `direct_outreach`, `health_system_verified` for location/phone/ANP (makes sense: a discovered location can only be a Keep/Add) |

### 1.3 Enum sheets (NPI, Specialty, Location, Phone, ANP)

Unchanged except the two NPI reason IDs (`nppes_deactivated`, `inactive_direct_outreach`). Verb-to-reason mapping identical to July, so our §5.5 semantics and the reading guide's decoder table stay valid.

### 1.4 What did **not** change

- Row grain: one row per NPI × client-submitted address.
- Five-column pattern per attribute; `_candor_value` "only when INVALID" rule still stated in the notes.
- Client-provided input set: `npi`, `first_name`, `last_name`, `specialties`, `practice_name`, `address_line1/2`, `city`, `state`, `zip`, `phone`, `fax`, `accepting_new_patients`, `pcp_at_location`. **Still no input for group affiliation, website, languages, telehealth, ADA** — five of our eleven mandated attestation fields are not in Candor's verification scope. Unchanged open question for the call.
- Evidence as a JSON array of `{reason, evidence: date|url, value}`.
- No `recommendation` verb column in the data; verb is implied by status + reason.

---

## 2. Is the September sample consistent with the September dictionary?

Mostly. MMO-era drift is gone: column names match the dictionary exactly on both sheets; `_evidence` and `_reason` columns are present; confidence is uppercase; relationship uses the dictionary's `practice` value; NPI columns use the dictionary names. Remaining mismatches, all found in the 31+16 sample rows:

| # | Dictionary says | Sample does | Rows |
| --- | --- | --- | --- |
| C1 | `_candor_value` populated only when status is `INVALID` | Populated on `VALID` rows as an echo: `first_name_candor_value` on all 31; `accepting_new_patients_candor_value` on all 13 VALID; phone 6, fax 7, practice_name 3 | 31 |
| C2 | Confidence ∈ {VERY HIGH, HIGH, MEDIUM} | `INCONCLUSIVE` used as the confidence for phone/fax/ANP on every INVALID-location row | 18 |
| C3 | "If location is INVALID, all remaining `_candor_value` / `_verification_status` / relationship fields are blank" | Statuses **are** blank, but `_verification_confidence = INCONCLUSIVE` and `_evidence = []` are still filled — half-applied cascade | 18 |
| C4 | `health_system_verified` evidence is a URL | 53 evidence items are `{"reason":"health_system_verified","evidence":"date",...}` vs 28 with a URL. A date proves nothing about a web verification | 53 items |
| C5 | Evidence array supports the reason | 194 of ~250 evidence cells are `[]` (empty). Evidence is the exception, not the rule | 194 |
| C6 | `pcp_at_location` is a client input; `_verification_status` grades it | Client column is **empty on every row**, yet Candor grades 13 rows `INVALID` with `candor_value = false`. Grading a value we never sent is not a verification; it is an unsolicited add, and `INVALID` is the wrong status for "you sent nothing" | 13 |
| C7 | `location_verification_status = VALID` means Candor holds the same address | 5 VALID rows where Candor's address legs differ from ours (suite added `Fl 2`/`Ste 204`, line-2 folded into line-1, **zip 10940 → 10941**). These are `typo_address`-style updates hiding under `VALID` | 5 |
| C8 | `practice_name_verification_status` | 10 of 13 VALID-location rows have `practice_name = INVALID`, with no reason column to explain, and the "correction" is often a different *level* of organisation (department vs hospital: "Physical Medicine And Rehabilitation Medical Service" → "Upstate University Hospital") | 10 |

C1–C3 are formatting; C4–C8 are semantic and will generate reviewer noise or false recommendations if ingested as-is.

---

## 3. The *Physicians Additional Locations* tab — what it is, and whether to keep it

### 3.1 What it is

Rows for **addresses Candor found for one of our NPIs that we did not send**. Because there is no client value to grade, the sheet has no bare client columns and no `_verification_status` columns — only `npi` plus `_candor_value` / `_reason` / `_confidence` / `_evidence`. Every row is implicitly an **Add** (Location enum sheet: Add + `direct_outreach` or `health_system_verified`).

Difference from the first tab, column by column: same practitioner-level Candor columns (`first/last_name_candor_value`, `specialties_*`, `degrees_candor_value`), same location/phone/fax/ANP/PCP Candor columns, **minus** every `Client`-provided column and every `_verification_status` column. It is the first tab's "Candor half" with the "client half" removed.

### 3.2 What the sample reveals about its semantics

| Observation | Count | Meaning |
| --- | --- | --- |
| NPIs in the main sheet with **zero** VALID locations | 4 of 11 | Candor says "not at any address you listed" |
| …of which appear in *Additional Locations* | 4 of 4 | …"but here is where they actually are". Functionally a **move**, split across two sheets with no link between the removed row and the added row |
| Additional-location rows whose address duplicates a main-sheet address | 0 | Good: the tab is genuinely net-new, not corrections |
| NPIs in *Additional Locations* that are **not in the main sheet at all** | 7 of 12 | Either the sample was cut independently per sheet, or Candor emits discovered locations for NPIs it dropped from the main sheet. **Must ask.** If the latter, a delivery can recommend adding locations for a provider without ever telling us whether the locations we sent were valid |
| `accepting_new_patients_candor_value` empty on discovered locations | 4 of 16 | A discovered location can arrive with unknown ANP; our ingestion must not read empty as `N` |

### 3.3 Do we want it?

**Not as a second tab.** Two reasons independent of format taste:

1. **It breaks the one-parse-path rule.** Our ingestion (doc 4) validates one signed schema per delivery. A workbook with two sheets of different shapes is two schemas, two parsers, two sets of column checks, and a join step (by NPI) that has to run before any row can be dispositioned — plus the 7-orphan-NPI problem above.
2. **It hides the move.** The reviewer's most important question on a location removal is "where did the provider go?" With two tabs the answer lives in a different sheet, keyed only by NPI, with no cross-reference. In our one-row-per-finding format the `REMOVE practice_address` and `ADD practice_address` rows sit side by side under the same NPI.

**Your instinct — one tab, more columns — is right, with a refinement.** There are two ways to flatten:

| Option | Shape | Pros | Cons |
| --- | --- | --- | --- |
| **A. Keep Candor's wide format, one sheet** | Add a `record_source` column (`client_submitted` / `candor_discovered`). Discovered rows have client columns blank and `location_verification_status` blank or a new value `NOT_SUBMITTED`. | Smallest change for Candor; one parser | Still ~57 columns; still 5 new columns per future attribute; status semantics for discovered rows are awkward; `_candor_value`-only-when-INVALID rule collides with discovered rows where everything is a candor_value |
| **B. Our long format (proposal §5)** | One row per finding; discovered location = `ADD practice_address` row with the new address in cols 3–6 and `|`-joined in `recommended_value`; its phone/ANP are further `ADD` rows against the same address | One schema forever; new attribute = new vocabulary value, zero column change; moves read naturally; every row self-describing | Candor must reshape their output (a pivot, not new data) |

**Recommendation: hold B as the primary ask** (it is already what the proposal says, and the September sample confirms the wide format keeps drifting). **Offer A as the fallback** if Candor cannot pivot for MVP — but only with: single sheet, CSV not XLSX, `record_source` column, explicit `recommendation` verb column, `schema_version` column, and our batch reference in the filename. Under A our ingestion adapter does the pivot to long rows internally; the vendor-specific parsing stays inside the one adapter (D2-11 vendor-agnostic principle), and the rest of the pipeline never sees the wide shape.

Either way, **the "extensibility" argument to make on the call is concrete:** in the wide format, adding one verifiable attribute (say `website`) adds five columns to two sheets and a new dictionary revision; in the long format it adds one value to the `attribute` vocabulary and nothing else. Candor themselves have now shipped three incompatible revisions in three months (July dictionary → MMO delivery → September dictionary); a shape that does not change when content changes is in their interest too.

---

## 4. Impact on what we have written

### 4.1 `candor-exchange-contract-proposal.md` — change

| Section | Change needed |
| --- | --- |
| §5.2 col 15 `confidence` | Note Candor's September dictionary drops `INCONCLUSIVE` from confidence but their sample still emits it; accept the union {`VERY HIGH`,`HIGH`,`MEDIUM`,`INCONCLUSIVE`} as inert strings |
| §5.2 col 12 `verification_reason` | Freeze the September reason IDs: `nppes_deactivated` (not `deactivated_nppes`/`deactivated_npi`), `inactive_direct_outreach` vs `inactive` — ask which |
| §5.3 attribute vocabulary | Add explicit statement: `practice_name`, `fax`, `pcp_at_location`, `degrees` are **not** in our vocabulary for v1 (Candor verifies them; we do not ask). Decide whether `fax` should be — it is not a CAA-mandated field, but Candor clearly has it |
| §5.4 | Add: "A discovered location arrives as `ADD practice_address`, not in a separate sheet"; add: "Do not grade fields we did not send — a value for an attribute whose export column was empty is an `ADD`, never an `INVALID`" (C6) |
| §5.4 | Add: "`VALID` means byte-identical after normalisation. A differing suite, line-2, or zip is `UPDATE practice_address` with reason `typo_address`, not `VALID`" (C7) |
| §5.4 | Add: `current_value_seen` on `KEEP` rows may echo our value (C1 is fine in long format; that is exactly what the column is for) |
| §5.4 | Add: evidence type must match reason — `health_system_verified` ⇒ URL, `direct_outreach` ⇒ date (C4); empty evidence allowed but the row is then displayed to reviewers as "unsupported" (C5) |
| §8 divergence table | **Rebase every row on the September dictionary**; drop rows that no longer diverge (column names, relationship enums, county, types); add rows for C1–C8 and the two-sheet structure |
| §9 agenda | Add: what the 7 orphan NPIs in *Additional Locations* mean; whether `pcp_at_location` grading of a blank input is intended; confirm `INCONCLUSIVE` as confidence; confirm reason ID spellings |
| Header | Update "The one real Candor delivery we have seen" → two artefacts now (MMO delta June, MVP sample Sept) |

### 4.2 `candor-data-dictionary-explained.md` — change

- §1 attribute table: remove `active_state_license`, `fax_deactivated_status`, `candor_additional_location`; note types.
- §3: replace "flag `candor_additional_location = TRUE` says look in second sheet" with "join by NPI; the flag was removed in September; 7 orphan NPIs observed".
- §4 reason table: `deactivated_npi` → `nppes_deactivated`; `inactive` → `inactive_direct_outreach`.
- §6 drift table: retitle "MMO delivery (June) vs September dictionary vs September sample"; most MMO rows now resolved; add C1–C8.
- New §: September sample walkthrough (Kanter, NPI 1003001371: sent 2 locations, 1 INVALID `no_evidence_health_system`, 1 VALID with `Fl 2` added and practice name "INVALID"; 1 discovered location on the other sheet).

### 4.3 `reference/samples/` — no change to shape; small content tweaks

Our sample recommendations file already models everything the September sample does, in long form. Two tweaks: use `nppes_deactivated` if we ever add an NPI-level example, and add one `UPDATE practice_address` / `typo_address` row (suite added) to mirror C7.

### 4.4 Unchanged

- Transport (`from/`/`to/`, `candor-health` account), batch id, filename echo, schema versioning, idempotency, processing guarantees (proposal §2, §3, §6, §7). Candor's sample has none of these; they remain our asks.
- Export template (§4). Candor's client-input set confirms we send fields, not NPIs.
- `ingestion.md` adapter design: the vendor-specific pivot lives in the Candor adapter either way.

---

## 5. Questions for Candor (ordered by how much they change our build)

1. **Shape.** Will you deliver one row per attribute-level finding (our §5), or must MVP use your wide format? If wide: single sheet + `record_source` + `recommendation` + `schema_version` columns, CSV — acceptable?
2. **Orphan NPIs.** 7 of 12 NPIs on *Additional Locations* are absent from the main sheet. Sample truncation, or do you emit discovered locations for NPIs you did not grade?
3. **Moves.** When every location we sent is INVALID and you found a new one, is that one finding (move) or two? We will model it as `REMOVE` + `ADD`; confirm you can populate both.
4. **Ungraded inputs.** `pcp_at_location` was blank in the client input yet graded `INVALID`. Rule we propose: blank input ⇒ `ADD` (or `UNKNOWN`), never `INVALID`.
5. **VALID with a different address.** Five rows are `VALID` but your address legs differ (suite, zip). Should these be `typo_address` updates? Our rule: `VALID` ⇒ identical after normalisation.
6. **Confidence enum.** Dictionary says {VERY HIGH, HIGH, MEDIUM}; sample uses `INCONCLUSIVE` on 18 rows. Which is right?
7. **Reason ID spellings.** `nppes_deactivated` vs `deactivated_nppes`; `inactive` vs `inactive_direct_outreach`. One list, please.
8. **Evidence.** `health_system_verified` with a *date* (53 items) — should be a URL. Can every non-empty evidence carry the URL you verified against? Empty evidence on 80% of cells — expected at scale?
9. **Practice name.** 10 of 13 valid locations flagged `practice_name INVALID` with organisation-level, not typo-level, differences and no reason. We do not ingest practice name in v1; confirm we may ignore the column.
10. **Fields you cannot verify.** Group affiliation, website, languages, telehealth, ADA are in our export and not in your dictionary. Which can you return findings for, and when?
11. **Filename, tenant, batch reference.** `MVP_09022026_Sample.xlsx` carries none. Confirm the §3.3 naming (`<tenantId>_<exportBatchRef>_<candorBatchId>_<yyyyMMdd>.csv`).
12. **Cadence and window.** Unchanged ask (§2.3).

---

## 6. Bottom line

- September is **better** than July/MMO: types fixed, names aligned, enums mostly consistent. Good sign for negotiation.
- It does **not** move us off the long format; it strengthens the case (third shape revision in three months; a second sheet that splits a move in two; 5 columns per attribute).
- Eight semantic issues (C1–C8) plus the orphan-NPI question need answers before any schema is signed — most would produce wrong recommendations in a reviewer queue if ingested literally.
- Concrete doc work queued: rebase proposal §8 and §5.4, update the reading guide, add one `typo_address` row to the samples. No change to transport, batch identity, or the export template.
