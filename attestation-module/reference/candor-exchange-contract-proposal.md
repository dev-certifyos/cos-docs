# Candor data exchange contract — CertifyOS proposal

**What this is.** The complete, internally consistent proposal for the CertifyOS ↔ Candor file exchange: the export file we place for Candor (the "template"), the recommendations file Candor returns, the naming and batch-identity rules that tie the two together, and the change-control rules that keep the contract from drifting. It merges the transport half (the SFTP-exchange design) and the content half (the ingestion design, Candor data-schema proposal) into the one document the technical call negotiates against. *(Both of those module drafts were retired 2026-09-08 and moved to `platform/directory-accuracy/source-material/` on 2026-09-09; their successor is the single `platform/directory-accuracy/directory-accuracy.md`.)* This closes open item O-1 ("prepare an internal proposal before the Candor technical call") once signed.

**Why we lead with a proposal.** The one real Candor delivery we have seen (the MMO delta report) diverged from Candor's own July 2026 data dictionary — different column names, missing evidence and reason columns, different relationship enums, and a ~1,000-row facilities sheet in a practitioner-only engagement. Signing one explicit version, columns and enums included, is how that drift stops being a per-delivery surprise and becomes a rejected batch with a clear error.

**Internal status.** Everything here is consistent with the module designs as of 2026-09-08. Two internal caveats, invisible to Candor: the no-manifest transport design (SFTP module rewrite of 2026-09-05) is pending final internal review, and the exact outbound column subset depends on Product's pending field-map answers (§4.4). Neither changes what we ask of Candor. **Transport facts updated 2026-09-08:** Candor's SFTP account is already live on the CertifyOS platform (Jira TS-111546, closed 2026-09-04), and the folder names below now use the platform's fixed `from/` and `to/` roots (earlier drafts said `outbound/`/`inbound/`, which do not exist on the platform). **Worked sample files for both directions live in `reference/samples/`** — bring them to the call. **Two decision menus added 2026-09-08** (§2.4 completeness signal, §5.6 file shape): each lists the options, our preference order, and our recommendation, so Candor chooses from a prepared set rather than an open question. ⚠ If Candor picks a manifest or sidecar (§2.4 options 2/3), the directory accuracy doc must carry a per-vendor `completenessSignal` setting (`NONE | RENAME | SHA256_SIDECAR | MANIFEST`) so the receiver triggers on the marker file instead of the data file; the retired SFTP draft's "no manifest" decision becomes "manifest not *required*". The ingestion half is unchanged under every option — row-grain idempotency stays the guarantee.

---

## 1. The exchange at a glance

1. **Monthly, per tenant.** At the start of each monthly cadence period, CertifyOS places one export file per tenant in Candor's SFTP folder. The file lists every practitioner whose attestation obligation is currently open, with the directory data we want verified.
2. **Candor verifies and returns one file per export.** Candor uploads a recommendations file to its `to/<tenant>/` folder — one row per attribute-level finding, echoing our export batch id in the filename.
3. **File placement is the signal, in both directions.** No trigger message, no API call. We detect Candor's upload within minutes; Candor detects our export by listing its folder. Whether Candor also marks a delivery as *complete* (manifest, checksum sidecar, or rename-on-finish) is a choice we put to Candor in §2.4 — we recommend the manifest.
4. **Every row Candor returns gets exactly one recorded outcome on our side** — staged for review, recorded as a no-op, excluded with a reason, or quarantined for a human. Nothing is silently discarded, and Candor's recommendations never write to our records directly; a payer reviewer decides each one.

What we need from Candor, in one line: **read the export from `from/<tenant>/`, return one CSV per export to `to/<tenant>/`, echo our batch id in the filename, and honor the signed column-and-enum schema.**

---

## 2. Transport — SFTP, folders, and delivery rules

### 2.1 Access — already provisioned

Candor's account on the CertifyOS SFTP platform exists (TS-111546, 2026-09-04; Candor's public key was registered — key-only login, no passwords):

| | Production | Staging |
| --- | --- | --- |
| Host | `sftp.prod.certifyos.com` | `sftp.staging.certifyos.com` |
| Port | `2222` | `2222` |
| Username | `candor-health` | `candor-health` |
| Auth | SSH public key (Ed25519 or rsa-sha2-256/512) | same |

Candor's session is rooted in its own storage area and sees exactly two root folders — the platform's fixed convention for every partner. Per-tenant subfolders keep the tenants Candor serves separated:

```
from/<tenant-id>/    ← CertifyOS writes; Candor READ-ONLY (list + download; no write, no delete)
to/<tenant-id>/      ← Candor writes (read/write/delete); CertifyOS reads
```

- `from/` is where our export appears; `to/` is where Candor's recommendations go. Candor cannot delete from `from/` — the platform denies write/delete there for every partner — so fetched exports simply age out on our side; Candor does not need to clean up.
- Candor may create the `to/<tenant-id>/` subfolder itself on first upload; `from/<tenant-id>/` is created by us with the first export.
- A file must land in the folder of the tenant named in its filename — a mismatch is rejected on our side, never ingested into the wrong tenant.
- Connectivity test: we have placed a test file under `from/` in production; Candor can confirm access by listing and downloading it.

### 2.2 Delivery rules (Candor's obligations)

- **Upload the data file, plus the completeness marker agreed under §2.4.** Our recommendation is a small manifest uploaded after the data file (§2.4 option 3). If Candor chooses option 1, the data file alone is the delivery — no ordering rule, no checksum required.
- **Never reuse a filename for different content.** A correction is a new upload under a new vendor batch id (§3.3).
- **On a failed or interrupted upload, re-upload the complete file.** Our side detects a quick retry and takes the newest version automatically; a partial file that does slip through is harmless — ingestion is idempotent per row, so the later complete file fills in exactly the missing rows (§6.3).
- Whatever option §2.4 lands on, **anything extra Candor sends is verified opportunistically, never load-bearing** — a `.sha256` sidecar alongside a manifest, or a rename-on-finish client behaviour on top of option 1, is welcome and changes nothing if absent.
- Temp-name-then-rename upload strategies (`.part`, `.tmp`, `.filepart` suffixes) are fine — we ignore the temp names.
- **Upload only into `to/<tenant-id>/`.** Writes anywhere else fail at the platform (`from/` is read-only) or are ignored by our receiver (root-level files).

