# CP-39604 — Vendor exchange service: scaffolding and infrastructure implementation plan

Parent story: CP-39602 (Candor vendor ingestion phase 1). Design doc: "CP-39602 - Design: Directory accuracy vendor export" (Confluence, version 4, 2026-09-28). Reference implementation of the same shape: `CertifyOS/file-ingestion-service` (Quarkus 3.33.3, JobRunr 8.8.2, MongoDB Atlas, two GCE managed instance groups from one image).

Decisions confirmed 2026-10-01: repository `vendor-exchange-service`; push delivery for the egress completion event with our own token verifier; two service accounts; dedicated Mongo user requested, shared user as interim; pins unchanged; Checkstyle, SpotBugs and Spotless enforced in CI from the first commit; permission check follows file-ingestion (tenant membership only); CODEOWNERS `@dev-certifyos @suhasini-certify`.

## 1. What this subtask delivers

- A new repository that boots as `api` or `worker` from one image, with every foundation piece the later subtasks (selection and export CP-39605, observability and audit CP-40438, phase 2 CP-40437) build on.
- The four MongoDB collections with every index the design names, a compare-and-set state transition helper, and the audit envelope and event catalogue from the design's Audit trail section.
- JobRunr wired on the worker group: job classes with payload and state precheck, the two recurring jobs registered idempotently by id, the final-failure filter, and the service-wide kill switch.
- The inbound auth chain (Auth0 platform JWT plus tenant membership lookup through DAL) and the Pub/Sub push token verifier for the egress completion endpoint.
- The outbound identity plumbing: Google ID tokens for IAP-protected DAL and egress, and an Auth0 client-credentials token for api-layer.
- Terraform for the `internal` environment, the Makefile deploy flow, CI, and the repository hygiene files, copied from file-ingestion-service with its recorded lessons applied.
- Live gate: both instance groups healthy, the load balancer answers, the worker's JobRunr server has registered, the recurring tick row exists, and an authenticated operator call returns a well-formed empty answer.

## 2. Nothing external blocks implementation

Every outside dependency has an interim value that lets the code be written, tested and deployed now, and a swap-in point that is configuration or one Terraform variable, never a code change. CP-39605 can start the moment steps 0 to 7 are merged.

| Dependency | Interim (works today) | Final (when it lands) | Swap-in point |
| --- | --- | --- | --- |
| Pub/Sub push token verifier | Built in step 5 and unit-tested with a locally signed token; the endpoint rejects everything until the two config values are set | `pubsub.pushServiceAccount` and `pubsub.pushAudience` set from Terraform once the subscription exists | Two environment variables |
| Egress completion topic (egress ask 5) | `var.egress_events_topic = null`, the subscription resource has `count = 0`; the handler is tested with WireMock-style direct POSTs | Set the variable to the topic name | One Terraform variable, `terraform apply` |
| Auth0 machine client for api-layer | `apiLayer.clientId` and `apiLayer.clientSecret` are optional config; when absent, `ApiLayerTokenService` throws `ApiLayerNotConfigured` with a clear message and the readiness check reports it | Secret version added to `vendor-exchange-apilayer-client-secret`, client id set | One secret version, one environment variable |
| Operator permission rows in DAL | Tenant membership check only, exactly as file-ingestion: the user must have a non-empty permission set for the `tenant-id` header | Per-action check (`vendor-export:manage`, `vendor-export:read`) once the DAL rows exist | A single `Permission` enum and one filter class kept in the tree with an `enabled` flag defaulting to false |
| IAP access to DAL and egress for our service accounts | DAL lookup fails with `503 DAL_UNAVAILABLE` and is logged; the smoke script treats that case as "grant pending" | DevOps grants `roles/iap.httpsResourceAccessor` on both backends | None in code; the same deployment starts passing |
| Dedicated Atlas user | URI for the shared `certifyos-app` user in `vendor-exchange-mongodb-uri` | URI for the `vendor-exchange` user in the same secret | One secret version, rolling restart |
| Service account grants (self token creator, Pub/Sub subscriber, dead-letter publisher) | The accounts exist (editor can create them); Google ID tokens come from the metadata server, which needs no grant | DevOps applies the bindings listed in `iam.tf` comments and `docs/runbook.md` | None in code |
| Vendor bucket name and read grant | `vendorBucket` set to a placeholder bucket in `certifyos-development` that our worker account can read | Real bucket name and `storage.objects.get` on `from/` | One environment variable |
| Route 53 record | Load balancer reached by IP with `Host` header in the smoke script; managed certificate stays `PROVISIONING` | DevOps adds the `A` record | None in code |

## 2a. Playbook alignment

"New Service Playbook: Lessons from api-layer and DAL" (2026-10-01) is the day-one standard; `vendor-exchange-service` ADR 0003 records how it is enforced. Items that change the steps below:

