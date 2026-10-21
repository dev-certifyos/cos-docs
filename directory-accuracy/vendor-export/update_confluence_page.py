#!/usr/bin/env python3
"""One-shot Confluence page updater for the vendor-export design doc (page 2252767300).

Fetches the page body in storage format, applies the review-comment patches
(comments 1-3: cancel-first, create-only copy, template pinning/provisioning,
skipped-NPI non-goal), and PUTs the new version back.

Safety: every patch anchor must match EXACTLY ONCE in the fetched body or the
script aborts before any write. Images and inline-comment markers are never
touched. A backup of the fetched body is written next to this script.

Usage:
  export ATLASSIAN_EMAIL="dev.pandey@certifyos.com"
  export ATLASSIAN_TOKEN="<api token from id.atlassian.com>"
  python3 update_confluence_page.py          # dry run: fetch + verify anchors only
  python3 update_confluence_page.py --apply  # verify, then push the new version
"""

import json
import os
import sys
import urllib.request
import base64

SITE = "https://certifyos.atlassian.net"
PAGE_ID = "2252767300"

# Each patch: (name, old, new). `old` is matched first as-is against the
# storage-format body; if not found, an HTML-entity variant (&#039; &quot;) is
# tried, since Confluence sometimes keeps entities in storage text.
PATCHES = [
    # ---------- Comment 2: step 0 provisioning ----------
    ("p1-step0-put-args",
     "with cadence, timezone, selection filter, and egress template id.",
     "with cadence, timezone, and selection filter."),

    ("p2-step0-provision-bullets",
     ", and confirms the template belongs to the tenant through api-layer's egress-template read.</p></li>",
     ".</p></li>"
     "<li><p><strong>It then provisions the tenant's vendor template.</strong> The vendor's mappings CSV "
     "— the column definitions for the contract file — is authored once by this team, reviewed like code, "
     "and versioned in this module's repository. Enabling a tenant submits it through api-layer's existing "
     "template-upload flow (<code>POST /egress-templates</code>, the same flow the ProviderHub UI uses), which "
     "validates every mapping against the field catalog, writes the CSV to "
     "<code>gs://&lt;bucket&gt;/egress-templates/&lt;tenantId&gt;/&lt;templateId&gt;/v1/mappings.csv</code>, and creates the "
     "tenant's <code>EgressTemplate</code> row in DAL. The returned <code>templateId</code> is stored on the schedule. "
     "If the tenant already has a template for this vendor, the call verifies it instead of creating one.</p></li>"
     "<li><p>Re-running provisioning rolls out a new master revision: api-layer writes <code>v2/mappings.csv</code> "
     "and bumps the row's <code>version</code>; earlier CSV revisions are never overwritten "
     "(<code>EgressTemplateService.java:992-997</code>), which is what per-batch pinning (step 3) relies on.</p></li>"),

    ("p3-step0-why-provisioning",
     "</ul></li><li local-id=\"473dc6272153\">",
     "</ul></li>"
     "<li><p><strong>Why this module provisions the template, not operators:</strong></p><ul>"
     "<li><p>Every participating tenant needs its own template row (egress scopes templates and their GCS paths "
     "by tenant), but every tenant's template must express the same vendor contract. Hand-creating twenty copies "
     "in the UI is twenty chances to drift; machine-creating them from one reviewed file in git is none.</p></li>"
     "<li><p>Egress cannot own this consistency: it is a generic export engine and has no way to know that these "
     "particular templates are meant to be identical. Only this module knows the vendor contract, so provisioning "
     "and keeping the copies consistent is this module's job.</p></li>"
     "<li><p>An operational rule follows: a vendor template with non-terminal batches must not be deleted — an "
     "open batch's pinned CSV revision (step 3) must stay readable.</p></li></ul></li>"
     "<li local-id=\"473dc6272153\">"),

    # ---------- Comments 1+2: step 3 what ----------
    ("p4-step3-cancel-and-pin",
     "check that its state is <code>NPIS_SELECTED</code>.</p></li><li local-id=\"0d3c5855ee9c\">",
     "check that its state is <code>NPIS_SELECTED</code>.</p></li>"
     "<li><p><strong>On a retry only (<code>attempt &gt; 1</code>): cancel the previous attempt first.</strong> "
     "The batch row still holds the prior <code>egress.correlationId</code>; call egress's existing cancel endpoint "
     "(<code>POST /api/v1/egress/jobs/practitioner/{tenantId}/{correlationId}/cancel</code>, CP-35570 — "
     "<strong>egress supports this today</strong>) with it, and write the audit event "
     "<code>EXPORT_PRIOR_ATTEMPT_CANCELLED</code> or <code>EXPORT_PRIOR_ATTEMPT_CANCEL_REJECTED</code> with the old id.</p><ul>"
     "<li><p><code>202</code>: egress marked the job <code>CANCELLED</code>; a still-running pipeline tears itself "
     "down at its next checkpoint. Proceed.</p></li>"
     "<li><p><code>409</code>: the prior attempt already reached a terminal state. If it reached "
     "<code>COMPLETED</code>, its file may already sit at the destination or land shortly — check the destination "
     "object. A complete object is the answer we wanted: move the batch to <code>EGRESS_COMPLETED</code>, enqueue "
     "<code>FinishJob</code>, and stop; no second export. Otherwise proceed.</p></li></ul></li>"
     "<li><p><strong>Pin the template — once per batch, before anything is sent.</strong> If the batch row does "
     "not yet carry a pin, read the tenant's template row once (<code>GET /egress-templates/{templateId}</code>, "
     "through api-layer) and write onto the batch row: <code>templateVersion</code>, <code>mappingsCsvUrl</code> "
     "(version-specific, e.g. <code>…/v3/mappings.csv</code>), <code>separator</code>, <code>outputFormat</code>, "
     "<code>rowExpansionKeys</code>. The pin is written before the export call, so a crash between the two leaves a "
     "rerun that reads the saved pin instead of re-resolving the row. A retry finds the pin already present and never "
     "touches the template row again; if the live row's version has moved since, log it — the pin makes the drift "
     "harmless, and the new version applies from the next batch.</p></li>"
     "<li local-id=\"0d3c5855ee9c\">"),

    ("p5-step3-call-pinned-fields",
     ", the tenant's <code>egressTemplateId</code>, and a <code>correlationId</code>",
     ", the tenant's <code>egressTemplateId</code>, <strong>the pinned </strong><code>mappingsCsvUrl</code>"
     "<strong>, </strong><code>separator</code><strong>, </strong><code>outputFormat</code><strong>, and </strong>"
     "<code>rowExpansionKeys</code> (all existing request fields — <strong>egress supports this today</strong>), "
     "and a <code>correlationId</code>"),

    ("p6-step3-destination-ask",
     "the request also carries a <code>destination</code> (bucket and object name). When the export completes, "
     "egress copies the file there, with the object metadata, and writes nothing else beside it.",
     "the request also carries a <code>destination</code> (bucket and object name). When the export completes, "
     "egress copies the file there <strong>create-only</strong> (<code>ifGenerationMatch: 0</code>), with the object "
     "metadata including the <code>correlationId</code>, and writes nothing else beside it. A "
     "<code>412 Precondition Failed</code> on the copy is a no-op success — a file is already in place. The copy "
     "is skipped when the job's status is <code>CANCELLED</code>."),

    ("p7-step3-why-cancel-and-pin",
     "<li local-id=\"2499687adb59\">",
     "<li><p><strong>Why cancel first, and why cancel alone is not enough:</strong></p><ul>"
     "<li><p>Marking our batch <code>FAILED</code> does nothing to the job egress is still running. The two "
     "<code>FAILED</code> causes an operator retries from (<code>EGRESS_DID_NOT_FINISH</code>, "
     "<code>FILE_NOT_FOUND</code>) are exactly the ones where the old attempt may still be alive, so without a cancel "
     "two egress jobs can hold a write claim on one destination object, and the older one can land late and silently "
     "replace the newer file.</p></li>"
     "<li><p>The cancel is cooperative: it sets the job <code>CANCELLED</code> atomically in egress's metadata, and "
     "running pipelines poll that flag at checkpoints (<code>SeedPipeline.java</code>, "
     "<code>DataflowJobLauncherService.java</code>). A pipeline past its last checkpoint still finishes and still "
     "writes its file. The cancel closes the window almost entirely; the create-only precondition on the destination "
     "copy is the atomic guard that closes it completely — first writer wins, the late writer's <code>412</code> "
     "is harmless, and the vendor never sees two files or a changed file.</p></li>"
     "<li><p>Whichever attempt's file wins is valid: the NPI set was fixed in <code>vendor_export_npis</code> at "
     "selection and both attempts use the same template, so the only difference is the read time of the data. "
     "<code>FinishJob</code> records which attempt produced the file from the <code>correlationId</code> in the object "
     "metadata.</p></li></ul></li>"
     "<li><p><strong>Why pin the template per batch, and why this needs nothing from egress:</strong></p><ul>"
     "<li><p>The template row is mutable — it always points at the latest definition — but everything "
     "downstream of it is already immutable: api-layer writes every template edit to a new "
     "<code>v&lt;N&gt;/mappings.csv</code> and never overwrites an old revision, and egress copies the request's "
     "<code>mappingsCsvUrl</code>, <code>separator</code>, <code>outputFormat</code>, and <code>rowExpansionKeys</code> "
     "into its own job metadata at request time (<code>ExportOrchestrationService.java:459-465</code>) and builds the "
     "file from that stored copy, never re-reading the template row mid-run "
     "(<code>TemplateMappingExportPipeline.java:524</code>). <code>templateId</code> itself does not select the "
     "columns; the URL does — egress uses the id only for display and file metadata.</p></li>"
     "<li><p>The one unpinned hop was between our attempts: without the pin, each attempt re-resolves the row, so an "
     "edit between attempt 1 and a retry silently changes what the same batch id produces. Pinning resolves the row "
     "once per batch and hands every attempt the same answer — the same reasoning that freezes the NPI set in "
     "<code>vendor_export_npis</code> at selection, applied to the file definition.</p></li>"
     "<li><p>This is also what makes the overwrite-race guarantee true: “either attempt's file is valid” holds "
     "because both attempts export the same NPI set with the same pinned definition.</p></li></ul></li>"
     "<li local-id=\"2499687adb59\">"),

    ("p8a-step3-whatif-cancel",
     "because egress accepted the first call, and completes normally.</p></li><li local-id=\"6393c27f414f\">",
     "because egress accepted the first call, and completes normally.</p></li>"
     "<li><p>The cancel call fails with <code>5xx</code>: the job throws and JobRunr retries it. The retry's cancel "
     "finds the job already <code>CANCELLED</code> and gets <code>409</code>, which is the terminal answer, and the "
     "job proceeds. Cancelling is safe to repeat.</p></li>"
     "<li local-id=\"6393c27f414f\">"),

    ("p8b-step3-whatif-template",
     "so the row is never half-updated.</p></li><li local-id=\"155b4a107743\">",
     "so the row is never half-updated.</p></li>"
     "<li><p>The template read for the pin fails: the job throws and JobRunr retries it. Once the pin is written it "
     "is never read again from DAL, so this failure mode exists only on a batch's first attempt.</p></li>"
     "<li><p>The pinned mappings CSV is gone at export time (the template was deleted despite the rule in step 0): "
     "the egress job fails to load its columns and reports <code>FAILED</code>; the batch fails with egress's error, "
     "and the operator re-provisions the template and supersedes the batch.</p></li>"
     "<li local-id=\"155b4a107743\">"),

    # ---------- Comment 1: step 5 ----------
    ("p9-step5-cancel",
     "If the second check also finds egress still running, the batch moves to <code>FAILED</code> with cause "
     "<code>EGRESS_DID_NOT_FINISH</code>. From here an operator decides.",
     "If the second check also finds egress still running, the job first requests cancellation of the stored "
     "<code>egress.correlationId</code> (best effort — a <code>409</code> means the job just reached a terminal "
     "state, and the next event or retry handles it), then moves the batch to <code>FAILED</code> with cause "
     "<code>EGRESS_DID_NOT_FINISH</code>. Cancelling here stops an abandoned pipeline from running on, and makes the "
     "retry's own cancel a cheap <code>409</code>. From here an operator decides."),

    # ---------- Comment 1: step 6 ----------
    ("p10-step6-metadata",
     "Read the metadata egress set on the object: <code>complete=true</code>, <code>totalRecords</code> (the number "
     "of practitioners), and <code>totalRows</code> (the number of file rows).",
     "Read the metadata egress set on the object: <code>complete=true</code>, <code>totalRecords</code> (the number "
     "of practitioners), <code>totalRows</code> (the number of file rows), and <code>correlationId</code> (the "
     "attempt that produced the file). Record the producing attempt on the batch row as "
     "<code>egress.fileProducedBy</code>."),

    ("p11-step6-overwrite-mechanism",
     "make the vendor's echo ambiguous. A re-run after delivery is a new sequence number (step 8).</p></li>",
     "make the vendor's echo ambiguous. A re-run after delivery is a new sequence number (step 8).</p><ul>"
     "<li><p>This is enforced at write time, not by convention: the destination copy is create-only "
     "(<code>ifGenerationMatch: 0</code>, step 3), so a late copy from a cancelled earlier attempt gets "
     "<code>412</code> and changes nothing.</p></li>"
     "<li><p>The object metadata carries the <code>correlationId</code> of the attempt that produced the file. "
     "<code>FinishJob</code> records it on the batch row as <code>egress.fileProducedBy</code>; a file from an "
     "earlier attempt is equally valid, because every attempt exports the same registered NPI set with the same "
     "template.</p></li></ul></li>"),

    # ---------- Comment 1: step 8 ----------
    ("p12-step8-retry-note",
     "A selection that already succeeded is not redone for an egress failure. The vendor never sees a different "
     "batch id.</p></li></ul></li>",
     "A selection that already succeeded is not redone for an egress failure. The vendor never sees a different "
     "batch id.</p></li>"
     "<li><p>The endpoint itself only flips state and enqueues. Cancelling the previous attempt and the destination "
     "preflight live inside <code>RequestEgressJob</code> (step 3), so they get JobRunr's retry and backoff if egress "
     "is unreachable at retry time — which is likely, given the batch just failed there.</p></li></ul></li>"),

    # ---------- Comment 3: non-goal ----------
    ("p13-notdo-skipped-npis",
     "It does not read the CSV. Reconciliation is by count from object metadata.</p></li></ul>",
     "It does not read the CSV. Reconciliation is by count from object metadata.</p></li>"
     "<li><p>It does not exclude individual NPIs from selection. NPIs skipped by a reviewer in the vendor-review "
     "flow (phase 2) are not filtered out: whether a skipped NPI should sit out later cadences is an open product "
     "decision, with reviewer-workload and per-NPI vendor-cost consequences. When it is taken, the hook is a "
     "module-owned exclusion list consulted by <code>SelectJob</code> after the criteria match — a "
     "practitioner-record flag was considered and set aside, since “do not send to this vendor” is a fact "
     "about the export relationship, not master data. Neither api-layer nor egress changes either way.</p></li></ul>"),

    # ---------- Decisions ----------
    ("p14a-d11-decision",
     "A placed file is never rewritten; a re-run after delivery is a new sequence number</p>",
     "A placed file is never rewritten: the destination copy is create-only (<code>ifGenerationMatch: 0</code>), the "
     "first attempt to finish wins, a retry cancels its predecessor first, and a re-run after delivery is a new "
     "sequence number</p>"),

    ("p14b-d11-instead",
     ">Overwriting under the same name</p>",
     ">Overwriting under the same name; cancel alone (cooperative, so a pipeline past its last checkpoint still "
     "writes)</p>"),

    ("p15-d15-row",
     "Rejected in <em>Alternatives considered</em></p></td></tr>",
     "Rejected in <em>Alternatives considered</em></p></td></tr>"
     "<tr><td><p>D15</p></td><td><p>The vendor mappings CSV is authored once in this repo and provisioned per tenant "
     "by the enable call; each batch pins the template's version-specific <code>mappingsCsvUrl</code>, "
     "<code>separator</code>, <code>outputFormat</code>, and <code>rowExpansionKeys</code> on the batch row at first "
     "request and sends the pin on every attempt</p></td><td><p>Hand-created templates per tenant; re-resolving the "
     "template row on each attempt; asking egress for immutable template revisions (a cross-team feature whose only "
     "saving here would be the pin fields we store anyway)</p></td></tr>"),

    # ---------- Contracts step 0 ----------
    ("p16-contract0-putjson",
     ",\n  \"egressTemplateId\": \"c9a418b5-348e-44e6-95f0-04c45f527367\"\n}",
     "\n}"),

    ("p17-contract0-validation",
     "Validation order: token and permission &rarr; tenant exists (internal) &rarr; schema (fields, operators, value "
     "types; tenant id contains no underscore, because the filename uses underscores as separators) &rarr; template "
     "belongs to tenant (api-layer egress-template read) &rarr; write. One write; nothing can half-complete.",
     "<code>egressTemplateId</code> is not in the request: create provisions the tenant's vendor template from this "
     "module's master mappings CSV (through api-layer's <code>POST /egress-templates</code> upload flow) and stores "
     "the returned id on the schedule. If the tenant already has a template for this vendor, it is verified and "
     "reused. An optional <code>egressTemplateId</code> override is accepted for the pilot, verified to belong to "
     "the tenant.</p></li><li><p>Validation order: token and permission &rarr; tenant exists (internal) &rarr; schema "
     "(fields, operators, value types; tenant id contains no underscore, because the filename uses underscores as "
     "separators) &rarr; template provisioned or verified (api-layer egress-template flow) &rarr; write. The schedule "
     "write is one write; if template provisioning succeeded but the schedule write fails, the template row is left "
     "in place and the retried call finds and reuses it."),

    # ---------- Contracts step 3 ----------
    ("p18-contract3-json",
     "\"templateId\": \"c9a418b5-348e-44e6-95f0-04c45f527367\",\n  \"npiFilter\": [\"1234567893\", \"2345678918\"],\n"
     "  \"outputFormat\": \"csv\",",
     "\"templateId\": \"c9a418b5-348e-44e6-95f0-04c45f527367\",\n"
     "  \"mappingsCsvUrl\": \"gs://egress-templates/org-xyz/c9a418b5-348e-44e6-95f0-04c45f527367/v3/mappings.csv\",\n"
     "  \"separator\": \";\",\n"
     "  \"outputFormat\": \"csv\",\n"
     "  \"rowExpansionKeys\": [\"locations\"],\n"
     "  \"npiFilter\": [\"1234567893\", \"2345678918\"],"),

    ("p19-contract3-templateid-bullet",
     " Egress loads <code>rowExpansionKeys</code> and the mappings CSV from it; nothing about columns travels in "
     "this request.</p></li>",
     " Provisioned by this module at enablement (step 0). Egress does not resolve columns from it: it uses the id "
     "for the Activity Center display and the output file's metadata only.</p></li>"
     "<li><p><code>mappingsCsvUrl</code>, <code>separator</code>, <code>outputFormat</code>, "
     "<code>rowExpansionKeys</code> — the batch's pinned template values (step 3), all existing request fields. "
     "Egress validates the URL is tenant-scoped (<code>GcsPathValidator.java:49-59</code>), stores all four on its "
     "job metadata at request time, and builds the file from that stored copy "
     "(<code>TemplateMappingExportPipeline.java:524</code>). Every attempt of a batch sends the same pinned values, "
     "so a retry is built from a byte-identical definition.</p></li>"),

    ("p20-contract3-destination",
     "output always goes to <code>gs://&lt;egress bucket&gt;/practitioner/&lt;tenantId&gt;/&lt;correlationId&gt;/"
     "export.csv</code> (<code>DataflowJobLauncherService.java:619-621</code>).</p></li>",
     "output always goes to <code>gs://&lt;egress bucket&gt;/practitioner/&lt;tenantId&gt;/&lt;correlationId&gt;/"
     "export.csv</code> (<code>DataflowJobLauncherService.java:619-621</code>). The copy to the destination is "
     "create-only (<code>ifGenerationMatch: 0</code>): a <code>412</code> is a no-op success, the object metadata "
     "carries the producing <code>correlationId</code>, and the copy is skipped when the job's status is "
     "<code>CANCELLED</code>.</p></li>"),

    # ---------- Contracts step 5 ----------
    ("p21-contract5-cancel",
     "with cause <code>EGRESS_DID_NOT_FINISH</code> (check 2).</p></li></ul>",
     "with cause <code>EGRESS_DID_NOT_FINISH</code> (check 2), plus <code>EXPORT_PRIOR_ATTEMPT_CANCELLED</code> or "
     "<code>EXPORT_PRIOR_ATTEMPT_CANCEL_REJECTED</code> for the cancel.</p></li>"
     "<li><p>Check 2, before failing the batch: <code>POST {egressBaseUrl}/api/v1/egress/jobs/practitioner/"
     "{tenantId}/{correlationId}/cancel</code> (best effort; <code>202</code> or <code>409</code> both proceed)."
     "</p></li></ul>"),

    # ---------- Contracts step 6 ----------
    ("p22-contract6-metadata",
     "Reads object metadata egress sets on completion: <code>complete=true</code>, <code>totalRecords</code>, "
     "<code>totalRows</code>, <code>templateId</code> (<code>TemplateMappingExportPipeline.java:626-637</code>).</p></li>",
     "Reads object metadata egress sets on completion: <code>complete=true</code>, <code>totalRecords</code>, "
     "<code>totalRows</code>, <code>templateId</code> (<code>TemplateMappingExportPipeline.java:626-637</code>), and "
     "<code>correlationId</code> (<strong>Needs egress change</strong>, part of ask 6) — recorded on the batch "
     "row as <code>egress.fileProducedBy</code>, since with the create-only copy the file may come from an earlier "
     "attempt than the current one.</p></li>"),

    # ---------- Contracts: batch row example ----------
    ("p23a-batchjson-pin",
     "\"templateId\": \"c9a418b5-348e-44e6-95f0-04c45f527367\",\n    \"destination\": {",
     "\"templateId\": \"c9a418b5-348e-44e6-95f0-04c45f527367\",\n"
     "    \"templateVersion\": 3,\n"
     "    \"mappingsCsvUrl\": \"gs://egress-templates/org-xyz/c9a418b5-348e-44e6-95f0-04c45f527367/v3/mappings.csv\",\n"
     "    \"separator\": \";\",\n"
     "    \"outputFormat\": \"csv\",\n"
     "    \"rowExpansionKeys\": [\"locations\"],\n"
     "    \"destination\": {"),

    ("p23b-batchjson-producedby",
     "\"completionSource\": \"EVENT\"\n  },",
     "\"completionSource\": \"EVENT\",\n    \"fileProducedBy\": \"org-xyz-candor-2026-10-001-r1\"\n  },"),

    # ---------- Audit events table ----------
    ("p24-audit-cancel-row",
     "<code>fromState</code>, <code>failedStep</code>, <code>newAttempt</code>, <code>reason</code></p></td></tr>",
     "<code>fromState</code>, <code>failedStep</code>, <code>newAttempt</code>, <code>reason</code></p></td></tr>"
     "<tr><td><p><code>EXPORT_PRIOR_ATTEMPT_CANCELLED</code> / <code>EXPORT_PRIOR_ATTEMPT_CANCEL_REJECTED</code></p>"
     "</td><td><p>its own write, before the new egress request (step 3) or before <code>EGRESS_DID_NOT_FINISH</code> "
     "(step 5, check 2)</p></td><td><p><code>cancelledCorrelationId</code>, <code>outcome</code> (<code>202</code> "
     "accepted / <code>409</code> already terminal)</p></td></tr>"),

    # ---------- Outbound file contract intro ----------
    ("p25-outbound-intro",
     "(&sect;4.3, 30 columns, one row per practitioner &times; practice location). Verified mapping paths",
     "(&sect;4.3, 30 columns, one row per practitioner &times; practice location). The mappings CSV expressing it is "
     "authored once by this team, reviewed like code, and versioned in this module's repository; step 0 provisions "
     "it per tenant through api-layer's template-upload flow, and step 3 pins the resulting version-specific CSV per "
     "batch. Verified mapping paths"),

    # ---------- Failure taxonomy ----------
    ("p26a-failure-inside",
     "the second turns an unfinished job into <code>FAILED</code> with a human decision. A failed egress job is "
     "retried by an operator under a new attempt suffix.",
     "the second cancels the job and turns an unfinished one into <code>FAILED</code> with a human decision. A "
     "failed egress job is retried by an operator under a new attempt suffix, after the previous attempt is "
     "cancelled."),

    ("p26b-failure-after",
     "idempotent by object path and NPI id; the file is never touched.",
     "idempotent by object path and NPI id; the file is never touched. The destination write itself is create-only, "
     "so no attempt — live or cancelled — can replace a file that has landed."),

    # ---------- Rollout ask 6 ----------
    ("p27a-ask6-text",
     "when present it overrides the configured connector. On <code>COMPLETED</code>, copy the file there with its "
     "metadata, write no manifest, and put the destination URI in the completion event",
     "when present it overrides the configured connector. On <code>COMPLETED</code>, copy the file there create-only "
     "(<code>ifGenerationMatch: 0</code>; a <code>412</code> is a no-op success), with its metadata including the "
     "producing <code>correlationId</code>, write no manifest, skip the copy when the job's status is "
     "<code>CANCELLED</code>, and put the destination URI in the completion event"),

    ("p27b-ask6-why",
     "The copy code from CP-37875 is reused; only the source of the target and the manifest switch change",
     "The copy code from CP-37875 is reused; only the source of the target and the manifest switch change. The "
     "create-only precondition and the <code>correlationId</code> in the metadata close the retry race: a cancelled "
     "earlier attempt that finishes late cannot replace the newer file, and we always know which attempt produced "
     "the file that landed"),
]


