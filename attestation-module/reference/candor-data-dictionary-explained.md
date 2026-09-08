# Candor data dictionary — how to read Candor's output file

**What this covers.** A plain-English reading guide to Candor's *Data Dictionary — Accuracy Scorecard (Certify-MVP), July 2026* (Jira CP-37409 attachment, uploaded by Madhunika Sivasankar 2026-08-20; local copy in `~/Downloads`). The dictionary is Candor's own definition of the file they return to a client. It is evidence source **S13** in `source-of-truth-v2.md`; the one real delivery we have seen, the MMO delta report (**S12**), is used below as the worked example and as the list of places where practice diverged from the dictionary (**CT-011**).

**Why it matters.** Our proposal (`candor-exchange-contract-proposal.md`) reshapes this file into one row per finding with an explicit recommendation verb. Understanding the dictionary is how we (a) know which of our fields Candor actually verifies, (b) reuse their reason and confidence vocabularies verbatim, and (c) recognise drift when a delivery does not match what was signed.

---

## 1. The mental model

Candor's file is **our directory handed back with grades**. The row shape is what we sent; Candor bolts extra columns onto each attribute. Every verifiable attribute is a group of up to five columns:

| Column suffix | Filled by | Meaning |
| --- | --- | --- |
| `<attr>` (bare name) | **Client** (us) | The value we sent. Candor echoes it back. |
| `<attr>_verification_status` | Candor | The grade: `VALID` / `INVALID` / `UNKNOWN` / `INCONCLUSIVE` |
| `<attr>_candor_value` | Candor | Candor's own value. Dictionary rule: **populated only when status is `INVALID`** |
| `<attr>_verification_reason` | Candor | Reason ID(s); comma-separated when several (`deceased,no_active_license`) |
| `<attr>_verification_confidence` | Candor | Tier: `VERY HIGH` / `HIGH` / `MEDIUM` / `INCONCLUSIVE` |
| `<attr>_verification_evidence` | Candor | JSON array of `{ "reason": <id>, "evidence": "date" \| "url", "value": <date or URL> }` |

Not every attribute gets all five:

| Attribute | Client column | status | candor_value | reason | confidence | evidence |
| --- | --- | --- | --- | --- | --- | --- |
| NPI | `npi` | ✓ | — (see §3) | ✓ | ✓ | ✓ |
| First / last name | `first_name`, `last_name` | ✓ | ✓ | — | — | — |
| Specialties | `specialties` (array) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Degrees | — | — | ✓ (`degrees_candor_value`) | — | — | — |
| Location | `address_line1/2`, `city`, `state`, `zip` | ✓ (`location_verification_status`) | ✓ (`*_candor_value` per address leg) | ✓ | ✓ | ✓ |
| Location relationship | — | — | ✓ (`practice` / `affiliation`) | — | — | — |
| Practice name | `practice_name` | ✓ | ✓ | — | — | — |
| Phone | `phone` | ✓ | ✓ | ✓ | ✓ | ✓ |
| Fax | `fax` | ✓ | ✓ (+ `fax_deactivated_status_candor_value`) | ✓ | ✓ | ✓ |
| Accepting new patients | `accepting_new_patients` (bool) | ✓ | ✓ | ✓ | ✓ | ✓ |
| PCP at location | `pcp_at_location` (bool) | ✓ | ✓ | — | — | — |
| Active state license | — | — | ✓ (`active_state_license_candor_value`, bool) | — | — | — |

Two flags sit outside the pattern: `candor_additional_location` (bool — "we found more addresses for this NPI, see the other sheet") and `location_relationship_candor_value` (`practice` vs `affiliation` — is this a place the provider sees patients, or only an organisational tie).

**Consequence for us:** the dictionary has **no client input** for group affiliation, website, languages, telehealth, or ADA accommodations — five of our mandated attestation fields. It does have fax, PCP, degrees and practice name, which we do not ask for. Which of our fields Candor can verify is agenda item on the technical call (proposal §8, "Attributes we verify but the dictionary does not list").

---

## 2. Row grain and the two physician sheets

**One row = one NPI × one address we sent.** A practitioner with three locations occupies three rows; practitioner-level columns (name, NPI status, specialties, degrees) repeat on each. This is why the scorecard counts "Number of NPI<>Address records provided by the Client".

| Sheet | What it holds | Client columns present? |
| --- | --- | --- |
| **Physicians Directory Accuracy** | Grades for every NPI×address row we sent | Yes — echoed |
| **Physicians Additional Locations** | Locations Candor found for our NPIs that we did **not** send | No — only `npi` plus `*_candor_value` columns, because there is nothing of ours to grade |

