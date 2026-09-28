> **Editing rule:** append only. Never overwrite or delete existing questions/answers. New Q&A goes at the bottom. Answers should be short, direct, concise — technical details only when relevant.

1. Runtime: Please avoid Cloud Run and use GCE.  Please update the runtime section accordingly.

2. Job processing: Let’s use JobRunr ?... It has a Quarkus extension and can use the existing Mongo cluster, so no new datastore is needed. How would maxParallelParses map to JobRunr?

3. Scheduled sweep: Why do we need Cloud Scheduler? Could we use quarkus-scheduler or JobRunr recurring jobs instead? Please pick one and explain why. With multiple GCE instances, we also need to ensure the sweep runs only once in 15 days.

4. Mongo collections: Please share the field-level structure of all six collections. If these are already defined in another low-level design, please link it or include the details here.

**Answer:** Five collections, not six — sources are gone (Q10), folded into `consumers`.

`consumers` — one row per registered consumer:

| Field | Type | Notes |
| --- | --- | --- |
| `identifier` | string | primary key |
| `subscriptionTopic` | string | completion topic, named at registration |
| `retentionDays` | int, default 30 | row retention; drives the TTL index |
| `registeredAt` | timestamp | |


`consumer_mappings` — one mapping version per consumer, immutable after activation:

| Field | Type | Notes |
| --- | --- | --- |
| `mappingId` | string | primary key |
| `identifier` | string | owning consumer |
| `name` | string | |
| `version` | int | a change is a new version, never an edit |
| `columns` | array of `{ fileColumn, targetColumn, validations[] }` | validations from the fixed engine list (Q9) |
| `createdAt` | timestamp | |

`ingestions` — one row per delivery, registration and delivery merged (Q5):

| Field | Type | Notes |
| --- | --- | --- |
| `ingestionId` | string | primary key |
| `identifier` | string | owning consumer |
| `tenantId` | string | from the caller claim, never the payload |
| `filename` | string | |
| `objectName` | string | chosen by the service |
| `mappingId` | string, optional | resolved mapping, or raw mode if absent |
| `externalRef` | string, optional | idempotency key |
| `meta` | object, ≤64 KiB | opaque, echoed unchanged |
| `status` | enum | `REGISTERED`/`RECEIVED`/`VALIDATING`/`PARSING`/`COMPLETED`/`PARTIAL`/`INVALID`/`EXPIRED`/`CONSUMED` |
| `parked` | bool | layered on top of any status |
| `counts` | `{ populatedRows, ingestedRows, invalidRows }` | |
| `objectGeneration` | long | claimed at `INBOUND_DELIVERY_DETECTED` |
| `registeredAt` | timestamp | |
| `dispatchedAt` | timestamp, nullable | cleared/retried by the sweep past 10 min with no claim |
| `terminalAt` | timestamp, optional | set on any terminal status; key for the TTL index |
| `consumedAt` | timestamp, optional | set by `POST /v1/ingestion-complete/{id}` |

`ingestion_rows` — one collection, envelope only (normalized fields are mapping-dependent, left dynamic):

| Field | Type | Notes |
| --- | --- | --- |
| `ingestionId`, `sheet`, `row` | — | compound unique key |
| `tenantId` | string | isolation check |
| `status` | enum | `INGESTED` / `INVALID` |
| `fields` | object, dynamic | present only when a mapping resolved |
| `raw` | array | ordered raw cell values, always present |
| `warnings` | array, optional | |

`ingestion_events` — audit, append-only, common envelope (`sourceId` dropped):

| Field | Type | Notes |
| --- | --- | --- |
| `eventId` | string | |
| `type` | string | one of the event names in the audit trail table |
| `tenantId`, `identifier`, `ingestionId` | string | correlation |
| `objectGeneration` | long, optional | |
| `messageId` | string, optional | Pub/Sub correlation |
| `actor` | string | system or operator |
| `occurredAt` | timestamp | |
| `detail` | object | free-form, event-specific |

5. Registration vs delivery: Why are registration and delivery stored in the same row? I would expect:
    -A temporary collection that creates the identifier and GCS path.
    -A main collection created only after GCS confirms the upload.
    Since the sweep already expires abandoned REGISTERED rows and If we keep one collection, how do we prevent abandoned rows from filling the main collection?

**Answer:** Registration and delivery aren't two separate things — they're two states of the same delivery, so they belong in one row. Splitting them into two collections wouldn't fix the "filling up" worry anyway, since the abandoned rows would just pile up in the new collection instead.

