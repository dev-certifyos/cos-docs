# Directory Accuracy & Attestations — Module Design Document Instructions

**Prepared:** 2026-08-27 · **Owner:** Dev Pandey
**What this is:** the standing instructions for writing the per-module deep-dive design documents. Read this file in full before starting (or resuming) any module document, so no context or requirement is lost between sessions. This file is *instructions*, not a design document — it decides nothing about any module.

---

## 1. Why these documents exist

The consolidated source of truth v2 (`source-of-truth-v2.md`) is **finalized**. It defines the end-to-end workflow, the modules, the decision register (D2-01…D2-30), and the two remaining open items (O-1 Candor file contract, O-5 partial-approval compliance). It deliberately stays at the workflow level.

The next layer of work is **one markdown design document per module**, each a genuine deep dive:

- every credible approach for that module, with alternatives;
- pros, cons, and limitations of each approach — including failure modes, operational burden, cost, and future limitations;
- the decision criteria stated **before** the decision, so the reader can check the reasoning;
- a clear recommendation, and an explicit "why not" for every losing alternative.

The goal of each document is that a reviewer can disagree intelligently: they see the full option space, the criteria, and the trade-offs — not just a conclusion.

## 2. Input documents and their authority

| Document | Authority | How to use it |
| --- | --- | --- |
| `source-of-truth-v2.md` | **Binding.** The finalized workflow and decision register. | Module docs must comply with it. If a deep dive uncovers a real reason to revisit a v2 decision, the module doc flags it explicitly as a proposed change to v2 — it never silently diverges. |
| `reference/architecture-brief.md` | **Not source of truth.** A pre-meeting position paper. | Treat its per-module recommendations as **one candidate approach** in each deep dive — usually the "reuse the existing rails" candidate, with its reasoning. The deep dive must not assume the brief's conclusion; it must re-derive or refute it against the alternatives. |
| `reference/audit-observability-brief.md` | **Not source of truth.** A pre-meeting design brief. | Its core distinctions are carried into every module doc as *requirements context*: audit trail ≠ observability telemetry; audit is append-only business state written in the same transaction as the mutation (never "just logs"); 7-year retention (D2-20, final); telemetry is best-effort and never blocks work. Each module doc must show how its chosen approach satisfies this — but may propose a different mechanism if it argues it properly. |
| `ticket-breakdown-mvp1.md` | Existing Jira tickets. | After each module doc is finalized, update the affected Jira tickets (this replaces the retired O-10 re-derivation pass). |

## 3. The module documents to produce

**Directory layout (restructured 2026-08-31).** Everything for this feature lives under `cos-docs/attestation-module/`: this file (`instructions.md`), `source-of-truth-v2.md`, and `ticket-breakdown-mvp1.md` at the root; the two briefs under `reference/` (the v1 SoT was retired and deleted 2026-09-01); each module in its own numbered folder under `modules/` — the number is the §3 workflow order:

```
attestation-module/modules/<NN>-<slug>/<slug>.md            ← the module doc (shareable)
attestation-module/modules/<NN>-<slug>/<slug>-concepts.md   ← its companion (local prep)
```

**The order follows the workflow itself** (v2 §6, Stage 0 → Stage 6): start where the data starts — the cycle/backfill — and move down the spine. Each document builds on, and explicitly references, the ones written before it (§8, §9).

**Amended 2026-09-08 (D2-38):** the external-vendor program (Candor) is decoupled from the attestation workflow — it is a separate directory-accuracy service with its own population query, cadence, staging, and review. Docs 3 (SFTP Exchange) and 4 (Ingestion) are **retired from this series** and replaced by **one standalone design document** for the external-source-ingestion service; their drafts stay in place as source material for it. The attestation-series docs are otherwise unchanged.

