# Attestation module — documentation index

The design and brainstorming home for the attestation module (CP-37409): the recurring cycle in which practitioners — or the provider admins acting for them — attest to their directory data, a payer reviewer accepts or rejects the submissions, and approved values are released to the Golden record.

## Where things live

```
attestation-module/
├── README.md                     ← this index
├── instructions.md               ← HOW the module docs are written (read first, every session)
├── source-of-truth-v2.md         ← WHAT we are building — binding workflow + decision register (D2-01…). The only document here that also records the boundary with neighbouring programs.
├── reference/                    ← inputs and archive — not source of truth
│   ├── architecture-brief.md     ← pre-meeting position paper (one candidate approach per module)
│   ├── audit-observability-brief.md ← audit ≠ telemetry requirements context
│   └── product-clarifications-2026-09-08.md ← Product's field-mapping answers analyzed; D2-17 reopening; termination sub-questions; Jira blocker comment drafts
└── modules/                      ← one folder per module doc, numbered in workflow order
    ├── 01-cycle/                 ← backfill, scheduler, task creation, outreach trigger
    ├── 02-portal-lane/           ← attestation task APIs (portal + non-PDM consumers)
    │                             (numbers 03 and 04 are retired and not reused)
    ├── 05-backend/               ← (to write) the workflow owner
    ├── 06-database-data-layer/   ← (to write) store + single gateway
    ├── 07-ui/                    ← (to write) reviewer queue and actions
    └── 08-client-export/         ← (to write) client export of attested data
```

## The file pair inside each module folder

- `<slug>.md` — the module design doc. Pure design.md template body. **The shareable artifact.**
- `<slug>-concepts.md` — concepts + supplementary material. **Local prep only — never shared, never referenced by the module doc.**

## Status

Authoritative checklist: `instructions.md` §9. Snapshot: 01–02 finalized and approved; 05–08 to write.
