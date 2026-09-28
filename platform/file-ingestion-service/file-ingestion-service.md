# Design: File ingestion service

**Tier 3.** Cross-service, provider PII, new datastore. Two reviewers, one outside the team. Status: decisions closed, pending Gate 2 review.

**The first release takes one intake.** A consumer registers a delivery, the service returns a presigned upload URL, and the client writes the bytes straight into the Google Cloud Storage (GCS) ingestion bucket. There is no other way in. Vendor push over the shared SFTP platform is out of scope for the first release.

## Purpose and scope

One question per tenant: **a file arrived for tenant T from consumer C. Is it whole, what is in it row by row, and who needs to know?**

A consumer registers a delivery and uploads the file to a URL this service signs. The service records the delivery, validates the structure against a named mapping, stores one record per row, serves the records through a tenant scoped API, and publishes one event per completed ingestion.

* **Owns:** consumer registration, mapping storage, delivery registration, structural validation, row storage, retrieval, audit, and the shared components the consumer embeds.
* **Does not own:** interpretation, review, release, workflow creation, the consumer's own screens around the upload component.
* **Neighbors:** every consumer frontend that uploads a file, every consumer backend that registers a delivery, and every consumer that reads a completion event.

A **consumer** is the party that registers, uploads, and reads. `identifier` names it throughout this document. There is no separate source concept: the consumer is the origin of its own files. An earlier draft kept a `sourceId` apart from the consumer so that a vendor source with no reader could exist later. No consumer planned now needs that, and the tradeoff is recorded under F1 and F29.

The consumer builds its own intake layer above this service. That layer holds the business vocabulary. A second consumer needs no change to this service.

### Where the boundary falls

A consumer with its own transaction vocabulary shows the line clearly. The shared credentialing programme sends one template that carries four transaction types. Only one of them starts work.

| This service owns | The consumer owns |
| --- | --- |
| Bytes, format gates, structural validation against the mapping | What a transaction type means, and which one starts work |
| One row per record, stored and readable | Rules that need the consumer's own data |
| Ingestion identity, deduplication, retry safety | Approval modes, the correction screen, notifying the sender |
| The upload component and the mapping screen | Where those components sit in its own product |
| Audit and retention | Creating the downstream workflow |

## Architecture

> Diagram (sequence of the intake flow) lives on the source Confluence page — not exported here; re-attach or redraw when this doc gets an assets folder.

1. **The consumer registers itself, once.** It gives an identifier and the topic it wants completion events on. One call, and calling it again returns the same row.
   `POST /v1/register-consumer { identifier, subscriptionTopic? }` &rarr; one `consumers` row, completion topic provisioned.
2. **The consumer creates a mapping.** File column name, target column name, and validations for each column. It is optional; without one, rows are kept raw.
   `POST /v1/mappings { name, columns[] }` (header: `identifier`), `GET /v1/mappings` &rarr; one `consumer_mappings` row, immutable after activation.
3. **The consumer registers one delivery.** It names the mapping and the filename. The tenant comes from the Auth0 token, never from the payload. The response carries one ingestion id and one URL to upload to.
   `POST /v1/ingestion { mappingId?, filename, externalRef?, meta }` (header: `identifier`) &rarr; `{ ingestionId, preSignedUrl, status: REGISTERED, meta }`.
4. **The client uploads the bytes.** Straight to storage. The bytes never cross this service. Nothing can retry this step, so an abandoned upload expires.
   `PUT preSignedUrl` &rarr; `inbound/<identifier>/<tenantId>/<ingestionId>/<registeredAt>__<file>`, URL bound to one object name, `PUT`, `x-goog-if-generation-match: 0`, `x-goog-meta-ingestion-id` required, expires at `uploadUrlTtl`.
5. **Storage tells us the file landed.** We read the ingestion id off the path and find its row, then run the format gates, before anything is parsed.
   `OBJECT_FINALIZE` &rarr; Pub/Sub push, ack deadline 600s, 5 attempts &rarr; dead-letter. `ingestionId` from the object path, cross-checked against the custom metadata. Container and format gates, `maxFileBytes` 500 MB, refused is `INVALID`.
6. **We parse it.** A named mapping turns each row into fields and runs its validations. With no mapping we keep the rows as they came.
   Mapping from the request, else raw mode. One parse cap for the service. Bulk inserts of 500 &rarr; `ingestion_rows`, key `(ingestionId, sheet, row)`. Row status `INGESTED` \| `INVALID`, invalid rows are stored, never dropped.
7. **We tell the consumer it is done.** The screen sees it on the status stream. The backend sees it on the topic. The tracking data it sent comes back unchanged.
   Publish `file-ingestion-completed-<consumer>`, `COMPLETED` \| `PARTIAL` \| `INVALID`. `GET /v1/ingestion/{id}` polls, or streams with `Accept: text/event-stream` (header: `identifier`): the handler re-reads the row every `statusPollInterval` inside the held-open request, pushes each change, and closes on a terminal status. Rows at `/v1/ingestion-rows/{id}`, one JSON page or an NDJSON stream (header: `identifier`).

## Approach

The flow, in order. Nothing is uploaded without a row, and nothing is parsed without its gates.

**1. One service, one image, two runtimes.**

* Quarkus `file-ingestion` on Cloud Run holds the API, the registrar, the receiver, and the sweep. A Cloud Run Job from the same image runs the parse, with its own CPU and memory and no request deadline.
* Why: a long parse must not share a runtime with request traffic. A second consumer is one registration call, not a service.

**2. Register the consumer. Once per consumer, not once per file.**

* `POST /v1/register-consumer` with `{ identifier, subscriptionTopic? }`. The service writes one `consumers` row and provisions the completion topic named on it.
* Why: the consumer needs an identity before it can hold a mapping or a delivery. The topic is named at registration, so onboarding a consumer is one call and one configuration document.
* If it fails: a repeated `identifier` returns the existing row. The call is idempotent.

**3. Create the mapping. Once per file shape.**

* `POST /v1/mappings` takes the file column name, the target column name, and the validations for each column, plus a mapping name. `GET /v1/mappings` lists the mappings a consumer holds.
* A mapping is immutable after activation. A change is a new version.
* Validations are chosen from a fixed engine list, listed under _Contracts and interfaces_. Every validator runs on one cell: there are no cross-field rules and no lookups against other data, and a column's `validations` list may be empty.
* Why: the consumer knows its own file shape, and a screen is faster than a pull request. A mapping is optional, so a consumer can send a first file before it authors one.
* If it fails: a mapping that names a duplicate target column, or a validation the engine does not hold, is refused with the difference. Nothing is written.

**4. Register the delivery and sign the upload.**

