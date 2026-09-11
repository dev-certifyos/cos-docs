# Roster Processing — upload, validation, ingestion: architecture, scaling, and reliability

**Repos referenced:** `api-layer` (Quarkus, Java), `frontend` (Next.js, `apps/web`), both under `pdm-dal-full/fe-api-coredal`. All file paths below are relative to the repo named in the path prefix. Line numbers are as of 2026-09-11 on the local checkout and will drift; the file and symbol names are the durable pointer.

---

## 1. Purpose and scope

This document explains how a roster file moves from a browser upload to imported practitioner data, which Cloud Run service or job runs each step, how the system behaves when many files arrive at once, and where the current design is strong or fragile. It is written so an engineer who has never opened the roster code can read it top to bottom and then navigate the code with the evidence pointers.

**In scope:**

- Upload size limits at every layer (browser, HTTP edge, Quarkus, Cloud Run).
- The two dispatch modes for background work: Pub/Sub consumer mode and Cloud Run Job mode.
- The full status lifecycle of a roster job.
- The scaling model of the roster worker, with a worked example of ten simultaneous uploads.
- The acknowledgement-deadline extension mechanism, what happens when it is exceeded, and how this compares to accepted practice on GCP.
- A reliability assessment and recommendations.

**Out of scope:** the content of the validation rules themselves (schema, NPI, address, group lookups), the column-mapping and template features, the roster editor, and the SmartFix flow.

---

## 2. One-paragraph summary

A user uploads a CSV, XLSX, or TXT file through the frontend. The `api-service` Cloud Run service accepts it over HTTP, writes the raw file to a GCS bucket, creates a roster job row, and publishes a small trigger message to a Pub/Sub topic. A second Cloud Run service built from the same image, `api-service-roster-worker`, holds a streaming-pull subscription on that topic. It processes one message per instance at a time, streams the file back out of GCS, validates it row by row into Spanner, and marks the job validated or failed. After a human approves the job, the same pattern repeats on a second topic for ingestion into the core data layer. In production the worker runs two instances, so at most two rosters are being processed at any moment and the rest queue in Pub/Sub. Long files are kept alive by extending the Pub/Sub acknowledgement lease for up to 3 hours (validation) or 10 hours (ingestion). A file that exceeds that window is redelivered and re-processed concurrently with the still-running first attempt, because the re-entry guard does not recognise the in-progress statuses. An alternate Cloud Run Job dispatch mode exists in the code and is live on the `internal` environment; it avoids most of these limits but is not enabled in production, staging, or demo.

---

## 3. Upload size limits

Each layer enforces a different number. The effective limit is the smallest one on the path, which today is the Cloud Run HTTP/1 ingress cap, not anything the application configures.

| Layer | Limit | Evidence |
| --- | --- | --- |
| Frontend `FileInput` default | 500 MB | `frontend/apps/web/components/common/file-input/file-input.tsx:11` sets `maxSizeMB = 500`. |
| Frontend roster upload form | Inherits 500 MB (no override) | `frontend/apps/web/features/roster/components/roster-upload/roster-upload-form/roster-upload-form.tsx:541` renders `<FileInput accept=… onFilesSelected=…>` with no `maxSizeMB` prop. |
| Frontend roster re-upload | No size check at all | `frontend/apps/web/features/roster/components/roster-list/roster-list.tsx:308` uses a raw `<input type="file">`, bypassing `FileInput`. |
| Frontend template import | 20 MB | `frontend/apps/web/features/roster/constants/index.ts:347` `TEMPLATE_IMPORT_MAX_FILE_SIZE_BYTES`. Applies only to template-from-scratch import, not roster upload. |
| Frontend generic documents | 50 MB | `frontend/apps/web/constants/common.ts:234` `MAX_FILE_SIZE`. Used by outreach and bulk-document uploads, not roster. |
| Quarkus request body | 500 MB | `api-layer/src/main/resources/application.properties:9` `quarkus.http.limits.max-body-size=500M`. The comment on line 8 says "extended to 20MB" and is stale. |
| Quarkus multipart attribute | 250 MB | `application.properties:15` `quarkus.http.limits.max-form-attribute-size=250M`. |
| Roster endpoint code | No check | `api-layer/src/main/java/com/certifyos/api_layer/roster/resource/RosterResource.java:230-245` accepts `FileUpload` with no size validation; `RosterService.java:664-669` reads the whole file into a byte array with `Files.readAllBytes`. |
| Cloud Run ingress | 32 MiB | Cloud Run enforces a 32 MiB request body limit on HTTP/1 services. The API service declares `ports { name = "http1" }` at `api-layer/terraform/feature/cloudrun.tf:143-145`. Requests above this receive a 413 before Quarkus sees them. |
| Load balancer timeout | 30 seconds | `api-layer/terraform/release/loadbalancer.tf:29` `timeout_sec = 30` on the backend service. Slow uploads on poor connections time out here regardless of size. |

**Consequences:**

- A user can select a 200 MB file, the browser will accept it, and the upload will fail with an opaque 413 from Cloud Run. The application never gets a chance to return a friendly error.
- The reupload path has no client-side guard, so the same failure occurs with no warning at all.
- Reading the whole upload into memory in `uploadFileAndGetPath` means the API instance holds the full file in heap before writing to GCS. At the current 32 MiB effective cap this is harmless, but it would become a problem if the ingress cap were lifted without changing this code.