- Constructor injection only, module layout, no cycles, one logger, one `ObjectMapper`, no ad hoc executors, no `Thread.sleep`, typed contracts: all enforced by `ArchitectureTest` (ArchUnit) from the first commit.
- Checkstyle caps: 500 lines per main file, 1,000 per test file, 7 constructor parameters, no ticket keys in source, `@Disabled` needs a ticket. Stub code names the behaviour it waits for, never a ticket key.
- Operator endpoints live under `/v1/` (`/v1/vendor-exports/...`); `/internal/...` stays unversioned. Design doc paths need the prefix added.
- HTTP policy denies by default; every endpoint carries an explicit security annotation and an ArchUnit rule fails on one that does not.
- Error bodies are RFC 9457 `application/problem+json` with a stable `code`, rendered only by global exception mappers. Downstream status codes are preserved; no exception text reaches a client.
- A committed OpenAPI contract (`src/main/openapi/vendor-exchange.yaml`) with a CI drift check against the runtime document and an `oasdiff` breaking-change gate.
- `version` on every mutable document (schedules and batches) with compare-and-set on every update; a limit on every list query; keyset cursors on read endpoints.
- Per-client timeouts (connect at or under 2 s, read sized per endpoint), jittered retry on idempotent errors only, circuit breaker, bulkhead through `quarkus-smallrye-fault-tolerance`.
- Logs carry ids and counts only; MDC set once in a request filter and once per job.
- Readiness checks MongoDB; the load balancer and the instance groups use liveness.
- Deploys from CI through Workload Identity Federation with a reusable workflow and the environment as an input; the laptop `make deploy` from file-ingestion is the interim only until the identity exists (ask A10).
- Repository: `AGENTS.md` is the one guidance file, `CLAUDE.md` points to it, ADRs in `docs/adr/`, `HANDOFF.md` under `docs/`, versions only in `gradle/libs.versions.toml`, actions and images pinned by SHA and digest, gitleaks in CI and pre-commit, Renovate configured.

## 3. Names

- Repository `CertifyOS/vendor-exchange-service`, created from the org template `CertifyOS/quarkus-base`.
- Java root package `com.certifyos.vendor_exchange`; export-lane code under `export`, shared foundations at the root packages listed in section 4. Phase 2 adds `ingestion`.
- Instance groups `vendor-exchange-api` and `vendor-exchange-worker`; service accounts with the same names; Artifact Registry repo `vendor-exchange`; hostname `vendor-exchange.internal.certifyos.com`; Mongo database `vendor_exchange`; secrets `vendor-exchange-mongodb-uri`, `vendor-exchange-apilayer-client-secret`, `vendor-exchange-sentry-dsn`.
- Collections and permission strings keep the design doc's module prefix: `vendor_export_schedules`, `vendor_export_batches`, `vendor_export_npis`, `vendor_export_events`; `vendor-export:manage`, `vendor-export:read`. Phase 2 adds `vendor_ingestion_*` and `vendor-ingestion:*`.
- Design doc follow-up: the group names `vendor-export-api` and `vendor-export-worker` in the Approach and Security sections become `vendor-exchange-api` and `vendor-exchange-worker`; the Pub/Sub filter attribute `initiator` becomes `vendor-exchange-worker`.

## 4. Target repository layout

```
vendor-exchange-service/
  .github/CODEOWNERS, pull_request_template.md, workflows/ci.yml
  build.gradle, settings.gradle, gradle.properties, gradlew*
  config/checkstyle/*, config/spotbugs/exclude.xml      (exclude rewritten for com.certifyos.vendor_exchange)
  githooks/commit-msg
  Makefile, .env.example, .dockerignore, .gitignore
  README.md, HANDOFF.md, docs/decisions.md, docs/runbook.md
  postman/vendor-exchange-service.postman_collection.json
  src/main/docker/Dockerfile.jvm
  src/main/resources/application.properties
  src/main/java/com/certifyos/vendor_exchange/
    config/      VendorExchangeConfig, RoleProfileCheck, MongoIndexes
    persistence/ Collections, Transactions, Ids
    audit/       AuditEvent, AuditEventType, AuditRepository
    auth/        ApiError, UserContext, UserContextFilter, DalUserClient, DalUserResponse,
                 DalResourcePermission, UserAuthCacheService, Permission, PermissionFilter (off),
                 GoogleIdTokenService, GoogleAuthFilter, PubSubPushAuthFilter
    clients/     EgressClient, EgressAuthFilter, ApiLayerClient, ApiLayerAuthFilter,
                 ApiLayerTokenService, egress and api-layer DTOs
    metrics/     VendorExchangeMetrics
    ops/         OpsResource
    export/
      schedule/  Schedule, ScheduleRepository
      batch/     ExportBatch, BatchState, ExportBatchRepository, ExportNpi, ExportNpiRepository
      jobs/      TickJob, SelectJob, RequestEgressJob, DeadlineCheckJob, FinishJob, ReconcilerJob,
                 RecurringJobs, BatchFinalFailureFilter, BatchFinalFailureFilterRegistration
      events/    EgressEventResource (stub handler), EgressEvent (message record)
      api/       VendorExportsResource (read stubs)
  src/test/java/...            unit tests
  src/integrationTest/java/... MongoResource, ApiTestProfile, WorkerTestProfile, *IT
  terraform/internal/ main, variables, network, compute, lb, iam, pubsub, secrets, registry,
                      outputs, startup.sh.tftpl, image.auto.tfvars
  scripts/ smoke-api.sh
```

## 5. Fixed inputs from the design doc that bind this scaffolding

