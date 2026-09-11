# Directory Accuracy Service — Design Document Instructions

**Prepared:** 2026-09-11 · **Owner:** Dev Pandey
**What this is:** the standing instructions for writing the directory accuracy module design documents and their companions. Read this file in full before starting (or resuming) any module document, so no context or requirement is lost between sessions. This file is *instructions*, not a design document — it decides nothing about the service.

---

## 1. What the documents are for

The service is designed as **two modules, in workflow order**, one markdown design document each:

1. **Vendor export** (`vendor-export/`) — tenant-configurable selection query, scheduled export run, file build (one row per practitioner × practice location), placement in the vendor's SFTP bucket, the export registry and batch identity the vendor echoes back.
2. **Vendor ingestion** (folder created when its turn comes) — arrival detection and completeness signal, ingestion with dispositions, rejected-recommendations lookup, the service's own staging and review APIs (approve / reject / skip), and the Sync Latest release under `<vendor>:{tenantId}`.

Each is a genuine deep dive:

- every credible approach for each stage, with alternatives;
- pros, cons, and limitations of each approach — failure modes, operational burden, cost, future limitations;
- the decision criteria stated **before** the decision, so the reader can check the reasoning;
- a clear recommendation, and an explicit "why not" for every losing alternative.

The goal is that a reviewer can disagree intelligently: they see the full option space, the criteria, and the trade-offs — not just a conclusion.

## 2. Input documents and their authority

| Document | Authority | How to use it |
| --- | --- | --- |
| Source-of-truth v2 (decision register D2-xx, open items O-xx) | **Binding** for the decisions it records about this program: D2-03/04/05/06/07 (transport folders, manifest-last, event → queue → registry → idempotent worker, internal contract proposal, rejected-recommendations lookup), D2-11 (source-agnostic adapters), D2-14 (Golden-record engines extend-only), D2-18 (survivorship ranking), D2-20 (7-year audit), D2-37 (tenant-scoped slice source, full-slice rebuild), D2-38 (standalone program and service). Open inputs: O-1, O-17, O-18, O-19. | The doc complies. A real reason to revisit a v2 decision is flagged explicitly as a proposed change — never a silent divergence. Cite as "v2 D2-xx" or "v2 §x.y", never as a file path. |
| `reference/candor-*.md`, `reference/samples/` | **Not source of truth.** Current Candor evidence (Sept 2026 dictionary + sample analysis), the O-1 contract proposal, the manifest example handed to Candor, sample files. | Inputs to the Contracts section; the adapter isolates all column knowledge. |
| `roster-pipeline-reuse-analysis.md`, `jira-ticket-draft.md` | Analysis and ticket drafts. | Feed the Alternatives sections and the ticket-impact lists. |

## 3. Files in this folder

```
directory-accuracy/
├── instructions.md                    ← this file
├── vendor-export/
│   ├── vendor-export.md               ← module 1 design doc (shareable)
│   └── vendor-export-concepts.md      ← its companion (local prep)
├── (vendor-ingestion/)                ← module 2 — same file pair, created when its turn comes
├── jira-ticket-draft.md               ← Jira story + subtasks (published 2026-09-09, CP-39602)
├── roster-pipeline-reuse-analysis.md  ← why not extend the roster pipeline; code receipts
└── reference/                         ← current vendor evidence, contract proposal, manifest example, samples
```

Module 2's contracts take module 1's as settled inputs (batch identity, registry row, echoed ids) once module 1 is ticked in §9; a genuine conflict is flagged as a proposed change to module 1, never silently diverged from.

No other program's documentation is an input to this folder. The service touches the rest of the platform only at the DAL (reads and the release write), the shared SFTP platform, the PDM reviewer UI (a separate front-end application that consumes this service's APIs), and Golden-record survivorship.

## 4. Required structure of every module document

**Each module doc follows the `design.md` structure from the `/create-design-doc` skill** (`~/.claude/skills/create-design-doc/SKILL.md` — the Spec-Driven Development Team Guide template). The body is the template sections in the template order; everything the template does not define goes in the module's companion under `# Supplementary material` — never interleaved with the body. The skill's **house style** applies: sentence-case headings, `YYYY-MM-DD` dates, pipe tables with plain `| --- |` separators, backticked `path/file.java:123` citations repo-qualified once per section, spaced em-dashes, `R1`/`D1`/`Q1` ID style, no emoji in headings.

