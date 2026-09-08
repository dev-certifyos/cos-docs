#!/usr/bin/env python3
"""Generate the worked Candor exchange sample files (export + recommendations + manifest).

Run from this folder: python3 generate_samples.py   (no dependencies)
Writes:
  from/org-xyz/org-xyz_org-xyz-candor-2026-09-001_20260901.csv                      (certify-export-v1, 31 cols)
  to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.csv          (candor-recs-v1, 26 cols)
  to/org-xyz/…_20260915.csv.manifest.json                                           (certify-manifest-v1, uploaded last)
All data is fictional: fake NPIs (valid Luhn check digit), .example domains, 555 phone numbers.
Column contracts: reference/candor-exchange-contract-proposal.md §4.3 (export), §5.2 (return), §5.3 (vocabulary).
"""
import csv
import hashlib
import json
import pathlib

HERE = pathlib.Path(__file__).parent


def npi(base9: str) -> str:
    """Append the NPI check digit (Luhn over the '80840' prefix + 9 digits)."""
    s = "80840" + base9
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return base9 + str((10 - total % 10) % 10)


N = [npi(x) for x in ["123456789", "234567891", "345678912", "456789123", "567891234", "678912345", "789123456"]]

# ------------------------------------------------------------------ export (certify-export-v1)
EXPORT_COLS = [
    "schema_version", "export_batch_id", "export_generated_at", "tenant_id", "entity_type",
    "certify_practitioner_id", "npi", "attestation_due_date",
    "name_prefix", "first_name", "middle_name", "last_name", "name_suffix", "group_affiliation",
    "certify_location_id", "location_name", "address_type",
    "address_line1", "address_line2", "city", "state", "zip",
    "location_phone", "practitioner_phone", "website", "specialty", "accepting_new_patients",
    "languages", "telehealth_available", "telehealth_url", "ada_accommodations",
]
SV, B, GEN, T, ET = "certify-export-v1", "org-xyz-candor-2026-09-001", "2026-09-01T06:00:00Z", "org-xyz", "PRACTITIONER"
DUE = "2026-09-30"


def x(cid, n, name, groups, lid, lname, addr, lphone, pphone, web, spec, anp, langs, tele, teleurl, ada):
    prefix, first, middle, last, suffix = name
    a1, a2, city, st, zc = addr
    return [SV, B, GEN, T, ET, cid, n, DUE, prefix, first, middle, last, suffix, groups,
            lid, lname, "PRACTICE", a1, a2, city, st, zc, lphone, pphone, web, spec, anp, langs, tele, teleurl, ada]


