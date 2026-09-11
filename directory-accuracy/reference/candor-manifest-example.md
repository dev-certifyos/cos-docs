# Candor delivery manifest — example and field guide

**Audience:** Candor Health engineering. **Status:** proposal, schema `certify-manifest-v1`, open for comment until signed.

Internal companions: `candor-exchange-contract-proposal.md` §2.4 (option 3, "manifest last") and the Confluence design *Design: Vendor ingestion service* (D11, D12, open question Q1 `sheetRowCounts`). This page is the one artifact we hand to Candor; those two stay internal.

---

## 1. The problem the manifest solves

SFTP gives the receiver exactly one signal: a file appeared in a folder. It does not say whether the upload finished, whether the bytes arrived intact, which request the file answers, or how to read it. Our storage layer commits an upload as an object as soon as the client closes it, and can commit a partial object if the client flushes mid-transfer. So a file that is *present* is not a file that is *complete*.

The manifest turns those unknowns into declarations we can verify before reading a single row. It is a small JSON file Candor writes **after** the data file has finished uploading, in the same folder, named `<data file name>.manifest.json`. CertifyOS reacts to the manifest only. The data file on its own is never parsed.

```
to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.xlsx                ← 1st: data file
to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.xlsx.manifest.json  ← 2nd: manifest (LAST)
```

## 2. Example — workbook delivery (current Candor format, two sheets)

```json
{
  "manifestVersion": "certify-manifest-v1",
  "dataFile": "org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.xlsx",
  "schemaVersion": "candor-directory-v1",
  "tenantId": "org-xyz",
  "exportBatchRef": "org-xyz-candor-2026-09-001",
  "candorBatchId": "CB-2026-09-001",
  "rowCount": 47,
  "sheetRowCounts": {
    "Physicians Directory Accuracy": 31,
    "Physicians Additional Locations": 16
  },
  "sha256": "4c1f0e9b7d2a6e8f3b5c9d1a0e2f4b6c8d0a2e4f6b8c0d2e4f6a8b0c2d4e6f80"
}
```

The `sha256` above is illustrative. In a real delivery it is the hash of the exact bytes uploaded.

## 3. Example — single-file CSV delivery (the long, one-row-per-finding format)

Same shape. `schemaVersion` names the CSV schema and `sheetRowCounts` is omitted because a CSV has one sheet. This exact pair exists as a worked, hash-verified sample in `samples/to/org-xyz/`.

```json
{
  "manifestVersion": "certify-manifest-v1",
  "dataFile": "org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.csv",
  "schemaVersion": "candor-recs-v1",
  "tenantId": "org-xyz",
  "exportBatchRef": "org-xyz-candor-2026-09-001",
  "candorBatchId": "CB-2026-09-001",
  "rowCount": 32,
  "sha256": "3a37e7f32df92351882092dd5e63ab320f7dc2cecb5c9458f02911091bcaf2cb"
}
```

## 4. Why each field is there

Every field answers one question our receiver must settle before, or immediately after, parsing. A field that did not answer such a question was cut.