* `POST /v1/ingestion` with `{ mappingId?, filename, externalRef?, meta }`. The tenant comes from the Auth0 token claim, never from the payload.
* The service inserts one `ingestions` row with status `REGISTERED`, chooses the object name, and returns `{ ingestionId, preSignedUrl, status, meta }`.
* The signed URL is a V4 signed URL. It is bound to one object name, to `PUT`, to `x-goog-meta-ingestion-id`, and to `x-goog-if-generation-match: 0`. It expires at `uploadUrlTtl`.
* `externalRef` **is the idempotency key.** It is optional, and the shared upload component generates it. A repeated call with the same `identifier` and `externalRef` while the row is still `REGISTERED` returns the existing row and a freshly signed URL for the same object name, so a retry on a dropped connection yields one row, not two. Once the row has moved past `REGISTERED`, the same call returns the existing record with its status and counts and no upload URL; a new file needs a new `externalRef`. This is request level idempotency only. Nothing detects repeated content.
* Why: the service owns the destination name, so the client cannot name the object and cannot write over another delivery. The signed generation precondition means the URL works only while the name is free, so a second upload on the same URL fails.
* If it fails: an unknown consumer, or an unresolved tenant, is refused at the API with no row written. The tenant is never guessed.

**5. Upload. The client writes the bytes.**

* Shared frontend components ship with this service: the upload component and the mapping screen
* The client `PUT`s the file to the signed URL with the two required headers. The object lands in the ingestion bucket under the name the row already holds.
* Why: the bytes go from the browser to GCS and never cross this service, so no request limit sits between the user and the file ceiling.
* If it fails: the row stays `REGISTERED` with no object. **Nothing retries it, because there is nothing to retry.** The sweep sets the row to `EXPIRED` at `registeredExpiryHours`, stamps `terminalAt`, and writes `UPLOAD_NEVER_ARRIVED`. The TTL index removes it later. Alert A1 fires first.
* ```
  FileIngestionUploader
  
  identifier
  onSuccess?
  onError?
  onUploadProgress?
  onIngestionProgress?
  useServerSentEvents?
  
  FileIngestionMappings
  identifier
  columnNames[]
  onSubmit?
  
  ```

NOTE: awaiting designs  
**6. Receive. One finalize event, on one bucket.**

* The receiver reads `bucketId`, `objectId`, `objectGeneration`, and `eventTime`. It takes `<ingestionId>` from the path, and compares it with `x-goog-meta-ingestion-id`. A disagreement parks the delivery. It never resolves by preference.
* Claim: insert `INBOUND_DELIVERY_DETECTED`, unique per object generation. A duplicate insert means a redelivery, so the handler acknowledges and exits.
* Format gates. Refuse anything that is not a plain workbook or a plain delimited file, and anything above `maxFileBytes`. A refused delivery is `INVALID`, and the receiver publishes the terminal event itself.
* Set the row to `RECEIVED`, then call `dispatch(ingestionId)`: take a free parse slot under `maxParallelParses`, set `dispatchedAt` where status is `RECEIVED` and `dispatchedAt` is null, and start one execution of `file-ingestion-parse` through the Cloud Run Jobs API. Acknowledge.
* Why: every object in this bucket was written through a URL this service signed, and the path holds an identifier this service generated. An event that resolves to no row therefore means a write reached the bucket outside this flow.
* If it fails: a failed Jobs API call writes `PARSE_START_DEFERRED` and still acknowledges, because the row is safe in the database. The sweep starts it. Only an infrastructure error negatively acknowledges, and after 5 attempts the message reaches its dead-letter topic and pages.

**7. Validate. Inside the parse job, before any row is read.**

* Claim the ingestion (`RECEIVED` to `VALIDATING`). A second execution exits. `dispatch` holds the parse cap, and the job checks it again as a backstop.
* **Resolve the mapping.** The `mappingId` on the request, if one was named. Otherwise the ingestion parses in raw mode.
* **Mapped.** Check the container, the declared schema version, the sheets, and the headers against the mapping. Any unknown structure rejects the whole delivery as `INVALID`, with the difference recorded.
* **Raw.** Check the container only. There is no schema to disagree with, so nothing can be rejected for structure.
* Why: storing the columns that still parse is silent drift, so a named mapping is enforced strictly. A consumer that only wants its rows kept must not have to write a mapping first.

**8. Parse. One record per populated row.**

* Stream each sheet or each delimited block. Skip and count empty rows.
* **Mapped.** Read each cell into its target column, then run the column validations the mapping carries. The document holds the raw row as an ordered array, the normalized fields, a status, and any warnings. `INGESTED`, or `INVALID` when a required value is empty, a validation fails, or the row belongs to another tenant.
* **Raw.** The document holds the header row and the raw row as an ordered array, and nothing else. Every populated row is `INGESTED`, because there is no rule that can make it invalid.
* Write: buffer 500 documents, then one unordered bulk insert. The unique key `(ingestionId, sheet, row)` makes a replayed insert a no operation. Invalid rows are written too.
* Why: the physical row is the only stable identity. A natural key such as an identifier plus an address is not unique in the samples we hold.

**9. Complete.**

* The ingestion closes only when populated rows equal ingested plus invalid. `COMPLETED`, or `PARTIAL` when invalid rows exist. Every terminal status stamps `terminalAt`.
* **Every terminal status publishes one event** on the consumer's `subscriptionTopic`, with the `meta` object echoed unchanged. Whichever component sets the terminal status publishes it: the receiver for a delivery refused at the gates, the parse job for everything else. A consumer backend waiting on the topic is therefore never left waiting on an `INVALID` delivery.
* If the counts disagree: stay `PARSING`, write `ROW_COUNT_MISMATCH`, page. Stored rows are kept.

**10. Serve and operate.**

* Rows are never edited. A correction is a new delivery, or an operator `reprocess` that creates a new ingestion. `retry` and `reprocess` are the only operator writes.
* **Status comes two ways.** `GET /v1/ingestion/{id}` returns the current status on a poll. The same path with `Accept: text/event-stream` holds the connection open, re-reads the row every `statusPollInterval`, and pushes each status change, so an upload screen shows progress without its own poll loop. The handler reads whichever process wrote the row last, so a status written by the parse job reaches a stream held on any API instance with no change stream and no topic in between (F26).
* **Rows come two ways.** `Accept: application/json` returns one keyset page, which is what a screen wants. `Accept: application/x-ndjson` streams every row straight off a database cursor in one response, so a one million row ingestion is one request instead of a thousand.
* **A row stream opens only once the ingestion is** `COMPLETED` **or** `PARTIAL`**.** Nothing reads a half parsed ingestion, because the completion gate is what makes the counts trustworthy. The status stream has no such gate, because it carries no rows.
* **The consumer can mark a delivery** `CONSUMED`**.** `POST /v1/ingestion-complete/{id}` is optional, and it records that the consumer has read the rows. It changes nothing this service does.
* Sweep, every 5 minutes: set every `REGISTERED` row past `registeredExpiryHours` to `EXPIRED`, `dispatch` every `RECEIVED` row with no live execution, re-offer objects with a claim and no parse after 15 minutes, restart stuck parses, and expire parked rows.
* A `dispatchedAt` older than 10 minutes with no claim is cleared and tried again. Every step above writes one audit event.