EXPORT_ROWS = [
    # Rivera: two locations
    x("cert-000123", N[0], ("Dr.", "Jane", "", "Rivera", "MD"), "Sunrise Medical Group", "loc-000501", "Sunrise Medical Group - Main St",
      ("100 Main St", "Suite 4", "Columbus", "OH", "43215"), "614-555-0100", "614-555-0142", "https://sunrisemed.example",
      "Cardiology", "Y", "English;Spanish", "Y", "https://sunrisemed.example/tele", "Wheelchair accessible"),
    x("cert-000123", N[0], ("Dr.", "Jane", "", "Rivera", "MD"), "Sunrise Medical Group", "loc-000502", "Sunrise Medical Group - Dublin",
      ("4500 Lakeview Blvd", "", "Dublin", "OH", "43017"), "614-555-0177", "614-555-0142", "https://sunrisemed.example",
      "Cardiology", "N", "English;Spanish", "Y", "https://sunrisemed.example/tele", ""),
    # Okafor: name typo in our data, two groups
    x("cert-000287", N[1], ("Dr.", "Chidi", "A", "Okafer", "DO"), "Riverside Family Practice;Ohio Primary Care Network", "loc-000503", "Riverside Family Practice",
      ("220 W Broad St", "", "Columbus", "OH", "43215"), "614-555-0210", "", "https://riversidefp.example",
      "Family Medicine", "Y", "English", "N", "", "Wheelchair accessible;Accessible restroom"),
    # Patel: stale location phone, incomplete languages
    x("cert-000341", N[2], ("Dr.", "Priya", "", "Patel", "MD"), "Buckeye Neurology Associates", "loc-000504", "Buckeye Neurology - Zollinger",
      ("1800 Zollinger Rd", "Bldg B", "Upper Arlington", "OH", "43221"), "614-555-0300", "614-555-0388", "https://buckeyeneuro.example",
      "Neurology", "Y", "English;Hindi;Gujarati", "Y", "https://buckeyeneuro.example/virtual", "Wheelchair accessible"),
    # Nguyen: PO box listed as practice address
    x("cert-000402", N[3], ("", "Linh", "T", "Nguyen", "NP"), "Eastside Pediatrics", "loc-000505", "Eastside Pediatrics",
      ("PO Box 9120", "", "Columbus", "OH", "43209"), "614-555-0410", "", "",
      "Pediatrics", "Y", "English;Vietnamese", "N", "", ""),
    # Schmidt: everything correct except a suite typo
    x("cert-000515", N[4], ("Dr.", "Marcus", "", "Schmidt", "MD"), "Capital City Orthopedics", "loc-000506", "Capital City Orthopedics",
      ("3535 Olentangy River Rd", "Suite 210", "Columbus", "OH", "43214"), "614-555-0500", "614-555-0555", "https://capcityortho.example",
      "Orthopedic Surgery", "Y", "English;German", "N", "", "Wheelchair accessible"),
    # Alvarez: second location missing, ANP wrong
    x("cert-000618", N[5], ("Dr.", "Sofia", "M", "Alvarez", "MD"), "Metro Behavioral Health", "loc-000507", "Metro Behavioral Health - Long St",
      ("900 E Long St", "", "Columbus", "OH", "43203"), "614-555-0600", "", "https://metrobh.example",
      "Psychiatry", "N", "English;Spanish", "Y", "https://metrobh.example/telepsych", "Wheelchair accessible"),
    # Bennett: deceased — the practitioner_status REMOVE case
    x("cert-000733", N[6], ("Dr.", "Harold", "J", "Bennett", "MD"), "Westerville Internal Medicine", "loc-000508", "Westerville Internal Medicine",
      ("55 N State St", "Suite 2", "Westerville", "OH", "43081"), "614-555-0700", "614-555-0777", "https://westervilleim.example",
      "Internal Medicine", "Y", "English", "N", "", "Wheelchair accessible"),
]

# ------------------------------------------------------------------ return (candor-recs-v1)
REC_COLS = [
    "schema_version", "export_batch_ref", "candor_batch_id", "finding_id", "tenant_id", "entity_type",
    "certify_practitioner_id", "npi", "certify_location_id", "address_line1", "city", "state", "zip",
    "attribute", "recommendation", "current_value_seen", "recommended_value",
    "verification_status", "verification_reason", "evidence_type", "evidence", "verified_at",
    "effective_from", "confidence", "candor_provider_ref", "candor_location_ref",
]
RV, CB = "candor-recs-v1", "CB-2026-09-001"
_seq = [0]


def r(cid, n, lid, addr, attr, rec, cur, new, status, reason, evtype, evidence, date, conf,
      effective_from="", cand_p="", cand_l=""):
    _seq[0] += 1
    a1, c, s, z = addr if addr else ("", "", "", "")
    return [RV, B, CB, f"CF-2026-09-001-{_seq[0]:04d}", T, ET, cid, n, lid, a1, c, s, z,
            attr, rec, cur, new, status, reason, evtype, evidence, date, effective_from, conf, cand_p, cand_l]


L_RIV1 = ("100 Main St", "Columbus", "OH", "43215")
L_RIV2 = ("4500 Lakeview Blvd", "Dublin", "OH", "43017")
L_OKA = ("220 W Broad St", "Columbus", "OH", "43215")
L_PAT = ("1800 Zollinger Rd", "Upper Arlington", "OH", "43221")
L_NGU_OLD = ("PO Box 9120", "Columbus", "OH", "43209")
L_NGU_NEW = ("2950 E Main St", "Bexley", "OH", "43209")
L_SCH = ("3535 Olentangy River Rd", "Columbus", "OH", "43214")
L_ALV1 = ("900 E Long St", "Columbus", "OH", "43203")
L_ALV2 = ("1275 Olentangy River Rd", "Columbus", "OH", "43212")
L_BEN = ("55 N State St", "Westerville", "OH", "43081")
HSV, DO = "health_system_verified", "direct_outreach"