### 2.3 Timing

- **Export cadence: monthly** (configurable per tenant later; monthly is the Candor starting point). The export is placed at the **start** of the cadence period so Candor works in parallel with the attestation window.
- **Response window:** Candor returns its recommendations file within an agreed number of days of the export (**to agree on the call** — our alerting flags a silent batch after this window).
- A practitioner still unresolved at the next cadence period appears in the next export again — an unanswered obligation stays on Candor's worklist. Duplicate findings across periods are handled on our side (§6.3); Candor never needs to track what it already sent.

### 2.4 Completeness signal — options, our preference, our recommendation

**The problem.** SFTP has no "upload finished" event. Our platform commits an uploaded file as soon as the client closes it — *or* flushes mid-transfer — so a file can appear in `to/` before Candor has finished writing it. Our pipeline is built to survive that regardless of option (settle window; every row idempotent; a later complete file fills in exactly the missing rows — §6.3). What the option changes is **how confidently and how quickly we can start**, and whether a partial file can ever be processed as a small batch before the full one lands. Candor should pick whichever they can operate reliably; the table below is what each choice buys.

| # | Option | What Candor does | Proves complete? | Proves intact? | Row count? | Batch id machine-readable? | Candor effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **Data file only** | Upload the CSV. Nothing else. | No — inferred (settle window + row-count heuristic) | No — per-hop only | No | Filename only | None |
| 2 | **Checksum sidecar** | Upload the CSV, then `<filename>.sha256` (`sha256sum` output format) | Yes — sidecar appears only after the data file | **Yes** — end-to-end SHA-256 | No | Filename only | Very low (one command) |
| 3 | **Manifest last** ★ | Upload the CSV, then `<filename>.manifest.json` (schema below) | **Yes** — we react to the manifest only | **Yes** — SHA-256 in manifest | **Yes** | **Yes** — batch ids, row count, produced-at in the manifest; filename becomes redundancy | Low (one small JSON per delivery, hash + row count) |
| 4 | **Rename on finish** | Upload as `<filename>.part`, rename to `<filename>` when done | Yes — final name appears whole | No | No | Filename only | Usually free (most SFTP clients do this) |

★ = our recommendation.

**Our preference order: 3 → 2 → 4 → 1.**

- **Option 3 (manifest last) — recommended.** It is the only option that turns "did we get the whole file, unchanged, and which request does it answer?" into three checks we run before parsing a single row: `sha256` matches, `rowCount` matches, `exportBatchRef` matches a batch we registered. A mismatch is a *rejected delivery with a precise error* instead of a half-ingested file and a phone call. It also removes the filename as the sole carrier of batch identity (§3.2 stays the rule; the manifest is the belt to its braces). Cost to Candor: one small JSON per delivery, written after the data file. Our safeguard against half-adoption: **a manifest whose hash or count does not match the data file is treated as "delivery not complete"** — we wait for a corrected manifest or a re-upload — so a stale or wrong manifest can never cause a bad ingest; it can only delay.
- **Option 2 (sidecar) — acceptable.** Same completeness and integrity proof as the manifest, none of the metadata. Fine if Candor's tooling cannot emit JSON but can run `sha256sum`.
- **Option 4 (rename) — acceptable as a floor.** Costs Candor nothing if their client already does it, and lets us shrink the settle window. No integrity or row-count proof. Welcome in combination with any other option.
- **Option 1 (data file only) — workable, least preferred.** This is what the design guarantees correctness under, so it is never a blocker. But a partial file that is *never* followed by a complete one is detected only by a row-count-vs-export heuristic, and a truncated-but-well-formed file would be processed as a small valid batch until the full one arrives. Choose this only if 2–4 are all impractical.

**Manifest schema (option 3), `certify-manifest-v1`:**

```
to/<tenant-id>/<tenantId>_<exportBatchRef>_<candorBatchId>_<yyyyMMdd>.csv
to/<tenant-id>/<tenantId>_<exportBatchRef>_<candorBatchId>_<yyyyMMdd>.csv.manifest.json   ← uploaded LAST
```

```json
{
  "manifestVersion": "certify-manifest-v1",
  "dataFile": "org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.csv",
  "schemaVersion": "candor-recs-v1",
  "tenantId": "org-xyz",
  "exportBatchRef": "org-xyz-candor-2026-09-001",
  "candorBatchId": "CB-2026-09-001",
  "rowCount": 26,
  "sha256": "9f2c4e…",
  "producedAt": "2026-09-15T09:30:00Z",
  "contact": "ops@candorhealth.example"
}
```

- `rowCount` excludes the header row. `sha256` is over the exact bytes uploaded. `producedAt` ISO 8601 UTC.
- **Ordering rule:** data file first, manifest last. We react to the manifest; a data file with no manifest within the response window is flagged to Candor, never processed.
- **Correction:** a re-send is a new data file + new manifest under a new `candorBatchId` (§2.2). Re-uploading the same pair identically is harmless (§6.3).
- `dataFile`, `tenantId`, `exportBatchRef`, `candorBatchId` must agree with the filename legs; disagreement = rejected with the mismatch named.

**Sidecar format (option 2):** `<filename>.sha256` containing the standard `sha256sum` line: `<64 hex chars>  <filename>`. Uploaded after the data file.

**What we guarantee under every option** (so Candor can choose on operational convenience alone): detection within minutes; immutable archive before processing; every row processed exactly once even if the same content arrives twice or in a partial-then-complete pair; every rejection reported with the reason (§6). The options differ only in how much of that we can prove *before* parsing rather than reconcile *after*.

---

## 3. Batch identity — how a delivery ties to the request that caused it

### 3.1 The export batch id

- Every export carries a CertifyOS batch id: `<tenant>-<vendor>-<yyyy-MM>-<seq>` — e.g. `org-xyz-candor-2026-09-001`. Deterministic per cadence period; a re-run of the same period reuses the same id, never mints a second batch.

