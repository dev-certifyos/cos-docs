# Candor exchange — worked sample files

Companion to `../candor-exchange-contract-proposal.md`. Three files showing one complete monthly round trip for a fictional tenant `org-xyz` (Ohio practitioners, fake NPIs with valid check digits, `.example` domains, `555` phone numbers). Folder layout mirrors what Candor sees over SFTP (user `candor-health`, `sftp.prod.certifyos.com:2222`).

```
from/org-xyz/org-xyz_org-xyz-candor-2026-09-001_20260901.csv                                   ← CertifyOS places (Candor read-only)
to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.csv                       ← Candor uploads first
to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.csv.manifest.json         ← Candor uploads LAST (recommended option, proposal §2.4)
```

Filename legs: `<tenantId>_<exportBatchId>_<yyyyMMdd>.csv` out; `<tenantId>_<exportBatchRef>_<candorBatchId>_<yyyyMMdd>.csv` back. `exportBatchRef` = our export batch id echoed verbatim (`org-xyz-candor-2026-09-001`) — the one non-negotiable filename leg.

## The export (schema `certify-export-v1`, 31 columns) — proposal §4.3

One row per practitioner per practice location; practitioner-level fields repeat per location row. 7 practitioners, 8 rows. Every row carries the two ids we ask Candor to echo back: `certify_practitioner_id` and `certify_location_id` (proposal §3.4).

| NPI | Practitioner | `certify_practitioner_id` | Location ids | Why this case is in the sample |
| --- | --- | --- | --- | --- |
| 1234567893 | Jane Rivera, MD — Cardiology | cert-000123 | loc-000501 (Columbus), loc-000502 (Dublin) | Multi-location practitioner; we believe Dublin still active |
| 2345678918 | Chidi Okafor, DO — Family Medicine | cert-000287 | loc-000503 | Our data has a typo (`Okafer`); two group affiliations |
| 3456789122 | Priya Patel, MD — Neurology | cert-000341 | loc-000504 | Stale location phone; languages incomplete |
| 4567891237 | Linh Nguyen, NP — Pediatrics | cert-000402 | loc-000505 | We list a PO box as the practice address |
| 5678912341 | Marcus Schmidt, MD — Orthopedic Surgery | cert-000515 | loc-000506 | Everything correct except a suite-number typo |
| 6789123455 | Sofia Alvarez, MD — Psychiatry | cert-000618 | loc-000507 | Practices at a second location we do not list; ANP flag wrong |
| 7891234560 | Harold Bennett, MD — Internal Medicine | cert-000733 | loc-000508 | Deceased — the `practitioner_status` REMOVE case |

## What Candor returns (schema `candor-recs-v1`, 26 columns) — proposal §5.2

One row per attribute-level finding. 32 rows, `finding_id` CF-2026-09-001-0001 … 0032.

| Practitioner | Findings | What they show |
| --- | --- | --- |
| Rivera | 0001–0007 | `KEEP` address/ANP/website at loc-000501, `UPDATE` its phone; `REMOVE` loc-000502 (Dublin) with `effective_from`; practitioner-level `KEEP` specialty + telehealth. Optional `candor_provider_ref` / `candor_location_ref` **filled** here |
| Okafor | 0008–0012 | `UPDATE provider_name` (typo; value `prefix|first|middle|last|suffix`); `REMOVE` one group (value = list that remains); `KEEP` website, ADA; `KEEP practitioner_status` = `ACTIVE` with reason `active_provider`, `evidence_type = NONE` |
| Patel | 0013–0017 | `UPDATE` location phone (`deactivated`); `KEEP` practitioner phone with `UNKNOWN` status + `INCONCLUSIVE` confidence and no evidence; `ADD` a language (value = full list incl. new); `UPDATE` telehealth URL; `KEEP` ADA |
| Nguyen | 0018–0021 | **A move.** `REMOVE practice_address` for loc-000505 (`po_box_address`), then `ADD practice_address` with **empty `certify_location_id`** and the new address in cols 10–13 and `|`-joined in `recommended_value`; `ADD` its phone and ANP against the same new address |
| Schmidt | 0022–0026 | `UPDATE practice_address` with `typo_address` (Suite 210 → 201) — the case the Sept 2026 Candor sample hid under `VALID`; `KEEP` phone/specialty/ANP; `KEEP practitioner_status` |
| Alvarez | 0027–0030 | `UPDATE` ANP N→Y; `ADD` second location (with `effective_from`) + its phone; `UPDATE` specialty (`alternative_specialty`, `MEDIUM`) |
| Bennett | 0031–0032 | `REMOVE practitioner_status` (`ACTIVE` → `INACTIVE`, reason `deceased`, evidence URL, `effective_from` = date of death) — routes to the terminations engine after reviewer approval; then `REMOVE practice_address` for loc-000508 |