REC_ROWS = [
    # Rivera (Candor refs supplied — shows the optional columns filled)                                   finding ids 0001–0007
    r("cert-000123", N[0], "loc-000501", L_RIV1, "practice_address", "KEEP", "100 Main St|Suite 4|Columbus|OH|43215", "", "VALID", DO, "DATE", "2026-09-08", "2026-09-08", "VERY HIGH", cand_p="cand-p-88120", cand_l="cand-l-30411"),
    r("cert-000123", N[0], "loc-000501", L_RIV1, "location_phone", "UPDATE", "614-555-0100", "614-555-0199", "INVALID", DO, "DATE", "2026-09-08", "2026-09-08", "VERY HIGH", cand_p="cand-p-88120", cand_l="cand-l-30411"),
    r("cert-000123", N[0], "loc-000501", L_RIV1, "accepting_new_patients", "KEEP", "Y", "", "VALID", DO, "DATE", "2026-09-08", "2026-09-08", "VERY HIGH", cand_p="cand-p-88120", cand_l="cand-l-30411"),
    r("cert-000123", N[0], "loc-000502", L_RIV2, "practice_address", "REMOVE", "4500 Lakeview Blvd||Dublin|OH|43017", "", "INVALID", DO, "DATE", "2026-09-09", "2026-09-09", "VERY HIGH", effective_from="2026-08-31", cand_p="cand-p-88120", cand_l="cand-l-30412"),
    r("cert-000123", N[0], "", None, "specialty", "KEEP", "Cardiology", "", "VALID", HSV, "URL", "https://sunrisemed.example/providers/jane-rivera", "2026-09-05", "HIGH", cand_p="cand-p-88120"),
    r("cert-000123", N[0], "", None, "telehealth_available", "KEEP", "Y", "", "VALID", HSV, "URL", "https://sunrisemed.example/providers/jane-rivera", "2026-09-05", "HIGH", cand_p="cand-p-88120"),
    r("cert-000123", N[0], "loc-000501", L_RIV1, "website", "KEEP", "https://sunrisemed.example", "", "VALID", HSV, "URL", "https://sunrisemed.example", "2026-09-05", "HIGH", cand_p="cand-p-88120", cand_l="cand-l-30411"),
    # Okafor                                                                                                0008–0012
    r("cert-000287", N[1], "", None, "provider_name", "UPDATE", "Dr.|Chidi|A|Okafer|DO", "Dr.|Chidi|A|Okafor|DO", "INVALID", HSV, "URL", "https://riversidefp.example/team/chidi-okafor", "2026-09-04", "HIGH"),
    r("cert-000287", N[1], "", None, "group_affiliation", "REMOVE", "Riverside Family Practice;Ohio Primary Care Network", "Ohio Primary Care Network", "INVALID", DO, "DATE", "2026-09-10", "2026-09-10", "HIGH"),
    r("cert-000287", N[1], "loc-000503", L_OKA, "website", "KEEP", "https://riversidefp.example", "", "VALID", HSV, "URL", "https://riversidefp.example", "2026-09-04", "HIGH"),
    r("cert-000287", N[1], "loc-000503", L_OKA, "ada_accommodations", "KEEP", "Wheelchair accessible;Accessible restroom", "", "VALID", DO, "DATE", "2026-09-10", "2026-09-10", "VERY HIGH"),
    r("cert-000287", N[1], "", None, "practitioner_status", "KEEP", "ACTIVE", "", "VALID", "active_provider", "NONE", "", "2026-09-04", "HIGH"),
    # Patel                                                                                                 0013–0017
    r("cert-000341", N[2], "loc-000504", L_PAT, "location_phone", "UPDATE", "614-555-0300", "614-555-0311", "INVALID", "deactivated", "DATE", "2026-09-07", "2026-09-07", "HIGH"),
    r("cert-000341", N[2], "", None, "practitioner_phone", "KEEP", "614-555-0388", "", "UNKNOWN", "no_evidence_health_system", "NONE", "", "2026-09-07", "INCONCLUSIVE"),
    r("cert-000341", N[2], "", None, "languages", "ADD", "English;Hindi;Gujarati", "English;Hindi;Gujarati;Marathi", "INVALID", HSV, "URL", "https://buckeyeneuro.example/dr-patel", "2026-09-06", "MEDIUM"),
    r("cert-000341", N[2], "", None, "telehealth_url", "UPDATE", "https://buckeyeneuro.example/virtual", "https://buckeyeneuro.example/telehealth", "INVALID", HSV, "URL", "https://buckeyeneuro.example/telehealth", "2026-09-06", "HIGH"),
    r("cert-000341", N[2], "loc-000504", L_PAT, "ada_accommodations", "KEEP", "Wheelchair accessible", "", "VALID", HSV, "URL", "https://buckeyeneuro.example/locations/zollinger", "2026-09-06", "HIGH"),
    # Nguyen — PO box out, real location in (a move: REMOVE with our id, ADD with no id)                    0018–0021
    r("cert-000402", N[3], "loc-000505", L_NGU_OLD, "practice_address", "REMOVE", "PO Box 9120||Columbus|OH|43209", "", "INVALID", "po_box_address", "NONE", "", "2026-09-03", "VERY HIGH"),
    r("cert-000402", N[3], "", L_NGU_NEW, "practice_address", "ADD", "", "2950 E Main St|Suite 100|Bexley|OH|43209", "INVALID", DO, "DATE", "2026-09-11", "2026-09-11", "VERY HIGH"),
    r("cert-000402", N[3], "", L_NGU_NEW, "location_phone", "ADD", "", "614-555-0410", "INVALID", DO, "DATE", "2026-09-11", "2026-09-11", "VERY HIGH"),
    r("cert-000402", N[3], "", L_NGU_NEW, "accepting_new_patients", "ADD", "", "Y", "INVALID", DO, "DATE", "2026-09-11", "2026-09-11", "VERY HIGH"),
    # Schmidt — all good except a suite typo (the typo_address UPDATE case)                                 0022–0026
    r("cert-000515", N[4], "loc-000506", L_SCH, "practice_address", "UPDATE", "3535 Olentangy River Rd|Suite 210|Columbus|OH|43214", "3535 Olentangy River Rd|Suite 201|Columbus|OH|43214", "INVALID", "typo_address", "URL", "https://capcityortho.example/locations", "2026-09-09", "HIGH"),
    r("cert-000515", N[4], "loc-000506", L_SCH, "location_phone", "KEEP", "614-555-0500", "", "VALID", DO, "DATE", "2026-09-09", "2026-09-09", "VERY HIGH"),
    r("cert-000515", N[4], "", None, "specialty", "KEEP", "Orthopedic Surgery", "", "VALID", HSV, "URL", "https://capcityortho.example/surgeons/schmidt", "2026-09-02", "HIGH"),
    r("cert-000515", N[4], "loc-000506", L_SCH, "accepting_new_patients", "KEEP", "Y", "", "VALID", DO, "DATE", "2026-09-09", "2026-09-09", "VERY HIGH"),
    r("cert-000515", N[4], "", None, "practitioner_status", "KEEP", "ACTIVE", "", "VALID", "active_provider", "NONE", "", "2026-09-02", "HIGH"),
    # Alvarez                                                                                               0027–0030
    r("cert-000618", N[5], "loc-000507", L_ALV1, "accepting_new_patients", "UPDATE", "N", "Y", "INVALID", DO, "DATE", "2026-09-12", "2026-09-12", "VERY HIGH"),
    r("cert-000618", N[5], "", L_ALV2, "practice_address", "ADD", "", "1275 Olentangy River Rd|Suite 300|Columbus|OH|43212", "INVALID", HSV, "URL", "https://metrobh.example/locations/olentangy", "2026-09-12", "HIGH", effective_from="2026-06-01"),
    r("cert-000618", N[5], "", L_ALV2, "location_phone", "ADD", "", "614-555-0650", "INVALID", HSV, "URL", "https://metrobh.example/locations/olentangy", "2026-09-12", "HIGH"),
    r("cert-000618", N[5], "", None, "specialty", "UPDATE", "Psychiatry", "Child & Adolescent Psychiatry", "INVALID", "alternative_specialty", "URL", "https://metrobh.example/providers/sofia-alvarez", "2026-09-12", "MEDIUM"),
    # Bennett — deceased: practitioner_status REMOVE, then the location falls away                          0031–0032
    r("cert-000733", N[6], "", None, "practitioner_status", "REMOVE", "ACTIVE", "INACTIVE", "INVALID", "deceased", "URL", "https://obituaries.example/harold-bennett", "2026-09-10", "VERY HIGH", effective_from="2026-07-14", cand_p="cand-p-88127"),
    r("cert-000733", N[6], "loc-000508", L_BEN, "practice_address", "REMOVE", "55 N State St|Suite 2|Westerville|OH|43081", "", "INVALID", DO, "DATE", "2026-09-10", "2026-09-10", "VERY HIGH", effective_from="2026-07-14", cand_p="cand-p-88127"),
]