Reading order: main sheet row → if `candor_additional_location = TRUE`, look up the same NPI in the second sheet for the discovered addresses.

The remaining sheets are not about individual rows: *Summary Metric Definitions*, *Directory Accuracy — Tiered*, *Directory Accuracy — Standard*, *CMS compliance metrics* define the scorecard percentages Candor reports to the payer (accuracy score, CMS deficiency-weighted compliance score). Useful context, irrelevant to ingestion.

---

## 3. Reading the four statuses

| Status | Meaning | `_candor_value` | Actionable? |
| --- | --- | --- | --- |
| `VALID` | Our value is correct | Blank per dictionary (MMO filled it anyway with an echo) | No — a positive verification |
| `INVALID` | Our value is wrong | **Holds the correction** | **Yes** — this is the recommendation |
| `UNKNOWN` | No evidence either way | Blank | No |
| `INCONCLUSIVE` | Conflicting evidence | Blank | No |

**Cascading rule (dictionary note on `location_verification_status`):** if the location is `INVALID`, *every other location-scoped column on that row is blank* — phone, fax, ANP, PCP, practice name, relationship, and all address `_candor_value` legs. Rationale: the provider is not at that address, so grading the phone there is meaningless. **Always read the location status first**, then the rest of the row.

Address `_candor_value` legs have a second rule: they are populated **only when `candor_additional_location = TRUE`** (i.e. Candor is telling us about a different address), and are blank when the sent location is `INVALID`. A `VALID` location means Candor holds the same address we sent.

NPI is special: there is no `npi_candor_value`. An `INVALID` NPI means "remove this provider" and the reason says why (`deceased`, `deactivated_nppes`, `no_active_license`).

---

## 4. Reason IDs → Keep / Update / Add / Remove

The five enum sheets (**NPI**, **Specialty**, **Location**, **Phone**, **Accepting New Patients**) are the decoder ring. Each row: attribute, recommendation verb, reason ID, plain-English description, default confidence tier. Note the dictionary has **no explicit `recommendation` column** in the data — the verb is implied by status + reason; our proposal makes it explicit.

| Attribute | Verb | Reason ID | Plain English | Tier |
| --- | --- | --- | --- | --- |
| NPI | Keep | `active_provider` | Active per high-confidence source | High |
| NPI | Remove | `deceased` | Obituary found online | Very High |
| NPI | Remove | `deactivated_npi` | Deactivated in NPPES | Very High |
| NPI | Remove | `inactive` | Practice confirmed by phone: no longer active | Very High |
| NPI | Remove | `no_active_license` | No active medical license in any state | High |
| Specialty | Keep | `health_system_verified` | Verified on health-system directory / practice profile | High |
| Specialty | Update | `alternative_specialty` | More accurate specialty found on profile | High |
| Specialty | Add | `health_system_verified` | Additional specialty on profile | High |
| Location | Keep | `direct_outreach` | Phone call confirmed provider practices here | Very High |
| Location | Keep | `health_system_verified` | Address on health-system directory / profile | High |
| Location | Remove | `direct_outreach` | Phone call confirmed provider does **not** practice here | Very High |
| Location | Remove | `po_box_address` | PO box — not a practice site | Very High |
| Location | Remove | `no_active_license` | No active license in this location's state | High |
| Location | Remove | `no_evidence_health_system` | Provider found on a profile, but not at this address | High |
| Location | Update | `typo_address` | Minor typographical correction to same address | High |
| Location | Add | `direct_outreach` / `health_system_verified` | New practice address confirmed by call / profile | Very High / High |
| Phone | Keep | `direct_outreach` / `health_system_verified` | Number confirmed by call / profile | Very High / High |
| Phone | Remove | `deactivated` | Automated dialing: number dead or unavailable | High |
| Phone | Update | `direct_outreach` | Call gave the correct number | Very High |
| Phone | Update | `deactivated` | Dead number, replacement found | High |
| Phone | Update | `alternative_number` | Different number on profile | High |
| Phone | Add | `direct_outreach` / `health_system_verified` | Number for a location that had none | Very High / High |
| ANP | Keep | `direct_outreach` / `health_system_verified` | Status confirmed | Very High / High |
| ANP | Update | `direct_outreach` | Call gave corrected status | Very High |
| ANP | Update | `invalid_anp_status` | Different status on profile | High |
| ANP | Add | `direct_outreach` / `health_system_verified` | Status for a location that had none | Very High / High |

The field-level `values` lists in the main sheet name a few more reason IDs than the enum sheets: `alternative_phone`, `alternative_fax`, `alternative_anp_status`, `no_eveidence_health_system` (sic — typo in the dictionary), and `deactivated_nppes` vs the enum sheet's `deactivated_npi`. Freezing one canonical list is agenda item 11 in the proposal.