### Key decisions

| ID | Decision | Instead of |
| --- | --- | --- |
| F0 | A service that owns the file's lifecycle | A shared parser library that each consumer runs in its own workers |
| F1 | A standalone consumer neutral service. Every per consumer choice lives on its `consumers` row | A module per consumer, or a second copy of the vendor pipeline |
| F2 | Receive, validate, store, notify. No interpretation | Dispositions or workflow creation here |
| F3 | Java 21, Quarkus, fastexcel, commons-csv, RE2J for consumer authored patterns | Node |
| F4 | A Cloud Run service plus a Cloud Run Job | A Cloud Task, which caps at 30 minutes, or an always on worker |
| F5 | MongoDB, the same technology as `attestation-db` | Spanner, which is right if this ever hosts review. PostgreSQL |
| F6 | The client uploads to a presigned URL this service signs, straight into the ingestion bucket | The consumer hands over a GCS URI and the service copies the bytes, or the consumer backend relays the bytes through our request path |
| F7 | Every object in the ingestion bucket arrived through a URL this service signed, under a name this service chose, carrying an identifier this service generated | Bucket IAM alone as proof of where an object came from. A finalize subscription on every consumer owned bucket |
| F8 | The ingestion identifier is a path segment of the object name, and custom metadata carries it as a cross-check. The signed URL binds both | Metadata alone, or a lookup by object name on every event |
| F9 | One `ingestions` collection. The registration and the delivery are two states of one entity, and `ingestionId` is its identity from the signed URL to the row key, so they are one row | A registrations collection and a separate batches collection, which would split storage without splitting identity |
| F10 | One `ingestion_rows` collection, the same envelope for every consumer | One rows collection per consumer |
| F11 | Key `(ingestionId, ``sheet``, row)` | A natural key, which collides |
| F12 | Rows are immutable. A correction is a new ingestion | Edit in place, with a revision history |
| F13 | GCS validates the upload it accepts, so a finalized object is the bytes the client sent. The format gates and the completion gate carry the rest | A client computed hash over the whole file, before the upload starts |
| F14 | `x-goog-if-generation-match: 0` is signed into the upload URL | An unconstrained signed URL, which a client can write twice |
| F15 | Invalid rows are stored and flagged, and the delivery still completes as `PARTIAL` | Rejecting the whole delivery because one row is bad |
| F16 | The receiver runs inline on the Pub/Sub push and acknowledges | A delayed named Cloud Task, which has no dead-letter queue |
| F18 | The mapping is optional and can be named on the request. Without one, rows are stored raw | A mapping is mandatory, so a consumer must author one before it can send its first file |
| F19 | Rows stream from a database cursor in one response, ordered and resumable | A consumer paginating a million rows over a thousand API calls |
| F20 | A row stream opens only after the completion gate | A change stream, which would expose a half parsed ingestion |
| F21 | A mapping is authored in a screen and stored in `consumer_mappings` | A versioned document in the repository, which needs a deploy for every header rename |
| F22 | A consumer registers itself once through the API, and names its own completion topic | A configuration document per consumer, applied by a deploy |
| F23 | Status reaches the upload screen over server sent events, with a poll on the same path as the fallback | The completion event only, which no browser can read |
| F24 | Shared frontend components ship with this service: the upload component and the mapping screen | Each consumer builds its own upload screen against the raw API |
| F25 | A `REGISTERED` row that never receives an object becomes `EXPIRED`, is audited, and is removed by the TTL index on `terminalAt` | Leaving the row in place, so abandoned uploads accumulate as live `REGISTERED` rows |
| F26 | The status stream re-reads the row inside the held-open request and pushes each change. Any API instance sees a write from any process | A Mongo change stream, which needs a background listener with CPU always allocated on every instance, or a Pub/Sub topic that every instance would have to filter |
| F27 | One signed `PUT` for the whole file. A dropped upload is abandoned and registered again | A resumable upload session, which is a longer lived write capability and chunk state in the browser. Revisited if A1, A2, or A12 show drop driven failures |
| F28 | Every call carries the Auth0 access token the consumer application already holds. The tenant is a claim on that token. `identifier` is routing, not an authentication boundary | A credential per consumer, which would authenticate a feature inside one Auth0 client |
| F29 | Source and consumer are one thing, named by `identifier` | A `sourceId` and an `ingestion_sources` collection kept apart from the consumer, so a vendor source with no reader could exist later |

F5 holds while the scope stays bounded, the cluster is a replica set with no sharding, and no other service reads this database.

### Alternatives considered

**Why not a shared library**

1. Retry logic is rebuilt per consumer. Idempotency, duplicate events, partial failures: solved three times, three different ways.
2. A two hour parse runs inside a product service. Portal or Credentialing gives up a worker for the length of the file.
3. Bursty file traffic becomes someone else's scaling problem. A product service scales on volume unrelated to its own traffic.
4. No single system answers the operator's question. What's processing, what failed today, which consumer is failing, which file is stuck.
5. A defect is fixed once per consumer. Same parser bug, three fixes, three release dates.
6. A bad file destabilizes the service it runs inside. No wall between a malformed workbook and request traffic.
7. A fourth consumer is another integration. With the service it's one configuration document.

_The parser libraries are still shared, and the service uses them. The library is not wrong. It is the part of the problem that was already solved._

## Components and files touched

| Zone | What runs there | Talks to |
| --- | --- | --- |
| Outside, not ours | Consumer frontends and consumer backends | Calls the API with the Auth0 token, then `PUT`s to a signed URL |
| Request plane | Cloud Run service `file-ingestion`: consumer API, mapping API, registration API, read API, status stream, finalize receiver | MongoDB, the ingestion bucket, Pub/Sub, the Cloud Run Jobs API |
| Work plane | Cloud Run Job `file-ingestion-parse`, one execution per ingestion, capped for the service | MongoDB, the ingestion bucket, the completion topics |
| Schedule | Cloud Scheduler `file-ingestion-sweep`, every 5 minutes | The request plane, over HTTP |
| Storage | GCS ingestion bucket, versioned. MongoDB `file_ingestion` | Written by this service account, and by holders of a URL it signed |
| Messaging | `file-ingestion-inbound` and its dead-letter topic. One `file-ingestion-completed-<consumer>` per consumer | GCS notifications in, consumer services out |

Two properties follow from that shape.

* No principal writes the ingestion bucket without a URL this service signed, and every such URL names one object whose path holds an identifier this service generated. An object that resolves to no row therefore means a write from outside the flow.
* A parse never runs in the request plane, so a two hour file cannot slow a registration call or a read.

These are the parts inside one service, not separate services. The names below are used in the code, in the logs, and in alerts. They do not appear in the flow above.

