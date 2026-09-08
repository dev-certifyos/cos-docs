# Crosswalk Field Guide

> Everything the CertifyOS PDM platform means by "crosswalk" — the healthcare context, the Spanner data model, how the ID is generated, and how it threads through ingestion, cleansing, matching, survivorship, and the API layer. Every section opens with a worked example, then explains what you just saw.
>
> Sources: `fe-api-coredal` (api-layer, core-data-access-layer), `dal-mdm-matching-layer`, `dal-mdm-cleansing-layer`, `dal-mdm-survivorship-layer`. Compiled 2026-08-24. All IDs in examples are illustrative.

**The running cast:** **Dr. Asha Patel**, a cardiologist with NPI `1093817465`, contracted with tenant **Acme Health** (`tenantId = tnt-acme-01`). Plus **Maple Pharmacy** (NCPDP `4823919`) and **Lakeside Imaging** (TIN `841234567`, NPI `1478523690`) at **120 Main St, Columbus OH 43215**.

## Contents

1. [The problem: one doctor, three systems](#1--the-problem-one-doctor-three-systems)
2. [What a crosswalk is, exactly](#2--what-a-crosswalk-is-exactly)
3. [The data model, as actual rows](#3--the-data-model-as-actual-rows)
4. [Crosswalk format per entity](#4--crosswalk-format-per-entity)
5. [How a crosswalk gets generated](#5--how-a-crosswalk-gets-generated)
6. [The pipeline: Dr. Patel end to end](#6--the-pipeline-dr-patel-end-to-end)
7. [Matching: how two slices become one person](#7--matching-how-two-slices-become-one-person)
8. [Cleansing: the crosswalk as lineage pointer](#8--cleansing-the-crosswalk-as-lineage-pointer)
9. [Survivorship: building the golden record](#9--survivorship-building-the-golden-record)
10. [API surfaces, with example calls](#10--api-surfaces-with-example-calls)
11. [Expert gotchas, each with a mini example](#11--expert-gotchas-each-with-a-mini-example)

---

## 1 · The problem: one doctor, three systems

**Example.** Dr. Asha Patel exists in three places, keyed three different ways, disagreeing about her own name:

| System | Their key for her | What they say |
|---|---|---|
| NPPES (CMS registry) | `NPI 1093817465` | ASHA PATEL, primary address from 2021 |
| Acme Health's roster CSV | row 4,812 of a spreadsheet | Asha R. Patel, current office, CAQH ID `16204531` |
| Acme's portal (manual edits) | their internal record | corrected phone number, updated specialty |

Question the platform must answer, thousands of times a day: *are these the same person, and if so, which value of each field wins?*

US provider data has no single authoritative registry. The same doctor exists simultaneously in **NPPES**, **CAQH**, state license boards, DEA registration, and every payer's rosters — each with its own keys, spellings, and staleness. Master Data Management (MDM) answers the question with three ideas:

- **Source record / slice** — one system's view of one entity (each row in the table above).
- **Crosswalk** — the mapping that says "this source record belongs to this real-world entity." Classic MDM products (IBM MDM, Reltio, Informatica) implement it as a literal join table between source-system IDs and a golden ID.
- **Golden record** — the single best composite, produced by *matching* (deciding slices are the same entity) and *survivorship* (picking the winning value per field).

The natural keys that make crosswalks work are healthcare identifiers: **NPI** for practitioners, **TIN** for organizations, **NCPDP ID** for pharmacies.

## 2 · What a crosswalk is, exactly

**Example.** After Acme's roster and NPPES are both ingested, `core_practitioners` holds **two rows for the one Dr. Patel**:

| certify_id | crosswalk_id | source (via source_id) | survivor_id | data (excerpt) |
|---|---|---|---|---|
| `cp-101` | `1093817465` | `nppes` | `NULL` | ASHA PATEL |
| `cp-102` | `1093817465` | `roster:tnt-acme-01` | `NULL` | Asha R. Patel, caqhProviderId 16204531 |

Same `crosswalk_id` (her NPI), different `source_id`. The pair `(source_id, crosswalk_id)` is unique — NPPES can only ever say one thing about NPI 1093817465. Re-ingesting the roster next week finds `(roster:tnt-acme-01, 1093817465)` already exists and *updates* cp-102 instead of creating a duplicate.

That's the whole trick: **there is no crosswalk table** on this platform. Every slice row carries a scalar column `crosswalk_id STRING(MAX) NOT NULL` — the record's natural key *inside its own source system*. The unique index `UNIQUE_CORE_PRACTITIONER_SOURCE_CROSSWALK_INDEX (source_id, crosswalk_id)` makes it the dedup contract for every ingestion path.

The canonical definition, from `api-layer/docs/CP-35725-facility-crosswalk-generation-guide.md`:

> "A crosswalk ID is the *natural key* of a core entity record… it answers the question *'is this incoming facility the same facility we already have?'* … If two paths compute **different** crosswalks for the same real-world facility, we create **duplicates**."

Two related-but-different things share the word:

| Thing | Lives on | Example value |
|---|---|---|
| `crosswalk_id` | every `core_*` slice row | `"1093817465"` — natural-key *string* |
| `contributing_crosswalks` | every `core_*_ov` golden row | `["cp-101","cp-102"]` — **certify_ids** of merged slices, not crosswalk strings |

> ⚠️ **Naming trap.** Despite the name, `contributing_crosswalks` holds **certify_ids**. Survivorship builds it as a copy of `contributingSlices` (`AbstractSurvivorshipService.java:1816`); every DAL join treats it as entity IDs: `ARRAY_INCLUDES(pov.contributing_crosswalks, tp.certify_practitioner_id)`. Never join it against crosswalk strings.

## 3 · The data model, as actual rows

**Example.** The three tables involved in Dr. Patel's story, after the full pipeline has run (spoiler for §6):

```sql
-- core_sources: registry of where data comes from
source_id  source_type            tenant_id
src-nppes  nppes                  NULL
src-ros-1  roster:tnt-acme-01     tnt-acme-01
src-ten-1  tenant:tnt-acme-01     tnt-acme-01
src-clean  certify-cleanser       NULL

-- core_practitioners: one row per (source, entity) = a "slice"
certify_id  crosswalk_id  source_id  survivor_id  data
cp-101      1093817465    src-nppes  NULL         {"npi":"1093817465","firstName":"ASHA",...}
cp-102      1093817465    src-ros-1  cp-101       {"npi":"1093817465","firstName":"Asha",...}
cp-103      1093817465    src-ten-1  cp-101       {"phone":"+16145550142",...}

-- core_practitioners_ov: the golden read model, one row per (tenant, entity)
certify_id  tenant_id    root_id  contributing_crosswalks           data
cp-102      tnt-acme-01  cp-101   ["cp-101","cp-102","cp-103"]      {merged best-of fields}
```

Reading that top to bottom:

- `survivor_id` is the **merge pointer** — a self-FK. cp-102 and cp-103 point at cp-101, forming a *merge tree* with cp-101 as root. Unmerge just sets it back to NULL.
- The OV (operational value) row is the golden record. Its PK `certify_id` is the tenant/roster slice with the *oldest createdAt* (here cp-102), `root_id` is the tree root, and `contributing_crosswalks` lists every slice that fed it.
- A hidden generated column `contributing_crosswalks_tokens TOKENLIST AS (TOKEN(contributing_crosswalks))` backs a Spanner search index per OV table, partitioned by tenant — that's what makes "which OV row contains slice cp-103?" fast.
- Property graphs (`PractitionerMergeGraph` etc.) expose `crosswalk_id` as a node property and traverse the merge tree over `(survivor_id, certify_id)` edges.
- `MdmCrosswalk` (`dal/mdm/common/model/MdmCrosswalk.java`) is just a projection record `(certifyId, survivorId, crosswalkId, sourceId, createdAt, updatedAt)` over these columns — not a table.

Tables carrying `crosswalk_id`: `core_practitioners`, `core_facilities`, `core_locations`, `core_entity_addresses`, `mdm_groups`, `core_providers`. Tables carrying `contributing_crosswalks`: the five `*_ov` read models.

## 4 · Crosswalk format per entity

| Entity in our cast | Its crosswalk_id | How it was built |
|---|---|---|
| Dr. Patel (practitioner) | `1093817465` | NPI, verbatim |
| Lakeside Imaging's billing group | `841234567:1478523690` | `{tin}:{npi}` |
| Maple Pharmacy (facility) | `4823919` | NCPDP ID (pharmacy short-circuit) |
| Lakeside Imaging (facility, default) | `841234567:1478523690` | fallback `tin:npi` |
| 120 Main St (entity address) | `9f3ac27b41…e71b` | SHA-256 of `addressLine1\|addressLine2\|city\|state\|zip\|addressType` → `"120 Main St\|Suite 4\|Columbus\|OH\|43215\|service"` |
| "Lakeside Imaging Downtown" (location) | `Lakeside%20Imaging%20Downtown_9f3ac27b41…` | `{encodedName}_{addressHash}` |
| Group created in portal, no NPI/TIN | `b7e1c2d4-…-uuid` | random UUID |
| Dr. Patel's *cleansed* slice | `cp-102` | original record's certifyId (see §8) |

Notice the pattern: wherever a healthcare identifier exists, it *is* the crosswalk (NPI, TIN:NPI, NCPDP). Where none exists (addresses, locations), the crosswalk is a **fingerprint of the content itself** — hash the standardized fields, and identical addresses collide into the same key on purpose. That collision is the dedup.

## 5 · How a crosswalk gets generated

**Example — facility precedence chain.** Acme uploads a roster with two facilities. `EntityCrosswalkGenerator` walks the precedence chain for each:

```text
Row 1: {name:"Maple Pharmacy", facilityType:"Pharmacy", ncpdpId:"4823919", tin:"911112222"}
  1. Pharmacy NCPDP?  facilityType=="Pharmacy" AND ncpdpId exists → YES
     crosswalk = "4823919"                                   (stop, no fallback)

Row 2: {name:"Lakeside Imaging", tin:"841234567", npi:"1478523690", externalId:"LKS-042"}
  1. Pharmacy NCPDP?  → no
  2. Tenant templates? Acme's crosswalk-generation-config:
       fallbacks: [
         {template:"{externalId}_{addressHash}",
          when:{all:[{field:"userDefinedFields.udfCredentialedType",equals:"location"}]}},
         {template:"{tin}:{npi}"}
       ]
     rule 1: udfCredentialedType != "location" → skip
     rule 2: no when-clause → matches
     crosswalk = "841234567:1478523690"
```

Preview without writing anything: `POST /facilities/crosswalk-preview` returns which rule fired, the resulting crosswalk, and the active template list.

All generation is centralized in `api-layer/.../common/generator/EntityCrosswalkGenerator.java` — deliberately one entry point so roster CSV, portal UI, and public API can never compute different keys for the same facility. The full facility precedence:

1. **Pharmacy NCPDP** — kill switch `roster.facility.pharmacy-ncpdp.enabled`.
2. **Tenant templates** — ordered `coreFacility.fallbacks` rules; each has a `template` and an optional `when` clause (`equals / notEquals / exists / in / all / any`). First match wins; no match falls through, never errors. Kill switch `roster.facility.crosswalk-templates.enabled`. Special tokens: `{addressHash}`, `{randomHash}` (16 hex; also the crosswalk for entity phones); anything else is a dot-path into the payload.
3. **Legacy flags** — `data-ingestion-config`: `isFacilityExternalIdAsCrosswalk` / `isAddressHashAsCrosswalk` (mutually exclusive; externalId wins).
4. **Default** — `tin:npi`.

At ingestion, the dedup FIND filters on `crosswalkId + sourceType` (`roster/service/ResourceConfiguration.java`); facilities outside NCPDP/template/externalId modes fall back to matching on `data.npi` + `data.name`.

> ⚠️ **Rule.** **Updates never regenerate the crosswalk.** Example: Acme edits Lakeside Imaging's address in the portal. The stored crosswalk `841234567:1478523690` is prefetched and reused verbatim — if it were re-derived from the new address under an `{addressHash}` template, the record would silently re-key and the next roster upload would create a duplicate. Corollary: changing a live tenant's crosswalk format is a data migration, not a config flip.

## 6 · The pipeline: Dr. Patel end to end

### The simplest technical version — one roster row, three services, six steps

Starting state: `core_practitioners` already holds Dr. Patel's NPPES slice `cp-101`. Acme uploads a roster CSV containing one row: `Asha Patel, NPI 1093817465`. Follow that row.

**Step 1 — INGEST** (api-layer → core-data-access-layer). api-layer computes the crosswalk (practitioner rule: crosswalk = NPI) and upserts:

```text
POST /core/practitioner/roster:tnt-acme-01/1093817465   body: {data:{firstName:"asha r.", lastName:"patel", npi:"1093817465"}}

DAL: SELECT ... WHERE source_type='roster:tnt-acme-01' AND crosswalk_id='1093817465'
     → not found → INSERT

core_practitioners now:
  cp-101  crosswalk=1093817465  source=nppes               survivor_id=NULL
  cp-102  crosswalk=1093817465  source=roster:tnt-acme-01  survivor_id=NULL   ← new
```

**Step 2 — CLEANSE** (dal-mdm-cleansing-layer). Slice-changed event for cp-102 hits `POST /mdm/slices/changed`. Cleanser standardizes `"asha r."` → `firstName:"Asha", middleName:"R"`, writes only the changed fields as a *new slice*, then merges it under the original:

```text
POST /core/practitioner/certify-cleanser/cp-102          ← crosswalkId in URL = original's certifyId
POST /core/practitioner/certify-cleanser/cp-102/merge    body: {survivorSourceType:"roster:tnt-acme-01",
                                                                survivorCrosswalkId:"1093817465"}
core_practitioners now:
  cp-101  crosswalk=1093817465  source=nppes               survivor_id=NULL
  cp-102  crosswalk=1093817465  source=roster:tnt-acme-01  survivor_id=NULL
  cp-201  crosswalk=cp-102      source=certify-cleanser    survivor_id=cp-102   ← new
```

**Step 3 — MATCH** (dal-mdm-matching-layer).

```text
POST /record-processing/process   {certifyId:"cp-102", recordType:"PRACTITIONER"}

BLOCKING   compute cp-102's keys: npi-exact=1093817465, lastname-soundex=P340, crosswalk-id-exact=1093817465
INDEXING   store keys in core_practitioners_index
MATCHING   index lookup by shared keys → candidate cp-101
           Drools: NPI exact ✓ + first/last name Soundex ✓
           → MatchAssessment(subject=cp-102, candidate=cp-101, MATCH, confidence 0.95)
```

**Step 4 — MERGE** (matching-layer → DAL). RootSelector: source priority `nppes:0` beats roster (default 100) → cp-101 is survivor. Child's crosswalk goes in the URL, survivor's in the body:

```text
POST /core/practitioner/roster:tnt-acme-01/1093817465/merge
     body: {survivorSourceType:"nppes", survivorCrosswalkId:"1093817465"}

DAL: UPDATE core_practitioners SET survivor_id='cp-101' WHERE certify_id='cp-102'

merge tree now:   cp-101  ←  cp-102  ←  cp-201        (arrows = survivor_id)
```

**Step 5 — SURVIVE** (dal-mdm-survivorship-layer).

```text
POST /events/ov/execute   {payload:{entityType:"practitioner", changedTreeIds:["cp-102"]}}

1. load full merge tree via graph:      {cp-101, cp-102, cp-201}
2. per-field source ranking (Drools):   npi       ← cp-101  (nppes ranked 1 for npi)
                                        firstName ← cp-201  (cleanser-of-roster ranked 2, beats roster 3, nppes 998)
3. write golden row:

core_practitioners_ov:
  certify_id=cp-102  tenant_id=tnt-acme-01  root_id=cp-101
  contributing_crosswalks=["cp-101","cp-102","cp-201"]        ← certify_ids of the tree
  data={npi:"1093817465", firstName:"Asha", ...}

4. publish OVCompletionPayload to Pub/Sub (carries each slice's real crosswalkId + sourceType)
```

**Step 6 — CONSUME** (api-layer reads via DAL).

```sql
-- caller knows the OV id:
SELECT * FROM core_practitioners_ov WHERE tenant_id='tnt-acme-01' AND certify_id='cp-102'

-- caller only knows a slice id (e.g. cp-201): PK miss → array fallback
SELECT * FROM core_practitioners_ov
 WHERE tenant_id='tnt-acme-01' AND ARRAY_INCLUDES(contributing_crosswalks,'cp-201') LIMIT 1

-- joins fan out from the golden row to tenant data:
FROM core_practitioners_ov cpo, UNNEST(cpo.contributing_crosswalks) AS crosswalk_id
JOIN tenant_practitioners tp ON tp.certify_practitioner_id = crosswalk_id
```

That's the whole machine: **crosswalk_id deduplicates within a source (steps 1–2), survivor_id links across sources (steps 3–4), contributing_crosswalks records what the golden record was built from (steps 5–6).**

### Summary flow (with the extra tenant slice cp-103 included)

1. **Ingest** (api-layer → DAL): roster row → crosswalk `1093817465` → `POST /core/practitioner/roster:tnt-acme-01/1093817465`. FIND misses → new slice `cp-102`. Optimistic locking keys partial updates on `sourceType + crosswalkId`.
2. **Cleanse** (dal-mdm-cleansing-layer): `POST /mdm/slices/changed` standardizes cp-102's fields; changed fields upserted as slice `cp-201` with **crosswalk_id = "cp-102"**, merged back under cp-102.
3. **Match** (dal-mdm-matching-layer): `POST /record-processing/process` → BLOCKING → INDEXING → candidate cp-101 via shared blocking keys → Drools NPI + Soundex → MATCH 0.95.
4. **Merge**: RootSelector picks nppes (priority 0) → merge call → `survivor_id='cp-101'` on cp-102.
5. **Survive** (dal-mdm-survivorship-layer): `POST /events/ov/execute` walks tree {cp-101, cp-102, cp-103, cp-201}, per-field source priority, writes OV row for tnt-acme-01, publishes `OVCompletionPayload`.
6. **Consume**: OV by PK, fallback `ARRAY_INCLUDES(contributing_crosswalks, …)`, joins fan out via `UNNEST` to tenant tables, workflows, networks.

DAL can also kick the whole thing by hand: `POST /mdm/practitioner/nppes/1093817465` — `CrosswalkResolverService` resolves crosswalk → certifyId (or fails `CROSSWALK_NOT_FOUND`) and feeds step 3.

## 7 · Matching: how two slices become one person

**Example — the rules firing, in salience order:**

```text
Subject: cp-102 {npi:"1093817465", firstName:"Asha",  lastName:"Patel", crosswalkId:"1093817465"}
Candidate: cp-101 {npi:"1093817465", firstName:"ASHA", lastName:"PATEL", crosswalkId:"1093817465"}

salience 400  NPI Mismatch Rule    both NPIs present, equal → pass through (no verdict)
salience 300  NPI Matching Rule    NPI exact ✓, Soundex(Asha)==Soundex(ASHA) ✓,
                                   Soundex(Patel)==Soundex(PATEL) ✓, middle empty → skip ✓
                                   → MATCH, score 1.0, confidence 0.95  ("practitioner-npi-rule-match")

Counter-example: candidate has npi:"1447312598" (different doctor, similar name)
salience 400  NPI Mismatch Rule    NPIs differ → NO_MATCH immediately, stop all rules.
```

And the crosswalk-specific rule — only for legacy sources:

```text
Subject from source "cbpsv" (migrated legacy system, no reliable name/DOB fields):
salience 200  CrosswalkId and Name Matching Rule
  gate: sourceType ∈ mdm.matching.classic-crosswalk.source-types  (prod: cbpsv, classic-dea)
  crosswalkId exact match → MATCH ("practitioner-crosswalkid-rule-match"),
  AttributeMatchScore("crosswalkId", EXACT, weight 1.0)
```

Structure behind the example:

- **Blocking** (`BlockingRulesetProvider.java`) narrows millions of rows to a candidate pool. Practitioner keys: `npi-exact`, first/middle/last Soundex, `crosswalk-id-exact`, `caqh-provider-id-exact`, `tenant-id-exact`. Crosswalk equality is itself a blocking key — same crosswalk always lands in the same pool. Groups/facilities/addresses block on TIN/NPI/zip/H3 geo-hash/street Soundex — no crosswalk key.
- **`crosswalkId` reaches the rules** because `CorePractitioner.getPipelineData()` injects it into the match payload alongside `data`.
- **Merge tree exclusion**: `MatchingCoordinator` drops candidates already in the subject's tree — never re-match what's merged.
- **Root selection**: `mdm.matching.root-selection.source-priority-map` = `nppes:0, cbpsv:1, classic-cds:1, classic-dea:1, classic-state-licenses:1`, default 100, deprioritized 1000. Lowest number wins; timestamps break ties. Merge semantics (`MergingStrategy.java:30`): *root's crosswalkId is the survivor in the body, child's crosswalkId goes in the URL path — root absorbs child.* Both-have-survivors or same-tree → skip; only-child-has-survivor → swap roles.
- **Tenant scoping**: sources with prefixes `roster, tenant, portal` additionally require tenant equality — Acme's roster slice never matches another tenant's roster slice.

## 8 · Cleansing: the crosswalk as lineage pointer

**Example:**

```text
Original slice:                       Cleansed slice written by the cleanser:
certify_id:   cp-102                  certify_id:   cp-201
crosswalk_id: 1093817465  (her NPI)   crosswalk_id: cp-102        ← original's certifyId!
source:       roster:tnt-acme-01      source:       certify-cleanser
data: {firstName:"asha r."}           data: {firstName:"Asha", middleName:"R"}
```

To find the cleansed view of any record: look up `("certify-cleanser", crosswalkId = cp-102)`. To go back: the cleansed record's crosswalk_id *is* the original's certifyId. The crosswalk column doubles as a bidirectional lineage pointer.

The cleanser (`dal-mdm-cleansing-layer`, `POST /mdm/slices/changed`) never mutates the original. It writes only *changed* fields as a sparse parallel slice, then immediately merges it under the original (`PractitionerCleansingService.java:262,343`). Idempotency check: a response where `certifyId == crosswalkId == survivorId` means "this is an already-merged cleansed slice, skip."

**Addresses get a dual-view policy** (`matching-layer docs/address-pipeline.md`), the only entity type that does:

- Matching/blocking/indexing *prefer cleansed*: `getEntityAddressPreferCleansed(certifyId)` tries `("certify-cleanser", crosswalkId=certifyId)`, falls back to the original. Standardized "120 Main Street" blocks better than raw "120 MAIN ST.".
- Merges *must target originals*: `getOriginalEntityAddressesResolvingCleansed(ids)` resolves any cleansed input back through its crosswalkId — max 2 queries, order-preserving.

## 9 · Survivorship: building the golden record

**Example — field-by-field, Dr. Patel's tree {cp-101 nppes, cp-102 roster, cp-103 tenant, cp-201 cleanser}:**

```text
Field        cp-101 (nppes)   cp-102 (roster)   cp-103 (tenant)  cp-201 (cleanser)   → winner
npi          1093817465       1093817465        —                —                   nppes (npi rule: nppes=1)
firstName    ASHA             asha r.           —                Asha                cleanser → "Asha"
             (nppes=998)      (roster=3)                         (cleanser-of-roster=2, lowest wins)
phone        (2021, stale)    +1 614 555 0142   +16145550142     —                   tenant (tenant:*=1 beats roster=3)

Resulting OV row (core_practitioners_ov):
certify_id: cp-102        ← tenant/roster slice with OLDEST createdAt (not the root!)
tenant_id:  tnt-acme-01   ← one OV row per tenant that touches the tree
root_id:    cp-101        ← merge-tree root
contributing_crosswalks: ["cp-101","cp-102","cp-103","cp-201"]   ← certify_ids
data: {npi:"1093817465", firstName:"Asha", phone:"+16145550142", ...}
```

And the event published afterward — note where the *real* crosswalk strings live:

```text
OVCompletionPayload {
  certifyId: "cp-102", tenantId: "tnt-acme-01", entityType: "practitioner",
  crosswalks: [
    {certifyId:"cp-101", crosswalkId:"1093817465", sourceType:"nppes",              updatedAt:"..."},
    {certifyId:"cp-102", crosswalkId:"1093817465", sourceType:"roster:tnt-acme-01", updatedAt:"..."},
    {certifyId:"cp-103", crosswalkId:"1093817465", sourceType:"tenant:tnt-acme-01", updatedAt:"..."},
    {certifyId:"cp-201", crosswalkId:"cp-102",     sourceType:"certify-cleanser",   updatedAt:"..."}
  ],
  triggeringCrosswalks: [ {certifyId:"cp-102", ...} ]   // matched the changedTreeIds
}
```

How the layer gets there (`AbstractSurvivorshipService.java`): receive `SliceMatchedPayload`, load the merge tree, group slices by tenant, then per tenant run source-ranking survivorship (`sourceRankingSurvivorshipRules.drl` + `config/survivorship-config-default.json`, per-tenant overrides in the `survivorship-rules` config namespace).

Default global source priority (lower wins):

| Priority | Source pattern | Meaning |
|---|---|---|
| 0 | `certify-cleanser:tenant:*` | cleansed tenant data |
| 1 | `tenant:*` | raw tenant edits (manual override) |
| 2 | `certify-cleanser:roster:*` | cleansed roster |
| 3 | `roster` / `roster:*` | raw roster |
| 4 | `certify-cleanser` | generic cleansed (e.g. cleansed NPPES) |
| 998 | `nppes` | registry — trusted least for most fields… |
| 999 | `caqh_pdqs` | lowest |

…except per-field overrides flip it: for `npi` and `data.npi`, NPPES is priority 1 — the registry is authoritative for the identifier itself, while the tenant knows the current phone. That inversion is the essence of field-level survivorship.

Read-side endpoints resolve by crosswalk too: `GET /events/ov/core-practitioner/{crosswalkId}/{sourceType}` looks up the slice, then returns its OV with `contributingSlices` renamed to `contributingCrosswalks` in the response.

## 10 · API surfaces, with example calls

```text
# Read Dr. Patel's NPPES slice
GET  /core/practitioner/nppes/1093817465

# Upsert her roster slice (creates or partial-updates by (sourceType, crosswalkId))
POST /core/practitioner/roster:tnt-acme-01/1093817465        body: {data: {...}}

# Merge: roster slice absorbed under the NPPES survivor
POST /core/practitioner/roster:tnt-acme-01/1093817465/merge
     body: {survivorSourceType:"nppes", survivorCrosswalkId:"1093817465"}

# Kick full MDM for one record by crosswalk
POST /mdm/practitioner/nppes/1093817465

# OV with contributing slices inlined
GET  /practitioner-ov/...?includeCrosswalks=true

# Recover an address's original hash when the OV row carries a UUID reference
GET  /core/entity-addresses/entity/{entityId}/crosswalk-id

# Dry-run Acme's facility crosswalk config
POST /facilities/crosswalk-preview

# QA CLI
dal get core-practitioner --sourceType nppes --crosswalkId 1093817465
```

Other surfaces: `GET/PUT /locations/{id}` — GET accepts certifyLocationId *or* crosswalkId (certifyId tried first); PUT's `{id}` *is* the crosswalkId. `crosswalkId` is a registered filterable string field on core models, usable through the DAL's generic filter machinery. The MDM trigger path validates crosswalk chars: alphanumeric plus `- . _ :` only.

## 11 · Expert gotchas, each with a mini example

1. **`contributing_crosswalks` ≠ crosswalk strings.** Dr. Patel's OV holds `["cp-101","cp-102","cp-103","cp-201"]`, never `["1093817465"]`. Real crosswalk strings live in `sliceMetadataMap` / the OVCompletionPayload.
2. **~88% of singleton OV rows have `contributing_crosswalks IS NULL`** (dal-internal: 96,589 of 109,563 single-slice rows). A doctor ingested from one source only may have no array at all. Every query must fall back:

   ```sql
   UNNEST(IF(contributing_crosswalks IS NULL OR ARRAY_LENGTH(contributing_crosswalks)=0,
             JSON_VALUE_ARRAY(contributing_slices),
             contributing_crosswalks))
   ```

   Plain `UNNEST(contributing_crosswalks)` silently resolves zero ids for those practitioners.
3. **OV-side address crosswalks are UUIDs, not hashes.** The base slice for 120 Main St carries `9f3ac27b41…`; a survivorship-promoted row carries a UUID reference. Re-hashing OV-enhanced field values ("120 Main Street" vs "120 Main St") produces a *different* hash → mismatched crosswalk → duplicate. Use `GET /entity/{entityId}/crosswalk-id`; roster dedup injects an `_existingCrosswalkId` marker so re-ingestion reuses the original hash (CP-30447).
4. **Never regenerate on update; format changes are migrations.** Flipping Acme from `tin:npi` to `{externalId}` templates means every existing facility is keyed on the old format — next roster upload FINDs nothing and duplicates the whole book of business.
5. **Cleansed-slice idempotency:** upsert response with `certifyId == crosswalkId == survivorId` (all `cp-102`) = already-merged cleansed slice, skip the merge.
6. **Cross-crosswalk workflow lookup is an incident lever:** `api-layer.credentialing.all-contributing-crosswalks-workflow-check.enabled` (global, default true) makes workflow lookups scan every TenantPractitioner behind the OV's contributing crosswalks — so a credentialing workflow created under cp-102 is still found by a request arriving under sibling cp-103.
7. **`mdm_groups.crosswalk_id` is the only nullable one** — every other slice table's is NOT NULL. Portal-created groups get UUID crosswalks; roster groups get `tin:npi`.
8. **Facility OV resolution tries three strategies** (PK → `root_id` → `contributing_crosswalks`); practitioner only two (PK → `contributing_crosswalks`). Intentional asymmetry, mirrors each entity's resolution layer.
9. **The "classic crosswalk" Drools rule fires only for migrated legacy sources** (`cbpsv`, `classic-dea` in prod) — sources with no reliable demographics, where the crosswalk string is the only trustworthy identity. Everywhere else, crosswalk equality contributes via blocking + the NPI rules.