| Question the receiver must answer | Field | What goes wrong without it |
| --- | --- | --- |
| Is the upload finished? | *(the manifest's existence)* | A partial file is parsed as a small valid batch. The complete file arrives later and looks like a duplicate. No field needed: the manifest is written last, so its presence is the answer. |
| Which object does this manifest describe? | `dataFile` | Two files in one folder, one manifest. Or a renamed data file. The receiver must not guess which file the hash and counts belong to. |
| Did every byte arrive, unchanged? | `sha256` | A truncated or corrupted upload that is still well-formed enough to parse is ingested silently. With the hash, a mismatch parks the delivery as "not complete yet" and nothing is read. |
| Did we read everything Candor meant to send? | `rowCount`, `sheetRowCounts` | Our parser skips rows (blank-row heuristic, hidden sheet, encoding). Nobody notices. With the counts, parsed must equal declared or the batch is held and both sides are told. Per-sheet counts localise the gap to one sheet. |
| Which of our exports does this answer? | `exportBatchRef` | A September delivery that lands in November attaches to the wrong cycle, or to none. This is the one load-bearing identity field: it must be a batch we registered. |
| How do we read the file: which columns, which sheets, which enums? | `schemaVersion` | Candor renames or adds a column. We store "the columns that still parse" and lose the rest without an error. With the version, the receiver selects the signed column mapping; an unsigned version rejects the whole batch with the difference named. |
| How do we read the manifest itself? | `manifestVersion` | See §5. |
| Is this file in the right tenant's folder? | `tenantId` | Redundant with the folder name on purpose. Folder says `org-xyz`, manifest says `org-abc`: Candor wrote one tenant's file into another's folder. Cheap catch for an expensive mistake. |
| Which delivery does Candor mean when they call us? | `candorBatchId` | Redundant with the filename on purpose. Never used as a key on our side. It exists so a support conversation can say "batch CB-2026-09-001" and both sides find the same thing. |

**Fields deliberately absent.** `format` (we read the container's magic bytes; a declaration adds nothing). `sizeBytes` (the hash already proves integrity). `producedAt` (the upload timestamp is on the object). `contact` (a mailbox does not change per delivery; it belongs in our per-vendor configuration, not in every manifest). Extra fields Candor adds for its own purposes are ignored, not rejected.

## 5. What `manifestVersion` means, and what `schemaVersion` does not

Two different things are versioned. They move independently.

| | `manifestVersion` | `schemaVersion` |
| --- | --- | --- |
| Versions the shape of | **this JSON file**: which keys exist and what they mean | **the data file**: sheets, columns, enums |
| Owned by | CertifyOS. Same for every vendor. | Negotiated per vendor. `candor-directory-v1`, `candor-recs-v1` |
| Changes when | We change what we ask every vendor to declare, e.g. `sheetRowCounts` becomes required | Candor changes its columns |
| On our side it selects | The manifest reader: which keys are required, how they are typed | The column mapping document for the parser |
| Unknown value | Delivery refused before the data file is opened | Batch rejected after the manifest is read, before any row is stored |

Why version a nine-key JSON at all: without it a missing key is ambiguous. If `sheetRowCounts` is absent, did Candor forget, or is Candor's tooling on an older shape where it was optional? The reader would have to guess. With the version there is no guess: v1 says one thing, a future v2 says another, an unknown string is refused. The cost is one literal string Candor copies. If the manifest shape ever changes, we announce it, accept both versions during a transition, then retire the old one. Candor never needs to know how we store or dispatch on the version.

## 6. Filename and folder rules the manifest must agree with

```
<tenantId>_<exportBatchRef>_<candorBatchId>_<yyyyMMdd>.<xlsx|csv>
org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.xlsx
```

- Underscore separates the four legs, so no leg may contain an underscore. Our batch ids use hyphens for that reason.
- `tenantId`, `exportBatchRef`, `candorBatchId` in the manifest must equal the corresponding filename legs. Disagreement is rejected, with the mismatching leg named.
- `dataFile` must equal the actual object name beside the manifest.
- `tenantId` must equal the folder name `to/<tenantId>/`.

## 7. What CertifyOS checks, in order, and what happens

| # | Check | On failure |
| --- | --- | --- |
| 1 | Manifest parses; `manifestVersion` known; required keys present and typed | Refused. Nothing read. |
| 2 | `dataFile` exists in the same folder | Parked as "not complete yet". If the data file arrives later, processing resumes on its own. |
| 3 | `sha256` equals the uploaded bytes | Parked as "not complete yet". We wait for a corrected manifest or a re-upload. A wrong manifest can only delay a delivery, never cause a bad ingest. |
| 4 | Filename legs and folder agree with `tenantId`, `exportBatchRef`, `candorBatchId`, `dataFile` | Rejected, mismatch named. |
| 5 | `exportBatchRef` is a batch we sent to this tenant | Rejected as unknown export. |
| 6 | `schemaVersion` is signed; sheets and headers match it | Rejected with the exact difference. |
| 7 | After parsing: rows read equal `rowCount`, and each `sheetRowCounts` entry | Batch held, not published. Both sides notified. |

A delivery still parked after 24 hours is reported to Candor's operational contact.

## 8. Field reference

| Field | Required | Type | Rule |
| --- | --- | --- | --- |
| `manifestVersion` | yes | string | Literal `certify-manifest-v1`. |
| `dataFile` | yes | string | Bare filename, no path. Must equal the object beside the manifest. |
| `schemaVersion` | yes | string | `candor-directory-v1` for the workbook, `candor-recs-v1` for the long CSV. Must be a signed version. |
| `tenantId` | yes | string | Must equal the folder name and the first filename leg. |
| `exportBatchRef` | yes | string | Our export batch id, copied verbatim from the export processed (`export_batch_id` column; second leg of our export filename). Must be a registered batch. |
| `candorBatchId` | yes | string | Candor's own delivery id. No underscores. Must equal the third filename leg. |
| `rowCount` | yes | integer | Populated data rows across all sheets, excluding header rows and fully empty rows. |
| `sheetRowCounts` | when the workbook has more than one sheet | object | Exact sheet name to populated data-row count, header excluded. Values must sum to `rowCount`. Omit for CSV. |
| `sha256` | yes | string, 64 lowercase hex | SHA-256 of the exact bytes of `dataFile` as uploaded, computed after the last write. |

## 9. Operational rules

1. **Order:** upload the data file, wait for the client to confirm the transfer finished, then upload the manifest. Never the reverse, never in parallel.
2. **Correction of the data:** a re-send is a **new** data file and a **new** manifest under a **new** `candorBatchId`. Do not overwrite a file we may already have archived.
3. **Correction of the manifest only:** if the data file is right and the manifest was wrong (bad hash, wrong count), upload a corrected manifest under the same name. The newest version wins.
4. **Idempotent:** re-uploading the identical pair is harmless. Same bytes are recognised and skipped.
5. **One export, one delivery pair.** If Candor must split one export's findings across files, each file is its own pair with its own `candorBatchId`, and every manifest carries the same `exportBatchRef`.
6. **Nothing else in the folder.** Temporary files (`.part`, `.tmp`, `~$…`) and zero-byte files are ignored, but please do not leave them behind.

## 10. Generating the manifest (reference snippet)

Any language works. Row counts must come from Candor's own writer, not from re-reading the file: the manifest states what Candor intended to send, and we verify that we received exactly that.

```bash
FILE=org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.xlsx
SHA=$(shasum -a 256 "$FILE" | cut -d' ' -f1)
cat > "$FILE.manifest.json" <<EOF
{
  "manifestVersion": "certify-manifest-v1",
  "dataFile": "$FILE",
  "schemaVersion": "candor-directory-v1",
  "tenantId": "org-xyz",
  "exportBatchRef": "org-xyz-candor-2026-09-001",
  "candorBatchId": "CB-2026-09-001",
  "rowCount": 47,
  "sheetRowCounts": { "Physicians Directory Accuracy": 31, "Physicians Additional Locations": 16 },
  "sha256": "$SHA"
}
EOF
```

---

## Internal notes (strip before sharing)

- **Where `manifestVersion` lives on our side.** (1) Code: one manifest reader per accepted version, hard-coded set, because the manifest contract is ours and vendor-neutral. Unknown version is `INBOUND_REFUSED` in the receive task before the data file is opened. (2) `ingestion_batches`: the declared facts (`manifestVersion`, `schemaVersion`, `exportBatchRef`, counts, hash) are stored on the batch record, so "under which contract did we accept this" is answerable years later. (3) Archive bucket: the raw manifest is copied beside the data file before registration (D11). `schemaVersion` by contrast resolves against `vendor_schema_mappings` (D12, D17), because column mappings are per-vendor data.
- Changes versus proposal §2.4: adds `sheetRowCounts` (Confluence Q1), drops `producedAt` and `contact`. Pre-signing, so still `v1` under §7 change control. §2.4 schema block should be updated to match. `samples/generate_samples.py` already regenerated to this shape (2026-09-11); CSV bytes and hash unchanged.
- `contact` moves to per-vendor config (`vendors.<id>.contact`, beside `vendors.<id>.paused`).
- Under `candor-directory-v1` (wide workbook) there is no per-row `tenant_id`, so manifest `tenantId` plus folder are the only tenant checks. `candor-directory-v2` / `candor-recs-v1` add the per-row echo.
- Sheet names in the example are from the Sept 2026 sample. Confirm exact spelling with Candor.