| Component | Runs as | Does |
| --- | --- | --- |
| Consumer API | `POST /v1/register-consumer` on Cloud Run service `file-ingestion` | Writes the `consumers` row, provisions the completion topic, idempotent on `identifier` |
| Mapping API | `POST /v1/mappings` and `GET /v1/mappings`, the same service | Validates a mapping against the engine, writes one immutable version, lists what a consumer holds |
| Registrar | `POST /v1/ingestion`, the same service | Validates the Auth0 token and the consumer, resolves the tenant from the token claim, writes the row, chooses the object name, signs the upload URL |
| Receiver | The same service, Pub/Sub push, request timeout 600 s | Claim, link check, format gates, register `RECEIVED`, dispatch, acknowledge |
| Parse job | Cloud Run Job `file-ingestion-parse`, one execution per ingestion, one cap for the service | Structural validation, streaming parse, column validations, bulk inserts, completion gate, publish |
| Sweep | Cloud Scheduler `file-ingestion-sweep`, every 5 minutes | Set `REGISTERED` rows with no upload to `EXPIRED`, dispatch waiting rows, re-offer lost objects, restart stuck parses, expire parked rows |
| Read API | The same service, tenant claim required | Keyset pages, NDJSON row streams off a database cursor, status polls, and SSE status streams |
| Subscriptions and DLQ | `file-ingestion-inbound`, `file-ingestion-inbound-dlq` | 5 attempts, then dead-letter. Operator drain |
| Completion topics | `file-ingestion-completed-<consumer>` | One per consumer, named at registration |
| Ingestion bucket | GCS, versioned, lifecycle by consumer retention | The only copy of every delivery, and the only event source the receiver reads |
| Database | MongoDB `file_ingestion`, 5 collections | Audit events, ingestions, rows, consumers, mappings |
| Identity | Service account `file-ingestion` | Grants in _Security, privacy, and access_ |

**Touched, not owned:** each consumer service that registers, uploads, or reads.

**Repository:** one new repository, `file-ingestion-service`. Packages `api`, `consumer`, `registrar`, `receiver`, `parse`, `mapping`, `sweep`, `audit`, `persistence`. Mapping fixtures and sample files under `resources`. No change to `api-layer` or `core-data-access-layer`.

**Frontend:** two shared components, published from this repository and embedded by each consumer product: the upload component and the mapping screen. They are the only frontend this service owns.

| Prop | Required | Does |
| --- | --- | --- |
| `identifier` | yes | Names the registered consumer. The component calls `GET /v1/mappings` with it to fill the mapping list |
| `onSuccess` | no | Called with the ingestion id and the final counts when the ingestion reaches `COMPLETED` or `PARTIAL` |
| `onError` | no | Called when registration, upload, or parsing fails, with the status and the reason |
| `onUploadProgress` | no | Called with bytes sent during the `PUT`, read from the browser upload, not from this service |
| `onIngestionProgress` | no | Called on every status change after the upload: `RECEIVED`, `VALIDATING`, `PARSING` with counts once available, then the terminal status. Delivered over whichever transport `useServerSentEvents` picked |
| `useServerSentEvents` | no | Default `true`. Opens the SSE status stream. `false` polls `GET /v1/ingestion/{id}` instead |

The upload component runs the four calls in order: list mappings, register the delivery, `PUT` the bytes to the signed URL, then watch the status. Every call carries the Auth0 access token the host application already holds. It never names an object and never holds a credential of its own.

**The status watch.** The component opens `GET /v1/ingestion/{id}` with `Accept: text/event-stream` through `fetch()`, not the browser's `EventSource`, because `EventSource` cannot send an `Authorization` header. It reads the response body off its `ReadableStream`, splits on blank lines, and parses each `event:`/`data:` block. `fetch()` has no reconnect of its own, so the component owns it: when the stream ends or errors before a terminal status, it reopens on a growing delay (1 s, 2 s, 4 s, up to about 30 s) with jitter, and resets the delay once a stream opens. The first frame of every stream is the row's current status, so a reconnect needs no event ids and no replay, and a terminal status reached while the browser was disconnected arrives as that first frame and closes the stream. `useServerSentEvents: false` polls the same path on the same delay instead. Either way, the row is the state; the connection never is.

| Prop | Required | Does |
| --- | --- | --- |
| `identifier` | yes | Names the registered consumer the mapping belongs to |
| `columnNames` | yes | The file's header row, so the screen can offer each file column for mapping |
| `onSubmit` | no | Called with the `mappingId` once `POST /v1/mappings` accepts the mapping |

The mapping screen is a table of file column name, target column name, and validations, plus a mapping name and a submit. It calls `POST /v1/mappings` and reads the validation list from the service, so a new validation needs no frontend release.

## Contracts and interfaces

**Register a consumer.** `POST /v1/register-consumer`

```
{ "identifier": "cred", "subscriptionTopic": "roster-file" }
```

Response: `{ "identifier": "cred", "subscriptionTopic": "file-ingestion-completed-cred", "message": "successfully registered" }`. A repeated call returns the existing row.

**Create a mapping.** `POST /v1/mappings`  
header - identifier 

```
{ "name": "roster-v1",
  "columns": [ { "fileColumn": "NPI", "targetColumn": "npi",
                 "validations": [ "required", "digits:10" ] } ] }
```

The response carries `mappingId`. `GET /v1/mappings` lists every mapping the calling consumer holds. Validations are chosen from a fixed engine list, never supplied as code or as an expression. Every validator runs on one cell. There are no row level or cross field rules, no validator reads lookup data (`enum` is a literal list on the mapping), and a column's `validations` list may be empty.

| Validator | Passes when | Example |
| --- | --- | --- |
| `required` | the cell is not empty | `required` |
| `digits:n` | exactly n digits, nothing else | `digits:10` for an NPI |
| `maxLength:n` | at most n characters | `maxLength:120` |
| `enum:a,b,c` | the value is one of the listed values | `enum:MD,DO,NP` |
| `date:pattern` | the value parses with the pattern | `date:yyyy-MM-dd` |
| `regex:pattern` | the whole value matches the pattern. Runs on RE2J, so no pattern can stall a parse. Compiled at save time, refused if it does not compile or exceeds 200 characters | `regex:^\d{5}(-\d{4})?$` for a US zip |

**Register a delivery.** `POST /v1/ingestion`  
header - identifier 

```
{ "mappingId": "optional, names the mapping to parse with",
  "filename": "roster-september.xlsx",
  "externalRef": "optional, generated by the shared upload component",
  "meta": { "…": "opaque, up to 64 KiB, echoed unchanged" } }
```

Response: `{ "ingestionId": "ing-…", "preSignedUrl": "https://storage.googleapis.com/…", "status": "REGISTERED", "meta": { "…" } }`.

The tenant is read from the Auth0 token claim and never from the payload. The response carries no object name, because the caller has no use for one.