### 3.2 The echo rule (the one non-negotiable ask)

- Candor's inbound filename must carry our export batch id back, verbatim, as its `exportBatchRef` leg (§3.3). This is how each returned row binds to the specific obligation cycle that requested it — a September delivery that arrives in November still attaches to September's obligation, never to a newer cycle that opened meanwhile.
- No per-row echo is required — the filename-level echo is sufficient, and our export registry does the row-level attribution deterministically on our side.
- **If Candor cannot echo the id:** fallback attribution by "latest open export for this tenant" is workable but ambiguous when periods overlap. We treat that as a degraded mode to price on the call, not a design to build for.

### 3.3 File naming

**Outbound (CertifyOS produces):**

```
<tenantId>_<exportBatchId>_<yyyyMMdd>.csv
org-xyz_org-xyz-candor-2026-09-001_20260901.csv
```

**Inbound (Candor produces):**

```
<tenantId>_<exportBatchRef>_<candorBatchId>_<yyyyMMdd>.csv
org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260903.csv
```

- `exportBatchRef` — the verbatim echo of our export batch id: the load-bearing leg.
- `candorBatchId` — Candor's own delivery id, any format; we record it, it is never load-bearing.
- `yyyyMMdd` — the file's production date.
- Underscore is the leg separator, so no leg may contain an underscore (our batch ids use hyphens internally for this reason).
- Exact spelling and separators are negotiable on the call; the **echo requirement is not**.
- A filename that does not parse is not lost — we archive it and route it to a human — but it cannot auto-attach to its export, so it delays Candor's own turnaround. Clean names are in both parties' interest.

### 3.4 Row identity — the end-to-end use cases and the echo we ask for

The filename ties a *delivery* to an *export*. Row identity ties each *finding* to the exact *practitioner and location we sent*. We designed this column-by-column from the things the pipeline and the reviewer actually have to do with a returned row, so nothing has to be bolted on later:

| # | Use case (what has to work) | What it needs from the row | Export column | Return column |
| --- | --- | --- | --- | --- |
| U1 | Attach a finding to the right practitioner **without re-resolving identity** | Our stable practitioner id echoed back | `certify_practitioner_id` | `certify_practitioner_id` (**required**) |
| U2 | Catch a mis-keyed or swapped row before it reaches a reviewer | Two independent identifiers that must agree | `certify_practitioner_id` + `npi` | both echoed; pair must match our export registry or the row is quarantined `IDENTITY_MISMATCH` |
| U3 | Attach a location-scoped finding to the right practice location **even when Candor normalises the address** (Sept sample: suite added, line-2 folded into line-1, zip 10940→10941 on rows marked `VALID`) | A stable location id, not string-matching on address legs | `certify_location_id` | `certify_location_id` (**required** on rows about a location we sent; empty on a discovered location) |
| U4 | Tell "you sent this, it is wrong" from "you never sent this" (Sept sample graded a blank `pcp_at_location` as `INVALID`) | Our value as we sent it, echoed | every attested column | `current_value_seen` (empty ⇒ we sent nothing ⇒ only `ADD` is valid) |
| U5 | Attribute a row even if the file was renamed, split, or concatenated on Candor's side | Batch identity **per row**, not only in the filename | `export_batch_id` on every row | `export_batch_ref` on every row (uniform; must equal the filename leg) |
| U6 | Detect staleness — our OV changed between export and return | When we read the value we sent | `export_generated_at` | (compared against OV `updated_at` at ingestion) |
| U7 | Reference one specific finding in a support thread, a rejection, or a future feedback file to Candor | Candor's own unique id per finding | — | `finding_id` (**required**, unique within a delivery) |
| U8 | Add facilities, groups, or another entity type later **without a new schema** | An entity discriminator from day one | `entity_type` (`PRACTITIONER` in v1) | `entity_type` |
| U9 | Act on "this provider is deceased / NPI deactivated / no active licence" — Candor's most valuable finding, and one our attribute list previously had no slot for | A practitioner-scoped status attribute | — | `attribute = practitioner_status`, `recommendation = REMOVE`, reason from Candor's NPI enum (§5.3) |
| U10 | Set the effective date of an accepted `ADD`/`REMOVE` (Product spec §2.3.3 open question: Candor "does not always supply one") | A slot for it, optional | — | `effective_from` (optional) |
| U11 | Render evidence correctly without guessing whether a string is a date or a link | Evidence type beside the value | — | `evidence_type` (`URL` / `DATE` / `NONE`) |
| U12 | Help Candor match our location to a health-system profile (their main verification source) | The location's name | `location_name` (matching aid, not attested) | — |
| U13 | Keep mailing/billing addresses out now and admit them later without ambiguity | Explicit address role | `address_type` (`PRACTICE` in v1) | — |
| U14 | Cross-reference Candor's records for the future feedback loop and cross-cycle dedup on their side | Candor's ids, if they have them | — | `candor_provider_ref`, `candor_location_ref` (optional) |
| U15 | Multi-location practitioner: know **which** location a `KEEP` refers to | Location identity on every location-scoped row | `certify_location_id` + address legs | same, echoed |

**Three ways to do row identity — and which we ask for:**

