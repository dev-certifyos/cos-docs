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
│   └── vendor-export-concepts.md      ← concepts + supplementary material. Local prep only.
├── (vendor-ingestion/)                ← module 2 — created when its turn comes
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

Authoritative checklist: `instructions.md` §9. Snapshot: vendor export — rewritten 2026-09-22, destination ask and pragmatic alternatives added 2026-09-24 (JobRunr tick on GCE, event-driven completion, egress copies the file to a destination on the request), published to Confluence 2026-09-24 under Engineering → PDM → [Directory accuracy](https://certifyos.atlassian.net/wiki/spaces/Engineerin/pages/2252603415/Directory+accuracy) as [CP-39602 - Design: Directory accuracy vendor export](https://certifyos.atlassian.net/wiki/spaces/Engineerin/pages/2252767300/CP-39602+-+Design+Directory+accuracy+vendor+export), pending review; companion `vendor-export-concepts.md` not yet updated to the rewrite; vendor ingestion — not started.