---

## 4. Components and deployment topology

### 4.1 The single image, three services, two jobs

All roster code lives in one Quarkus application and one container image. Behaviour is switched by environment variables at deploy time.

| Deployed unit | Kind | Role in the roster flow | Key env vars | Evidence |
| --- | --- | --- | --- | --- |
| `api-service` | Cloud Run service | Accepts uploads, writes GCS, creates the job row, publishes or dispatches the trigger. Does **not** run consumers. | `PUB_SUB_CONSUMERS_ENABLED=false`, `ROSTER_VALIDATION_DISPATCH_MODE` and `ROSTER_INGESTION_DISPATCH_MODE` from GitHub environment variables | `api-layer/.github/workflows/deploy-production.yml:168, 190-191` |
| `api-service-roster-worker` | Cloud Run service | Runs the three roster Pub/Sub consumers. Parses, validates, ingests. | `PUB_SUB_CONSUMERS_ENABLED=true`, `--no-cpu-throttling`, prod `MEM=6Gi MIN_INSTANCES=2 MAX_INSTANCES=20`, staging `MEM=4Gi MIN_INSTANCES=1 MAX_INSTANCES=10` | `deploy-production.yml:291-336`, `deploy-roster-worker-staging.yml:81-120` |
| `api-service-worker` | Cloud Run service | Webhook and monitoring consumers. Not involved in roster processing. | `PUB_SUB_CONSUMERS_ENABLED=false`, `WEBHOOK_CONSUMER_ENABLED=true` | `deploy-production.yml:421-461` |
| `roster-validation-job` | Cloud Run Job | One execution per roster, used only when dispatch mode is `cloud-run-job`. Native image. | `ROSTER_JOB_TYPE=VALIDATION` and roster identity as per-execution overrides; `JOB_MEM=2Gi` | `deploy-roster-jobs-native-prod.yml:113-117` |
| `roster-ingestion-job` | Cloud Run Job | Same, for ingestion. | `ROSTER_JOB_TYPE=INGESTION` | `deploy-roster-jobs-native-prod.yml:137-141` |

The consumers check a single flag at startup. `RosterValidationConsumer` reads `gcp.pubsub.roster.enabled` (bound to `PUB_SUB_CONSUMERS_ENABLED`) at `api-layer/src/main/java/com/certifyos/api_layer/roster/messaging/RosterValidationConsumer.java:84` and returns early from `init()` at line 126 when it is false. This is why only the roster-worker service ever pulls from the roster subscriptions.

### 4.2 Which dispatch mode is live where

The dispatch mode is a deploy-time setting, not a code default. The code default is `pubsub` (`application.properties:790, 797`).

| Environment | Validation / ingestion dispatch mode | Source |
| --- | --- | --- |
| production | `pubsub` | GitHub environment variable `ROSTER_VALIDATION_DISPATCH_MODE=pubsub`, set 2026-03-05 (`gh variable list --env production`) |
| staging | `pubsub` | GitHub environment variable, set 2026-05-07 |
| demo | `pubsub` | GitHub environment variable, set 2026-03-05 |
| internal | `cloud-run-job` | Hard-coded in `deploy-roster-worker-internal.yml:116-117` and `deploy-canary-internal.yml:97-98` |
| feature branches | `cloud-run-job` | Hard-coded in `api-layer/terraform/feature/cloudrun.tf:60-61`; the feature roster-worker also has `PUB_SUB_CONSUMERS_ENABLED=false` at line 28 and therefore idles |

The Cloud Run Jobs are created and updated on every production and staging deploy too (`deploy-production.yml:388, 408`), so they exist in those projects and are ready to use, but nothing calls them while the mode stays `pubsub`.

### 4.3 Pub/Sub topics and subscriptions

| Topic | Subscription | Publisher | Consumer | Payload | Evidence |
| --- | --- | --- | --- | --- | --- |
| `roster-validation` | `roster-validation-sub` | `RosterEventPublisher.publishValidationRequest` | `RosterValidationConsumer` | `{rosterId, tenantId, userId}` plus trace attributes | `application.properties:733, 736`; `RosterEventPublisher.java:219-236` |
| `roster-ingestion` | `roster-ingestion-sub` | `RosterEventPublisher.publishIngestionRequest` | `RosterIngestionConsumer` | same | `application.properties:734, 737` |
| `roster-ingestion-rows-request` | (consumed by the DAL worker, outside this repo) | `RosterEventPublisher.publishRosterTransaction` | DAL | base64 `BatchRequest` JSON | `application.properties:809`; only used when `roster.processing.use-pubsub=true` |
| (DAL publishes) | `roster-ingestion-rows-response-sub` | DAL | `RosterRowResultConsumer` | `{status: success\|error, rowData…}` | `application.properties:810` |
| `roster-logging` | `roster-logging-sub` | `RosterLoggingService.publishEvent` | observability pipeline | structured events (`ROSTER_CREATED`, `ROSTER_VALIDATION_STARTED`, …) | `application.properties:752-758` |

The file itself never travels over Pub/Sub. Every consumer re-reads it from GCS using the path stored on the roster row.

