# Addresses and Locations in the DAL

> Plain-English answer to: *I have a facility (or a practitioner, or a group). Where is its address? What is its location? Why are there so many tables?*
>
> Sources: `fe-api-coredal/api-layer` (roster schemas, `ResourceConfiguration`, `TransactionOrchestrator`, `FacilityService`), `fe-api-coredal/core-data-access-layer` (Liquibase changelogs, `queries/*.sql`), live `dal-dev`. Compiled 2026-08-27.

## Contents

1. [Two words that get confused](#1--two-words-that-get-confused)
2. [Three ways an address is stored](#2--three-ways-an-address-is-stored)
3. [I have a core facility. Where is its address?](#3--i-have-a-core-facility-where-is-its-address)
4. [I have a core practitioner. Where is its address?](#4--i-have-a-core-practitioner-where-is-its-address)
5. [I have a group. Where is its address?](#5--i-have-a-group-where-is-its-address)
6. [I have a location. Where is its address?](#6--i-have-a-location-where-is-its-address)
7. [Which roster decides which path](#7--which-roster-decides-which-path)
8. [How an entity reaches a location](#8--how-an-entity-reaches-a-location)
9. [Copy-paste queries](#9--copy-paste-queries)
10. [Gotchas](#10--gotchas)

---

## 1 · Two words that get confused

**Address** = a postal string. Street, city, state, zip. Nothing else. No hours, no panel, no accessibility.

**Location** = a place where care happens. It has a name, office hours, bed count, ADA info, whether it accepts new patients.

A location **has** addresses. An address never has a location.

| | `core_entity_addresses` | `core_locations` |
|---|---|---|
| Primary key | `certify_id` | `certify_location_id` |
| Holds | `addressLine1`, `addressLine2`, `city`, `state`, `zip`, `county`, `latitude`, `longitude`, `formattedAddress`, **`addressType`** | `name`, `officeHours[]`, `bedCount`, `acceptsNewPatients`, `adaCompliance`, `locationType`, `active` |
| Does **not** hold | anything about hours or services | any street field &mdash; searching `core_locations.data` for `city` returns nothing |

Both are MDM slice tables: one row per contributing source, with `crosswalk_id` (content hash), `source_id`, `survivor_id`, and a `data` JSON blob. Survivorship output lands in `core_entity_addresses_ov` and `core_locations_ov`, keyed by `(id, tenant_id)`.

**`addressType`** is the label that says what an address is *for*: `service` (where care is delivered), `billing`, `remittance`, `mailing`, `irs`, `w9`, `office`, `outreach`. It matters more than it looks &mdash; it decides which tables the address gets linked into, and it is part of the address's identity hash.

## 2 · Three ways an address is stored

This is the part that makes the table list confusing. There is no single answer to "where is the address" &mdash; there are three mechanisms, and they all run in production side by side.

### Path A &mdash; embedded in the entity's own JSON

The address is a plain object inside the entity's `data` blob. No separate row, no join.

```
core_facilities.data.addresses[]     -> [{address1, address2, city, state, country,
                                          zipcode, type, isPrimary, source}]
core_practitioners.data.addresses[]  -> same shape
```

Note the field names differ from `core_entity_addresses`: `address1` not `addressLine1`, `zipcode` not `zip`, `type` not `addressType`. That is deliberate &mdash; the `CoreFacility` schema documents `zipcode` as kept lowercase "for historical reasons… to avoid a breaking rename".

### Path B &mdash; its own row, attached by a link table

The address becomes a real `core_entity_addresses` row, and a small join table attaches it to the entity.

```
core_entity_addresses  (the shared address row)
    ^ facility_entity_addresses     (certify_facility_id, entity_address_id)
    ^ group_entity_address          (certify_group_id,    entity_address_id)
    ^ location_entity_addresses     (location_id,         entity_address_id)
    ^ practitioner_entity_addresses (certify_practitioner_id, entity_address_id)  <- never written
```

One address row can be linked from several of these at once. A facility's service address is typically linked three times: to the facility, to its group, and to its location.

### Path C &mdash; reached through a location

The entity has no address of its own. It is attached to a **location**, and the location has the address.

```
entity -> ... -> core_locations -> location_entity_addresses -> core_entity_addresses
```

This is how a practitioner gets a street address in practice.

---

## 3 · I have a core facility. Where is its address?

Four possible places. Check them in this order.

### 3.1 Inside the facility row (`core_facilities.data.addresses[]`)

Written by the **Humana** and **credentialing** facility rosters. Eight roster columns map straight into an embedded array:

```json
"primaryFacilityLocationAddressLine1": {
  "metadata": {
    "entity": "CoreFacility",          // NOT CoreEntityAddress
    "entityKey": "address1",
    "objectArrayGrouping": {
      "arrayKey": "addresses",         // core_facilities.data.addresses[]
      "defaultValues": { "type": "service", "isPrimary": true }
    }
  }
}
```

The eight columns: `primaryFacilityLocationAddressLine1`, `…AddressLine2`, `…City`, `…State`, `…Zip`, `…Latitude`, `…Longitude`, `…County`.

For these tenants the address creates **no** `core_entity_addresses` row and **no** link row at all. In dev this is the most common facility address by far: **329,733** of 2,308,586 facilities carry one.

### 3.2 `facility_entity_addresses` &mdash; the facility's back-office addresses

The facility's own administrative addresses: where you mail it, bill it, file its 1099. Two writers.

**Writer 1 &mdash; the facility upsert API** (`FacilityService`, used by the UI/SDK). Exactly four types, hardcoded:

```java
if (billingAddress != null) { billingAddress.put("addressType", "billing"); ... }
if (officeAddress  != null) { officeAddress.put("addressType", "office");   ... }
if (mailingAddress != null) { mailingAddress.put("addressType", "mailing"); ... }
if (irsAddress     != null) { irsAddress.put("addressType", "irs");         ... }
// each builds: CoreEntityAddress op + FacilityEntityAddress op
```

This path writes **only** the facility link. No group link, no location link.

**Writer 2 &mdash; the default facility roster** (`facility-roster-system-fields.json`). It writes the facility link for **every** address type, alongside the group and location links (see 3.3).

`FacilityEntityAddress` is declared with `multiplyByEntity("CoreEntityAddress")` and **no** `entityFilter`, so every address instance in the row produces a facility link.

### 3.3 `location_entity_addresses` &mdash; the practice site address

The facility's *service* address, reached through its location. The default facility roster writes all three links from one row:

| Address type in the row | `core_entity_addresses` | `location_entity_addresses` | `group_entity_address` | `facility_entity_addresses` |
|---|:---:|:---:|:---:|:---:|
| `service` | yes | **yes** | yes | yes |
| `remittance` | yes | no | yes | yes |
| `mailing` | yes | no | yes | yes |
| `w9` | yes | no | yes | yes |

The `service`-only rule lives in one line of `ResourceConfiguration`:

```java
configs.put("LocationEntityAddress", ResourceConfig.builder()
    .dependencies(List.of("CoreLocation", "CoreEntityAddress"))
    .multiplyByEntity("CoreEntityAddress")
    .entityFilter((key, data, ctx) ->
        "service".equals(data.get("addressType")))     // <-- the whole rule
    .build());
```

Verified in `dal-dev` &mdash; two facilities, two different writers:

| Facility | addressType | in `facility_entity_addresses` | in `location_entity_addresses` | in `group_entity_address` |
|---|---|:---:|:---:|:---:|
| `468d247f…` (roster) | `service` | 1 | **1** | 1 |
| `468d247f…` (roster) | `remittance` | 1 | 0 | 1 |
| `5ce28dc7…` (API upsert) | `billing` | 1 | 0 | 0 |
| `5ce28dc7…` (API upsert) | `mailing` | 1 | 0 | 0 |
| `5ce28dc7…` (API upsert) | `irs` | 1 | 0 | 0 |

At scale the same pattern holds: **2,052 of the 2,878** `facility_entity_addresses` rows share their `entity_address_id` with a `location_entity_addresses` row. One address row, linked from three places &mdash; not three copies. The remaining ~826 are the API-upsert types (billing / office / mailing / irs) and null-type rows, which have no location link by design.

### 3.4 `group_entity_address` &mdash; via the facility's group

The default facility roster also links every address to the group named in the same row (`groupName` / `groupNpi` / `groupTin`). No filter &mdash; all types.

### Summary for a facility

```
core_facilities.data.addresses[]  -> Humana / credentialing rosters      (329,733 facilities)
facility_entity_addresses         -> facility upsert API (billing/office/mailing/irs)
                                     + default facility roster (all types)   (451 facilities)
location_entity_addresses         -> default facility roster, service only
group_entity_address              -> default facility roster, all types
```

**What is a facility's "location"?** A separate `core_locations` row, reached one of two ways:

```
tenant_facilities -> tenant_facility_locations -> core_locations
                     (direct, no group, 65,062 rows in dev)

tenant_facilities -> tenant_group_facilities -> tenant_group_facility_locations
                     -> tenant_group_locations -> group_locations -> core_locations
                     (through the group; this is where networks and specialties live)
```

## 4 · I have a core practitioner. Where is its address?

**Almost always: through their location.** A practitioner has no address of their own in practice.

- `practitioner_entity_addresses` exists as a table but has **no writer anywhere in api-layer**. `grep -r PractitionerEntityAddress api-layer/src/main` returns nothing. 3 rows in dev.
- `core_practitioners.data.addresses[]` exists as a field but is barely used: 10,471 rows out of 84,315,565.

The practitioner roster's address columns (`groupPracticeLocation*`, `groupBilling*`, `groupRemittance*`, `groupMailingMailing*`, `groupIRS*`, `groupOutreach*`) all map to `CoreEntityAddress`, and then link to the **group** and the **location** &mdash; never to the practitioner.

So the real chain is:

```
tenant_practitioners
 -> tenant_group_practitioners
 -> group_practitioner_locations      <- "the practitioner is at this site"
 -> tenant_group_locations
 -> group_locations
 -> core_locations
 -> location_entity_addresses
 -> core_entity_addresses             <- the street address
```

There is **no `tenant_practitioner_locations` table**. The name you are looking for is `group_practitioner_locations`. Its `data` carries `panelStatus`, `acceptingNewPatients`, effective and termination dates, roles.

## 5 · I have a group. Where is its address?

`group_entity_address`, and it holds **every** address type &mdash; service, billing, remittance, mailing, IRS, outreach. 541,814 rows in dev, the largest of the four link tables.

```java
configs.put("GroupEntityAddress", ResourceConfig.builder()
    .dependencies(List.of("Group", "CoreEntityAddress"))
    .multiplyByEntity("CoreEntityAddress")
    .entityFilter((key, data, ctx) -> data.get("addressType") != null)   // any type
    .build());
```

**This is where non-service addresses live.** A practitioner's or facility's billing / mailing / remittance address is not on the practitioner, not on the facility, and not on the location. It is on the **group**.

A group's locations are in `group_locations` (`group_id`, `location_id`) &mdash; MDM level, no tenant. Per-tenant, the same site appears again as `tenant_group_locations` (`tenant_group_id`, `group_location_id`), and every tenant-scoped attribute (networks, specialties, participation) attaches there.

## 6 · I have a location. Where is its address?

`location_entity_addresses` (`location_id`, `entity_address_id`) &mdash; **service addresses only**. 529,871 rows.

A location can still have more than one row here if several distinct service addresses were ingested for it, but billing / mailing / remittance will never appear.

## 7 · Which roster decides which path

The roster schema a tenant uses decides whether its facility addresses go embedded (Path A) or normalized (Path B). Selected from `tenant_configurations`, keys `facility-system-fields-path` / `practitioner-system-fields-path` / `group-system-fields-path`, read by `RosterSchemaService.fetchTenantSpecificSchemaPath`. No config row = the default schema.

| Roster schema | Address columns go to | Link rows written |
|---|---|---|
| `facility-roster-system-fields.json` *(default)* | `CoreEntityAddress` | facility + group + location(service only) |
| `humana-facility-roster-system-fields.json` | `core_facilities.data.addresses[]` | **none** |
| `credentialing-facility-roster-system-fields.json` | `core_facilities.data.addresses[]` | **none** |
| `roster-system-fields.json` *(default practitioner)* | `CoreEntityAddress` | group + location(service only) |
| `humana-practitioner-roster-system-fields.json` | *no address columns at all* | none |
| `zing-roster-system-fields.json` | *no address columns at all* | none |
| `group-roster-system-fields.json` | `CoreEntityAddress` | group + location(service only) |

Tenants configured in `dal-dev`:

| Tenant | Facility schema | Practitioner schema |
|---|---|---|
| Humana Staging &ndash; Dev | `humana-facility-…` | `humana-practitioner-…` |
| Humana Pre-Prod | `humana-facility-…` | `humana-practitioner-…` |
| Humana Staging &ndash; Testing | `humana-facility-…` | `humana-practitioner-…` |
| Roster Pod Testing | `humana-facility-…` | `humana-practitioner-…` |
| Zing (Stg &ndash; Internal) | `humana-facility-…` | `humana-practitioner-…` |
| NM &ndash; United Healthcare | `credentialing-facility-…` | `humana-practitioner-…` |
| Zing (Test) / Zing (New) | *default* | `zing-roster-system-fields.json` |

### One roster row can become several addresses

`addressType` in `entityInstanceGrouping.defaultValues` is a discriminator, not a constant. `EntityMappingService.emitAddressInstancesByTypeIfApplicable` buckets the row's address columns by type and emits one address object per bucket. A practitioner roster row carrying service + billing + remittance + mailing + IRS columns produces **five** `core_entity_addresses` rows, five `group_entity_address` rows, and one `location_entity_addresses` row.

### Why the same street can appear twice

The address crosswalk is a SHA-256 hash over `addressLine1 | addressLine2 | city | state | zip | addressType`. `addressType` is **inside** the hash, so the same street ingested as `service` and again as `billing` becomes two distinct rows with two lineages.

The location crosswalk is `urlencode(normalize(locationName)) + "_" + addressHash`. A location's identity is therefore **name + address** &mdash; renaming a site in the roster forks a new `core_locations` slice instead of updating the old one.

## 8 · How an entity reaches a location

Neither a practitioner nor a facility joins to a location directly.

```
PRACTITIONER                        FACILITY (via group)              FACILITY (direct)
tenant_practitioners                tenant_facilities                 tenant_facilities
 -> tenant_group_practitioners       -> tenant_group_facilities         -> tenant_facility_locations
 -> group_practitioner_locations     -> tenant_group_facility_locations -> core_locations
 -> tenant_group_locations           -> tenant_group_locations
 -> group_locations                  -> group_locations
 -> core_locations                   -> core_locations
```

The direct facility path skips the group entirely &mdash; and with it, every network and specialty table.

Three network tables sit at the same location at different grains. Mixing them up is the most common read bug:

| Table | Keyed on | Means |
|---|---|---|
| `tenant_group_location_networks` (TGLN) | `tenant_group_location_id` | location &times; network &mdash; shared by everyone at that site |
| `tenant_group_location_facility_networks` (TGLFN) | `tenant_group_facility_location_id` | facility-at-location &times; network |
| `tenant_group_location_practitioner_networks` (TGLPN) | `group_practitioner_location_id` | practitioner-at-location &times; network |

CP-36570 moved location NPI writes from TGLPN to TGLN. Readers merge both, **TGLPN wins, TGLN is the fallback**, and a `networkId` filter must match *either* or rows get silently dropped.

## 9 · Copy-paste queries

**A facility's embedded address (Humana / credentialing tenants):**

```sql
SELECT certify_facility_id,
       JSON_VALUE(data, '$.addresses[0].address1') AS line1,
       JSON_VALUE(data, '$.addresses[0].city')     AS city,
       JSON_VALUE(data, '$.addresses[0].zipcode')  AS zip,
       JSON_VALUE(data, '$.addresses[0].type')     AS type
FROM core_facilities
WHERE certify_facility_id = @facilityId;
```

**A facility's linked addresses (default roster / API upsert tenants):**

```sql
SELECT JSON_VALUE(cea.data, '$.addressType') AS type,
       JSON_VALUE(cea.data, '$.addressLine1') AS line1,
       JSON_VALUE(cea.data, '$.city')         AS city,
       JSON_VALUE(cea.data, '$.zip')          AS zip
FROM facility_entity_addresses fea
JOIN core_entity_addresses cea ON cea.certify_id = fea.entity_address_id
WHERE fea.certify_facility_id = @facilityId;
```

**A facility's practice-site address, through its location:**

```sql
SELECT JSON_VALUE(cl.data,  '$.name')         AS location_name,
       JSON_VALUE(cea.data, '$.addressLine1') AS line1,
       JSON_VALUE(cea.data, '$.city')         AS city
FROM tenant_facilities tf
JOIN tenant_facility_locations tfl   ON tfl.tenant_facility_id = tf.id
JOIN core_locations cl               ON cl.certify_location_id = tfl.location_id
JOIN location_entity_addresses lea   ON lea.location_id        = tfl.location_id
JOIN core_entity_addresses cea       ON cea.certify_id         = lea.entity_address_id
WHERE tf.tenant_id = @tenantId
  AND tf.certify_facility_id = @facilityId;
```

**A practitioner's address, through their location:**

```sql
SELECT JSON_VALUE(cl.data,  '$.name')         AS location_name,
       JSON_VALUE(cea.data, '$.addressLine1') AS line1,
       JSON_VALUE(cea.data, '$.city')         AS city
FROM tenant_practitioners tp
JOIN tenant_group_practitioners tgp  ON tgp.tenant_practitioner_id   = tp.id
JOIN group_practitioner_locations gpl ON gpl.tenant_group_practitioner_id = tgp.id
JOIN tenant_group_locations tgl      ON tgl.id = gpl.tenant_group_location_id
JOIN group_locations gl              ON gl.id  = tgl.group_location_id
JOIN core_locations cl               ON cl.certify_location_id = gl.location_id
JOIN location_entity_addresses lea   ON lea.location_id        = gl.location_id
JOIN core_entity_addresses cea       ON cea.certify_id         = lea.entity_address_id
WHERE tp.tenant_id = @tenantId
  AND tp.certify_practitioner_id = @practitionerId;
```

**Enriching with survivorship (OV) &mdash; note this is NOT a primary-key join:**

```sql
-- correct: relationship tables hold slice ids, OV rows are merged survivors
FROM core_locations_ov cl
WHERE cl.tenant_id = @tenantId
  AND gl.location_id IN UNNEST(JSON_VALUE_ARRAY(cl.contributing_slices))
LIMIT 1

-- wrong: silently returns nothing once the slice has been merged
WHERE cl.certify_location_id = gl.location_id
```

## 10 · Gotchas

- **A facility's address may not be in any address table.** For Humana and credentialing tenants it is embedded in `core_facilities.data.addresses[]`. A query that only checks `facility_entity_addresses` misses 329,733 facilities in dev.
- **Street fields are never on `core_locations`.** They are on the linked `core_entity_addresses` row.
- **Only `service` addresses reach a location.** Billing, mailing, remittance and IRS are group-level or facility-level and never appear under `locationEntityAddresses`.
- **Non-service addresses live on the group.** Not the practitioner, not the location.
- **`addressType` is in the address hash.** Same street, two types = two rows, two crosswalks, two lineages.
- **Renaming a site forks the location.** The crosswalk is `name + "_" + addressHash`.
- **`tenant_practitioner_locations` and `tenant_facility_addresses` do not exist.** The names are `group_practitioner_locations` and `facility_entity_addresses`.
- **`group_locations` is not tenant-scoped.** Filtering it by tenant is a mistake &mdash; go through `tenant_group_locations`.
- **Never join an OV table on its primary key** from a relationship table. Use `contributing_slices`.
- **TGLN is not TGLPN.** Location-level network data does not appear in a practitioner's TGLPN array.

## Reference: what dev holds

| Table | Rows |
|---|---|
| `core_entity_addresses` | 1,178,402 |
| `core_locations` | 739,831 |
| `group_locations` | 567,787 |
| `group_entity_address` | 541,814 |
| `location_entity_addresses` | 529,871 |
| `tenant_facility_locations` | 65,062 |
| `core_facilities` with embedded `addresses[]` | 329,733 |
| `facility_entity_addresses` | 2,878 (451 facilities; 2,052 also linked to a location) |
| `core_practitioners` with embedded `addresses[]` | 10,471 |
| `practitioner_entity_addresses` | 3 |

`addressType` in `core_entity_addresses`: null 794,023 &middot; `service` 332,854 &middot; `remittance` 25,956 &middot; `billing` 8,877 &middot; `mailing` 8,303 &middot; `irs` 4,971 &middot; `office` 1,680 &middot; `w9` 1,677 &middot; `outreach` 33.

The 794k null-type rows can never gain a location link, because the filter tests `"service".equals(addressType)`.

### Where to read it yourself

| Question | File |
|---|---|
| Which entity does column X write to? | `api-layer/src/main/resources/schemas/*-roster-system-fields.json` (`metadata.entity`) |
| Which link tables get created, filtered how? | `api-layer/…/roster/service/ResourceConfiguration.java` |
| In what order? | `api-layer/…/roster/service/TransactionOrchestrator.java` |
| How does one row become N addresses? | `api-layer/…/roster/service/EntityMappingService.java` |
| How is the crosswalk hashed? | `api-layer/…/roster/service/LocationHashService.java` |
| Facility upsert address writes | `api-layer/…/facility/service/FacilityService.java` |
| How does a read traverse it all? | `core-data-access-layer/src/main/resources/queries/*.sql` |
| Column DDL | `core-data-access-layer/src/main/resources/db/changelog/<table>/001-create-*.yaml` |