- Collections: `vendor_export_schedules`, `vendor_export_batches`, `vendor_export_npis`, `vendor_export_events`, plus JobRunr's five (`jobrunr_jobs`, `jobrunr_recurring_jobs`, `jobrunr_background_job_servers`, `jobrunr_metadata`, `jobrunr_migrations`) in the same database `vendor_exchange`. JobRunr creates them without the `jobrunr_` prefix by default; `quarkus.jobrunr.database.table-prefix=jobrunr_` gives them these names.
- Indexes: schedules unique `_id`, `(enabled, nextDueAt)`, `(tenantId)`; batches unique `(tenantId, vendor, period, seq)`, `(state, updatedAt)`, `(egress.correlationId)`, `(tenantId, deliveredAt)`; npis `(exportBatchId)`, `(tenantId, npi)`; events `(exportBatchId, occurredAt)`, `(tenantId, vendor, occurredAt)`, and a partial unique index on `detail.messageId` where `type = EXPORT_EVENT_RECEIVED` for the handler's idempotency.
- Batch states: `SCHEDULED → NPIS_SELECTED → EGRESS_REQUESTED → EGRESS_COMPLETED → DELIVERED`, side states `EMPTY`, `FAILED`, `SUPERSEDED`. Every transition is a compare-and-set on `state`.
- Ids: schedule `_id = <tenantId>|<vendor>`; batch `_id = <tenantId>-<vendor>-<yyyy-MM>-<seq>`; npi `_id = <exportBatchId>|<npi>`; event `id = ev-<uuid v7>`, event `_id` is the driver's ObjectId (file-ingestion's `_id` policy, finding 66).
- Audit envelope: `id, type, tenantId, vendor, exportBatchId, attempt, actor, jobId, occurredAt, detail`. Event types: the 24 names in the design's per-event contracts table.
- Deployment configuration keys: `egressBaseUrl`, `egressDeadlineHours` (6), `egressAbandonHours` (48), `vendorBucket`, `jobRetries` (8), `reconcilerStaleMinutes` (30), `enabled` (true). No feature flags.
- Recurring jobs: `vendor-export-tick` at `0 6 * * *`, `vendor-export-reconciler` hourly, both registered in code at startup, idempotent by id.
- Endpoints owned by the api group: `/vendor-exports/**` (platform JWT, tenant membership), `/internal/vendor-exports/egress-events` (Pub/Sub identity token only), `/q/*` (health, metrics, OpenAPI).
- Runtime sizing: api group 2 × e2-small behind an external HTTPS load balancer, worker group 1 × e2-standard-2, no inbound traffic to the worker.
- Retention: schedules, batches, npis, events kept 7 years. No TTL index on any of the four collections.

## 6. Steps

Each step ends with a gate. The gate evidence is shown and approved before the next step starts. Every task is test first: the failing test is written and compiled before the implementation. Steps 0 to 7 need no external input of any kind. Step 8 applies everything the editor account can apply alone, and records the rest as asks.

### Step 0 — Repository from template, pins, roles, CI skeleton (3 hours)

- Create `CertifyOS/vendor-exchange-service` from the `quarkus-base` template with branch protection on `main` and CODEOWNERS `* @dev-certifyos @suhasini-certify`.
- Replace file-ingestion's first-commit changes: pin Quarkus 3.33.3 and JobRunr 8.8.2 in `gradle.properties`, `quarkus.package.jar.type=fast-jar`, remove `Dockerfile.native*`, `Dockerfile.legacy-jar`, the Spanner config and the template health check; add `src/main/docker/Dockerfile.jvm` on `ubi9/openjdk-21:1.21`.
- Dependencies in `build.gradle`: `quarkus-rest`, `quarkus-rest-jackson`, `quarkus-rest-client-jackson`, `quarkus-oidc`, `quarkus-security`, `quarkus-mongodb-client`, `quarkus-cache`, `quarkus-smallrye-health`, `quarkus-smallrye-openapi`, `quarkus-micrometer-registry-prometheus`, `quarkus-opentelemetry`, `quarkus-logging-json`, `quarkus-google-cloud-secret-manager:2.15.0`, `quarkus-logging-sentry:2.3.4`, `quarkus-jobrunr`, `google-cloud-storage:2.45.0`, `google-cloud-pubsub:1.134.0`, `google-auth-library-oauth2-http` (for `TokenVerifier`), `java-uuid-generator:5.1.0`; test: `quarkus-junit`, `quarkus-junit-mockito`, `quarkus-test-security-jwt`, `rest-assured`, `testcontainers:mongodb:1.20.4`, `wiremock-standalone:3.9.2`, `awaitility:4.2.2`.
- `integrationTest` source set and task exactly as file-ingestion (`check` depends on it, `testAll`).
- `config/RoleProfileCheck`: exactly one of profiles `api` or `worker` must be active, else startup throws. Unit test for all four combinations.
- `config/spotbugs/exclude.xml` rewritten for `com.certifyos.vendor_exchange`; Checkstyle and Spotless kept in the `check` task graph and in CI from day one.
- `.github/workflows/ci.yml`: `./gradlew check --no-daemon` on push and pull request to `main`, Temurin 21, Gradle cache, test and JaCoCo reports uploaded.
- `githooks/commit-msg` (Conventional Commits), `installGitHooks` Gradle task, `.github/pull_request_template.md` with the `Resolves CP-####` line.
- **Gate:** CI green on the empty service; `RoleProfileCheckTest` passes; `./gradlew quarkusBuild` produces `build/quarkus-app`; `docker build --platform linux/amd64` succeeds locally.