`externalRef` is the idempotency key, and it is optional. The shared upload component generates one per upload attempt. A repeat with the same `identifier` and `externalRef` while the row is `REGISTERED` returns the same row and a fresh URL for the same object name. A repeat after the row has moved on returns the existing record with its status and counts and no URL; a new file needs a new `externalRef`. It is request level idempotency only. Nothing detects repeated content, so the same bytes registered twice are two ingestions.

**Completeness is the format gates plus the completion gate, and nothing more.** GCS accepts only a whole `PUT`, the gates refuse a broken container, and the ingestion closes only when populated rows equal ingested plus invalid. No caller declares a row count.

**Upload.** One `PUT` to `preSignedUrl`, with `x-goog-meta-ingestion-id` and `x-goog-if-generation-match: 0`. Both headers are signed into the URL, so a client that omits or changes either is refused. The URL is valid for `uploadUrlTtl`, and it works only while the object name is free.

**Object name**, chosen by the service and never supplied by a caller:

```
inbound/<identifier>/<tenantId>/<ingestionId>/<registeredAt>__<sanitized filename>
```

Every character outside `A-Za-z0-9._` and the hyphen becomes `_`, and the result is truncated so the whole name stays under the 1024 byte limit. The extension stays last, so content type detection and a human reading the bucket both still work.

**Status.** `GET /v1/ingestion/{id}` returns the row: status, counts, and timestamps. The same path with `Accept: text/event-stream` holds the connection open, sends the current status as the first event, re-reads the row every `statusPollInterval`, pushes one event per change, then closes on a terminal status. An ingestion that is already terminal when the stream opens gets one event and a close.

```
event: status
data: {"ingestionId":"ing-…","status":"PARSING","counts":{"ingestedRows":120000}}
```

**Complete**, on the consumer topic. One event per terminal status, so `COMPLETED`, `PARTIAL`, and `INVALID` all publish. An `INVALID` event carries the reason in place of the counts.

```
{ "ingestionId": "ing-…", "tenantId": "org-xyz", "identifier": "cred",
  "status": "COMPLETED", "counts": { "populatedRows": 47, "ingestedRows": 45, "invalidRows": 2 },
  "externalRef": "org-xyz-2026-09-001", "meta": { "…": "echoed unchanged" },
  "rows": "/v1/ingestion-rows/ing-…" }
```

**Rows.** `GET /v1/ingestion-rows/{id}`. A tenant claim on every call, projections by default and the raw row on request. `Accept: application/json` returns one keyset page.

**Stream.** The same rows path with `Accept: application/x-ndjson`. One row per line, read straight off a database cursor, so neither side holds the result set in memory.

```
{"sheet":"Sheet1","row":2,"status":"INGESTED","fields":{"npi":"1234567890"},"raw":[...]}
{"sheet":"Sheet1","row":3,"status":"INVALID","warnings":["npi missing"],"raw":[...]}
```

Rows arrive in `(sheet, row)` order. `after=<sheet>:<row>` resumes from the last row the consumer committed, so a connection that breaks costs the rows after that point and not the whole file. A row stream is refused while the ingestion is still `PARSING`.

**Mark consumed.** `POST /v1/ingestion-complete/{id}` sets the status to `CONSUMED`. It is optional, it records that the consumer has read the rows, and it changes nothing else.

**Statuses.** `REGISTERED`, `RECEIVED`, `VALIDATING`, `PARSING`, `COMPLETED`, `PARTIAL`, `INVALID`, `EXPIRED`, `CONSUMED`. `EXPIRED` is a `REGISTERED` row whose object never arrived. A delivery held for an operator is `PARKED` on any of them. `COMPLETED`, `PARTIAL`, `INVALID`, and `EXPIRED` are terminal and stamp `terminalAt`.

**Configuration and flags.**

| Key | Where it lives | Type | Default | Controls |
| --- | --- | --- | --- | --- |
| `enabled` | service configuration | boolean | `true` | The service kill switch. Messages wait 7 days |
| `uploadUrlTtl` | service configuration | duration | 60 minutes | How long a signed upload URL stays valid |
| `registeredExpiryHours` | service configuration | integer | `24` | When the sweep sets a `REGISTERED` row that never received an object to `EXPIRED` |
| `statusPollInterval` | service configuration | duration | 2 seconds | How often a held-open status stream re-reads its row |
| `maxParallelParses` | service configuration | integer | `2` | The parse cap for the service |
| `maxConcurrentStreams` | service configuration | integer | `4` | Open row cursors allowed at once |
| `maxFileBytes` | service configuration | integer | 500 MB | The size a delivery is refused above |
| `ingestionTtlDays` | service configuration | integer | `30` | How long an `EXPIRED` or `INVALID` ingestion row is kept before the TTL index removes it. Never below any consumer's `retentionDays` |

Row retention is per consumer, on `consumers.retentionDays`, not in this table. No feature flags outside this table.

## Data model and migration

MongoDB database `file_ingestion`. One replica set, no sharding. Five collections.

| Collection | Holds |
| --- | --- |
| `consumers` | One row per registered consumer, and its retention |
| `consumer_mappings` | One mapping version per consumer: name, columns, validations. Immutable after activation |
| `ingestions` | One delivery: consumer, tenant, chosen object name, `meta`, status, counts, timestamps |
| `ingestion_rows` | One document per populated row, every consumer in one collection. Normalized fields are present only when a mapping was resolved |
| `ingestion_events` | Audit. One event per lifecycle step. Unique per object generation for the claim. Its own retention, never below any consumer's |

**`consumers`**

| Field | Type | Notes |
| --- | --- | --- |
| `identifier` | string | Primary key |
| `subscriptionTopic` | string | Completion topic, named at registration |
| `retentionDays` | integer, default 30 | Row retention. Drives the TTL index on this consumer's rows |
| `registeredAt` | timestamp | |

**`consumer_mappings`**

| Field | Type | Notes |
| --- | --- | --- |
| `mappingId` | string | Primary key |
| `identifier` | string | Owning consumer |
| `name` | string | |
| `version` | integer | A change is a new version, never an edit |
| `columns` | array of `{ fileColumn, targetColumn, validations[] }` | Validations from the fixed engine list |
| `createdAt` | timestamp | |

**`ingestions`**

| Field | Type | Notes |
| --- | --- | --- |
| `ingestionId` | string | Primary key |
| `identifier` | string | Owning consumer |
| `tenantId` | string | From the Auth0 token claim, never the payload |
| `filename` | string | |
| `objectName` | string | Chosen by the service |
| `mappingId` | string, optional | Resolved mapping, or raw mode if absent |
| `externalRef` | string, optional | Idempotency key |
| `meta` | object, up to 64 KiB | Opaque, echoed unchanged |
| `status` | enum | `REGISTERED`, `RECEIVED`, `VALIDATING`, `PARSING`, `COMPLETED`, `PARTIAL`, `INVALID`, `EXPIRED`, `CONSUMED` |
| `parked` | boolean | Layered on top of any status |
| `counts` | `{ populatedRows, ingestedRows, invalidRows }` | |
| `objectGeneration` | long | Claimed at `INBOUND_DELIVERY_DETECTED` |
| `registeredAt` | timestamp | |
| `dispatchedAt` | timestamp, nullable | Cleared and retried by the sweep past 10 minutes with no claim |
| `terminalAt` | timestamp, optional | Set on any terminal status. Key for the TTL index |
| `consumedAt` | timestamp, optional | Set by `POST /v1/ingestion-complete/{id}` |