| Approach | How a returned row is attached | Pros | Cons | Position |
| --- | --- | --- | --- | --- |
| 1. NPI + address components (Candor's current shape) | Match `npi`, then match `address_line1/city/state/zip` strings | No new columns for Candor | Address strings drift under normalisation (proven in the Sept sample); a `KEEP` on a "different" address becomes an unmatched row; no defence against a swapped row | Fallback only |
| 2. **NPI + echoed CertifyOS ids** ★ | `certify_practitioner_id` + `certify_location_id` echoed verbatim; `npi` cross-checked against the pair; address legs kept as human-readable redundancy | Exact attachment, zero string matching; U2 mismatch guard; discovered locations are simply rows with an empty location id | Candor carries two opaque columns through their pipeline (they already carry `npi`) | **Recommended — the ask** |
| 3. Export row id | One opaque `export_row_id` per export row, echoed | Simplest possible join | One id conflates practitioner and location, so a practitioner-level finding needs an arbitrary row; no cross-check value | Not proposed |

Approach 2 is cheap for Candor — the ids are just columns to carry from our export to their return — and it is the only one where a returned row can be **proven** to belong to the practitioner and location we think it does. If Candor cannot echo the ids for MVP, we fall back to approach 1 with exact, normalised address matching and route every non-match to a human; that is priced as degraded, not designed for.

### 3.5 Columns we add now so we never reopen the schema

Each of these costs one column today and prevents a `v2` later. All are in the v1 tables below (§4.3, §5.2):

- **Both files:** `entity_type` (U8), `tenant_id` per row, `schema_version` per row.
- **Export:** `export_generated_at` (U6), `certify_location_id` (U3), `location_name` (U12), `address_type` (U13).
- **Return:** `export_batch_ref` per row (U5), `finding_id` (U7), `certify_practitioner_id` + `certify_location_id` (U1–U3), `evidence_type` (U11), `effective_from` (U10), `candor_provider_ref` / `candor_location_ref` (U14).
- **Vocabulary:** `practitioner_status` (U9), `location_relationship` (optional, Candor's `practice`/`affiliation` distinction).

The v1 shape carries every slot the end-to-end flow needs; adding a *verifiable field* later is a vocabulary value (§5.3), adding an *entity* later is an `entity_type` value, and neither touches a column.

---

## 4. The outbound file — what CertifyOS sends (the export template)

### 4.1 File conventions (both directions)

- CSV, UTF-8, RFC 4180 quoting, first row = header, header names exactly as signed (case-sensitive).
- Dates: **ISO 8601** (`2026-09-30`); timestamps ISO 8601 UTC (`2026-09-03T14:05:00Z`). No `MM/DD/YY` anywhere.
- Empty string = "no value"; the literal `NULL` is never used.
- One schema version string per file, uniform on every row; the version names the signed column-and-enum set (§7).

### 4.2 Row grain

**One row per practitioner per practice location.** Practitioner-level fields repeat on each of that practitioner's location rows; a practitioner with three practice locations occupies three rows. The location is identified by its address components (`address_line1`, `city`, `state`, `zip`) — the same four fields that identify a location row in the inbound file, so the identity vocabulary is one and the same in both directions.

### 4.3 Columns

Identity and context first (1–8), then the practitioner block (9–14), then the location block (15–23), then the remaining attested fields. Column numbers are referenced by §5.3.

| # | Column | Content | Notes |
| --- | --- | --- | --- |
| 1 | `schema_version` | `certify-export-v1` | Uniform per file |
| 2 | `export_batch_id` | e.g. `org-xyz-candor-2026-09-001` | Same value on every row; matches the filename |
| 3 | `export_generated_at` | ISO 8601 UTC timestamp the file was produced | Uniform per file; staleness reference (§3.4 U6) |
| 4 | `tenant_id` | The tenant this row belongs to | Matches the filename and folder |
| 5 | `entity_type` | `PRACTITIONER` | Always this value in v1; facilities/groups later are new values, not new columns |
| 6 | `certify_practitioner_id` | Our stable practitioner reference | **Echo required** on every returned row (§5.2 col 7) |
| 7 | `npi` | The practitioner's NPI | Matching identity for Candor; cross-checked against col 6 on return |
| 8 | `attestation_due_date` | The obligation's due date, ISO | Context: when this practitioner's attestation window closes |
| 9–13 | `name_prefix`, `first_name`, `middle_name`, `last_name`, `name_suffix` | Provider name | |
| 14 | `group_affiliation` | Group name(s) the practitioner is affiliated with under this tenant | Multi-valued: `;`-separated |
| 15 | `certify_location_id` | Our stable id for this practice location | **Echo required** on location-scoped returned rows (§5.2 col 9) |
| 16 | `location_name` | The practice/location name as we hold it | Matching aid for Candor's health-system lookups; **not** an attested field, no findings expected |
| 17 | `address_type` | `PRACTICE` | Always this value in v1; mailing/billing excluded by design |
| 18–21 | `address_line1`, `address_line2`, `city`, `state` | The practice-location address | Human-readable location identity beside col 15 |
| 22 | `zip` | 5-digit, string (leading zeros preserved) | |
| 23 | `location_phone` | The practice location's phone | |
| 24 | `practitioner_phone` | The practitioner's own phone number(s) | Multi-valued: `;`-separated |
| 25 | `website` | The practice website | |
| 26 | `specialty` | The practitioner's specialty(ies) under this tenant | Multi-valued: `;`-separated |
| 27 | `accepting_new_patients` | `Y` / `N` — this practitioner, at this location | |
| 28 | `languages` | Languages spoken by the practitioner | Multi-valued: `;`-separated |
| 29 | `telehealth_available` | `Y` / `N` — practitioner-level | |
| 30 | `telehealth_url` | Practitioner's telehealth URL | |
| 31 | `ada_accommodations` | Disability/ADA accommodations at this location | Multi-valued: `;`-separated; rendering of the structured value to agree on the call |

Sample (one practitioner with two locations = two rows; practitioner columns repeat):

```csv
schema_version,export_batch_id,export_generated_at,tenant_id,entity_type,certify_practitioner_id,npi,attestation_due_date,name_prefix,first_name,middle_name,last_name,name_suffix,group_affiliation,certify_location_id,location_name,address_type,address_line1,address_line2,city,state,zip,location_phone,practitioner_phone,website,specialty,accepting_new_patients,languages,telehealth_available,telehealth_url,ada_accommodations
certify-export-v1,org-xyz-candor-2026-09-001,2026-09-01T06:00:00Z,org-xyz,PRACTITIONER,cert-000123,1234567893,2026-09-30,Dr.,Jane,,Rivera,MD,Sunrise Medical Group,loc-000501,Sunrise Medical Group - Main St,PRACTICE,100 Main St,Suite 4,Columbus,OH,43215,614-555-0100,614-555-0142,https://sunrisemed.example,Cardiology,Y,English;Spanish,Y,https://sunrisemed.example/tele,Wheelchair accessible
certify-export-v1,org-xyz-candor-2026-09-001,2026-09-01T06:00:00Z,org-xyz,PRACTITIONER,cert-000123,1234567893,2026-09-30,Dr.,Jane,,Rivera,MD,Sunrise Medical Group,loc-000502,Sunrise Medical Group - Dublin,PRACTICE,4500 Lakeview Blvd,,Dublin,OH,43017,614-555-0177,614-555-0142,https://sunrisemed.example,Cardiology,N,English;Spanish,Y,https://sunrisemed.example/tele,
```

Full worked example (7 practitioners, 8 location rows): `reference/samples/from/org-xyz/org-xyz_org-xyz-candor-2026-09-001_20260901.csv`.

### 4.4 Internally pending (does not block the call)

Product is finalizing which physical field feeds a few columns — the **column list above is stable**; only our side's source mapping is in flight:

- Which of the location phone fields (main, appointment, after-hours, call-coverage) is "the" directory phone.
- Whether website stays location-level or moves to practitioner level.
- Which address-type value marks the practice address.
- Whether cultural competency renders as its own column or folds into `languages`.

If any answer adds or renames a column before signing, it lands in the signed v1; after signing, it is a v2 (§7).

---

## 5. The inbound file — what Candor returns

### 5.1 Row grain

**One row per attribute-level finding**: one practitioner + one attribute (+ one location, when the attribute is location-scoped) + one recommendation. A practitioner with a confirmed phone, a corrected address, and a new location contributes three rows. A discovered location is an `ADD practice_address` row (plus further `ADD` rows for its phone/ANP against the same new address) — never a separate sheet (§5.6).

### 5.2 Columns

Identity first (1–13), then the finding (14–17), then Candor's verification detail (18–26).

| # | Column | Content | Rules |
| --- | --- | --- | --- |
| 1 | `schema_version` | `candor-recs-v1` | Uniform per file; unknown version rejects the whole batch (§6.2) |
| 2 | `export_batch_ref` | Our export batch id, verbatim | Uniform per file; **must equal the filename leg** (§3.3) — row-level redundancy so a renamed file still attributes |
| 3 | `candor_batch_id` | Candor's own delivery id | Uniform per file; matches the filename leg; recorded, never load-bearing |
| 4 | `finding_id` | Candor's unique id for this row | **Required**; unique within the delivery; how either side refers to one finding later (support, rejection, feedback loop) |
| 5 | `tenant_id` | The tenant | Must match folder and filename |
| 6 | `entity_type` | `PRACTITIONER` | Rows with any other value are set aside in v1 |
| 7 | `certify_practitioner_id` | **Echo of export col 6** | **Required.** Must match the `(npi, export_batch_ref)` pair in our export registry, else the row is quarantined `IDENTITY_MISMATCH` and reported |
| 8 | `npi` | The practitioner the finding concerns | Required; cross-checked against col 7 |
| 9 | `certify_location_id` | **Echo of export col 15** | **Required on rows about a location we sent**; **empty** on practitioner-level rows and on `ADD practice_address` (a location we did not send) |
| 10–13 | `address_line1`, `city`, `state`, `zip` | The location's address legs | For a sent location: **as we sent them, verbatim** (redundancy beside col 9). For a discovered location: the new address. Empty on practitioner-level rows |
| 14 | `attribute` | Which field the recommendation concerns | From the signed attribute vocabulary (§5.3) |
| 15 | `recommendation` | `KEEP` / `UPDATE` / `ADD` / `REMOVE` | Explicit verb — never implied by status + reason (§5.5) |
| 16 | `current_value_seen` | The value Candor believes we published — i.e. what we sent | Our drift detector. **Empty means we sent nothing**, in which case only `ADD` is valid (never `INVALID`) |
| 17 | `recommended_value` | The proposed value | Required on `UPDATE` and `ADD`; empty on `KEEP`; on `REMOVE` it is **what remains** — the full list minus the entry for multi-valued attributes, empty for a single-valued attribute such as `practice_address` (the location is simply gone), `INACTIVE` for `practitioner_status`. Composite/list encodings per §5.3 |
| 18 | `verification_status` | `VALID` / `INVALID` / `UNKNOWN` / `INCONCLUSIVE` | Candor's dictionary enum, passed through to reviewers as-is. `VALID` means identical to what we sent after normalisation — a differing suite or zip is an `UPDATE`, not a `VALID` |
| 19 | `verification_reason` | Reason ID(s) from Candor's dictionary (`direct_outreach`, `health_system_verified`, `deceased`, …) | `;`-separated when several; **first is primary**. One canonical spelling per reason (agenda item 11) |
| 20 | `evidence_type` | `URL` / `DATE` / `NONE` | Tells us how to render col 21 without guessing. `health_system_verified` ⇒ `URL`; `direct_outreach` ⇒ `DATE` |
| 21 | `evidence` | **One plain string**: the URL or the ISO date | Empty when `evidence_type = NONE`; the row is then shown to reviewers as "unsupported" |
| 22 | `verified_at` | When Candor verified, ISO date | Default effective date of a change on our side |
| 23 | `effective_from` *(optional)* | Effective date for an `ADD`/`REMOVE` when Candor knows it (a location opened/closed on a date) | Overrides col 22 as the effective date when present |
| 24 | `confidence` *(optional)* | Candor's tier label (`VERY HIGH` / `HIGH` / `MEDIUM` / `INCONCLUSIVE`) | Stored verbatim for audit; **no CertifyOS logic reads it** (§5.4) |
| 25 | `candor_provider_ref` *(optional)* | Candor's own id for the provider | For the future feedback loop; never load-bearing |
| 26 | `candor_location_ref` *(optional)* | Candor's own id for the location | Same |

Sample (a corrected location phone, a confirmed specialty, and a provider found deceased):

```csv
schema_version,export_batch_ref,candor_batch_id,finding_id,tenant_id,entity_type,certify_practitioner_id,npi,certify_location_id,address_line1,city,state,zip,attribute,recommendation,current_value_seen,recommended_value,verification_status,verification_reason,evidence_type,evidence,verified_at,effective_from,confidence,candor_provider_ref,candor_location_ref
candor-recs-v1,org-xyz-candor-2026-09-001,CB-2026-09-001,CF-2026-09-001-0002,org-xyz,PRACTITIONER,cert-000123,1234567893,loc-000501,100 Main St,Columbus,OH,43215,location_phone,UPDATE,614-555-0100,614-555-0199,INVALID,direct_outreach,DATE,2026-09-08,2026-09-08,,VERY HIGH,cand-p-88120,cand-l-30411
candor-recs-v1,org-xyz-candor-2026-09-001,CB-2026-09-001,CF-2026-09-001-0005,org-xyz,PRACTITIONER,cert-000123,1234567893,,,,,,specialty,KEEP,Cardiology,,VALID,health_system_verified,URL,https://sunrisemed.example/providers/jane-rivera,2026-09-05,,HIGH,cand-p-88120,
candor-recs-v1,org-xyz-candor-2026-09-001,CB-2026-09-001,CF-2026-09-001-0031,org-xyz,PRACTITIONER,cert-000733,7891234560,,,,,,practitioner_status,REMOVE,ACTIVE,INACTIVE,INVALID,deceased,URL,https://obituaries.example/harold-bennett,2026-09-10,2026-07-14,VERY HIGH,cand-p-88127,
```

Full worked example (32 rows covering every verb, both scopes, a PO-box removal + new-location add, a typo-address update, a deceased provider, `UNKNOWN`/`INCONCLUSIVE`, and optional columns both filled and empty): `reference/samples/to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.csv`, answering the export in `reference/samples/from/org-xyz/`.

### 5.3 Attribute vocabulary

One `attribute` value per verifiable field of the export, signed as part of the schema. Adding a field later = adding a value here, no column change.

| `attribute` value | Scope | Export column(s) it answers | Value encoding in cols 16/17 |
| --- | --- | --- | --- |
| `practitioner_status` | practitioner | — (derived from `npi`; Candor's NPI enum) | `ACTIVE` / `INACTIVE`. `REMOVE` + reason `deceased` / `nppes_deactivated` / `no_active_license` / `inactive_direct_outreach` ⇒ after reviewer approval, routed to the terminations engine as a full disassociation. `KEEP` + `active_provider` is a positive verification. The NPI value itself is never changed |
| `provider_name` | practitioner | 9–13 | `prefix\|first\|middle\|last\|suffix`, `\|`-joined, empty legs kept |
| `group_affiliation` | practitioner | 14 | Full `;`-separated list after the change |
| `practice_address` | location | 18–22 | `address_line1\|address_line2\|city\|state\|zip`, `\|`-joined, empty legs kept — e.g. `4500 Lakeview Blvd\|\|Dublin\|OH\|43017` |
| `location_relationship` *(optional)* | location | — | `PRACTICE` / `AFFILIATION` — Candor's distinction; an `UPDATE` to `AFFILIATION` tells us the address is an organisational tie, not a place patients are seen |
| `location_phone` | location | 23 | Single value |
| `practitioner_phone` | practitioner | 24 | Full `;`-separated list |
| `website` | location | 25 | Single value |
| `specialty` | practitioner | 26 | Full `;`-separated list |
| `accepting_new_patients` | location | 27 | `Y` / `N` |
| `languages` | practitioner | 28 | Full `;`-separated list |
| `telehealth_available` | practitioner | 29 | `Y` / `N` |
| `telehealth_url` | practitioner | 30 | Single value |
| `ada_accommodations` | location | 31 | Full `;`-separated list |

Rules that make the vocabulary unambiguous:

- **Location-scoped rows about a location we sent** carry `certify_location_id` (col 9) **and** the address legs exactly as we sent them (cols 10–13). **A discovered location** (`ADD practice_address`) has an empty col 9, the new address in cols 10–13, and the full `\|`-joined address in `recommended_value`; its phone/ANP arrive as further `ADD` rows with the same empty col 9 and the same new address legs. A **closed location** (`REMOVE practice_address`) carries our location id and address legs; `current_value_seen` holds the full address.
- A practitioner who **moved** is `REMOVE practice_address` (old id) + `ADD practice_address` (no id) under the same `certify_practitioner_id` — two rows, adjacent, self-explanatory.
- **Multi-value attributes** (`group_affiliation`, `practitioner_phone`, `specialty`, `languages`, `ada_accommodations`): `current_value_seen` and `recommended_value` carry the **full `;`-separated list** after the change, not just the delta — `ADD` = list with the new entry, `REMOVE` = list without the entry, `UPDATE` = replaced list. One row per attribute per practitioner, never one row per list element.
- **Composite values use `|`** (name legs, address legs) and **lists use `;`** — the two separators never mix roles, so neither `|` nor `;` may appear inside a leg.
- **Attributes Candor verifies that we do not ask for in v1**: `practice_name`, `fax`, `pcp_at_location`, `degrees`. Rows with an `attribute` outside this table are recorded and set aside, never processed — they are not an error, they are simply not part of the engagement yet. Adding any of them later is one vocabulary value.
- NPI is never an attribute — it is the identity, display-only on every CertifyOS surface, and no recommendation *changing* it is accepted. `practitioner_status` is how "this NPI is dead/deactivated" is expressed.

### 5.4 What Candor should know about how we treat their data

- **Confidence is inert.** We store whatever `confidence` says, reviewers can see it, but no automated decision reads it. Candor should not expect high-confidence rows to auto-apply — every actionable recommendation is decided by a human payer reviewer.
- **`KEEP` rows are wanted.** A `KEEP` with `verification_status = VALID` is a positive verification our reviewers see alongside provider attestations — send them, don't filter them out.
- **Practitioners only.** Facility/organization rows are out of scope for this engagement; any that appear are counted and set aside, never processed (the MMO delta report contained a ~1,000-row facilities sheet — that must not recur in this feed).
- **Only practitioners from the referenced export.** A row about a practitioner who was not in the export named by the filename's `exportBatchRef` is recorded and set aside — we did not ask for it that cycle, and it will not reach a reviewer. If Candor believes proactive findings have value, that is a separate conversation, not a row in this feed.
- **Recommendations previously rejected by a reviewer are suppressed on re-send** — an identical recommendation (same practitioner, attribute, operation, value) that a reviewer already rejected is filtered with a full audit trail. Candor does not need to track this; it explains why a re-sent finding may not generate action.

### 5.5 Recommendation semantics

| `recommendation` | Meaning | What happens on our side |
| --- | --- | --- |
| `KEEP` | The current value is correct | Recorded as a verification; no change staged |
| `UPDATE` | The current value is wrong; `recommended_value` is the correction | Staged for reviewer decision |
| `ADD` | A value/entry is missing (e.g. a location we don't list) | Staged for reviewer decision |
| `REMOVE` | A value/entry should not be listed (e.g. a closed location) | Staged for reviewer decision |

### 5.6 File shape — options, our preference, our recommendation

Candor's September 2026 sample (`MVP_09022026_Sample.xlsx`) is a two-sheet workbook in a wide shape: one row per NPI × address, five columns per attribute (`<attr>`, `_verification_status`, `_candor_value`, `_verification_reason`, `_verification_confidence`, `_verification_evidence`), plus a second sheet *Physicians Additional Locations* for addresses Candor found that we did not send. Full analysis: `candor-sept-2026-sample-analysis.md`. Three shapes are on the table:

| # | Shape | Rows | Columns | New attribute costs | Discovered location appears as | Sheets / format |
| --- | --- | --- | --- | --- | --- | --- |
| A | **Candor's current** | One per NPI × submitted address, plus a second sheet for discovered addresses | ~57 + ~31 | +5 columns on each of two sheets, new dictionary revision | A row on another sheet, joined by NPI only | 2 sheets, XLSX |
| B | **Candor's wide, flattened** | One per NPI × address (submitted *or* discovered) | ~60 | +5 columns | A row with `record_source = candor_discovered`, client columns blank | 1 sheet, CSV |
| C | **One row per finding** ★ (§5.1–5.5) | One per NPI × attribute × (location) | 26, fixed | +1 value in the `attribute` vocabulary, **no column change** | An `ADD practice_address` row next to the `REMOVE` for the old one | 1 file, CSV |

★ = our recommendation.

**Our preference order: C → B → A (A not acceptable as signed).**

- **Option C (one row per finding) — recommended.** One schema that never changes when content changes: adding `website` or `languages` later is a vocabulary entry, not a dictionary revision. Every row is self-describing (`schema_version`, explicit `recommendation` verb, one value, one reason, one evidence). A provider who moved reads as `REMOVE` + `ADD` under the same NPI, side by side, which is exactly what a reviewer needs to see. For Candor it is a **pivot of data they already have**, not new work — every wide-row cell pair (`status`, `candor_value`) becomes one long row.
- **Option B (wide, single sheet) — acceptable fallback for MVP.** If Candor cannot pivot for the first cycle, we accept their wide shape with four conditions: (1) **one sheet, CSV**; (2) a `record_source` column (`client_submitted` | `candor_discovered`) so discovered locations live in the same file with client columns blank; (3) an explicit `recommendation` column (`KEEP`/`UPDATE`/`ADD`/`REMOVE`) rather than a verb implied by status + reason; (4) a `schema_version` column. Our Candor adapter pivots B into C internally; nothing downstream sees the wide shape. Cost we carry: every future attribute is a five-column change on both sides and a new signed version.
- **Option A (two-sheet XLSX) — not acceptable as the signed contract.** Two schemas per delivery, two parsers, a join by NPI before any row can be dispositioned, and a move split across sheets with no cross-reference. The September sample also has 7 of 12 NPIs on the second sheet that are absent from the first — under A we cannot even tell whether that is truncation or intended. Fine as an internal Candor artefact; not as the interface.

**The argument to make plainly:** Candor has shipped three mutually incompatible shapes in three months (July dictionary, June MMO delivery, September dictionary/sample). Every revision so far changed column names, enums, or sheet layout. Shape C is the only one where *no* future content change touches the file structure — which protects Candor's other clients as much as it protects us.

---

## 6. Processing contract — what CertifyOS guarantees per delivery

Candor doesn't need our internals, but these behaviors are contract-relevant:

### 6.1 Detection and acknowledgment

- We detect an upload within minutes (a short settle window absorbs interrupted-then-retried uploads by taking the newest version).
- Every accepted delivery is archived immutably (7-year retention) before processing — a delivery can always be re-examined or reprocessed from the original bytes.

### 6.2 Validation, and how failures surface

- **Unknown `schema_version` → the entire batch is rejected**, nothing partially processed, and Candor is notified with the version string we saw. This is deliberate: half-parsing an unsigned schema is how silent drift happens.
- Filename tenant ≠ folder tenant → rejected with a clear error.
- `(certify_practitioner_id, npi)` not a pair we exported under `export_batch_ref`, or `certify_location_id` not one of that practitioner's exported locations → the **row** is quarantined `IDENTITY_MISMATCH` and listed in the delivery report back to Candor; the rest of the file processes normally (§3.4 U2/U3).
- `exportBatchRef` that matches no export we made → the batch is parked and a human follows up with Candor.
- **Notification channel for rejected/parked batches: to agree on the call** (email to a Candor ops contact is our default assumption).

### 6.3 Idempotency — Candor can always safely re-send

- The same file re-sent identically → recognized and skipped, no duplicate processing.
- A corrected file for the same period → processed as a new batch; rows identical to already-processed ones are no-ops, new/changed rows process normally.
- A partial upload followed by the complete file → converges automatically; every row is processed exactly once.
- Net effect: **when in doubt, re-upload the complete file.** It is never harmful.

### 6.4 Every row is accounted for

- A batch is complete only when every row has exactly one recorded outcome and the counts reconcile against the file. If Candor asks "what happened to row N of our September file," we can answer, per row, indefinitely.

---

## 7. Change control — schema versioning

- The signed contract = this document's column lists, enum values, formats, and naming rules, labeled `certify-export-v1` / `candor-recs-v1`.
- **Any change — a column added, renamed, or removed; an enum value added — is a new version string**, agreed before the first file using it is sent, in both directions.
- We reject unknown inbound versions whole (§6.2); we commit to the same courtesy — we will not change the export shape under an existing version string.
- Version strings travel in-file (column 1) so every archived file is self-describing.

---

## 8. Divergences from Candor's July 2026 data dictionary — to resolve on the call

Where this proposal deliberately differs from the dictionary, and where the observed MMO delivery differed from both:

| Topic | Dictionary (July 2026) | Observed MMO delivery | This proposal | Why |
| --- | --- | --- | --- | --- |
| Evidence | JSON array of items (date or URL) | Evidence columns missing entirely | **One plain string** — a date or a URL | One reviewer-renderable value; arrays of evidence complicate parsing for no reviewer benefit |
| `recommended_value` | `candor_value` populated only when status is `INVALID` | — | Required whenever recommendation ≠ `KEEP` | An `UPDATE`/`ADD`/`REMOVE` without a value is unactionable regardless of status |
| Confidence | Tier enum | — | Accepted as optional, stored inert | We run no confidence-based logic; sending it is fine, relying on it is not |
| Column names | Dictionary names | Drifted from the dictionary | Signed names in §5.2, case-sensitive | The drift is the problem this contract exists to end |
| Relationship enums | Dictionary enums | Different enums in the file | Signed enums only; unknown enum value = invalid row (recorded, reported back) | Same |
| Facilities | — | ~1,000-row facilities sheet | Practitioner rows only | Practitioner-only engagement |
| Multi-sheet / XLSX | — | Spreadsheet with sheets | **Single CSV per delivery** | One file, one schema, one parse path |
| Batch tie-back | — | — | Export batch id echoed in the inbound filename | Deterministic attribution of every delivery to its request |
| Row grain | One wide row per NPI×address, one `*_verification_status` / `*_candor_value` column pair per attribute (~47 columns) | Same, columns drifted | **One row per attribute-level finding**, fixed 26 columns (identity echo + finding + verification detail) | Adding a verifiable attribute adds a vocabulary value, not a column pair; every finding has the same shape for the reviewer queue |
| Client inputs echoed | Dictionary's `Client`-provided columns (`npi`, names, `specialties`, `practice_name`, address, `phone`, `fax`, `accepting_new_patients`, `pcp_at_location`) repeated on every row | Same | Only `npi` + address identity + `current_value_seen` for the one attribute the row concerns | We already hold the values we sent; echoing everything back doubles file size for no reviewer benefit |
| Attributes we verify but the dictionary does not list | — (`fax`, `pcp_at_location`, `practice_name`, `degrees` are dictionary attributes) | — | We ask for `group_affiliation`, `website`, `languages`, `telehealth_available`, `telehealth_url`, `ada_accommodations`; we do **not** ask for fax, PCP, degrees | Mandated directory field set (CAA §116) drives our list — confirm on the call which of ours Candor can actually verify |

---

## 9. Call agenda — our position

**Non-negotiable (the design depends on these):**

1. The `exportBatchRef` echo in the inbound filename (§3.2) **and per row** (`export_batch_ref`, §5.2 col 2).
1a. **Echo of `certify_practitioner_id` on every row and `certify_location_id` on every row about a location we sent** (§3.4, approach 2). Two opaque columns carried from our export to their return; the only way a returned row is provably about the practitioner and location we sent.
2. One signed schema version per direction; unknown versions rejected whole (§7).
3. Practitioner-only rows; per-tenant folders (`from/<tenant>/`, `to/<tenant>/`); ISO 8601 dates.
4. CSV per §4.1 — single file, no multi-sheet spreadsheets.

**Negotiable (we propose, Candor may counter):**

5. Exact filename spelling and separators (§3.3).
6. Column names and order, within the signed-vocabulary rule.
7. Evidence as one string vs. their dictionary's array — we hold this position but will listen.
8. Multi-value separator (`;`) and the `Y`/`N` boolean rendering.
9. Whether `schema_version` rides as a column (proposed) or a filename leg.

**Decisions we ask Candor to make from a prepared menu (options, preference, recommendation in the doc):**

9a. **Completeness signal** (§2.4): manifest-last ★ / checksum sidecar / rename-on-finish / data file only. We recommend the manifest; any choice works.
9b. **File shape** (§5.6): one row per finding ★ / wide single-sheet CSV with `record_source` + `recommendation` + `schema_version` / two-sheet XLSX (not signable). We recommend one row per finding; we accept the flattened wide shape for MVP.

**To obtain from Candor:**

10. Response SLA — days from export placement to recommendations file (§2.3).
11. The full `verification_reason` enum list from the current dictionary, to freeze into v1.
12. Their `candorBatchId` format (any is fine; we record it).
13. An ops contact + channel for batch rejection/parking notifications (§6.2).
14. Confirmation they can consume per-tenant exports (one file per tenant, not one combined file), and that the per-tenant subfolder layout under `from/` and `to/` works for them.
15. ~~Key ceremony~~ — done (Candor's public key registered under `candor-health`, TS-111546). Still to agree: rotation expectations and who Candor notifies.
15a. Confirm Candor has tested login to `sftp.prod.certifyos.com:2222` and can list/download `from/`; confirm they understand `from/` is read-only.
16. `finding_id` — confirm they can emit a unique per-row id per delivery (§5.2 col 4), and whether they hold stable provider/location ids they can share (`candor_provider_ref` / `candor_location_ref`, optional).
16a. `effective_from` — for a location they confirm closed or newly opened, do they know the date? (Product spec §2.3.3 open question; the column is there either way.)
16b. `practitioner_status` findings (deceased / NPI deactivated / no active licence): confirm they arrive as `REMOVE` rows with their NPI-enum reasons, so the terminations path has a defined input.

**To state plainly so there are no surprises later:**

17. Recommendations never auto-apply — human reviewer decides every actionable row; confidence is not read by automation.
18. Re-sent previously-rejected recommendations are suppressed (with audit) rather than re-reviewed each cycle.
19. Practitioners still open at the next cadence appear in the next export again — no "already sent" tracking is expected of Candor.
20. Rows about practitioners outside the referenced export are set aside, not processed.