**Writing style:**

- **Plain language first.** Write for any teammate — product, ops, a new engineer.
- **Explain jargon inline, in brackets, at first use** — e.g., "idempotent (running it twice produces the effect once)". Banned outright: "fan-out" (say "split the work into one task per chunk").
- **Bullet points over paragraphs** wherever the content is a list.
- **Professional, literal wording only.** No invented metaphors, dramatic shorthand, or compressed insider phrasing. Name the thing plainly. Where an industry-standard term is the right word (keyset pagination, dead-letter queue), use it and explain it in brackets.
- **Hard bullet-length cap: one bullet = 1–3 lines, never more.** A longer bullet is a paragraph in disguise — split it into nested sub-bullets, one idea per bullet.
- **Evidence stays.** File:line receipts, numbers, and trace references are kept.
- **The presentation test.** Every named parameter or design term carries its *what* and its *why* close to where it appears.
- **Presentable, not a discussion log.** No author names, no "(Dev, date)", no review framing ("asked during review", "approved by …"). State the fact or decision directly; verification receipts stay. No metadata header table — the doc opens with the title and Purpose and scope.
- **Cost estimates carry their derivation.** Never a bare "$X/mo" — show unit price × volume.
- **Breathing room.** One idea per bullet; blank line between logical groups and around every heading, table, and code block; nesting stops at two levels in the body; prefer a short table over five parallel bullets; simple words ("use", not "leverage").
- **Companion doc — one per module doc,** named `<slug>-concepts.md`, holding the concepts section and the module's entire supplementary material. **Companions are never mentioned in the doc.** No "see the companion" pointers, no filenames, nothing that hints a second file exists. Where the body condenses something analyzed in depth, it states the conclusion and the deciding reasons — no pointer. All style rules apply to the companions too.
- **Approach ↔ Contracts cross-links.** Every Approach step with a concrete call ends with "Payloads: *Contracts → lifecycle step N*"; the Contracts lifecycle intro carries the reverse mapping.

### 4.1 Top of the doc

1. Title, then **Purpose and scope** — what the module owns / does not own / neighbors. No header table.

### 4.2 Body — the template sections, in template order

One **section triage table** first (template rows only, one-line reason for every skip), then:

| Section | Status | Content mapping |
| --- | --- | --- |
| Approach | Always | The chosen design as numbered lifecycle steps, **each written as bullets**: bold one-line step title, then **What**, **Why** (traced to v2 D2/O or evidence), **Why not `<the alternative a reviewer would ask about>`**, **What if it fails** (2–3 bullets, pointing to *Failure modes and rollback*), **Payloads** pointer. Each step self-contained enough to present live. Contains the `### Decisions` subsection. |
| Alternatives considered | Always | **Self-contained per alternative:** what it is / how it would work (2–3 bullets), pros, cons, why rejected (one direct comparison on the deciding points), cost line. Never a bare name with two bullets. Weightage tables and comparison tables live in the companion. |
| Components and files touched | **Skipped** | Pre-implementation discussion doc — one-line triage reason. |
| Contracts and interfaces | Always | Endpoints, payloads, file contracts both directions, manifests, event contracts. **Mandatory subsection "Configuration and flags":** one table of every tenant configuration key (`directory-accuracy-config` entry) and every feature flag — name, where it lives, type, default, behavior controlled. A key used anywhere but missing here is a finding. "No feature flags" is stated explicitly when true. |
| Data model and migration | Always | **Tables inventory first:** every table the service **creates** and every table it **interacts with** (with the owning system named). A table used anywhere but missing here is a finding. |
| Security, privacy, and access | Always | Tenant isolation, identities and keys (per-deployable service accounts, vendor bucket credentials), trust boundaries, PHI handling. |
| Performance and scale | Always | NFR numbers (volumes, cadence, budgets) with derivations; the 1M-row stress scenario stated. |
| Observability | Always | Metrics, alerts + thresholds, correlation, stuck-work detection; telemetry never blocks work. |
| **Audit trail** | **Always (added section, right after Observability)** | Events, payloads, same-transaction rule, 7-year retention. **Implementation-ready contracts:** the common envelope once (id, type, tenant, identity fields, batch/run correlation ids, actor, timestamp, detail) plus a per-event detail contract. **Table name first:** the section opens by naming every audit table the service writes to. |
| Failure modes and rollback | Always | Edge-case table (case → handling → where) plus a **Rollback** paragraph (kill switch, what stops, what already-written data stays). |
| Test strategy | Skipped or condensed | One-line triage reason, or the decisive test cases only. |
| Rollout | Always | Flags / kill switches, ordering, cross-team asks (DAL owners, DevOps, api-layer team, Product), pilot tenant sequencing. |

