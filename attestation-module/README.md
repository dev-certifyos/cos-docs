# Attestation module — documentation index

The design and brainstorming home for the Directory Accuracy & Attestations feature (CP-37409).

**Amended 2026-09-08 (D2-38):** the external-vendor program (Candor) is **decoupled from the attestation workflow** — separate population (tenant-configured query, not the attestation window), separate cadence, separate staging and review, own service and own standalone design doc (planned home: `cos-docs/platform/external-source-ingestion/`). This folder now covers the **attestation module only**; the `modules/03-sftp-exchange/` and `modules/04-ingestion/` drafts are retired from the series and kept as source material for the standalone doc. The two programs meet only at Golden-record survivorship.

## Where things live

```
attestation-module/
├── README.md                     ← this index
├── instructions.md               ← HOW the module docs are written (read first, every session)
├── source-of-truth-v2.md         ← WHAT we are building — binding workflow + decision register (D2-01…)
├── ticket-breakdown-mvp1.md      ← the Jira tickets (DA-01…DA-32), updated as module docs finalize
├── reference/                    ← inputs and archive — not source of truth
│   ├── architecture-brief.md     ← pre-meeting position paper (one candidate approach per module)
│   ├── audit-observability-brief.md ← audit ≠ telemetry requirements context
│   ├── product-clarifications-2026-09-08.md ← Product's field-mapping answers analyzed; D2-17 reopening; termination sub-questions; Jira blocker comment drafts
│   ├── candor-exchange-contract-proposal.md ← the O-1 proposal we negotiate against on the Candor call (export + recommendations schema, naming, folders)
│   ├── candor-data-dictionary-explained.md  ← how to read Candor's July 2026 data dictionary + the MMO delta report; drift list (CT-011)
│   ├── candor-sept-2026-sample-analysis.md  ← Sept 2026 dictionary + MVP sample vs July/MMO: what changed, 8 semantic issues, Additional Locations tab, contract impact, questions for Candor
│   └── samples/                  ← worked sample CSVs for both directions (from/ export, to/ recommendations) + generator script
└── modules/                      ← one folder per module doc, numbered in workflow order
    ├── 01-cycle/                 ← backfill, scheduler, task creation, outreach trigger
    ├── 02-portal-lane/           ← attestation task APIs (portal + non-PDM consumers)
    ├── 03-sftp-exchange/         ← RETIRED 2026-09-08 (D2-38) — source material for the standalone external-source-ingestion doc
    ├── 04-ingestion/             ← RETIRED 2026-09-08 (D2-38) — source material for the standalone external-source-ingestion doc
    ├── 05-backend/               ← (to write) the workflow owner
    ├── 06-database-data-layer/   ← (to write) store + single gateway
    ├── 07-ui/                    ← (to write) reviewer queue and actions
    └── 08-client-export/         ← (to write) own doc since D2-38
```

## The file pair inside each module folder

- `<slug>.md` — the module design doc. Pure design.md template body. **The shareable artifact.**
- `<slug>-concepts.md` — concepts + supplementary material. **Local prep only — never shared, never referenced by the module doc.**

## Status

Authoritative checklist: `instructions.md` §9. Snapshot: 01–02 finalized and approved; 03–04 retired (D2-38); 05–08 to write; standalone external-source-ingestion doc (Candor program) to write.