Both trigger topics and their subscriptions are created by the application on first use if they do not exist (`RosterEventPublisher.java:221-227` for topics; `RosterValidationConsumer.java:92-100` for the subscription). Subscription settings such as ack deadline and retry policy are applied only at creation time; changing the property later does not alter an existing subscription.

---

## 5. Status lifecycle

`RosterJobStatus` is the enum at `api-layer/src/main/java/com/certifyos/api_layer/roster/model/RosterJobStatus.java:4-14`. It contains `PENDING`, `IN_PROGRESS`, `VALIDATION_IN_PROGRESS`, `VALIDATION_FAILED`, `PRE_PROCESSING`, `VALIDATED`, `COMPLETED`, `APPROVED`, `PARTIAL_IMPORTED`, `REVALIDATE`, `FAILED`.

```
Upload
  │
  ├─ internal admin or .txt file ──► PRE_PROCESSING  (analyst scripts; no automatic dispatch)
  │
  └─ everyone else ───────────────► PENDING  ──► trigger dispatched
                                                  │
                                    VALIDATION_IN_PROGRESS  (worker sets on entry)
                                                  │
                              ┌───────────────────┴───────────────────┐
                        VALIDATED                             VALIDATION_FAILED
                              │                                       │
                              │◄─────── user fixes rows / REVALIDATE ─┘
                              │
                        user approves ──► ingestion trigger dispatched
                              │
                        IN_PROGRESS  (worker sets on entry)
                              │
              ┌───────────────┼───────────────┐
         COMPLETED       CRED_STARTED       FAILED
```

- The decision between `PRE_PROCESSING` and `PENDING` is made by `UserPermissions.determineStatus()`. Validation is dispatched only when the result is `PENDING` (`api-layer/src/main/java/com/certifyos/api_layer/roster/service/UserPermissions.java:51-53`, called from `RosterService.java:655`).
- `VALIDATION_IN_PROGRESS` is written by the validator at `RosterValidator.java:218-221` immediately before it begins streaming the file.
- `IN_PROGRESS` is written by the ingestion processor at `RosterProcessor.java:150-152`.
- Both in-progress states are real, persisted, and visible in the UI. What they are **not** used for is discussed in section 8.

---

## 6. Lifecycle in Pub/Sub mode (production, staging, demo)

### 6.1 Upload: synchronous HTTP on `api-service`

1. The browser posts `multipart/form-data` to `POST /roster` with fields `file`, `templateId`, `jobType`, optional `sheetName` and `mappings` (`frontend/apps/web/features/roster/hooks/use-roster-file-upload.ts:29-40`; `frontend/apps/web/features/roster/services/index.ts:115-125`).
2. `RosterResource` binds the multipart to `RosterCreateRecord` and calls `RosterService.createRoster` (`RosterResource.java:230-298`).
3. `createRoster` pre-generates the roster ID, reads the temp upload fully into memory, and writes it to GCS at `roster-uploads/{rosterId}/{fileName}` (`RosterService.java:664-669`).
4. It creates the roster row in the DAL with the status decided in section 5 (`RosterService.java:638-644`).
5. If the status is `PENDING`, it calls `RosterValidationDispatcher.dispatch(rosterId, tenantId, userId)` (`RosterService.java:655-657`). In `pubsub` mode the concrete dispatcher is `PubSubValidationDispatcher`, which calls `RosterEventPublisher.publishValidationRequest` (`PubSubValidationDispatcher.java:18-20`). This publishes a JSON body of exactly three fields to the `roster-validation` topic (`RosterEventPublisher.java:229-232`).
6. The HTTP response returns. Parsing has not started. The entire step takes seconds regardless of file size, bounded only by the GCS write.

### 6.2 Trigger pickup: `api-service-roster-worker`

1. At boot, each worker instance's `RosterValidationConsumer.init()` builds a streaming-pull `Subscriber` on `roster-validation-sub` (`RosterValidationConsumer.java:104-160`).
2. Flow control is `max-concurrent-messages=1` and `parallel-pull-count=1` (`application.properties:740, 742`; applied at `RosterValidationConsumer.java:106-111`). Each instance leases **one** message at a time and does not lease another until it acks or nacks the first.
3. The subscriber is configured with `setMaxAckExtensionPeriod(3 hours)` and `setMinDurationPerAckExtension(600 seconds)` (`RosterValidationConsumer.java:157-158`; values from `application.properties:745, 748`). While the handler runs, the client library silently extends the lease every ten minutes, up to three hours total.
4. The handler `processValidationMessage` parses the three-field payload, restores the trace context, and calls `RosterValidator.validateRosterFromFileStream(rosterId, tenantId, userId)` **synchronously on the callback thread** (`RosterValidationConsumer.java:190-216`).

### 6.3 Validation: `RosterValidator.validateRosterFromFileStream`