The real problem: the cleanup job (sweep) currently only *labels* an abandoned row as `UPLOAD_NEVER_ARRIVED` — it never deletes it. So the table grows forever regardless of the schema.

**Fix:** add auto-expiry (TTL) on the `ingestions` table, but only for dead-end statuses (`EXPIRED`, `INVALID`) — a partial TTL index on `terminalAt`. Completed/partial/consumed deliveries stay, for audit. The TTL is never shorter than that consumer's `retentionDays`, so an ingestion can't vanish before its rows do.

Tracked as follow-up decision H15 in the design doc.

6. externalRef: Who generates it, and is it optional? Step 4 says optional, but the contract and completion event always include it. Please confirm:
    -externalRef is only for request-level idempotency.
    -or handles content-level duplicates.
    -If the same externalRef is sent again after ingestion completes, do we return the existing ingestion record?

**Answer:** The shared UI component (caller) generates `externalRef`. It is optional.
- Request-level idempotency only. Same `externalRef` twice gives one row, not two.
- Does not handle content duplicates.
- Same `externalRef` after the ingestion is finished: returns the existing record with its status and counts, no upload URL. A new file needs a new `externalRef`.
7. SSE backend: Which SSE implementation are we using? More importantly, the parse job may run on one instance while the browser connection is on another. How does the SSE instance learn about status changes — Mongo change streams, polling, or Pub/Sub?

**Answer:** Polling. The SSE handler re-reads the `ingestions` row on an interval (same indexed lookup as the plain `GET`) from inside its own held-open connection, emits `event: status` on a diff, closes on terminal. No Mongo change streams, no Pub/Sub.

Change streams were considered. They need "CPU always allocated" on every autoscaled instance (not just the min floor) so the background listener doesn't stall between requests, which is a continuous cost regardless of traffic. Parked until volume justifies it.

8. SSE frontend: Please add the frontend architecture for SSE, including reconnect/backoff and what happens if a terminal status is reached while the browser is disconnected. Also, the code uses onIngestionProgress, while the props table says onUploadProgress. Which one is correct?

**Answer:** `FileIngestionUploader` runs 4 calls in order: list mappings, register delivery, `PUT` bytes to the signed URL, watch status. Status watch opens `GET /v1/ingestion/{id}` with `Accept: text/event-stream` when `useServerSentEvents` is `true` (default); `false` polls the same path with `Accept: application/json` instead. The stream is opened with `fetch()`, not native `EventSource` — `EventSource` cannot set an `Authorization` header, and every call carries the Auth0 token (Q11). The component reads the response body off its `ReadableStream`, splits on blank lines, and parses each `event:`/`data:` block.