### Step 1 — Configuration and test profiles (3 hours)

- `config/VendorExchangeConfig` as a closed `@ConfigMapping(prefix = "vendor-exchange")`: `enabled` (true), `egress.url`, `egress.iapClientId`, `egress.deadlineHours` (6), `egress.abandonHours` (48), `vendorBucket`, `jobRetries` (8), `reconcilerStaleMinutes` (30), `dal.url`, `dal.iapClientId`, `apiLayer.url`, `apiLayer.clientId` (optional), `apiLayer.clientSecret` (optional), `pubsub.pushServiceAccount` (optional), `pubsub.pushAudience` (optional), `permissions.enforce` (false), `auth.cacheTtl` (PT5M), `auth.cacheMaxSize` (1000). Keys with no default: `egress.url`, `egress.iapClientId`, `vendorBucket`, `dal.url`, `dal.iapClientId`, `apiLayer.url`, so a forgotten environment value fails at boot. Optional keys are the swap-in points from section 2.
- `application.properties`: Mongo connection string from `MONGODB_URI` locally and `${sm//vendor-exchange-mongodb-uri}` under `%prod`; database `vendor_exchange`; `write-concern.w=majority`; Dev Services off; OIDC tenant off at base and on under `%prod`; JSON console logging with `level=severity`; `quarkus.http.auth.permission` blocks for `/q/*` permit, `/vendor-exports/*` authenticated, `/internal/*` permit at the HTTP layer (the push filter does its own 401); JobRunr `database.type=mongodb`, `default-number-of-retries=${vendor-exchange.job-retries}`, background server on only under `%worker`, `poll-interval-in-seconds=15`, `worker-count=4`; dashboard on under `%worker` bound to port 8000 (reached only through an IAP SSH tunnel; no firewall rule exposes it); shutdown timeout 30 s with 10 s delay for load balancer draining.
- `.env.example` listing every `${...}` the properties file reads, with the replica-set note for local Mongo transactions and the `gcloud` command to read each IAP client id.
- Integration test support: `MongoResource` (Testcontainers `mongo:7.0` replica set), `ApiTestProfile` (`test,api`), `WorkerTestProfile` (`test,worker`, poll interval 5 s), both supplying dummy values for every no-default key.
- **Gate:** Quarkus boots under `test,api` and `test,worker`; a missing no-default key is refused with every missing name in one validation message (SmallRye-level test; Quarkus applies the same validation at boot); `%worker` turns the JobRunr server on (heartbeat row appears) and `%api` leaves it off (`BackgroundJobServer.isRunning()` false and no heartbeat row across more than one poll interval; the bean itself exists on both roles, so resolvability proves nothing).

### Step 2 — Persistence foundation: collections, indexes, transactions, domain records (6 hours)