1. Publishes `ROSTER_VALIDATION_STARTED` to the logging topic and loads the roster row from DAL (`RosterValidator.java:190-193`).
2. **Re-entry guard.** If the status is `REVALIDATE`, it branches to a Spanner-only re-check. If the status is in `{VALIDATED, VALIDATION_FAILED, COMPLETED, FAILED}`, it logs an error event and returns without doing work, which makes redelivery of an already-finished job harmless (`RosterValidator.java:204-216`).
3. Sets status to `VALIDATION_IN_PROGRESS` and calls `initializeSpannerRosterData(rosterId)`, which resets the roster's row table (`RosterValidator.java:218-224`).
4. Opens a streaming read from GCS (`bucketService.readFileAsStream`) and fetches the effective JSON schema for the template and roster type (`RosterValidator.java:241-243`).
5. Branches on file extension: `.xlsx` goes to `processExcel` (Apache POI), `.csv` to `processCsv` (Commons CSV with first record as header and trimming) (`RosterValidator.java:244-262`). Rows are pulled through an iterator, so the file is never fully materialised in memory.
6. `consumeIteratorInBatches` accumulates `VALIDATION_BATCH_SIZE = 300` rows (`api-layer/src/main/java/com/certifyos/api_layer/utils/RosterProcessingUtils.java:65`) and hands each batch to `processBatch`, which fans every row to the shared thread pool with `CompletableFuture.runAsync` and joins on `allOf` (`RosterValidator.java`, method `processBatch`). Per row: schema validation, business validation with Caffeine-cached NPI, location, and group lookups, and address standardisation.
7. Row results are written to Spanner in `SPANNER_MUTATION_CHUNK = 500` mutation chunks (`RosterValidator.java`, constant `SPANNER_MUTATION_CHUNK` and method `writeMutationsInChunks`).
8. On completion, if any row is `VALIDATION_FAILED` or `VALIDATED_WITH_WARNINGS` the job becomes `VALIDATION_FAILED`, otherwise `VALIDATED` (`RosterValidator.java`, the `jobStatus` assignment following `processCsv`/`processExcel`). An export CSV is generated asynchronously, a webhook is fired, and a logging event is published.
9. Any exception in the outer try marks the job `VALIDATION_FAILED` (`RosterValidator.java:225-231`).

### 6.4 Acknowledgement

- When `validateRosterFromFileStream` returns normally, the consumer calls `consumer.ack()` (`RosterValidationConsumer.java:216`, via `acknowledgeMessage`).
- `IllegalArgumentException` (roster not found) is also acked, because retrying cannot help (`RosterValidationConsumer.java:218-220`).
- Any other exception calls `consumer.nack()`, which makes Pub/Sub redeliver the message immediately (`RosterValidationConsumer.java:221-235`).
- Only after ack or nack does the subscriber lease the next message.

### 6.5 Approval and ingestion

1. A user calls `GET /roster/{rosterId}/import?action=APPROVE` (`frontend/apps/web/features/roster/services/index.ts:145-156`). `RosterService` calls `RosterIngestionDispatcher.dispatch` (`RosterService.java:864`), which publishes the same three-field payload to `roster-ingestion`.
2. `RosterIngestionConsumer` on the roster worker has identical flow control and a 10-hour ack extension cap (`application.properties:741, 746`). It calls `RosterProcessor.processRosterFromFileStream` synchronously (`RosterIngestionConsumer.java:153`).
3. Despite its name, `processRosterFromFileStream` does not re-read the file. Its re-entry guard checks only `{COMPLETED, FAILED}` (`RosterProcessor.java:141-146`). It sets `IN_PROGRESS` (`RosterProcessor.java:150-152`) and streams `VALIDATED` and `VALIDATED_WITH_WARNINGS` rows from Spanner in batches of `roster.processing.batch-size=100` (`application.properties:68`; `RosterProcessor.java:50-51`).
4. Each row goes through `RosterRecordHandler`, which builds a DAL transaction. The branch on `roster.processing.use-pubsub` (`application.properties:787`, default `false` including `%prod`) decides:
   - **false (live everywhere today):** `executeTransactionalBatch` calls DAL over HTTP synchronously; the row is marked `COMPLETED` or `FAILED` inline; when the stream ends `updateRosterWithFinalStatus` sets the job status (`RosterRecordHandler.java:303, 1408`; `RosterProcessor.java:387-388`).
   - **true (built, not enabled):** `publishRosterTransaction` sends a base64 `BatchRequest` to `roster-ingestion-rows-request`; the row is marked `PENDING`; `RosterRowResultConsumer` later updates each row from the DAL's response and, after every update, runs `SELECT 1 FROM roster_rows WHERE roster_id=@rosterId AND status IN ('VALIDATED','VALIDATED_WITH_WARNINGS','PENDING') LIMIT 1`; when it returns nothing it calls `completeRosterFromSpanner` (`RosterRecordHandler.java:374, 1391`; `RosterRowResultConsumer.java:504-524`).

---

## 7. Lifecycle in Cloud Run Job mode (internal, feature branches)