def entity_variants(s: str):
    """Yield the string plus entity-encoded variants Confluence storage may use."""
    yield s
    yield s.replace("'", "&#039;").replace('"', "&quot;")
    yield s.replace("'", "&rsquo;")
    yield s.replace("—", "&mdash;").replace("'", "&#039;").replace('"', "&quot;")


def api(path, method="GET", payload=None):
    email = os.environ.get("ATLASSIAN_EMAIL")
    token = os.environ.get("ATLASSIAN_TOKEN")
    if not email or not token:
        sys.exit("Set ATLASSIAN_EMAIL and ATLASSIAN_TOKEN environment variables first.")
    req = urllib.request.Request(SITE + path, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(req, data=data) as r:
        return json.loads(r.read().decode())


def main():
    apply_changes = "--apply" in sys.argv

    page = api(f"/wiki/api/v2/pages/{PAGE_ID}?body-format=storage")
    body = page["body"]["storage"]["value"]
    title = page["title"]
    version = page["version"]["number"]
    print(f"Fetched '{title}' v{version}, body {len(body)} chars")

    backup = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"confluence-backup-v{version}.storage.html")
    with open(backup, "w") as f:
        f.write(body)
    print(f"Backup written: {backup}")

    new_body = body
    failures = []
    for name, old, new in PATCHES:
        matched = None
        for variant in entity_variants(old):
            n = new_body.count(variant)
            if n == 1:
                matched = variant
                break
            if n > 1:
                failures.append(f"{name}: anchor matches {n} times (must be 1)")
                matched = "AMBIGUOUS"
                break
        if matched is None:
            failures.append(f"{name}: anchor not found")
            continue
        if matched == "AMBIGUOUS":
            continue
        # Apply the same entity transformation to the replacement that matched the anchor
        if matched != old:
            if "&#039;" in matched or "&quot;" in matched:
                new = new.replace("'", "&#039;").replace('"', "&quot;")
            if "&mdash;" in matched:
                new = new.replace("—", "&mdash;")
        new_body = new_body.replace(matched, new, 1)
        print(f"  ok  {name}")

    if failures:
        print("\nABORTED — the following anchors failed (page NOT modified):")
        for f in failures:
            print("  " + f)
        sys.exit(1)

    print(f"\nAll {len(PATCHES)} patches verified. New body: {len(new_body)} chars")

    if not apply_changes:
        print("Dry run complete. Re-run with --apply to push.")
        return

    result = api(f"/wiki/api/v2/pages/{PAGE_ID}", "PUT", {
        "id": PAGE_ID,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": new_body},
        "version": {"number": version + 1, "message": "Review-comment fixes: cancel-first + create-only copy, template provisioning + per-batch pin, skipped-NPI non-goal"},
    })
    print(f"Pushed v{result['version']['number']}: {SITE}/wiki{result['_links']['webui']}")


if __name__ == "__main__":
    main()
