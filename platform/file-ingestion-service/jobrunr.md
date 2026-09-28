# JobRunr lifecycle

## Flow

```mermaid
flowchart TD
    C[Client]
    GCS[(GCS bucket)]
    PS[Pub/Sub inbound]
    CT[Pub/Sub completion topic]

    subgraph API["GCE api MIG"]
        A1[POST /v1/ingestion<br/>REGISTERED + signed URL]
        A2[Receiver<br/>claim, gates, RECEIVED<br/>enqueue parse job]
    end

    subgraph MONGO["Mongo file_ingestion"]
        ING[(ingestions<br/>ingestion_rows<br/>ingestion_events)]
        JOBS[(jobrunr_jobs)]
        REC[(jobrunr_recurring_jobs)]
    end

    subgraph WORK["GCE worker MIG"]
        M[Master<br/>oldest worker<br/>every 5s]
        W[Any worker<br/>idle thread]
        P[ParseJob.run<br/>validate, stream, bulk 500<br/>COMPLETED / PARTIAL]
        S[IngestionSweep.run<br/>expire, re-offer, enqueue lost]
    end

    C -->|1 register| A1
    A1 -->|2 URL| C
    C -->|3 PUT file| GCS
    GCS -->|4 OBJECT_FINALIZE| PS
    PS -->|5 push| A2
    A2 -->|6 write ENQUEUED| JOBS
    W -->|7 fetch ENQUEUED<br/>atomic PROCESSING| JOBS
    W --> P
    P -->|8 rows + status| ING
    P -->|9 terminal event| CT
    P -->|10 SUCCEEDED| JOBS

    REC -.->|recipe<br/>*/5 * * * *| M
    M -->|due + no live run<br/>create SCHEDULED to ENQUEUED| JOBS
    W --> S
    S -->|expire, re-offer| ING
    S -->|enqueue orphan RECEIVED| JOBS
    M -.->|orphan PROCESSING to FAILED<br/>delete SUCCEEDED| JOBS
```

## Roles
- api VM: enqueue only
- worker VM: run jobs
- master: oldest worker, auto re-elected
- Mongo: `jobrunr_jobs`, `jobrunr_recurring_jobs`, `jobrunr_background_job_servers`
- removed: Cloud Run, Cloud Scheduler
- kept: GCS, Pub/Sub, `ingestions`, `ingestion_events`

## 1. Code
- `@Job` parse method
- `@Recurring` sweep method

## 2. Boot
- all VMs: upsert recurring recipe
- workers: register, heartbeat 5s
- oldest alive = master

## 3. Register
- `POST /v1/ingestion` → `REGISTERED`, signed URL

## 4. Upload
- client `PUT` → GCS

## 5. Receive
- api VM
- Pub/Sub push
- claim, gates, `RECEIVED`
- enqueue parse, id = ingestionId
- ack

## 6. Claim
- worker fetches `ENQUEUED`
- atomic → `PROCESSING`

## 7. Parse
- CAS `RECEIVED` → `VALIDATING`
- validate, stream, bulk 500
- `COMPLETED` / `PARTIAL`, publish
- job heartbeat every 5s

## 8. Done
- `SUCCEEDED`, deleted after 36h

## 9. Fail
- exception → `FAILED` → retry
- worker dies → orphan → retry
- master dies → next oldest
- alert: `state = FAILED`

## 10. Sweep
- every 5 min
- master: recipe due? live instance? no → create job
- any worker runs it
- duties: expire `REGISTERED`, expire parked, re-offer lost, enqueue orphan `RECEIVED`
- once per tick, cluster-wide

## Q&A
- Q1: api MIG + worker MIG
- Q2: cap = `worker-count` × worker VMs; set 1 × MIG size
- Q3: JobRunr recurring, not Cloud Scheduler, not quarkus-scheduler

## Open
- Mongo cluster exists? F5 vs attestation brief says Spanner
- retry-aware claim CAS