- The API-side steps 1 through 4 of section 6.1 are identical. The only change is the concrete dispatcher chosen by `RosterValidationDispatcherProducer` when `roster.validation.dispatch-mode=cloud-run-job` (`RosterValidationDispatcherProducer.java:21-56`).
- `CloudRunValidationDispatcher.dispatch` builds a `RunJobRequest` for `JobName.of(projectId, region, validationJobName)` and attaches a container override whose environment carries `ROSTER_JOB_TYPE=VALIDATION`, `ROSTER_JOB_ROSTER_ID`, `ROSTER_JOB_TENANT_ID`, `ROSTER_JOB_USER_ID`, and optionally trace IDs and the user's email (`CloudRunValidationDispatcher.java:51-58, 101-112`). It then calls `jobsClient.runJobAsync` and returns.
- A fresh Cloud Run Job execution starts. It runs the same image (native build, `JOB_MEM=2Gi`, `deploy-roster-jobs-native-prod.yml:113-117`). `RosterJobRunner` reads the roster identity via `System.getenv` (`RosterJobRunner.java:47-53`), calls `rosterValidator.validateRosterFromFileStream` (`RosterJobRunner.java:117`), and exits with `Quarkus.asyncExit(0)` on success or `asyncExit(1)` on failure (`RosterJobRunner.java:120-124`). Ingestion is the same with `ROSTER_JOB_TYPE=INGESTION` calling `rosterProcessor.processRosterFromFileStream` (`RosterJobRunner.java:160`).
- There is no Pub/Sub message, no subscriber, no ack, and no lease. Retries come from the job definition's `max-retries`, and the validator's status guard still applies on retry.
- Each roster gets its own isolated container with its own CPU and memory. Ten uploads produce ten parallel executions. Cloud Run Jobs allow a task timeout of up to 24 hours.
- The trade-offs are a cold start per roster (mitigated by the native image), dependence on the Cloud Run Admin API being reachable at dispatch time (a failed `runJobAsync` has no durable queue behind it), and no natural back-pressure on the DAL if many rosters arrive at once.

---

## 8. Scaling model: what happens when ten files are uploaded at once

### 8.1 Worked example in production Pub/Sub mode

| Time | `api-service` | Pub/Sub `roster-validation-sub` | `api-service-roster-worker` (2 instances) |
| --- | --- | --- | --- |
| t+0 to t+5s | Ten `POST /roster` requests handled, possibly across several autoscaled instances. Ten files land in GCS. Ten job rows created as `PENDING`. Ten trigger messages published. | 10 messages | Both instances idle, each pulling with `max-concurrent-messages=1` |
| t+5s | — | 8 unleased, 2 leased | Instance A leases roster 1, instance B leases roster 2. Both set `VALIDATION_IN_PROGRESS` and start streaming. |
| t+5s to t+T₁ | — | 8 waiting. Their ack lease has **not** started, because they have not been delivered. | A and B each process one file. Lease extended every 10 min. |
| t+T₁ | — | 7 unleased | A finishes roster 1, acks, leases roster 3. |
| … | — | … | Files drain two at a time, first-in first-out, until the queue is empty. |

**Key facts the example depends on:**

- Production roster worker is deployed with `MIN_INSTANCES=2` and `MAX_INSTANCES=20` (`deploy-production.yml:335-336`). Staging is `MIN_INSTANCES=1`, `MAX_INSTANCES=10` (`deploy-roster-worker-staging.yml:119-120`).
- Each instance processes one message at a time (`application.properties:740`).
- Effective concurrency is therefore `MIN_INSTANCES × 1` = **2 rosters in production, 1 in staging**.

### 8.2 Why the worker does not scale past `MIN_INSTANCES`

- Cloud Run's autoscaler adds instances based on incoming HTTP request concurrency and, when CPU is always allocated, CPU utilisation. It has no awareness of Pub/Sub backlog on a pull subscription.
- The roster worker receives no HTTP traffic. Nothing routes to it; it is deployed only so its startup hook can open the pull subscribers.
- A single-threaded pull processing one message rarely sustains CPU high enough, for long enough, to trigger CPU-based scale-out on a 2 vCPU instance, and even when it does, each new instance adds exactly one more concurrent roster.
- `MAX_INSTANCES=20` is therefore an upper bound that normal operation never approaches. Eight of the ten files in the example wait in the subscription no matter how large the backlog grows.

### 8.3 Interaction between validation and ingestion

- Both consumers run on the same two instances. Each instance leases one validation message **and** one ingestion message concurrently, because the two subscribers have independent flow control.
- A long ingestion on instance A and a long validation on instance B do not block each other, but the third validation and the third ingestion both wait.
- There is no priority, no size-based routing, and no per-tenant fairness. One tenant's very large file occupies a slot for its full duration.

---

## 9. The acknowledgement-deadline extension

### 9.1 What the mechanism is

- Pub/Sub delivers a message under a lease called the acknowledgement deadline. If the subscriber does not ack within it, Pub/Sub assumes the subscriber died and redelivers. The per-extension maximum the service accepts is 600 seconds.
- The Java client library can keep a lease alive automatically by sending `modifyAckDeadline` calls while the handler runs. The total window is capped by `setMaxAckExtensionPeriod`, whose library default is 60 minutes.
- This code raises that cap to 3 hours for validation and 10 hours for ingestion (`RosterValidationConsumer.java:157`, `RosterIngestionConsumer.java:154`; `application.properties:745-746`), and extends in 600-second steps (`application.properties:748-749`).

### 9.2 Is this an accepted practice

- No. Google's own guidance for Pub/Sub is that the ack deadline covers receiving and handing off a message, and that work taking minutes or hours should be persisted to a job store and acked quickly, with the long work run in something designed for it (Cloud Run Jobs, Cloud Batch, Dataflow, or an orchestrator such as Workflows).
- The lease is a **liveness** signal to the broker. Stretching it to hours turns it into a **correctness** mechanism, a distributed lock over a multi-hour business process, which it was not designed to be.
- The need to override the library's 60-minute safety cap by three to ten times is itself the signal that the tool is operating outside its design envelope.