**`ingestion_rows`** — the envelope. Normalized fields depend on the mapping and are not listed.

| Field | Type | Notes |
| --- | --- | --- |
| `ingestionId`, `sheet`, `row` | | Compound unique key |
| `tenantId` | string | Isolation check |
| `status` | enum | `INGESTED`, `INVALID` |
| `fields` | object, dynamic | Present only when a mapping resolved |
| `raw` | array | Ordered raw cell values, always present |
| `warnings` | array, optional | |

**`ingestion_events`** — the common envelope.

| Field | Type | Notes |
| --- | --- | --- |
| `eventId` | string | |
| `type` | string | One of the event names in _Audit trail_ |
| `tenantId`, `identifier`, `ingestionId` | string | Correlation |
| `objectGeneration` | long, optional | |
| `messageId` | string, optional | Pub/Sub correlation |
| `actor` | string | System or operator |
| `occurredAt` | timestamp | |
| `detail` | object | Free form, event specific |

**Indexes and retention.** `ingestion_rows` carries a TTL index driven by the owning consumer's `retentionDays`. `ingestions` carries a partial TTL index on `terminalAt` where `status` is `EXPIRED` or `INVALID`, set to `ingestionTtlDays`, which is never below any consumer's `retentionDays`, so an ingestion cannot vanish before its rows do. `COMPLETED`, `PARTIAL`, and `CONSUMED` ingestions are kept. `ingestion_events` has its own retention, never below either.

**Rows are a landing zone, not evidence.** Every consumer's rows expire at its `retentionDays`, 30 by default. A consumer that needs a row beyond that copies it. Nothing warns a consumer that rows are about to expire, so the completion event is the signal to read.

Migration is additive only. No PDM, DAL, or Spanner table is touched.

## Security, privacy, and access

* **Caller authentication reuses Auth0.** Every call to this API carries the Auth0 access token the consumer application already holds, in the `Authorization` header. This service validates it through the existing Auth0 flow and reads the authenticated user and the tenant from its claims. A consumer backend that registers a delivery sends the same token the same way. Individual consumers such as Roster or Credentialing are not authenticated separately: one Auth0 client can hold several feature level consumers, so `identifier` is routing and tracking, never an authentication boundary.
* **Least privilege:** create and read objects on the ingestion bucket, sign upload URLs for it, subscribe, publish, run the job, read and write its own database. No PDM or DAL access, and no consumer bucket grants.
* **A signed URL authorizes the write as the signing identity.** The service account therefore needs `storage.objects.create` on the ingestion bucket even though it never sends the bytes, plus `iam.serviceAccounts.signBlob` when the URL is signed through the IAM API rather than a local key. Signing through IAM is preferred, because it keeps no private key in the image.
* **The signed URL is the attack surface of the intake.** It is bound to one object name, to `PUT`, to `x-goog-meta-ingestion-id`, and to `x-goog-if-generation-match: 0`, and it expires at `uploadUrlTtl`. A leaked URL can write one object once, under a name that already has a row, and that write raises the same event the real one would. The URL is a write capability held by client code outside this service, so it is scoped as tightly as GCS signing allows.
* **The caller cannot name the object.** The name is chosen at registration and signed into the URL, so a caller cannot write over another tenant's delivery or plant an object the receiver would misread.
* **Every object resolves to a row.** The path holds an identifier this service generated, and the metadata carries it again. A finalize event that resolves to no row pages, because it means a write reached the bucket outside this flow.
* **The parser is the attack surface of the work plane.** Format by magic bytes, container inspected before it opens, macros and external parts refused, XML entities disabled, no formula evaluation, bounded streaming, nothing parsed that failed a gate.
* **Tenant isolation on every path.** The tenant comes from the Auth0 token claim at registration and is written into the row and the object name. The claim is checked on every query.
* **A mapping is consumer authored, so it is untrusted input.** Validations are chosen from a fixed engine list, never supplied as code or as an expression. A mapping that names a validation the engine does not hold is refused. The one validator that takes a pattern, `regex`, runs on RE2J, which matches in linear time, so no pattern can stall a parse; the pattern is compiled when the mapping is saved and refused if it does not compile or exceeds 200 characters.
* **Data:** provider names, addresses, telephone numbers, national provider identifiers. No member data. TLS and encryption at rest. Logs carry identifiers and counts, never values. `meta` is stored and echoed, never interpreted and never logged.

## Performance and scale

| # | Item | Value |
| --- | --- | --- |
| N1 | Volume | Tens of rows today. Headroom 1,000,000 rows per file |
| N2 | Upload budget | The bytes go from the client to GCS and never cross this service. The upload is bounded by the client's own connection, not by any limit here |
| N3 | Signing budget | Signing a URL is one local operation with no network call. It costs the same whatever the file size |
| N4 | Parse budget | 100,000 rows in 15 minutes. 1,000,000 rows in 2 hours on one execution |
| N5 | Handoff | Registration to signed URL within one request. Upload to parse start within 1 minute. Sweep fallback within 5 minutes |
| N6 | File ceiling | 500 MB, set on `maxFileBytes`. A one million row delimited file lands at roughly that size, so the ceiling and the row headroom agree. The parse job is the binding limit, not any request path |
| N7 | Read budget | A keyset page is 1,000 rows. A stream delivers 1,000,000 rows in one response, bounded by the Cloud Run request timeout rather than by round trips |
| N8 | Status stream budget | One open SSE connection per active upload, carrying status changes only, so it is idle for most of a parse. It cannot outlive the Cloud Run request timeout, so a 2 hour parse means the component reconnects and reads the current status on reconnect |

Stream input. Bulk writes of 500. The parse cap bounds write pressure.

**Cost, at 40 deliveries a month of 200,000 rows each.** GCS Class A operations: $0.005 per 1,000, and 2 operations per delivery, so 80 operations is under $0.01. Ingestion bucket storage, standard class in one region: $0.020 per GB per month, and 40 files at 100 MB is 4 GB, so $0.08 a month and $0.96 after a year of accumulation. Cloud Run Job: $0.00002400 per vCPU second and $0.00000250 per GiB second, and 40 runs of 30 minutes on 2 vCPU with 4 GiB is 144,000 vCPU seconds plus 288,000 GiB seconds, so $3.46 plus $0.72, which is $4.18. Pub/Sub: first 10 GiB free, and this volume stays inside it. MongoDB runs on the existing `attestation-db` cluster at no new cluster cost. Total about $5 a month, plus storage growth.

## Observability

Every alert is evaluated from the database, the queue, or the absence of an audit event, so a dead service still alerts.