def write(path: pathlib.Path, cols, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        w.writerows(rows)
    print(f"wrote {path.relative_to(HERE)} ({len(rows)} rows, {len(cols)} cols)")


def write_manifest(data_path: pathlib.Path):
    """Manifest-last completeness marker (proposal §2.4 option 3): sha256 over the exact bytes, rowCount excl. header."""
    data = data_path.read_bytes()
    row_count = data.count(b"\n") - 1
    manifest = {
        "manifestVersion": "certify-manifest-v1",
        "dataFile": data_path.name,
        "schemaVersion": RV,
        "tenantId": T,
        "exportBatchRef": B,
        "candorBatchId": CB,
        "rowCount": row_count,
        "sha256": hashlib.sha256(data).hexdigest(),
        "producedAt": "2026-09-15T09:30:00Z",
        "contact": "ops@candorhealth.example",
    }
    out = data_path.with_name(data_path.name + ".manifest.json")
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(HERE)} (rowCount={row_count})")


def validate():
    assert all(len(x_) == len(EXPORT_COLS) for x_ in EXPORT_ROWS)
    assert all(len(r_) == len(REC_COLS) for r_ in REC_ROWS)
    ec = {c: i for i, c in enumerate(EXPORT_COLS)}
    rc = {c: i for i, c in enumerate(REC_COLS)}
    pairs = {(row[ec["certify_practitioner_id"]], row[ec["npi"]]) for row in EXPORT_ROWS}
    locs = {(row[ec["certify_practitioner_id"]], row[ec["certify_location_id"]]) for row in EXPORT_ROWS}
    loc_addr = {row[ec["certify_location_id"]]: (row[ec["address_line1"]], row[ec["city"]], row[ec["state"]], row[ec["zip"]]) for row in EXPORT_ROWS}
    ids = [r_[rc["finding_id"]] for r_ in REC_ROWS]
    assert len(ids) == len(set(ids)), "finding_id must be unique"
    for r_ in REC_ROWS:
        # U2: (certify_practitioner_id, npi) must be a pair we exported
        assert (r_[rc["certify_practitioner_id"]], r_[rc["npi"]]) in pairs, r_
        lid = r_[rc["certify_location_id"]]
        if lid:
            # U3: location id belongs to that practitioner and address legs echo ours verbatim
            assert (r_[rc["certify_practitioner_id"]], lid) in locs, r_
            assert (r_[rc["address_line1"]], r_[rc["city"]], r_[rc["state"]], r_[rc["zip"]]) == loc_addr[lid], r_
        else:
            # discovered location or practitioner-level row: never carries a location we sent
            pass
        if r_[rc["recommendation"]] == "KEEP":
            assert r_[rc["recommended_value"]] == ""
        elif r_[rc["recommendation"]] in ("UPDATE", "ADD"):
            assert r_[rc["recommended_value"]] != ""
        # REMOVE: recommended_value = what remains (list minus entry) or empty for single-valued attributes
        if r_[rc["recommendation"]] == "ADD" and r_[rc["attribute"]] == "practice_address":
            assert lid == "" and r_[rc["current_value_seen"]] == ""
        assert r_[rc["evidence_type"]] in ("URL", "DATE", "NONE")
        assert (r_[rc["evidence"]] == "") == (r_[rc["evidence_type"]] == "NONE")


def main():
    validate()
    write(HERE / "from/org-xyz/org-xyz_org-xyz-candor-2026-09-001_20260901.csv", EXPORT_COLS, EXPORT_ROWS)
    rec_path = HERE / "to/org-xyz/org-xyz_org-xyz-candor-2026-09-001_CB-2026-09-001_20260915.csv"
    write(rec_path, REC_COLS, REC_ROWS)
    write_manifest(rec_path)


if __name__ == "__main__":
    main()