### 9.3 What happens in this code when a file exceeds the cap

This is the central reliability finding. Your instinct that the design fails rather than degrades is correct, and the failure mode is worse than a timeout.

1. At 3 hours (validation) or 10 hours (ingestion) the client library stops extending. The lease expires while the worker thread is still mid-file.
2. Pub/Sub redelivers the message to whichever instance next pulls, which may be the same instance or the other one.
3. The redelivered handler runs the re-entry guard. **The guard checks only `{VALIDATED, VALIDATION_FAILED, COMPLETED, FAILED}`** (`RosterValidator.java:209-216`). The roster is in `VALIDATION_IN_PROGRESS`, which is a real status in the enum (`RosterJobStatus.java:6`) and was written at line 220, but it is **not** in the guard set, so the guard passes.
4. The second run calls `initializeSpannerRosterData(rosterId)` (`RosterValidator.java:224`), wiping the roster's rows that the first run is still writing, and starts parsing from the top.
5. Two validators now write to the same roster concurrently. Counters and row states interleave. When the first run finally finishes and calls `ack()`, Pub/Sub ignores it because that lease already expired. The second run continues and may itself exceed the cap, triggering a third.
6. The status write is unconditional. `RosterRepository.updateRosterStatusById` reads the current row, overwrites `status`, and saves; it performs no compare-and-set on the previous value (`api-layer/src/main/java/com/certifyos/api_layer/roster/repository/RosterRepository.java`, method `updateRosterStatusById`). There is no lease, lock, heartbeat, or last-updated check anywhere in `roster_subscribers` or `roster/messaging` (grep for `lease|lock|heartbeat|lastUpdated` returns only unrelated in-flight request caches).
7. Ingestion has the same hole with worse consequences. `RosterProcessor` guards only on `{COMPLETED, FAILED}` (`RosterProcessor.java:141`), so redelivery re-enters `IN_PROGRESS` and re-executes DAL transactions against real practitioner records.

**Correction to an earlier statement in the conversation that led to this document:** it is not accurate to say the roster "has nothing" for in-progress state. `VALIDATION_IN_PROGRESS` and `IN_PROGRESS` exist, are written, and are shown to users. The accurate statement is that these statuses are **informational only**: they are not consulted by the re-entry guards and are not written with any concurrency control, so they do not prevent a second processor from starting on the same roster.

### 9.4 How likely is it to hit the cap

- The queue wait does not count. A message's lease begins at delivery, and the ten-file backlog in section 8 delays delivery, not processing. The exposure is a **single file whose own processing exceeds 3 or 10 hours**.
- Validation performs per-row remote lookups (NPI, location, group, address standardisation) with caches. A very large XLSX with low cache hit rate is the realistic path to exceeding 3 hours. No runtime measurement or hard budget exists in the code to detect an approaching cap; the only timeout-shaped roster properties are for export, SmartFix, and AI suggestions (`application.properties:90, 1459, 1582`).

---

## 10. Reliability and scalability assessment

### 10.1 What is solid

- **Durability of the trigger and the file.** The file lives in GCS and the trigger lives in Pub/Sub. A worker crash before ack loses nothing; the message is redelivered and the status guard makes redelivery of a finished job a no-op. A stuck job can also be re-triggered manually via `PUT /roster/republish-event/{eventType}` (`RosterResource.java`, documented in `roster/README.md:466`).
- **Bounded memory on the worker.** The file is streamed from GCS, parsed through iterators, batched at 300 rows, and written in 500-mutation chunks. File size does not translate to heap size on the worker.
- **Decoupled upload latency.** The API returns as soon as GCS and the publish succeed. The API service scales on HTTP load independently of the worker.
- **Correct handling of long work within the cap.** The lease extension does what it is meant to: a two-hour roster is not redelivered mid-flight and not double-processed.
- **Idempotency on finished jobs.** The status guard, while incomplete, correctly short-circuits redelivery of `VALIDATED`, `VALIDATION_FAILED`, `COMPLETED`, and `FAILED` jobs.

### 10.2 Where it is weak