| # | Condition | Threshold | Response |
| --- | --- | --- | --- |
| A1 | Registered, no object in the ingestion bucket | 1 hour warn, `registeredExpiryHours` expire | The user abandoned the upload, or the upload failed every time. Nothing retries it. The consumer registers again |
| A2 | The rate of registrations that never receive an object, counting one row per `externalRef` where one was sent and one per `ingestionId` otherwise | 20 percent over an hour | The upload component or a consumer network path is failing. Page |
| A3 | Object present, no claim | 15 minutes | Page. The event was lost. The sweep re-offers it |
| A4 | Received, no execution | 30 minutes warn, 4 hours page | The cap is holding, or every dispatch failed |
| A5 | Parse stuck, no running execution | 30 minutes | Page. The sweep restarts it |
| A6 | Counts do not reconcile | at once | Page. The gate holds |
| A7 | No sweep run | 20 minutes | Page. Nothing above self heals without it |
| A8 | Dead-letter topic not empty | at once | Page |
| A9 | A finalize event whose identifier resolves to no row | at once | Page. A write reached the bucket outside this flow |
| A10 | A delivery rejected because its structure no longer matches the mapping, or a file refused at the gates | at once | Notify or page |
| A11 | Delivery parked, not cleared | 4 hours notify, 24 hours the sweep expires it | The delivery is inconsistent |
| A12 | A `PUT` refused on a signed URL, by expiry or by the generation precondition | 5 in an hour for one consumer | Notify. A slow client, or a retry loop in the upload component |

Correlation: Pub/Sub message identifier, then object generation, then `ingestionId`, then sheet and row, then row identifier. `identifier` and `tenantId` are on every metric and every log line.

## Audit trail

One collection, `ingestion_events`, created and owned here. Append only. **Audit retention is set separately from row retention and never below it.** Rows expire at each consumer's `retentionDays`; the events that record what arrived, from whom, and when do not. Written in the same transaction as the state change where one exists, otherwise as a fact record idempotent on its natural key. Every step writes exactly one event.

Common envelope on every event: `eventId`, `type`, `tenantId`, `identifier`, `ingestionId`, `objectGeneration`, `messageId`, `actor`, `occurredAt`, `detail`.

| Step | Event | Committed in |
| --- | --- | --- |
| Consumer API: registered | `CONSUMER_REGISTERED` | the `consumers` insert |
| Mapping API: created, or refused | `MAPPING_CREATED` / `MAPPING_REFUSED` | the `consumer_mappings` insert, or its own write |
| Registrar: row written and URL signed | `INGESTION_REGISTERED` / `UPLOAD_URL_SIGNED` | the `ingestions` insert |
| Registrar: refused | `CONSUMER_UNKNOWN` / `TENANT_UNRESOLVED` | its own write |
| Sweep: no object ever arrived | `UPLOAD_NEVER_ARRIVED` | the `REGISTERED` to `EXPIRED` transition |
| Receiver: object seen | `INBOUND_DELIVERY_DETECTED` | its own write, unique per object generation |
| Receiver: identifier resolves to nothing, or the two links disagree | `INGESTION_UNRESOLVED` / `INGESTION_LINK_MISMATCH` | its own write |
| Receiver: gates, then the terminal event | `INBOUND_REFUSED` / `COMPLETION_EVENT_PUBLISHED` | its own write |
| Receiver: parked, received, or deferred while the service is disabled | `INGESTION_PARKED` / `INGESTION_RECEIVED` / `INBOUND_DEFERRED_PAUSED` | the status transition |
| Receiver: parse started or deferred | `PARSE_EXECUTION_STARTED` / `PARSE_START_DEFERRED` | its own write |
| Parse: claimed, or exited at the cap | `INGESTION_CLAIMED` / `PARSE_DEFERRED_AT_CAP` | the `RECEIVED` to `VALIDATING` transition |
| Parse: structure | `SCHEMA_VERSION_RESOLVED` / `STRUCTURE_VALIDATED` / `STRUCTURE_REJECTED` | the status transition |
| Parse: one sheet done | `SHEET_PARSED` | its own write, after the last bulk write |
| Parse: gate | `INGESTION_COMPLETED` / `ROW_COUNT_MISMATCH` | the completion transaction |
| Parse: published, or exhausted | `COMPLETION_EVENT_PUBLISHED` / `PARSE_FAILED` | its own write |
| Consumer marks it read | `INGESTION_CONSUMED` | the status transition |
| Sweep | `SWEEP_COMPLETED` / `SWEEP_DISCREPANCY_FOUND` | its own write |
| Operator | `INGESTION_RETRY_REQUESTED` / `INGESTION_REPROCESS_REQUESTED` / `DLQ_REPLAYED` | its own write |
| Admin hard delete of a row | `ROW_HARD_DELETED` | the delete transaction |

## Failure modes and rollback

| Case | Handling |
| --- | --- |
| The user closes the tab before the upload finishes | The row stays `REGISTERED` with no object, and nothing retries it. The sweep sets it to `EXPIRED` at `registeredExpiryHours`, stamps `terminalAt`, and writes `UPLOAD_NEVER_ARRIVED`; the TTL index removes it at `ingestionTtlDays`. Alert A1. The consumer registers again |
| The signed URL expires mid upload | The `PUT` is refused. The row stays `REGISTERED` and expires as above. Alert A12 counts it. `uploadUrlTtl` is raised if this is common. If A1, A2, or A12 show drop driven failures are recurring, a resumable upload session is the next step (F27) |
| The client uploads twice on the same URL | `x-goog-if-generation-match: 0` refuses the second write. One object, one finalize event |
| A signed URL leaks | It writes one object once, under a name that already has a row, and that write raises the same event the real one would. The URL expires at `uploadUrlTtl` |
| The same event is delivered twice | The claim insert fails on the duplicate. The second run acknowledges and exits |
| A finalize event resolves to no row | Parked and alerted, never dropped. A row is never created from an event |
| The path identifier and the metadata disagree | Parked. It never resolves by preference |
| The handler exceeds the acknowledgement deadline | Pub/Sub redelivers. The claim insert fails. The second run acknowledges and exits |
| The owner of a claim crashes | The object has an event and no parse. The sweep re-offers it after 15 minutes |
| The upload is truncated by a dropped connection | GCS never finalizes a partial `PUT`, so no object appears and no event fires. It is the abandoned upload case above |
| A data file is corrupted before the client sends it | Not detectable here. The container and format gates catch a broken workbook. A file that parses but holds wrong values is the consumer's to catch |
| The same bytes are registered twice | Two rows, two objects, two ingestions, two completion events. Nothing detects repeated content; `externalRef` is request level idempotency only, and the consumer decides what a resend means |
| A bad container or an unknown structure | Refused or rejected before any row is stored, status `INVALID`, with the difference recorded |
| A consumer authors a mapping that names an unknown validation | Refused at `POST /v1/mappings` with the difference. Nothing is written |
| Two executions for one ingestion | `dispatchedAt` stops the second start. The claim lets one proceed |
| A crash mid sheet | Re-stream. Inserts are no operations |
| Counts disagree | Stays `PARSING`. Page. Stored rows are kept |
| A status stream breaks part way | The component reopens it on a growing delay with jitter, or falls back to a poll on the same path. The first event of the new stream is the current status, so a terminal status reached while disconnected is not missed. No state is lost, because the row holds the status |
| A read stream breaks part way | The consumer resumes with `after=<sheet>:<row>`, the last row it committed. Rows are immutable and ordered, so a resumed stream cannot skip or repeat |
| A consumer reads slowly and holds a cursor open | `maxConcurrentStreams` bounds open cursors for the service. Beyond it a stream is refused, not queued |
| Rejected, then fixed | Operator `retry` on the same ingestion. The object is already in the bucket, so no re-upload |
| The service is disabled | Registration is refused, so no URL is signed and no upload starts. An object already in flight acknowledges and writes `INBOUND_DEFERRED_PAUSED`. Never negatively acknowledge, or the dead-letter topic fills |