**Reconnect/backoff:** `fetch()` has no built-in reconnect, so the component owns it — which is what we want anyway. Two different things look like the same dropped connection: a routine disconnect (the request hits its max duration, backend's fine) and a real outage (backend down, every open connection drops at once). A flat retry interval treats both the same and hammers a struggling backend in lockstep.

On stream end or error: reopen on a growing delay (1s → 2s → 4s → … capped ~30s), with jitter so many clients don't retry in sync, and reset the counter to 1s once a stream opens successfully. No `Last-Event-ID`/event-replay logic needed — every reconnect just re-hits `GET /v1/ingestion/{id}` and the backend answers with the row's current status as the first frame, so there's nothing to replay.

**Terminal reached while disconnected:** not a problem. Every new connection — first one or a reconnect — has the backend emit the row's *current* status as its first frame before anything live. If already terminal, that first frame closes the connection immediately and fires the terminal callback. Status lives in Mongo, not in the dropped connection, so nothing is missed.

**`onUploadProgress` vs `onIngestionProgress`: both are real, both needed** — this is a props-table gap, not a naming conflict.
- `onUploadProgress` — bytes sent during the `PUT`, from the browser's own upload event. Unrelated to the backend.
- `onIngestionProgress` — called on every status change (`RECEIVED`, `VALIDATING`, `PARSING` with counts once available, then the terminal status). Delivered over whichever transport `useServerSentEvents` picked for the status watch: SSE frame by default, poll diff when `false`. Same status source as `onSuccess`/`onError`, just called on every intermediate change too.

Props table needs a new row added for `onIngestionProgress`.

9. /v1/mappings validation: Please document the fixed validation rules supported by the engine. Specifically:
    -Are row-level or cross-field rules supported?
    -Can rules reference lookup data?
    -Does every field need at least one rule?

**Answer:** Fixed per-column validators, chosen from this list, attached in the mapping:

| Validator | Passes when | Example |
| --- | --- | --- |
| `required` | the cell is not empty | `required` |
| `digits:n` | exactly n digits, nothing else | `digits:10` for an NPI |
| `maxLength:n` | at most n characters | `maxLength:120` |
| `enum:a,b,c` | the value is one of the listed values | `enum:MD,DO,NP` |
| `date:pattern` | the value parses with the pattern | `date:yyyy-MM-dd` |
| `regex:pattern` | the whole value matches the pattern. Runs on RE2J, so no pattern can stall a parse. Compiled at save time, refused if it does not compile or exceeds 200 characters | `regex:^\d{5}(-\d{4})?$` for a US zip |

- **Row-level or cross-field rules:** no. Every validator runs on one cell, independent of every other column. Nothing here expresses "end date after start date" or "if type = X then column Y required."
- **Lookup data:** no. `enum:a,b,c` is a fixed literal list written into the mapping itself, not a reference to another collection or table.
- **Every field needs at least one rule:** no. A column's `validations` array can be empty — it still gets mapped and converted, just with nothing to check.
10. sourceId: Why is sourceId required in the /v1/ingestion payload? It is already available in the header, and the consumer record already defines which sources they can use. Since source and consumer are the same party for all first-release sources, can we default it? Also, what happens if a caller provides a source they don’t have access to?

**Answer:** Decided: `sourceId` and the separate `ingestion_sources` per-source config are being removed. Source and consumer collapse into one concept — the consumer `identifier` becomes the only identity. The doc will be updated to reflect this; this trades away the future flexibility of a vendor source with no reader or multiple sources per consumer, which isn't needed for any source planned right now.

Because there's no separate `sourceId` anymore, "a caller provides a source they don't have access to" stops being a real case — there's nothing left to cross-check against the consumer record.

The deeper question underneath it still stands, just reframed: can a caller claim an `identifier` that isn't theirs? Today, the registrar only *validates* — checks the `identifier` is a known, registered consumer — it does not *authenticate* — it doesn't prove the caller is entitled to claim that `identifier`. That's a real gap, but it's an authentication gap, not a source-access gap — already tracked under Q11 (API authentication) and should be resolved there rather than duplicated here.

11. API authentication: Please clarify how the API is authenticated. The security section covers signed URLs, parser security, tenant isolation, and mapping input, but not caller authentication. identifier is just a header, and the tenant comes from the “caller claim” without explaining who issues or validates it. Since the upload happens from a browser and . Please document the authentication flow. Or at least, how will it happen?

**Answer:** Reuses the existing Auth0 setup and the current user access token — no separate File Ingestion auth.

- The token carries the authenticated user and tenant/client context. File Ingestion validates it through the existing Auth0 flow, doesn't run its own.
- Individual consumers (Roster, Credentialing, Monitoring) are not separately authenticated. A single Auth0 client (e.g. PDM) can hold multiple feature-level consumers, so consumer identity isn't an authentication boundary.
- The `identifier` header still gets sent, but only for routing/tracking within File Ingestion — not as an auth mechanism.
- Backend callers (a consumer service registering a delivery) send the same Auth0 token in the same `Authorization` header. No separate service-to-service path.

**Flow:** User logs in via Auth0 → consumer app renders the shared upload component → component calls the File Ingestion API with the Auth0 token in `Authorization` → File Ingestion validates the token, extracts user/tenant context → creates the ingestion record, returns a signed GCS URL → browser uploads directly to GCS → File Ingestion parses → shared UI polls status using the same token.

`User → Consumer App → Shared Upload UI → File Ingestion API (Auth0 validation) → Signed GCS URL → Browser uploads to GCS → File Ingestion processing → Shared UI polls status`

12. Large uploads: The current design uses a single signed PUT of up to 500 MB with no resume or retry. Why not use a resumable upload session instead?

**Answer:** We're staying with the single signed `PUT` for now, not adding a resumable upload session. The plan is to observe rather than pre-build: A1/A2 (abandoned uploads) and A12 (`PUT` refused by expiry or generation mismatch) already give visibility into drop-driven upload failures through the existing observability channels. If that data shows it's a real, recurring cost, resumable upload gets added then, based on evidence rather than upfront.