**Two evidence sources drive everything:**
- `direct_outreach` — Candor phoned the practice. Confidence *Very High*. Evidence item is a **date**.
- `health_system_verified` — found on a hospital / health-system directory or the practice's own website. Confidence *High*. Evidence item is a **URL**.

Everything else (`deactivated`, `po_box_address`, `no_active_license`, `deceased`, `deactivated_npi`) is a rule or registry check.

Confidence tiers are Candor's, not numeric. Our design stores them verbatim and never branches on them (D2-07 dropped confidence-based demotion; proposal §5.4).

---

## 5. Worked example — one real MMO row

MMO delta report, sheet *Physicians — Directory Accuracy*, Carmelita Reyes, NPI `1770579724` (Podiatrist, East Liverpool, OH):

| Column group | What the row says | Reading |
| --- | --- | --- |
| NPI | `npi_status_nppes = True`, `active_state_license_candor_value = True` | Provider active. Keep. |
| Name | both `VALID` | Nothing to do |
| Specialties | `VALID`; `specialties_candor_value = ["Foot & Ankle Surgery Podiatrist","Podiatrist"]` | Ours is correct; Candor knows a finer specialty. Informational. |
| Location | 15303 State Route 170, East Liverpool — `VALID`, confidence `high`, relationship `primary` | She practices here. Rest of the row is meaningful. |
| Phone | `(330) 385-2227` — `VALID` | Keep |
| Fax | we sent none — `INVALID`, reason `missing_value`, candor_value `(330) 385-4242` | **Add fax** |
| PCP | we sent none — `INVALID`, candor_value `False` | Not a PCP here |
| ANP | `True` — `VALID` | Keep |

Same NPI in *Physicians — Additional Locations*: two rows, 3622 Belmont Ave Youngstown and 1749 S Racoon Rd Austintown, relationship `associated`, confidence `high`, each with phone, fax, ANP `True`, PCP `False`. Two practice locations we did not list — **candidate location adds**.

Net for one practitioner: 1 graded row + 2 discovered rows ≈ 3–4 items a reviewer would see (fax add, PCP flag, two location adds). In our proposal's shape those become separate `candor-recs-v1` rows: `ADD practice_address` ×2, `ADD location_phone` ×2, plus whatever we decide to do with fax/PCP (currently not in our attribute vocabulary).

---

## 6. Where the MMO delivery breaks the dictionary (CT-011)

Read a real file with these in mind — column names cannot be trusted blindly:

| Dictionary says | MMO file has |
| --- | --- |
| `npi_verification_status` (`VALID`/`INVALID`/…) | `npi_status_nppes`, `npi_status_candor_value` (booleans), `npi_status_verification_reason` |
| `location_relationship_candor_value` ∈ {`practice`, `affiliation`} | `primary`, `associated` |
| `location_verification_confidence` | `location_verification_status_confidence` |
| Confidence tiers uppercase (`VERY HIGH`, `HIGH`…) | lowercase `high` |
| Reason IDs from enum sheets | `missing_value` (not in dictionary); most `_reason` columns absent |
| `*_verification_evidence` JSON arrays on every graded attribute | **No evidence columns at all** |
| `_candor_value` only when `INVALID` | Populated on `VALID` rows too (echo) |
| No `county`, degrees as string | `county` + `county_candor_value` columns; `degrees_candor_value` as JSON array |
| `pcp_at_location` | `pcp` |
| Physicians only (for this engagement) | Third sheet *Facilities — Directory Accuracy*, ~1,000 rows |
| Address `_candor_value` blank unless additional location | Populated on `VALID` rows as an echo |

This drift is the reason the proposal insists on one signed `schema_version` per direction, rejects unknown versions whole, and flips to one-row-per-finding with an explicit `recommendation` verb (proposal §7, §8).

---

## 7. Quick reference — reading a row in six steps

1. Check `npi_verification_status` (or its MMO equivalent). `INVALID` → provider-level remove; stop.
2. Check `location_verification_status`. `INVALID` → location remove; everything else on the row is blank by design; stop.
3. For each remaining attribute, read `_verification_status`. Only `INVALID` carries a recommendation; `_candor_value` is the proposed value.
4. Read `_verification_reason` + the enum sheet to get the verb (Keep / Update / Add / Remove) and the plain-English why.
5. Read `_verification_evidence` for the date (phone call) or URL (profile) a reviewer can check.
6. If `candor_additional_location = TRUE`, pull the same NPI from *Additional Locations* for net-new addresses.