## The manifest (recommended completeness signal) — proposal §2.4

`…_20260915.csv.manifest.json` is the small JSON Candor uploads *after* the data file. We react to the manifest, not the CSV: `sha256` must match the uploaded bytes, `rowCount` (excluding header) must match, and `exportBatchRef` must be a batch we registered — otherwise the delivery is treated as "not complete yet". The values in the sample manifest are computed from the sample CSV, so the pair validates as-is. Under the other §2.4 options this file is absent and the CSV alone is the delivery.

## Conventions demonstrated

- CSV, UTF-8, RFC 4180, header row exact, `schema_version` on every row, ISO 8601 dates, empty string = no value.
- **Identity echo (proposal §3.4, approach 2):** `certify_practitioner_id` on every returned row; `certify_location_id` on every row about a location we sent, **empty** on practitioner-level rows and on discovered locations. `(certify_practitioner_id, npi)` must be a pair from the export — the generator asserts this.
- **Row-level batch identity:** `export_batch_ref` and `candor_batch_id` on every row, equal to the filename legs.
- **`finding_id`** unique per row per delivery — how either side refers to one finding later.
- **`entity_type = PRACTITIONER`** in both files; facilities later are a new value, not a new schema.
- Lists use `;` (`English;Spanish`). Composite values use `|` (`100 Main St|Suite 4|Columbus|OH|43215`; empty legs kept). Neither character appears inside a leg.
- `recommended_value`: required on `UPDATE`/`ADD`; empty on `KEEP`; on `REMOVE` = what remains (list minus entry, or empty for a single-valued attribute). `current_value_seen` echoes what we sent; empty on `ADD` (we sent nothing).
- `evidence_type` ∈ {`URL`, `DATE`, `NONE`} tells the reviewer how to render `evidence`; `health_system_verified` ⇒ URL, `direct_outreach` ⇒ DATE.
- `verification_reason` values are Candor's Sept 2026 dictionary reason IDs (`direct_outreach`, `health_system_verified`, `deactivated`, `po_box_address`, `typo_address`, `alternative_specialty`, `no_evidence_health_system`, `active_provider`, `deceased`).
- `confidence` uses Candor's tier vocabulary (`VERY HIGH` / `HIGH` / `MEDIUM` / `INCONCLUSIVE`) and is stored inert. `effective_from`, `candor_provider_ref`, `candor_location_ref` are optional and shown both filled and empty.
- Every NPI in the recommendations file appears in the referenced export; nothing about anyone else.

## Regenerating

All three files are produced by `generate_samples.py` in this folder (`python3 generate_samples.py`, no dependencies). Edit the script and re-run rather than hand-editing: it keeps quoting RFC 4180, recomputes the manifest hash and row count, and asserts the identity rules (every `(certify_practitioner_id, npi)` pair and every `certify_location_id` exists in the export; address legs echo ours verbatim; `finding_id` unique; `recommended_value` / `evidence_type` rules).