| Issue | Effect | Evidence |
| --- | --- | --- |
| Throughput is fixed at `MIN_INSTANCES × 1` | Two concurrent rosters in production, one in staging. Ten uploads means eight wait. No mechanism adds capacity in response to backlog. | Section 8; `application.properties:740`; `deploy-production.yml:335` |
| Head-of-line blocking | One huge roster occupies a slot for hours and halves throughput for every other tenant. No priority or fairness. | Same |
| Redelivery past the ack cap causes concurrent double-processing | Data corruption on validation; duplicate DAL transactions on ingestion. | Section 9.3; `RosterValidator.java:209-224`; `RosterProcessor.java:141-152`; `RosterRepository.updateRosterStatusById` |
| No dead-letter topic on roster subscriptions | A deterministically failing message is nacked and redelivered immediately, forever, until the 7-day retention expires, occupying one of two slots each time. The README claims dead-letter topics exist (`roster/README.md:434`); the only dead-letter configuration in `application.properties` is for anchor-date (`:1019-1021, 1045-1047`) and webhook subscriptions. | `RosterValidationConsumer.java:221-235`; grep `dead` in `roster/messaging/*.java` matches only "deadline" |
| Partial-failure restarts from zero | A crash mid-file triggers redelivery, `initializeSpannerRosterData` resets rows, and all prior work is discarded. Correct but wasteful. | `RosterValidator.java:224` |
| Cost profile inverted | Two warm 6Gi instances with CPU always allocated, 24/7, to protect a bursty and mostly idle workload, yet no burst headroom is gained. | `deploy-production.yml:292, 334-335` |
| Validation and ingestion share instances | Different resource shapes and ack windows compete for the same two slots. | Section 8.3 |
| Subscription settings apply only at creation | Ack deadline, retry backoff, and flow control changes in `application.properties` do nothing to an existing subscription without a manual `gcloud pubsub subscriptions update`. | `RosterValidationConsumer.java:92-100` |
| Stale README and stale comments | `README.md:434` claims dead-lettering; `application.properties:8` says the body limit is 20 MB while line 9 sets 500 MB. | — |
| Upload limit mismatch across layers | Users can select 500 MB, Cloud Run rejects above 32 MiB with an opaque 413, reupload has no client check at all. | Section 3 |

---

## 11. How the same problem is solved at scale on GCP

The shared principle across all mature designs: **the message is a trigger; the work is tracked in a durable state store you own, not in the broker's lease.** Three common shapes:

### Shape 1: Message as trigger, Cloud Run Job or Cloud Batch as executor

- The consumer receives the message, confirms or writes a job row (`QUEUED`), starts one Cloud Run Job execution or Cloud Batch task for that roster, and acks within seconds.
- The long work runs inside the job with a task timeout up to 24 hours (Cloud Run Jobs) or effectively unbounded (Batch). The job updates the row as it progresses and writes a terminal state.
- Retries come from the job runner's `max-retries`, and the job checks the state row on start to skip or resume.
- **This codebase already contains this executor.** `CloudRunValidationDispatcher` and `RosterJobRunner` are the job half. What is missing is making the Pub/Sub consumer the thin dispatcher instead of the executor, which would keep Pub/Sub's durability and gain per-roster elasticity.

### Shape 2: Orchestrator-driven pipelines

- Cloud Workflows or Cloud Composer own the lifecycle. A workflow step waits on a job with built-in long-wait semantics measured in days and drives status transitions.
- Natural fit for multi-stage flows with a human gate in the middle, which the roster's validate → approve → ingest sequence is.
- Cost is an extra moving part; teams usually adopt it once they have several such pipelines.

### Shape 3: Chunked work with small, fast messages

- A coordinator splits the file into row ranges up front and emits one message per chunk. Each chunk validates in well under a minute and is acked normally.
- A completion condition finalises the roster. `RosterRowResultConsumer`'s `SELECT 1 … LIMIT 1` check (`RosterRowResultConsumer.java:504-524`) is already this pattern for row results.
- This is the only shape that makes a single huge file **faster**, because chunks fan out across every available worker. The cost is that cross-row logic such as first-in-file-wins duplicate handling needs a pre-pass or a shared dedupe table.

### Very large file processing generally

- Companies processing large files on GCP typically do not run them through Pub/Sub consumers at all. The file lands in GCS, a GCS object-finalize notification fires, and a Dataflow or Spark job reads the file in parallel splits. Pub/Sub carries only the "file arrived" event and is acked immediately.
- That is heavier than a roster validator needs, but it illustrates the through-line: nobody holds a broker lease for the length of the job.

---

## 12. Recommendations, in priority order

1. **Close the double-processing hole regardless of architecture.** Add `VALIDATION_IN_PROGRESS` to the validator guard and `IN_PROGRESS` to the processor guard, with a staleness rule: in progress and last-updated within the ack window means skip and ack; older than that means treat as orphaned and reprocess. Write the in-progress transition with a compare-and-set on the previous status so two workers cannot both claim the same roster. This turns a past-cap redelivery from data corruption into a wasted redelivery.
2. **Move to Shape 1 using the existing job code.** Make `RosterValidationConsumer` and `RosterIngestionConsumer` thin dispatchers: receive, verify dispatchable, call `runJobAsync` with the roster ID override, ack. Reset the ack extension cap to the library default. Validation and ingestion each get an isolated execution, a 24-hour task timeout, per-roster logs, and elastic parallelism. The roster worker service can then shrink to `MIN_INSTANCES=1` on default CPU throttling, or be folded into `api-service-worker`.
3. **Add a hard runtime budget inside the job.** Even at 24 hours, decide what a file that exceeds it becomes. Marking it `FAILED` with a "file too large, please split" reason surfaced in the UI is better than any unbounded process.
4. **Configure dead-letter topics on the roster subscriptions**, or if staying on Pub/Sub consumers, at minimum set a retry policy with exponential backoff so a poison message does not hot-loop.
5. **Align upload limits.** Pick a single limit that the worst-case processing time supports. Enforce it in `FileInput` for both upload and reupload, in `RosterResource`, and document the Cloud Run HTTP/1 32 MiB ceiling. If a higher limit is needed, switch the API service port to `h2c` to lift the ingress cap and stop reading the whole upload into memory in `uploadFileAndGetPath`.
6. **Fix the stale documentation.** Remove the dead-letter claim from `roster/README.md` until it is true, and correct the 20 MB comment in `application.properties`.
7. **Consider Shape 3 later** if single-file latency becomes the complaint. It is the only change that makes large files faster rather than merely safer.