| # | Module doc | Slug | Covers (v2 §) | Notes |
| --- | --- | --- | --- | --- |
| 1 | Cycle (backfill + scheduler + task creation + outreach trigger) | `cycle` | §6.0–§6.2 | First, per the workflow order. Includes the backfill and the deterministic identity computation. |
| 2 | Portal UI lane | `portal-lane` | §6.7 | Prefill, staleness guard, delta capture, submission envelope to the backend. |
| ~~3~~ | ~~SFTP Exchange (inbound + outbound)~~ | ~~`sftp-exchange`~~ | — | **Retired 2026-09-08 (D2-38)** — folded into the standalone external-source-ingestion doc below. Draft kept as source material. |
| ~~4~~ | ~~External Source Ingestion Layer~~ | ~~`ingestion`~~ | — | **Retired 2026-09-08 (D2-38)** — folded into the standalone external-source-ingestion doc below. Draft kept as source material. |
| 5 | Attestation Module Backend | `backend` | §6.8–§6.10, sync worker §6.11 handoff | The workflow owner. Biggest decisions live here — earlier docs' needs (task creation, staging writes, portal serving) are its requirements inputs. |
| 6 | Attestation Module Database + Data Layer | `database-data-layer` | §6.8 | One doc: the store and its single gateway are one design. Written right after the backend, whose access patterns it serves. |
| 7 | Attestation Module UI | `ui` | §6.9, §6.10 | Queue, assignment (manual, D2-10), actions, Sync Latest, rollback. Vendor rows no longer appear here (D2-38; whether the vendor program's review surface shares this application is v2 O-19). |
| 8 | Client Export (downstream trigger) | `client-export` | §6.12 | Own doc (the earlier "fold into ingestion" option died with D2-38 — the ingestion doc left the series). Cadence: every 30 days (D2-13). |

**Standalone external-source-ingestion doc (the "Candor doc", one file — decided 2026-09-08):** covers the entire vendor program end to end — tenant-configurable selection query (v2 O-17), scheduled export, SFTP transport on the shared platform, inbound ingestion + dispositions, rejected-recommendations lookup, its own staging and reviewer actions (approve/reject/skip, v2 O-18), Sync Latest release under `candor:{tenantId}` (D2-37), and the Candor contract proposal (O-1). Same template, house style, and companion rules as the module docs; proposed home `cos-docs/platform/external-source-ingestion/` (same pattern as the Smart Outreach Service doc); listed in §9's related-docs section. One doc, not two — the transport shrank to "reuse the shared SFTP platform" and the task-attribution machinery died with the decoupling; split later only if review size demands it.

**No document** for Golden Record Processing (existing engines, extend-only, D2-14) or for the downstream directory vendors (out of MVP scope). Where a module touches them (sync worker writes slices; config-only source-ranking rows), that integration is described inside the touching module's doc.

## 4. Required structure of every module document

**As of 2026-08-28 (Dev's directive), module docs follow the `design.md` structure from the `/create-design-doc` skill** (`~/.claude/skills/create-design-doc/SKILL.md` — the Spec-Driven Development Team Guide template), adapted for module-level discussion docs. The body is the EM's template sections in the EM's order; everything module-specific that the template does not define goes **at the end under `# Supplementary material`** — never interleaved with the body. The skill's **house style** applies (sentence-case headings, `YYYY-MM-DD` dates, pipe tables with plain `| --- |` separators, backticked `path/file.java:123` citations repo-qualified once per section, spaced em-dashes, `R1`/`D1`/`Q1` ID style, no emoji in headings).

**Writing style (unchanged, applies everywhere):**

- **Plain language first.** Write for any teammate — product, ops, a new engineer.
- **Explain jargon inline, in brackets, at first use** — e.g., "idempotent (running it twice produces the effect once)". Some terms are banned outright rather than explained: "fan-out" (say "split the work into one task per chunk"), and anything on the banned list below.
- **Bullet points over paragraphs** wherever the content is a list.
- **Professional, literal wording only (Dev directive, 2026-08-28).** No invented metaphors, dramatic shorthand, or compressed insider phrasing — banned examples: "review tsunami", "pages the window query", "spike days blow the deadline", "fan out / fan-out" (Dev, 2026-08-28 — say what actually happens: "creates one Cloud Task per chunk of practitioners"). Name the thing plainly: "all 70k reminders would be sent on the same day"; "reads the query results in small batches". Where an industry-standard term is genuinely the right word (keyset pagination, dead-letter queue), use it and explain it in brackets. The test: a reader outside the team understands the sentence on first read, and it sounds like an engineering document, not a chat message.
- **Hard bullet-length cap (Dev directive, 2026-08-28): one bullet = 1–3 lines, never more.** A bullet that grows beyond ~3 lines is a paragraph in disguise — split it into **nested sub-bullets**, one idea per bullet. This applies everywhere, most strictly in the Approach section: step title → sub-bullets (Why / Why-not / What) → sub-sub-bullets for the individual arguments, examples, and receipts. Nobody reads a wall; everybody scans a tree.
- **Evidence stays.** File:line receipts, numbers, and trace references are kept.
- **The presentation test.** Every named parameter or design term carries its *what* and its *why* close to where it appears.
- **Presentable, not a discussion log (2026-08-28).** These docs are presented to the wider team, so they must read as finished designs, not meeting notes:
  - **No author names anywhere** — no "Author" row, no "(Dev, date)", no "decided by X".
  - **No review/discussion framing** — banned phrases: "asked during review", "proposed by …, analyzed in review", "corrected after …", "simplified during review", "approved by …". State the fact or decision directly; verification receipts ("verified", file:line) stay.
  - **No metadata header table** (Module i/8 / Author / Status / Evidence-verified rows) — the doc opens with the title and Purpose and scope. *(Supersedes §4.1 item 1.)*
- **Cost estimates carry their derivation (2026-08-28).** Never a bare "$X/mo" — each figure shows the unit price and the volume it is multiplied by (e.g., "Cloud Tasks: first 1M ops free, $0.40/M after; ~90 chunk tasks/mo = $0"), so any number in the doc can be defended on the spot.
- **Breathing room — the anti-congestion rule (2026-08-29).** Dense nested bullet walls read as badly as paragraphs. Rules:
  - One idea per bullet, one line where possible — a bullet is a sentence, not a stanza.
  - Blank line between logical groups of bullets; blank line before and after every heading, table, and code block.
  - Nesting stops at two levels in the body (three only inside Supplementary deep-dives). Deeper means the content wants a sub-heading or a table instead.
  - Prefer a short table over five parallel bullets saying "X — because Y".
  - Simple words: "use", not "leverage"; "start", not "initiate". If a simpler word exists, the simpler word wins.
  - Read-aloud test: every bullet should be sayable in one breath.
- **Companion doc — one per module doc (2026-08-29, expanded same day).** Every module doc gets a sibling file named `<module-doc-name>-concepts.md` with two jobs: (a) the concepts/vocabulary section below, and (b) the module's entire `# Supplementary material` (per §4.3). The module doc stays pure design.md template, is the file shared with the team, and **never references the companion** (see §4.3); the companion is the author's local prep + deep-dive material. One companion per module doc — this is a standing decision. Concepts part:
  - It explains, from zero, every service, tool, and concept the module doc names: what it is, what problem it solves, how it behaves here (e.g., "What is Cloud Tasks — is it a queue? How does it retry?", "What is a dead-letter queue?", "What is SFTP?").
  - Entries are short and self-contained: 3–8 bullets each — definition, how it works, the one or two properties the design actually leans on, and any limit worth quoting (numbers included).
  - It carries NO design decisions — it explains vocabulary; the module doc owns the design. A term explained in the companion may be used freely in the module doc with only its usual short bracket gloss.
  - Written (or updated) whenever its module doc is written or materially revised; listed alongside the doc in §9's checklist.
- **Approach ↔ Contracts cross-links (2026-08-28).** Every Approach step that has a concrete call ends with a "Payloads: *Contracts → lifecycle step N*" pointer; the Contracts lifecycle intro carries the reverse mapping. Readers can jump from behavior to payload and back.

### 4.1 Top of the doc (the permitted extras above the template body)

1. ~~Header line + metadata table~~ — **REMOVED 2026-08-28** (presentable-doc rule above): no artifact line, no Module/Author/Status/Evidence table. The doc opens with the title, then Purpose and scope. Status/approval tracking lives in this instruction file's checklist, not in the presented doc.
2. **Purpose and scope** — what the module owns / does not own / neighbors (Dev's explicit keep-at-top). *(The earlier "Recommended approach at a glance" box requirement was removed on 2026-08-28 — the design.md Approach section is the summary now.)*

### 4.2 Body — the template sections, in template order

One **section triage table** first (template rows only, one-line reason for every skip), then:

| Section | Our status | Content mapping |
| --- | --- | --- |
| Approach | Always | The chosen design as numbered steps, **each step written as bullets, never paragraph blocks**: a bold one-line step title, then sub-bullets — **What** (the change, plain words), **Why** (traced to v2 §/D2/decision or evidence), **Why not `<the alternative a reviewer would ask about>`** (the strongest counter-argument, answered inline with its evidence), and **What if it fails** (2–3 brief bullets on the step's failure behavior, pointing to *Failure modes and rollback* for the full treatment — Dev directive 2026-08-28: the failure story must be readable inline with the step, not only in the dedicated section). Each step must be self-contained enough to present live without jumping to other sections. Contains the `### Decisions carried in from review` subsection. |
| Alternatives considered | Always | **Self-contained per alternative (2026-08-29)** — the shared doc must let a reader understand and judge each alternative without any other file. Each alternative gets: **What it is / how it would work** (2–3 bullets), **Pros** (explicit list), **Cons** (explicit list), **Why rejected** (one direct statement comparing it to the chosen approach on the deciding points), and its **cost line**. Never a bare name with two bullets. The per-criterion weightage tables, failure modes, and the comparison table stay in the companion. |
| Components and files touched | **Skipped** | Dev's directive — still discussion phase, no implementation file lists. One-line triage reason: "skipped — pre-implementation discussion doc." |
| Contracts and interfaces | Always in practice | Endpoints, payloads, manifests, envelopes, event contracts — every module so far has these. **Mandatory subsection (2026-09-01): "Configuration and flags"** — one table listing every tenant configuration key and every feature flag the module reads or introduces: key/flag name, where it lives (e.g. `attestation-module-config` entry, Flagsmith flag), type, default, and what behavior it controls. A config key or flag used anywhere in the doc but missing from this table is a finding. A module that uses no feature flags states so explicitly ("No feature flags") rather than omitting the topic. Doc 5 still owns the consolidated JSON schema (§8.1); this subsection is the per-module declaration that feeds it. |
| Data model and migration | Always in practice | The table/schema *contracts* this module defines (final DDL stays with doc 6). **Tables inventory first (2026-09-01):** the section opens with an explicit inventory, before anything else, of (a) every table the module **creates** (new tables it owns) and (b) every table it **interacts with** (reads or writes but does not own, with the owning doc/system named). A module that creates no tables still lists what it interacts with — the inventory is never omitted. A table used anywhere in the doc but missing from this inventory is a finding. |
| Security, privacy, and access | Always | Tenant isolation, identities/keys, trust boundaries, PHI handling — or the explicit "no security review required" statement with reasons. |
| Performance and scale | Always in practice | The NFR numbers (volumes, cadence, budgets) that also feed the cost estimates. |
| Observability | **Always (Dev's keep)** | Metrics, alerts + thresholds, correlation, stuck-work detection; telemetry never blocks work. |
| **Audit trail** | **Always (deliberate addition to the template, placed right after Observability, noted in the triage table as an added section)** | Events, payloads, same-transaction rule, 7-year retention, which audit questions the module answers. **Implementation-ready contracts (2026-08-30):** the section defines the common event envelope once (id, type, tenant, identity fields, run/task correlation ids, actor, timestamp, detail) plus a per-event detail contract — the exact fields each event carries and the transaction it commits in. The rule: an engineer implementing from the doc must never have to invent an audit field; if a field would need inventing, the design is incomplete. **Table name first (2026-09-01):** the section's opening lines state, before anything else, the exact name of every audit table the module writes to (e.g. `attestation_audit_events`) and whether the module creates it or writes to one owned by another doc — a reader must never have to infer where the audit rows physically live. |
| Failure modes and rollback | **Always (Dev's keep)** | The edge-case table (case → handling → where) lives here, plus a **Rollback** paragraph (kill switch, what stops, what already-written data explicitly stays). |
| Test strategy | **Skipped or condensed** | Dev's directive — skip with a one-line triage reason, or a short list of the decisive test cases when genuinely relevant. No per-requirement matrix at this phase. |
| Rollout | **Always (Dev's keep)** | Flags/kill switches, ordering, backfill sequencing, cross-team coordination, DevOps handoffs. |

### 4.3 Supplementary material — lives in the COMPANION doc, not the module doc (changed 2026-08-29)

The module doc contains ONLY the design.md template body — it is the shareable artifact. Everything beyond the template moves to the module's companion file `<module-doc-name>-concepts.md`, which holds two parts:

1. **Concepts** (the vocabulary section defined in the writing-style rules) — first.
2. **`# Supplementary material`** — second, in the order below.

**The module doc must NEVER mention the companion** (added 2026-08-29) — no "see the companion doc" pointers, no filename references, nothing that hints a second file exists. The shared module doc reads as fully self-contained; the companion is the author's private preparation material, and a pointer to it only invites "where is that doc?". Where the body condenses something analyzed in depth (an alternative, an engine detail), it simply states the conclusion and the deciding reasons — no pointer. All writing-style rules (bullets, anti-congestion, plain language, no names/review framing) apply to BOTH files.

Supplementary material order (in the companion):

1. **Baseline — verified current behavior** ("What CertifyOS has today"): numbered claim/evidence rows with file:line receipts, ⚠ corrections to earlier claims, a closing consequence line.
2. **Approaches analyzed in depth**: the decision criteria (stated before the approaches), then every approach — chosen and rejected alike — with the SAME full treatment. Mandatory per approach (2026-08-29 — no alternative may be name-dropped without all of these):
   - **How it works** — enough that a reader could implement it.
   - **Pros** and **Cons** — explicit bullet lists, never prose asides.
   - **Per-criterion weightage** — score the approach against each numbered decision criterion (a short table or one line per criterion), so "why it lost" is arithmetic anyone can check, not taste.
   - **Why it loses to the chosen approach** — one direct statement naming the deciding criteria.
   - **Failure modes / what-would-break-it.**
   - **Cost estimate with its derivation** (unit price × volume, sized to the NFRs) — GCP-current-stack alternative always included; AWS variant where a genuine infrastructure choice exists.
   The comparison table closes the section with a cost row.
3. **Requirements traceability**: the functional/non-functional tables traced to v2 §/D2 numbers.
4. **Context from previous module docs**: inherited contracts (only from §9-ticked docs) and exports to later docs.
5. **Design rationale — anticipated questions**: question-and-answer blocks written as timeless FAQ (no review dates, no names, per the presentable-doc rule) — the presentation defense material.
6. **Open questions and sign-offs**: numbered, each with an owner.
7. **Ticket impact**: which DA-xx tickets change, plus new tickets.

### 4.4 What we do NOT adopt from the skill

The skill's *workflow* is ticket-scoped and does not apply here: no Jira-key spec dirs, no repo-resident `specs/` layout (these docs live in `cos-docs/`), no tier rationale, no Gate 2 checklist, no Confluence auto-publish step. We adopt its **template structure, section order, triage discipline, and house style** only. Codebase verification (its Phase 3/5 evidence rules) we already do — every claim with receipts, adversarial check before writing.

### 4.5 Retrofit note

Docs 1–4 were written to the earlier 13-section skeleton and contain all the same content. They are restructured to this format on Dev's go-ahead (content unchanged — sections reordered and re-labeled, deep-dive material moved to Supplementary material). Docs 5–8 are written in this format from the start.

## 5. Non-negotiable requirements (apply to every module, every approach)

These come from v2 and the audit/observability brief. An approach that cannot satisfy one of these **without future limitations** is disqualified regardless of its other merits — say so in its cons.

1. **Idempotency everywhere.** Deterministic identities + database uniqueness constraints, never application memory. Retries, duplicate events, double-clicks, and replays must never create a duplicate business effect.
2. **Full audit trail.** Append-only, written transactionally with the state change it describes, 7-year retention (D2-20, final), able to answer "what changed, why, who acted, when, from which source, and what result reached the Golden record" without reconstruction. The audit trail is business state, not logs.
3. **Observability.** Correlation identifiers carried end to end (task → submission/batch row → staged item → review action → release → Golden-record outcome → client export); metrics and alerts for queue depth, aging, disposition counts, DLQ depth, stuck work. Telemetry is best-effort and never blocks the workflow.
4. **Tenant isolation server-side on every path** — queries, jobs, events, reports, replays, exports. UI filtering is never a boundary.
5. **Deterministic identity** (`tenant_id + practitioner_id + due_period`) computed once by the backend and reused unchanged everywhere (D2-12). Physical column convention: the practitioner leg is stored as `certify_practitioner_id` (the platform's name for a column referencing the OV's `certify_id` — matches `credentialing_workflows`/`monitoring_workflows`); never `practitioner_id` or bare `certify_id` on our tables.
6. **Source-agnostic design.** Vendor-specific knowledge lives only in adapters/configuration; no vendor name in a table, topic, class, or API name. Candor is adapter #1 (D2-11).
7. **Sync Latest is the only release path** (D2-01); nothing upstream of the sync worker ever writes toward the Golden record.
8. **Quarantine over guessing.** When a module cannot safely decide (dependency outage, ambiguous scope, unknown schema version), it parks the work for retry or audited operator replay — it never mislabels and never loses a row.
9. **Nothing silently discarded.** Every input gets a counted, audited disposition; batch counts reconcile.
10. **Golden-record engines are extend-only** (D2-14). No module reimplements survivorship, merge, dedup, or termination cascade logic.
11. **ISO 8601 dates everywhere in contracts and data models (2026-08-30).** Payloads, columns, and ids use `YYYY-MM-DD` (timestamps `YYYY-MM-DDThh:mm:ssZ`); filenames use the compact form `yyyyMMdd`; batch-id month legs use `yyyy-MM`. Human-facing formats (e.g. MM/DD/YYYY for US display) are the consumer's rendering concern — never stored, never in a contract.

## 6. Decided constraints module docs must NOT reopen

The full register is v2 §7; the ones module authors are most likely to bump into:

- **The external-vendor program is decoupled from attestation (D2-38, 2026-09-08):** vendor NPIs come from a tenant-configured query, not the attestation window; vendor rows never bind to tasks, never touch the clock, and are never joined to attestations in review. The three constraints below now bind the standalone external-source-ingestion doc, not the attestation series.
- Manifest-last delivery; ingestion triggers on the manifest, never the data file (D2-04). *(External-source doc.)*
- Event → durable queue → batch registry → idempotent worker ingestion shape (D2-05). *(External-source doc.)*
- Rejected-recommendations memory is a lookup table, matches suppressed from the queue in MVP with audit kept (D2-07). *(External-source doc.)*
- NO_CHANGE records are reviewer-visible; task SUBMITTED ≠ workflow complete; reviewer acknowledges NO_CHANGE (D2-09; the weighed-against-vendor-rows rationale is superseded by D2-38).
- Manual assignment in MVP (D2-10). Rule-based later = backend extension.
- Provider admin is tenant-scoped only, no group-level association (D2-17).
- Default source ranking `portal-attestation` > `candor` > roster/UI, per-tenant config (D2-18).
- 7-year audit retention, final (D2-20). T+1 final email; overdue tasks stay submittable (D2-23).
- Client export: attested practitioners only, 30-day cadence (D2-13).
- SFTP: we design, DevOps builds and operates (D2-30).

### 6.1 Architecture direction — microservices (approved 2026-09-01)

**The Attestation Module is built as its own service(s), the microservices way.** Approved decision; module docs comply and do not reopen it.

- The design is **not constrained by the current architecture** (`api-layer` monolith, existing deployment shapes). Each module doc chooses the best feasible design on its own merits.
- "Build inside `api-layer`" is no longer a candidate approach — where earlier docs or the architecture brief recommend reusing the monolith's rails, that recommendation is superseded. Reuse of existing *platform* capabilities (OV data layer, Golden-record engines per D2-14, existing egress rails) is still expected where they are the right tool — this decision is about service boundary and deployment, not about rewriting the platform.
- Still open (module docs decide): the service decomposition itself (one attestation service vs. several — e.g. separate ingestion worker), inter-service communication, datastore per service, deployment topology. §5 non-negotiables and the v2 decision register still bind.
- Docs already ticked in §9 (cycle, portal-lane, sftp-exchange) are re-checked against this decision; needed changes flow through the §8.1 amendment process (explicit flags, amend after review) — never silent edits.

What module docs **are** free to decide (this is the whole point of them): datastore choice (e.g., Spanner vs. MongoDB vs. PostgreSQL for the Attestation Module database), service decomposition within the §6.1 microservices direction, language/runtime, queue/eventing technology, schema and contract details, adapter design, concrete file/manifest formats, indexing and partitioning, deployment topology — provided §5's non-negotiables hold and v2's decisions are respected.

## 7. Module-specific questions each doc must answer

Beyond the template, each doc has known hard questions. Minimum list — extend as discovered:

**Backend** — service decomposition under §6.1 (one attestation service or several; deployment, scaling, blast radius, on-call)? How are the state machines enforced (conditional versioned updates)? Where does the sync worker run and how does it verify outcomes against the OV (verified: survivorship returns 200 on internal error — verification must read back)? How is the outreach service triggered? API surface for Portal, ingestion, and UI.

**Database + Data Layer** — Spanner vs. MongoDB vs. alternatives, argued against the actual access patterns (queue queries with filters/counts/aging, append-only audit, exact-match rejected-recommendations lookups, assignment concurrency); schema for every table v2 names; indexing/partitioning per tenant; how the data layer enforces tenant scoping, validation, and audit stamping; migration/versioning strategy.

**External-source ingestion service (standalone doc, D2-38)** — the tenant-configurable selection query: language/format, storage in tenant configuration, validation, injection safety (v2 O-17); export job and cadence configuration; SFTP transport on the shared platform (per-vendor identities, events, archive, sweep); the proposed Candor contract, both directions (columns, enums, formats, schema versioning) — the O-1 internal proposal; adapter architecture and how a second vendor onboards; disposition implementation and count reconciliation; rejected-recommendations lookup mechanics; **skip** semantics and cross-cadence re-send policy (v2 O-18); its own staging store and reviewer surface, and whether Sync Latest machinery is shared with the attestation module or its own (v2 O-19).

**Cycle** — backfill mechanics on the core practitioner table and the stagger; scheduler cadence and window query; where the deterministic identity is computed and stored; clock rules implementation (D2-28); how duplicate-task prevention is enforced under concurrency.

**Portal lane** — prefill source and snapshot-version mechanism (content hash rationale from v1); delta + metadata envelope; first-writer-wins enforcement; tokenless entry route + portal login (D17/D2-35); non-PDM client routing (D2-21).

**UI** — framework/hosting (extend `apps/web` vs. standalone); queue data contract and pagination under tenant scale; manual assignment mechanics without double-assignment; side-by-side cross-lane view; Sync Latest status surfacing; bulk-action result rendering.

**Client Export** — trigger (sync-complete event) vs. 30-day cadence reconciliation; attested-only selection; format and delivery channel; relation to existing egress rails.

## 8. Working process

1. One module doc at a time, **in the §3 workflow order** — cycle/backfill first, then down the spine — unless there is a stated reason to deviate.
2. Start each doc by re-reading: v2 (the relevant §6.x + the D2 register), this instruction file, **every previously completed module doc (per the §9 checklist)**, the two briefs' relevant sections, and v1's evidence for that module (§19, §6B).
3. **Each doc references its predecessors** in its "Context from previous module docs" section (Supplementary material, §4.3 item 4): the contracts they decided (schemas, events, identities, APIs) are inputs to this doc, not things to re-decide. A genuine conflict with a predecessor is flagged explicitly as a proposed change to that doc — never silently diverged from.
4. Analyze the actual codebase where the incumbent approach is being evaluated — verify claims, don't inherit them (the original v1 evidence pass showed how much verification uncovered).
5. Write to the §4 structure (design.md body + Supplementary material). Decision criteria before approaches; cost estimate on every approach; Audit trail and Observability as body sections; Failure modes and rollback + Rollout always present; Components/files and Test strategy skipped or condensed with a triage reason.
6. Finalize → review with the owning team → **tick the §9 checklist** → update the affected Jira tickets (DA-xx) → then start the next module.
7. Per the phase-review working agreement: stop after each document for Dev's review before starting the next.

### 8.1 Cross-document compatibility check (added 2026-08-30)

The module docs are pieces of **one system** — the v2 §6 end-to-end workflow. Before a module doc is finalized (and before it is ticked in §9), run an explicit compatibility pass against **every previously ticked doc** and against the v2 workflow. The pass walks each shared surface:

- **Tables and data models** — a table another doc owns is used with its exact column names, grain, states, and unique keys. A later doc never invents a column, state, or enum value the owning doc does not define; a needed new field is a **proposed amendment to the owning doc**, not a local addition.
- **Contracts and payloads** — every payload a doc consumes was defined by a predecessor with the same field names and shapes; every payload a doc produces names its consumer. Worked examples (dates, ids, math like "submission + 90") must agree across files.
- **Lifecycle handoffs** — each doc's last step is some doc's first step; walking v2 §6 Stage 0 → Stage 6, no step is unowned. An unowned transition (nobody flips a state, nobody triggers a job) is a finding, not a footnote.
- **Audit envelope and correlation ids** — new correlation fields extend the shared envelope explicitly; doc 6 collects the union. No module redefines the base fields. The same carry-forward applies to the shared `attestation-module-config` entry: each doc adds and lists its own keys; doc 5 owns the consolidated JSON schema.
- **Naming** — the §5 conventions (`certify_practitioner_id`, "next attestation date" never "due date", identity tuple) hold identically in every file, module docs and companions alike.

**Conflict flagging rule:** when a later doc discovers that a predecessor's data model or contract will not work (missing column, wrong grain, impossible query, unowned transition), it never silently redesigns or diverges. It records an explicit flag — `⚠ Proposed change to doc N: what breaks, why, the proposed amendment` — in its own text (and in its companion's open questions), and the predecessor is amended **only after review**. After any amendment, re-run this check on the docs written in between, since their contracts may have inherited the amended shape.

## 9. Completion checklist

Tick each entry (change `[ ]` to `[x]`, add the date) when that module doc is finalized and reviewed. Later docs may reference **only ticked files** as settled context; an unticked file is still a draft and its contracts are not yet inputs.

- [x] 1. `modules/01-cycle/cycle.md` — finalized and approved by Dev 2026-08-27 (chunked fan-out on the AutoRecred pattern; identity = tenant + practitioner + due date); **amended and re-approved 2026-09-07** (outreach handoff via the Smart Outreach Service: D12 superseded, D14–D16 added, contracts 0b/4b/5/6, audit events, rollout)
- [x] 2. `modules/02-portal-lane/portal-lane.md` — finalized and approved by Dev 2026-08-27 (generic attestation-task APIs on the backend; non-PDM = direct consumers; snapshot_version mechanics)
- [x] ~~3. `modules/03-sftp-exchange/sftp-exchange.md`~~ — **retired 2026-09-08 (D2-38)**; was approved 2026-08-27 and rewritten 2026-09-05 (pending review). Draft kept as source material for the external-source-ingestion doc.
- [ ] ~~4. `modules/04-ingestion/ingestion.md`~~ — **retired 2026-09-08 (D2-38)**, never ticked. Draft kept as source material for the external-source-ingestion doc.
- [ ] 5. `modules/05-backend/backend.md`
- [ ] 6. `modules/06-database-data-layer/database-data-layer.md`
- [ ] 7. `modules/07-ui/ui.md`
- [ ] 8. `modules/08-client-export/client-export.md` *(or confirmed as folded into the ingestion/outbound doc — note which)*

**Related standalone design docs (not part of the attestation series; same template and house style):**

- [ ] `cos-docs/platform/external-source-ingestion/external-source-ingestion.md` (+ `-concepts.md`) — the **external-source-ingestion service (Candor program)**, decoupled from the attestation workflow 2026-09-08 (D2-38). One doc covering the whole vendor workflow: tenant-configurable selection query, scheduled export, SFTP transport, ingestion + dispositions, rejected-recommendations lookup, staging, review (approve/reject/skip), Sync Latest release under `candor:{tenantId}`. To write; source material = the retired `modules/03-sftp-exchange/` and `modules/04-ingestion/` drafts plus `reference/candor-*` files. Open inputs: v2 O-1, O-17, O-18, O-19.

- [ ] `cos-docs/platform/smart-outreach-service/smart-outreach-service.md` (+ `-concepts.md`) — the Smart Outreach Service, a standalone domain-blind email delivery service consumed by the Attestation Module (and any other producer) over Pub/Sub commands and outcomes. Drafted 2026-09-05. **Cycle doc (doc 1) amended 2026-09-07 per §8.1** (D12 superseded; D14–D16 added; Approach steps 5–6, Contracts 0b/4b/5/6, reminder contract, data model, audit events, failure modes, rollout): reminders are `SCHEDULE_SENDS` / `CANCEL_SENDS` commands to this service, practitioners are synced with `UPSERT_RECIPIENT`, and the `OUTREACH_*` audit events are written from outcomes. Doc 5 (backend) inherits the relay, the recipient sync, the outcome consumer, and template registration.