Negatively acknowledge only on infrastructure errors. Every business outcome acknowledges.

**Rollback.** `enabled = false` holds the service, and refusing registration stops new uploads at the door. There is no per consumer pause. Messages wait 7 days. Uploaded objects, audit events, ingestions, and rows always stay. The schema is additive only.

## Test strategy

One behaviour to prove first per sub-task, written before the code.

| Sub-task | Prove first |
| --- | --- |
| Consumer API | A repeated `identifier` returns the existing row and writes no second one. The named topic is provisioned once |
| Mapping API | A mapping naming an unknown validation is refused with no write. A `regex` that does not compile on RE2J, or exceeds 200 characters, is refused with no write. A mapping is immutable after activation. `GET` lists only the calling consumer's mappings |
| Registrar | An unknown consumer is refused with no row. A call without a valid Auth0 token is refused with no row. One call yields one row, one object name, and one URL. The tenant comes from the token claim, and a tenant in the payload is ignored |
| Signed URL | A `PUT` to the URL lands the object at the exact name in the row. A second `PUT` on the same URL is refused. A `PUT` after `uploadUrlTtl` is refused. A `PUT` to a different object name, or one missing a signed header, is refused |
| Idempotent registration | Two calls with the same `identifier` and `externalRef` yield one row and one object name. A2 counts them once. The same call after the ingestion is terminal returns the record with status and counts and no URL |
| Abandoned upload | A registration with no upload becomes `EXPIRED` at `registeredExpiryHours` with `terminalAt` set, writes `UPLOAD_NEVER_ARRIVED`, and fires A1. It is never retried. The TTL index removes it at `ingestionTtlDays` |
| Receiver | Five deliveries of one event yield one claim and one parse. An identifier with no row parks and alerts. A metadata disagreement parks |
| Gates | Truncated, macro, OLE2, renamed CSV, entity expansion, and oversize files each land on their outcome |
| Validation | A renamed sheet resolves. An extra sheet is set aside. A removed required column rejects with the difference |
| Parse | The first consumer's sample yields one row per populated row and `COMPLETED`. A column validation from the mapping marks a bad row `INVALID` and the delivery `PARTIAL`. A kill during a bulk write still leaves one record per row |
| Completion | Two executions: one proceeds. Populated not equal to ingested plus invalid holds the gate. `meta` is echoed byte for byte |
| Isolation | A one million row parse does not delay a registration call or a row read |
| Sweep | A lost event is re-offered. A stuck parse is restarted. A parked row expires at 24 hours. An abandoned registration expires |
| Status stream | An SSE stream sends the current status first, shows every status change in order, and closes on a terminal status. A stream opened on an already terminal ingestion gets one event and a close. A dropped stream reopens with backoff, or falls back to a poll, with no lost state. A status written by the parse job reaches a stream held on a different API instance |
| Streaming | A million row ingestion streams in one response. A connection killed at row 500,000 resumes from that row with no gap and no repeat. A row stream on a `PARSING` ingestion is refused |
| Upload component | The four calls run in order against a stub. An upload failure calls `onError` with the reason. Progress is reported during the `PUT` |
| Performance | 1,000,000 generated rows within 2 hours. Read latency holds during a parse |

## Rollout

1. Agree the first consumer contract: the file kinds, and who is told about a rejected delivery.
2. Provision in a non-production project: database, ingestion bucket, subscription and dead-letter topic, one completion topic, job, scheduler, and service account.
3. Register the first consumer and author its first mapping through the API. Confirm the mapping list fills the component.
4. Ingest the first consumer's sample end to end through the component. Replay it. Confirm one ingestion and one record per row.
5. Enable production ingestion for the first consumer behind the kill switch. Watch A1 to A12 through one delivery cycle, and watch A1, A2, and A12 closely, because the abandoned upload is the one failure mode nothing retries and those three decide whether a resumable upload is ever needed.
6. Register the second consumer through the API. No engine change proves the seam.
7. Reopen the datastore decision before this service is asked to host review.

### Decisions taken

| ID | Question | Decision |
| --- | --- | --- |
| H1 | Are rows evidence, with multi-year retention? | No. Rows are a landing zone: every consumer's rows expire at its `retentionDays`, 30 by default, through a TTL index. Audit retention is set separately and never falls below it |
| H2 | Service name and repository | `file-ingestion-service` |
| H3 | Owning team and on-call | Dev and Suhasini |
| H5 | Which intake ships? | The presigned upload, and nothing else |
| H6 | The file ceiling | 500 MB, set on `maxFileBytes`. It agrees with the one million row headroom |
| H7 | Which store owns a row | MongoDB, in this service. There is no Spanner path |
| H9 | Does this service read any consumer bucket? | No. The presigned intake needs none |
| H12 | Where the bytes land | Straight into the ingestion bucket. There is no staging bucket |
| H13 | What proves the file is whole | GCS validates the upload it accepts. No declared checksum is computed or compared |
| H14 | Who owns the upload screen | This service ships two shared components. The consumer product embeds them |
| H15 | What stops abandoned registrations filling `ingestions`? | A partial TTL index on `terminalAt` for `EXPIRED` and `INVALID` rows, at `ingestionTtlDays`, never below any consumer's `retentionDays`. Completed, partial, and consumed rows are kept |
| H16 | Is a source distinct from a consumer? | No. `identifier` names both. `sourceId` and `ingestion_sources` are removed, and the per source settings became service settings, except `retentionDays`, which sits on `consumers` |
| H17 | How does the status stream learn about a change? | The handler re-reads the row inside the held-open request every `statusPollInterval`. The client opens it through `fetch()` so it can carry the Auth0 token, and owns its own reconnect. Change streams are parked until volume justifies a background listener on every instance |

Source of truth for this document: `cos-docs/platform/file-ingestion-service/file-ingestion-service.md`.