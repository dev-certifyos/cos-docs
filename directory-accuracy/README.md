# Directory accuracy — documentation index

The design and brainstorming home for the directory accuracy service: the standalone program that selects practitioners by a tenant-configured query, exports them to an external accuracy vendor (Candor first) over the shared SFTP platform, ingests the vendor's per-attribute recommendations into its own staging store, lets a payer reviewer approve, reject, or skip each one, and releases approved rows to the Golden record under `<vendor>:{tenantId}`.

The service is two modules, designed in order: **vendor export** first (select NPIs, build the sheet, drop it in the vendor's SFTP bucket), **vendor ingestion** later (pick up the vendor's response, stage, review, release). Each module has its own folder and its own design doc.

## Where things live

```
directory-accuracy/
├── README.md                          ← this index
├── instructions.md                    ← HOW the module docs are written (template, house style, non-negotiables) — read first, every session
├── jira-ticket-draft.md               ← Jira story and subtasks for the service (published 2026-09-09, CP-39602)
├── roster-pipeline-reuse-analysis.md  ← why the service is not built by extending the roster pipeline; talking-points version (full receipts in reference/roster-pipeline-reuse-analysis-detailed.md)
├── vendor-export/                     ← module 1 — vendor export (draft written 2026-09-11, pending review)
│   ├── vendor-export.md               ← the module design doc. The shareable artifact.
│   ├── vendor-export-concepts.md      ← concepts + supplementary material. Local prep only; in line with the design doc as of 2026-10-21.
│   ├── vendor-export-flow-steps.png   ← "the flow in six steps" image, rendered from the lifecycle artifact; replaces the mermaid block on Confluence
│   ├── publish_confluence_page.py     ← user-run publish: md → storage format, flow image + artifact link, create-or-update by title
│   ├── update_confluence_page.py      ← earlier anchored-patch publisher (storage format), kept for reference
│   ├── confluence-backup-v3/v4.storage.html ← live page bodies saved before full republishes
│   └── *-implementation-plan.md       ← CP-39604 / CP-39605 / CP-40438 implementation plans (done)
├── doc-update-draft-2026-10-21.md   ← change list for the saved-filter flow and the directory-accuracy-service rename (applied)
├── vendor-ingestion/                  ← module 2 — vendor ingestion (phase 2, CP-40437; started 2026-10-15)
│   ├── vendor-ingestion.md            ← the module design doc. Steps 1–8 (arrival through review, rejected-value memory, Sync Latest release) written.
│   ├── vendor-ingestion-concepts.md   ← concepts + supplementary material. Local prep only.
│   ├── vendor-ingestion-lifecycle.png ← the lifecycle board (rendered from the flow artifact); embedded under ## Approach, uploaded as a Confluence attachment by the script
│   └── publish_confluence_page.py     ← user-run publish: md → storage format, local images → attachments, create-or-update by title under Directory accuracy
└── reference/                         ← inputs — not source of truth
    ├── candor-exchange-contract-proposal.md ← the O-1 proposal we negotiate against on the Candor call (export + recommendations schema, naming, folders)
    ├── candor-manifest-example.md           ← the manifest example + field guide handed to Candor engineering
    ├── candor-sept-2026-sample-analysis.md  ← Sept 2026 dictionary + MVP sample (current Candor evidence): what changed, 8 semantic issues, contract impact, questions for Candor
    └── samples/                             ← worked sample CSVs for both directions (from/ export, to/ recommendations) + generator script
```

## The file pair inside each module folder

- `<slug>.md` — the module design doc. Pure design.md template body. **The shareable artifact.**
- `<slug>-concepts.md` — concepts + supplementary material. **Local prep only — never shared, never referenced by the module doc.**

## Status

Authoritative checklist: `instructions.md` §9. Snapshot: vendor export — rewritten 2026-09-22, destination ask and pragmatic alternatives added 2026-09-24 (JobRunr tick on GCE, event-driven completion, egress copies the file to a destination on the request), published to Confluence 2026-09-24 under Engineering → PDM → [Directory accuracy](https://certifyos.atlassian.net/wiki/spaces/Engineerin/pages/2252603415/Directory+accuracy) as [CP-39602 - Design: Directory accuracy vendor export](https://certifyos.atlassian.net/wiki/spaces/Engineerin/pages/2252767300/CP-39602+-+Design+Directory+accuracy+vendor+export), pending review; companion `vendor-export-concepts.md` brought in line 2026-10-21; vendor ingestion — service name settled 2026-10-19 as directory accuracy service (instance groups `directory-accuracy-api` / `directory-accuracy-worker`; module 1 names `vendor-export-*` map to these); folder created 2026-10-15, design doc has steps 1–8 (arrival through release); data model settled 2026-10-16 as one document per `INVALID` field (`field_recommendations`), confirmations and rejects counted on the inbound-file entry, release by read-merge-write of our own DAL slice (no ledger); step 8 cross-checked 2026-10-19 against roster and tenant-UI slice conventions (platform crosswalk recipes, in-place edits for junction fields, read back on survivorship's `mdm.slice.ov_generated`); Alternatives written 2026-10-19; published to Confluence 2026-10-19 as [CP-40437 - Design: Directory accuracy vendor ingestion](https://certifyos.atlassian.net/wiki/spaces/Engineerin/pages/2302345225/CP-40437+-+Design+Directory+accuracy+vendor+ingestion) (page 2302345225, user-run `vendor-ingestion/publish_confluence_page.py`); Performance, Observability, Audit trail written 2026-10-19 and published (page version 2); no placeholders left.

2026-10-21: both pages updated for egress resolving a saved filter id (CP-41069; module 1 keeps no selection job or per-NPI registry; completion event asks for `failureReason` and the `selection` block), the repository rename to `directory-accuracy-service` (PR #25), and `/v1` paths. Phase 1 page version 9, phase 2 page version 5. Lifecycle artifact updated: https://claude.ai/artifact/3y3YLP3EutD9yYtPy7qNvf