---

## 13. Evidence index

Every claim in this document traces to one of these locations. Line numbers are as of 2026-09-11.

**Frontend (`frontend/apps/web`)**

- `components/common/file-input/file-input.tsx:11, 27, 54` — 500 MB default.
- `features/roster/components/roster-upload/roster-upload-form/roster-upload-form.tsx:541` — `FileInput` without `maxSizeMB`.
- `features/roster/components/roster-list/roster-list.tsx:308-313` — raw reupload input.
- `features/roster/constants/index.ts:347` — 20 MB template import cap.
- `constants/common.ts:234` — 50 MB generic cap.
- `features/roster/hooks/use-roster-file-upload.ts:29-40` — multipart fields.
- `features/roster/services/index.ts:115-156` — `POST /roster`, `PUT /roster/{id}/reupload`, `GET /roster/{id}/import`.

**API layer (`api-layer/src/main`)**

- `resources/application.properties:8-9, 15` — body and form-attribute limits and stale comment.
- `resources/application.properties:68, 71` — ingestion batch size 100, batch threads 100.
- `resources/application.properties:733-737` — topic and subscription names.
- `resources/application.properties:740-749` — concurrency 1, parallel pull 1, ack extension 3 h / 10 h, 600 s step.
- `resources/application.properties:752-758` — roster-logging topic.
- `resources/application.properties:787-799` — `use-pubsub=false`, dispatch modes default `pubsub`.
- `resources/application.properties:807-810` — consumer enable flag, transaction topics.
- `resources/application.properties:1019-1021, 1045-1047` — dead-letter config exists only for anchor-date.
- `java/…/roster/model/RosterJobStatus.java:4-14` — status enum including `VALIDATION_IN_PROGRESS` and `IN_PROGRESS`.
- `java/…/roster/resource/RosterResource.java:230-298, 364-522` — upload and reupload endpoints, `FileUpload` with no size check.
- `java/…/roster/service/RosterService.java:638-669, 864` — roster create, dispatch, `readAllBytes` upload, ingestion dispatch.
- `java/…/roster/service/UserPermissions.java:51-53` — `canDispatchValidation`.
- `java/…/roster/repository/RosterRepository.java` — `updateRosterStatusById`, unconditional write.
- `java/…/roster/messaging/RosterEventPublisher.java:133-149, 167-215, 219-236` — publish methods, three-field payload, topic auto-create.
- `java/…/roster/messaging/PubSubValidationDispatcher.java:18-20` — Pub/Sub dispatcher.
- `java/…/roster/messaging/RosterValidationDispatcherProducer.java:21-56` — mode selection.
- `java/…/roster/messaging/CloudRunValidationDispatcher.java:51-58, 101-112` — job dispatch with env overrides.
- `java/…/roster/messaging/RosterJobRunner.java:47-53, 117-124, 160` — job entry point.
- `java/…/roster/messaging/RosterValidationConsumer.java:84, 92-111, 126, 157-158, 190-240` — enable guard, subscription auto-create, flow control, ack extension, handler, ack/nack.
- `java/…/roster/messaging/RosterIngestionConsumer.java:153-174` — handler and ack/nack.
- `java/…/roster/messaging/RosterRowResultConsumer.java:504-524` — completion check.
- `java/…/roster_subscribers/service/RosterValidator.java:186-262` — status guard, in-progress write, Spanner reset, GCS stream, extension branch.
- `java/…/roster_subscribers/service/RosterProcessor.java:128-160, 387-388` — ingestion guard, in-progress write, sync finalisation.
- `java/…/roster/service/RosterRecordHandler.java:303, 374, 1391, 1408` — `use-pubsub` branches.
- `java/…/utils/RosterProcessingUtils.java:65` — `VALIDATION_BATCH_SIZE = 300`.
- `java/…/roster/README.md:434, 466` — dead-letter claim (inaccurate), republish endpoint.

**Infrastructure and deploy (`api-layer`)**

- `terraform/feature/cloudrun.tf:28, 60-61, 104-300` — feature env flags, three services, `http1` ports.
- `terraform/release/loadbalancer.tf:29` — 30 s backend timeout.
- `.github/workflows/deploy-production.yml:168, 190-191, 291-336, 388, 408, 421-461` — production service matrix.
- `.github/workflows/deploy-roster-worker-staging.yml:81-120` — staging worker sizing.
- `.github/workflows/deploy-roster-worker-internal.yml:116-117` — internal uses `cloud-run-job`.
- `.github/workflows/deploy-roster-jobs-native-prod.yml:113-141` — job image and `JOB_MEM=2Gi`.
- `Makefile:326, 346-350, 515-524` — `PUB_SUB_CONSUMERS_ENABLED` default, sizing defaults, `update-cloud-run-job`.
- GitHub environment variables (`gh variable list --env {staging,production,demo}`, read 2026-09-11) — `ROSTER_VALIDATION_DISPATCH_MODE=pubsub`, `ROSTER_INGESTION_DISPATCH_MODE=pubsub`.