### 4.3 Supplementary material — lives in the companion, not the doc

The module doc contains ONLY the template body. Its companion holds, in order: **Concepts** (every service, tool, and concept the doc names, explained from zero in 3–8 bullets each, no design decisions), then `# Supplementary material`:

1. **Baseline — verified current behavior** (claim/evidence rows with file:line receipts).
2. **Approaches analyzed in depth** — criteria first, then every approach with the same full treatment: how it works, pros, cons, per-criterion weightage, why it loses, failure modes, cost with derivation (GCP-current-stack alternative always included).
3. **Requirements traceability** to v2 D2/O numbers.
4. **Inputs and outputs** — what the module consumes from and provides to its neighbors (the other module, DAL, SFTP platform, PDM reviewer UI, survivorship).
5. **Design rationale — anticipated questions** as timeless FAQ.
6. **Open questions and sign-offs**, numbered, each with an owner.
7. **Ticket impact** — which Jira tickets change, plus new tickets.

### 4.4 What we do NOT adopt from the skill

No Jira-key spec dirs, no repo-resident `specs/` layout, no tier rationale, no Gate 2 checklist, no Confluence auto-publish. Template structure, section order, triage discipline, and house style only. Codebase verification (every claim with receipts, adversarial check before writing) is already the working method.

## 5. Non-negotiable requirements (apply to every module, every approach)

An approach that cannot satisfy one of these **without future limitations** is disqualified regardless of its other merits — say so in its cons.

1. **Idempotency everywhere.** Deterministic identities + database uniqueness constraints, never application memory. Retries, duplicate events, double-clicks, and replays never create a duplicate business effect.
2. **Full audit trail.** Append-only, written transactionally with the state change it describes, 7-year retention (D2-20), able to answer "what changed, why, who acted, when, from which source, and what result reached the Golden record" without reconstruction. The audit trail is business state, not logs.
3. **Observability.** Correlation identifiers carried end to end (export batch → vendor batch → row → staged item → review action → release → Golden-record outcome); metrics and alerts for batch liveness, queue depth, aging, disposition counts, DLQ depth, quarantine depth, stuck work. Telemetry is best-effort and never blocks the workflow.
4. **Tenant isolation server-side on every path** — queries, jobs, events, reports, replays, exports. UI filtering is never a boundary.
5. **Practitioner identity column convention:** the practitioner leg is stored as `certify_practitioner_id` (the platform's name for a column referencing the OV's `certify_id`); never `practitioner_id` or bare `certify_id`.
6. **Vendor-agnostic naming.** Vendor-specific knowledge lives only in adapters and configuration; no vendor name in a table, topic, class, or API name. Vendor names appear only in registry/config values (`candor` source row, `candor:{tenantId}` slice source), adapter registration keys, per-vendor infrastructure identities, and prose (D2-11).
7. **Sync Latest is the only release path**; nothing upstream of the release worker ever writes toward the Golden record. Release writes the **full slice** under the tenant-scoped vendor source, never a sparse write (D2-37).
8. **Quarantine over guessing.** When a stage cannot safely decide (dependency outage, ambiguous scope, unknown schema version), it parks the work for retry or audited operator replay — it never mislabels and never loses a row.
9. **Nothing silently discarded.** Every input row gets a counted, audited disposition; batch counts reconcile.
10. **Golden-record engines are extend-only** (D2-14). The service never reimplements survivorship, merge, dedup, or termination cascade logic.
11. **ISO 8601 dates everywhere** in contracts and data models: `YYYY-MM-DD`, timestamps `YYYY-MM-DDThh:mm:ssZ`; filenames use the compact `yyyyMMdd`.