- `persistence/Collections` returning the four `MongoCollection<Document>` handles from database `vendor_exchange`.
- `config/MongoIndexes` on `StartupEvent` creating every index in section 5 by name, with the same boot-safety comment file-ingestion carries (identical `createIndex` is a no-op or joins an in-progress build).
- `persistence/Transactions.run(Function<ClientSession, T>)` with the documented rule: body may contain MongoDB writes only, never a publish or an enqueue, because `withTransaction` re-runs it on transient errors.
- `persistence/Ids`: `eventId()` as `ev-<uuid v7>` through `Generators.timeBasedEpochGenerator()`; `batchId(tenantId, vendor, period, seq)`; `npiId(exportBatchId, npi)`; `scheduleId(tenantId, vendor)`; `correlationId(exportBatchId, attempt)`. Unit tests on format, ordering, and the hyphen-only rule for batch ids.
- Domain records with `toDocument()` and `fromDocument()`: `export/schedule/Schedule` (every field in the design sample including `disabledAt`, `disabledReason`, `version`), `export/batch/ExportBatch` with nested `Selection`, `Egress`, `Destination`, `File`, `Reconciliation` records and optional `failedStep`, `lastError`, `supersededBy`, `acknowledgedAt`, `inboundBatchId`; `export/batch/ExportNpi`; `export/batch/BatchState` enum with `canTransitionTo(BatchState)` encoding the design's state graph and `isTerminal()`.
- Repositories with only what the foundation needs: `ScheduleRepository.insert`, `find`, `findDue(now)` (the tick's query), `advance(session, id, nextDueAt, lastBatchId, lastRunAt)`; `ExportBatchRepository.insert(session, batch)`, `find`, `findByCorrelationId`, `findStale(state set, olderThan)`, `transition(session, id, from, to, Bson updates)` returning `boolean` on `matchedCount`; `ExportNpiRepository.upsertAll(session, List<ExportNpi>)` in bulk writes of 500, `countForBatch`.
- **Gate:** `MongoIndexesIT` asserts every index exists by name with the right keys, uniqueness and partial filter; `ExportBatchRepositoryIT` proves a stale `transition` is a no-op and a second `(tenantId, vendor, period, seq)` insert throws `DUPLICATE_KEY`; `TransactionsIT` proves a thrown body rolls back both writes; `BatchStateTest` covers every allowed and refused edge.

### Step 3 — Audit trail (3 hours)

- `audit/AuditEventType` with the 24 types from the design's per-event contracts table, from `SCHEDULE_CREATED` to `EXPORT_BATCH_ACKNOWLEDGED`.
- `audit/AuditEvent` record matching the envelope in section 5, builder with `actor` defaulting to `system:vendor-export`, `detail` copied defensively (so SpotBugs `EI_EXPOSE_REP` passes and the Checkstyle gate stays green).
- `audit/AuditRepository.write(ClientSession, AuditEvent)` for events committed with a transition and `write(AuditEvent)` for standalone facts; `writeIdempotent(AuditEvent)` that swallows `DUPLICATE_KEY` on the `detail.messageId` partial index for `EXPORT_EVENT_RECEIVED`.
- **Gate:** `AuditRepositoryIT` proves an event inside a rolled-back transaction is absent, a standalone write is present, and a repeated `EXPORT_EVENT_RECEIVED` with the same `messageId` is stored once.

### Step 4 — JobRunr wiring: jobs, recurring registration, final-failure filter, kill switch (6 hours)

- Job classes as `@ApplicationScoped` beans invoked through JobRunr's IoC lambda form (`scheduler.<SelectJob>enqueue(id, job -> job.run(exportBatchId))`), each with: load the batch, check `cfg.enabled()` and exit as a no-op when false, check the expected state and exit as a no-op on mismatch, then a body that throws `UnsupportedOperationException("CP-39605")`. Jobs: `SelectJob`, `RequestEgressJob`, `DeadlineCheckJob(exportBatchId, attempt, check)`, `FinishJob`. Job ids are `nameUUIDFromBytes(<jobName>:<exportBatchId>:<attempt>)` so a repeated enqueue is a no-op.
- `export/jobs/TickJob.run()`: runs `ScheduleRepository.findDue(now)` and writes `EXPORT_TICK_COMPLETED` with `schedulesDue`, `batchesCreated = []`, `durationMs`. The per-row transaction and `SelectJob` enqueue are CP-39605's body. This gives the live gate a real recurring job that proves the schedule, the query, the index and the audit write end to end.
- `export/jobs/ReconcilerJob.run()`: runs `ExportBatchRepository.findStale` and logs the count; the re-enqueue per state is CP-40438's body.
- `export/jobs/RecurringJobs` on `StartupEvent` under the worker profile: `scheduleRecurrently("vendor-export-tick", "0 6 * * *", ...)` and `scheduleRecurrently("vendor-export-reconciler", "0 * * * *", ...)`. Idempotent by id across restarts.
- `export/jobs/BatchFinalFailureFilter` implementing `ApplyStateFilter`: when a job's applied state is `FAILED` and `job.getState() == FAILED` after JobRunr's own `RetryFilter` has declined to reschedule, transition the batch to `FAILED` with `failedStep` from the job class, `lastError` from the exception, write `EXPORT_BATCH_FAILED` in the same transaction, and increment the `jobs.final_failures` counter. Registered through `BatchFinalFailureFilterRegistration` with `@Priority(Interceptor.Priority.APPLICATION)` on `StartupEvent`, because quarkus-jobrunr does not scan CDI filters and `JobRunrStarter` observes the same event without a priority (file-ingestion findings 5 and 10).
- **Gate:** `JobRunrGateIT` under `WorkerTestProfile`: enqueue twice with one id runs once; a job that fails twice then succeeds ends `SUCCEEDED`; both recurring ids are registered exactly once after two boots; a job that always throws ends `FAILED` after `jobRetries` and the batch row reads `FAILED` with `failedStep` and an `EXPORT_BATCH_FAILED` event; with `vendor-exchange.enabled=false` every job exits without touching the row.

### Step 5 — Inbound auth chain and HTTP skeleton (7 hours)

- `auth/UserContextFilter` (`@PreMatching`, priority `AUTHENTICATION - 1`): skips `/q/*` and `/internal/*`; reads the email claim `https://certifyos.com/email` from `SecurityIdentity.getPrincipal()` cast to `JsonWebToken` (not an injected `JsonWebToken`, quarkus#30445); requires header `tenant-id` on tenant-scoped paths; resolves the user through `UserAuthCacheService.byEmail`; requires a non-empty `permissions[tenantId]` exactly as file-ingestion does; fills the request-scoped `UserContext(email, tenantId, permissions)`.
- `auth/Permission` enum `MANAGE("vendor-export", "manage")`, `READ("vendor-export", "read")` and `auth/PermissionFilter` that reads a `@RequiresPermission` annotation and answers `403 PERMISSION_DENIED` only when `permissions.enforce = true`. Default false, so today the filter is a pass-through and the annotations document intent on each endpoint. Flipping the flag once the DAL rows exist needs no code change.
- `auth/DalUserClient` as file-ingestion's, with the two headers the DAL `HeaderInterceptor` requires (`requesting-user-id: vendor-exchange-service`, `requesting-organization-id: <tenantId>`) and the 404 translation by status code (Quarkus throws `ClientWebApplicationException`, never `NotFoundException`).
- `auth/PubSubPushAuthFilter` bound to `/internal/vendor-exports/egress-events`: extracts the bearer token, verifies it with `com.google.auth.oauth2.TokenVerifier` (issuer `https://accounts.google.com`, audience `cfg.pubsub().pushAudience()`), checks `email == cfg.pubsub().pushServiceAccount()` and `email_verified`, answers `401` otherwise before the body is read. When either config value is absent the filter answers `401` with code `PUSH_NOT_CONFIGURED`. The verifier is injected so the unit test can supply a signing key and a fake certificate source.
- `export/events/EgressEvent` record parsing the message body from the design (`schemaVersion`, `type`, `tenantId`, `correlationId`, `phase`, `outputUri`, `totalRecords`, `totalRows`, `failureMessage`, `completedAt`) plus the Pub/Sub envelope (`messageId`, `publishTime`, attributes). `export/events/EgressEventResource` stub: verified token, parse, log, answer `200`. The state machine branch is CP-39605's.
- `export/api/VendorExportsResource` read stubs: `GET /vendor-exports/schedules` and `GET /vendor-exports` return `{ "items": [] }` through the full auth chain, so the live gate can prove 401, 403 and 200 against the deployed service. `ops/OpsResource`: `POST /vendor-exports/tick` running `TickJob` inline and returning its result.
- `auth/ApiError(code, message)` record; OpenAPI annotations on every stub.
- **Gate:** `UserContextFilterIT` with WireMock DAL proves the two headers are sent, missing `tenant-id` is 400, a user with no role in the tenant is 403 `TENANT_FORBIDDEN`, DAL down is 503 `DAL_UNAVAILABLE`, and with `permissions.enforce = true` a read-only user gets 403 on `POST /vendor-exports/tick`; `PubSubPushAuthFilterTest` proves a token with the wrong audience, wrong issuer, wrong email or no token is 401 and a correct one reaches the resource. `quarkus.http.auth.permission.*.policy=authenticated` has no `@QuarkusTest` coverage (file-ingestion finding 20); it is proven in the step 8 live gate.

### Step 6 — Outbound identity and client interfaces (5 hours)

- `auth/GoogleIdTokenService`: per-audience cached ID token minted from Application Default Credentials with `FORMAT_FULL` and `INCLUDE_EMAIL` (without them the Compute Engine token has no `email` claim and IAP answers 401, file-ingestion finding 63), background refresh 5 minutes before expiry. Two audiences: `dal.iapClientId` and `egress.iapClientId`.
- `auth/GoogleAuthFilter(audience)` as a `ClientRequestFilter`; `clients/EgressAuthFilter` adds `X-Forwarding-Tenant-Id` from the call and the initiating identity header egress reads.
- `clients/EgressClient` REST client interface: `export(tenantId, request)`, `status(tenantId, correlationId)`, `cancel(tenantId, correlationId)` with the DTOs from the design's Contracts section (`EgressExportRequest` including `destination`, `EgressJobResponse`, `EgressStatusResponse`). Interface and DTOs only; the first call is CP-39605's.
- `clients/ApiLayerTokenService`: `POST {apiLayer.url}/auth/client-credentials` with `clientId` and `clientSecret`, caches `accessToken` until `expiresIn` minus 60 s; throws `ApiLayerNotConfigured` when the two optional keys are absent. `clients/ApiLayerAuthFilter` adds the bearer and the `tenant-id` header. `clients/ApiLayerClient` interface: `practitionerFindMany(tenantId, filter, page, pageSize)`, `getEgressTemplate(tenantId, templateId)`, `createEgressTemplate(tenantId, multipart)`; DTOs for the fields the design reads (`id`, `npi`; template `version`, `mappingsCsvUrl`, `separator`, `outputFormat`, `rowExpansionKeys`).
- Readiness: a `@Readiness` health check reports `apiLayer` as `DOWN` with reason `not configured` while the client is absent, so the deployed service shows the pending hook-up on `/q/health/ready` instead of hiding it.
- **Gate:** `GoogleIdTokenServiceTest` asserts the two options are passed and the cache is per audience; `ApiLayerTokenServiceTest` with WireMock asserts the request body, the expiry handling and the not-configured error; `EgressClientIT` with WireMock asserts the bearer and tenant headers on all three calls.

### Step 7 — Observability baseline (2 hours)

- `metrics/VendorExchangeMetrics` registering every metric name from the design's Observability section with its type and tag keys (`vendor_export.tick.duration` timer, `tick.schedules_due` gauge, `batches.by_state` multi-gauge refreshed every 60 s from a `countDocuments` per state, counters for `batches.created`, `egress.failed`, `completion.source`, `event.rejected`, `reconcile.mismatch`, `jobs.retries`, `jobs.final_failures`). Tags carry tenant and vendor only, never an NPI.
- Logging: JSON console with `level=severity`; `exportBatchId`, `tenantId`, `jobId` put on the MDC by a small `JobContext` helper the job classes call first thing.
- OpenTelemetry exporter configured from `OTEL_EXPORTER_OTLP_ENDPOINT` and disabled when unset (file-ingestion's `localhost:4317` noise); Sentry DSN from Secret Manager under `%prod`, off elsewhere.
- **Gate:** `/q/metrics` on a test boot lists every registered name; a log line from a job carries the three MDC keys.

### Step 8 — Container image, Terraform `internal`, Makefile, live deploy (10 hours)

- `terraform/internal/main.tf`: provider `hashicorp/google` 5.38.0, `project = "certifyos-development"` hardcoded (gcloud's ambient project is production), state in `certifyos-tf-state-development` under prefix `vendor-exchange-internal/state`, Terraform `>= 1.9.0`.
- `network.tf`: data sources for VPC `default` and subnet `default` (Atlas peering and the existing Cloud NAT live there; a second NAT is rejected); firewall rules by tag: health checkers to 8080, IAP SSH range to 22, deny everything else at 65000.
- `iam.tf`: service accounts `vendor-exchange-api` and `vendor-exchange-worker`; project roles `logging.logWriter`, `monitoring.metricWriter`, `artifactregistry.reader` for both; Secret Manager `secretAccessor` on each secret for the account that reads it. Every binding that needs `setIamPolicy` on a resource this account does not own is listed in a comment block and in `docs/runbook.md` as a DevOps ask (section 8).
- `secrets.tf`: containers `vendor-exchange-mongodb-uri`, `vendor-exchange-apilayer-client-secret`, `vendor-exchange-sentry-dsn`; values added out of band with `gcloud secrets versions add`.
- `registry.tf`: Artifact Registry repository `vendor-exchange` (DOCKER).
- `pubsub.tf`: dead-letter topic `vendor-exchange-egress-events-dlq` and its drain subscription; push subscription `vendor-exchange-egress-events` on the egress-owned topic with filter `attributes.initiator = "vendor-exchange-worker"`, ack deadline 60 s, dead-letter after 5 attempts, push endpoint `https://vendor-exchange.internal.certifyos.com/internal/vendor-exports/egress-events` with OIDC token for the push service account and audience equal to the endpoint URL. The subscription resource has `count = var.egress_events_topic == null ? 0 : 1`.
- `compute.tf`: one `google_compute_instance_template` per role through `for_each`, `cos-stable` image, Shielded VM, no external IP, `startup.sh.tftpl` rendered with image, registry and `-e` flags; env common to both roles: `JAVA_OPTS_APPEND`, `AUTH0_ISSUER`, `AUTH0_AUDIENCE`, `DAL_URL`, `DAL_IAP_CLIENT_ID`, `EGRESS_URL`, `EGRESS_IAP_CLIENT_ID`, `API_LAYER_URL`, `VENDOR_BUCKET`, plus the optional `API_LAYER_CLIENT_ID`, `PUBSUB_PUSH_SERVICE_ACCOUNT`, `PUBSUB_PUSH_AUDIENCE`, `PERMISSIONS_ENFORCE`; api role `QUARKUS_PROFILE=prod,api`, e2-small, service account `vendor-exchange-api`; worker role `QUARKUS_PROFILE=prod,worker`, e2-standard-2, service account `vendor-exchange-worker`. Startup script: writable `HOME` for `docker-credential-gcr`, `iptables -C || iptables -I INPUT -p tcp --dport 8080 -j ACCEPT` (Container-Optimized OS drops inbound by default), `docker pull` in a retry loop of five attempts with backoff (file-ingestion's single pull under `set -e` left a worker down), then `docker run --restart=always --network=host`. Health check `vendor-exchange-live` on `/q/health/live`. api MIG regional over three zones, `target_size = 2`, PROACTIVE update with `max_surge_fixed = 3`, `max_unavailable_fixed = 0`, auto-healing after 120 s; worker MIG `target_size = 1`, OPPORTUNISTIC update, same surge rule.
- `lb.tf`: global address, Google-managed certificate for `vendor-exchange.internal.certifyos.com`, backend service `EXTERNAL_MANAGED` on the api MIG with `timeout_sec = 60` and connection draining 300 s, URL map, HTTPS proxy, forwarding rule on 443. The Route 53 record is a DevOps ask with the `lb_ip` output.
- `variables.tf` and `outputs.tf`: `env` (internal), `image` (no default, written by the Makefile into `image.auto.tfvars`), `auth0_issuer`, `auth0_audience`, `dal_url`, `dal_iap_client_id`, `egress_url`, `egress_iap_client_id`, `api_layer_url`, `vendor_bucket`, `egress_events_topic` (nullable), `pubsub_push_service_account` (nullable), `permissions_enforce` (false); outputs for both service account emails, `lb_ip`, hostname, image repo, both template self-links, subscription name.
- `Makefile`: `build-image` (`quarkusBuild` then `docker build --platform linux/amd64`), `push-image`, `create-template`, `deploy` (push, write tfvars, targeted apply of templates and both groups, filtered output, one short command per step so the user's runner never kills a long apply mid-state), `rollout-api`, `rollout-worker` (`rolling-action start-update --type proactive --max-surge 3 --max-unavailable 0`, waits for `is-stable`), `rollback-<role>`, `tunnel-dashboard` (`gcloud compute start-iap-tunnel` to the worker's port 8000).
- `scripts/smoke-api.sh`: no token → 401; token for a user with no role in the tenant → 403; token for a tenant member → `200 { "items": [] }` on `GET /vendor-exports/schedules`; `POST /vendor-exports/tick` → `202` with `schedulesDue: 0`. While the DAL IAP grant is pending, the 403 and 200 cases print `503 DAL_UNAVAILABLE (grant pending)` and the script exits non-zero with that label so the gap is visible, never silent.
- **Gate (all live, evidence pasted into `docs/decisions.md`):** `terraform validate` and `terraform plan` clean; apply run by the user; both MIGs `HEALTHY` and stable; `curl` to `/q/health/live` is 200 through the load balancer (by IP with `Host` header until the DNS record exists); `/q/health/ready` lists `apiLayer` as the one pending check; `jobrunr_background_job_servers` holds the worker's heartbeat; `jobrunr_recurring_jobs` holds `vendor-export-tick` and `vendor-export-reconciler`; `smoke-api.sh` passes the 401 and tick cases and labels the DAL cases; the next 06:00 UTC tick writes `EXPORT_TICK_COMPLETED` with `schedulesDue: 0`.

### Step 9 — Documentation, handoff, Jira (3 hours)

- `README.md`: prerequisites, first-time setup, running locally in each role, what local runs touch for real, build and deploy.
- `docs/runbook.md`: deploy order (deploy, proactive worker rollout, wait for stable, apply to restore OPPORTUNISTIC), the hand grants and who holds them, the swap-in table from section 2 with a checkbox per row, how to reach the JobRunr dashboard, how to read a job by batch id.
- `docs/decisions.md` seeded with every deviation from file-ingestion and every finding from the live gate; `HANDOFF.md` with the working discipline and the state at hand-over.
- `postman/` collection with the stub endpoints and an `internal` environment.
- Jira: CP-39604 Implementation Details field (`customfield_12400`) filled with the repository link, the step list, the live-gate evidence and the swap-in table; story points (`customfield_10026`) set to the agreed re-baseline. Draft shown in chat before any write.
- **Gate:** a second engineer can clone, run `make build-image`, and boot both roles locally against a replica-set Mongo from the README alone.

## 7. Estimate

| Step | Hours |
| --- | --- |
| 0 Repository, pins, roles, CI | 3 |
| 1 Configuration and test profiles | 3 |
| 2 Persistence foundation | 6 |
| 3 Audit trail | 3 |
| 4 JobRunr wiring | 6 |
| 5 Inbound auth and HTTP skeleton | 7 |
| 6 Outbound identity and clients | 5 |
| 7 Observability baseline | 2 |
| 8 Image, Terraform, live deploy | 10 |
| 9 Documentation, handoff, Jira | 3 |
| **Total** | **48** |

Waiting time on grants and provisioning (section 8) is calendar time, not effort, and is excluded from the total. Steps 0 to 7 run with no external input; step 8 applies everything the editor account can apply alone.

## 8. Asks to raise on day one

| # | Ask | Owner | What it unblocks |
| --- | --- | --- | --- |
| A1 | Auth0 machine-to-machine client for `vendor-exchange-service` against audience `https://ng-api-stg.certifyos.com/` (internal); secret into `vendor-exchange-apilayer-client-secret` | Owner to be named (Auth0 admin) | First real api-layer call in CP-39605 |
| A2 | `roles/iap.httpsResourceAccessor` on the DAL internal backend for `vendor-exchange-api@` and `vendor-exchange-worker@`, and on the egress internal backend for `vendor-exchange-worker@` | DevOps | Operator calls (DAL lookup) and egress calls |
| A3 | `roles/iam.serviceAccountTokenCreator` on each service account to itself; `roles/pubsub.subscriber` on `vendor-exchange-egress-events` for `vendor-exchange-api@`; `roles/pubsub.publisher` on the dead-letter topic and `roles/pubsub.subscriber` on the subscription for the Pub/Sub service agent | DevOps | Pub/Sub path and any IAM-based signing |
| A4 | `storage.objects.get` on the vendor SFTP bucket under `from/` for `vendor-exchange-worker@`, and the bucket name per environment | Platform team (TS-111546) | `FinishJob` in CP-39605 |
| A5 | Route 53 `A` record `vendor-exchange.internal.certifyos.com` to the `lb_ip` output | DevOps | Managed certificate, hostname-based smoke |
| A6 | Dedicated Atlas user `vendor-exchange` (readWrite on `vendor_exchange`) through `certifyos-pulumi` `mongodb.yaml`; URI into `vendor-exchange-mongodb-uri` | DevOps | Replaces the shared-user interim |
| A7 | Egress topic `egress-export-events` (egress ask 5 in the design) | Egress team | The push subscription resource |
| A8 | DAL `permissions` rows `vendor-export/manage` and `vendor-export/read` attached to the operator role | DAL team | Flipping `permissions.enforce` to true |
| A9 | Design doc edits: group names to `vendor-exchange-*`, `initiator` attribute to `vendor-exchange-worker`, operator paths under `/v1/`, error bodies as problem+json, and the event handler's token check described as new code in this service | Dev (doc owner) | Wording only |
| A10 | Workload Identity Federation provider and a deploy service account bound to this repository, so CI pushes the image and runs the deploy (needs `iam.serviceAccounts.setIamPolicy`) | DevOps | The reusable deploy workflow; until then `make deploy` from a laptop is the interim |
| A11 | Renovate enabled on the organisation (config committed); a shared Gradle convention plugin for Quarkus services | Platform / DevOps | Dependency hygiene across services |

## 9. Definition of done for CP-39604

- Every gate in section 6 shows evidence in `docs/decisions.md`.
- `main` has branch protection, CI runs `./gradlew check` green with Checkstyle, SpotBugs and Spotless enforced.
- `terraform plan` on `terraform/internal/` is zero-change after the live gate, with the open asks recorded as comments next to the resources they belong to.
- `/q/health/ready` on the deployed service names exactly the pending hook-ups from section 2 and nothing else.
- CP-39605 and CP-40438 can start from the job bodies, the resource stubs and the client interfaces without touching configuration, persistence, auth or deployment.