## 6. Decided constraints the doc must NOT reopen

- The program stands alone: query-selected population, own cadence, own staging and review, own service, one design doc (D2-38).
- One microservice, two deployables (`-api` always-on, `-worker` scale-to-zero), one database, one repo, per-deployable service accounts (2026-09-09).
- No own UI ever — the PDM reviewer UI consumes the review APIs (CP-39602).
- Transport = the shared DevOps-operated SFTP platform; folders `from/` (CertifyOS drops, vendor read-only) and `to/` (vendor drops) (TS-111546).
- Manifest-last delivery is the ratified default (D2-04); the filename-identity alternative must be argued in the module doc if adopted.
- Event → durable queue → batch registry → idempotent worker ingestion shape (D2-05).
- Rejected-recommendations memory is a lookup table; matches suppressed from the queue in MVP with audit kept (D2-07).
- Release under `candor:{tenantId}` with full-slice rebuild (D2-37); per-tenant survivorship ranking rows must exist before the first release (D2-18; an unranked source falls to rank 997).
- Manual review only in MVP; no auto-approval.

What the module docs **are** free to decide: datastore, runtime language per deployable, queue technology, schema and contract details, adapter design, file and manifest formats, indexing and partitioning, deployment topology — provided §5 holds and the constraints above are respected.

## 7. Questions each module doc must answer

**Vendor export**

- **Selection:** query language/format and storage (v2 O-17); validation and injection safety; location-level rules; the practitioner read path (DAL vs api-layer) and its concurrency limit.
- **Export:** cadence configuration and staggering; registry before file; row grain and identity (practitioner × practice location); file assembly and placement; batch identity and the echo the vendor must return; never rewriting a placed batch; failure semantics and the reconciler.

**Vendor ingestion**

- **Transport and arrival:** completeness signal (manifest vs filename identity); duplicate, delayed, and lost events; archive-before-parse.
- **Ingestion:** adapter architecture and how a second vendor onboards; chunked streaming parse; disposition cascade and count reconciliation; quarantine and replay; the business-logic extension point.
- **Rejected recommendations:** normalization rules and versioning; suppression vs visibility.
- **Review APIs:** query surface, one-decision-per-row enforcement, mandatory rejection reasons, skip semantics and cross-cadence re-send (v2 O-18), tenant isolation.
- **Release:** Sync Latest mechanics, released-items ledger, full-slice rebuild, ranking guardrail, outcome verification (survivorship returns 200 on internal error — read back), shared vs own machinery (v2 O-19).
- **Scale:** the 1M-row file and the 10,000-files scenario; database choice under global uniqueness.

## 8. Working process

1. Re-read this file, the module's companion, every previously ticked module doc, and the relevant v2 sections before each session.
2. Verify claims against the actual codebase where an incumbent approach is evaluated — never inherit a claim.
3. One module doc at a time, in the §1 order. Write to the §4 structure. Decision criteria before approaches; cost estimate on every approach; Audit trail and Observability as body sections; Failure modes and rollback + Rollout always present.
4. Amendments to a ratified decision or to a ticked module doc are explicit flags (`⚠ Proposed change to D2-xx / module N: what breaks, why, the proposed amendment`), never silent edits.
5. Finalize → review with the owning team → tick §9 → update the affected Jira tickets (CP-39602 subtasks; earlier spike tickets DA-14…DA-18 re-homed here).
6. Stop after each module doc for review before starting the next.

## 9. Completion checklist

Tick each entry (change `[ ]` to `[x]`, add the date) when that module doc is finalized and reviewed. Later docs may reference **only ticked files** as settled context.

- [ ] 1. `vendor-export/vendor-export.md` (+ `-concepts.md`) — placeholders created 2026-09-11; to write first.
- [ ] 2. vendor ingestion module (folder and file pair to create when its turn comes) — to write after vendor export is approved.
- [x] `jira-ticket-draft.md` — published 2026-09-09 (CP-39602 + six subtasks); estimates re-baseline after the design spike.
