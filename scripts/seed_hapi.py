"""Seed the sandbox HAPI server with reproducible synthetic Washington patients from Synthea.

Synthea runs offline in the compose "synthea" service with a fixed population seed, clinician
seed and simulation date. Before upload every bundle is scrubbed: NPIs become deterministic
synthetic values that cannot collide with issued numbers, displays and free text from licensed
code systems (AMA CPT, SNOMED CT, ADA CDT) are removed, and narratives and embedded clinical
notes are dropped. Entries are uploaded with PUT under their Synthea ids, so two seeds from the
same inputs leave identical server content and the same fingerprint.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import sys
import tarfile
import time
from collections import Counter
from collections.abc import Iterator
from typing import Any

from sandbox_common import (
    SEED_MANIFEST,
    STATE_DIR,
    JSONObject,
    SandboxError,
    canonical_json,
    expect,
    hapi_base,
    http,
    load_lock,
    run_compose,
    server_fingerprint,
    verify_sandbox_target,
    wait_until_ready,
)

HAPI_BASE = hapi_base()

CLINICIAN_SEED = 20260101
REFERENCE_DATE = "20261101"
YEARS_OF_HISTORY = 5

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
LICENSED_SYSTEMS = frozenset(
    {
        "http://www.ama-assn.org/go/cpt",
        "urn:oid:2.16.840.1.113883.6.12",
        "http://snomed.info/sct",
        "urn:oid:2.16.840.1.113883.6.96",
        "http://www.ada.org/cdt",
        "urn:oid:2.16.840.1.113883.6.13",
    }
)
# Clinical notes, claims and provenance carry free text or billing detail the sandbox does not need.
DROPPED_TYPES = frozenset(
    {"Claim", "ExplanationOfBenefit", "Provenance", "DocumentReference", "ImagingStudy", "Media"}
)
STATE_CODES = {"Washington": "WA"}


def luhn_check_digit(digits: str) -> str:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def synthetic_npi(original: str, taken: set[str]) -> str:
    """Map an NPI to a check-digit-valid value starting with 9, a prefix NPPES does not issue."""
    counter = 0
    while True:
        digest = hashlib.sha256(f"medverify-npi:{original}:{counter}".encode()).hexdigest()
        body = "9" + f"{int(digest, 16) % 10**8:08d}"
        candidate = body + luhn_check_digit("80840" + body)
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        counter += 1


class Scrubber:
    """Applies the scrubbing rules and counts what it changed."""

    def __init__(self) -> None:
        self.npi_map: dict[str, str] = {}
        self.taken: set[str] = set()
        self.stats: Counter[str] = Counter()

    def npi(self, value: str) -> str:
        if value not in self.npi_map:
            self.npi_map[value] = synthetic_npi(value, self.taken)
        return self.npi_map[value]

    def resource(self, resource: JSONObject) -> JSONObject:
        cleaned = copy.deepcopy(resource)
        if isinstance(cleaned.get("text"), dict):
            del cleaned["text"]
            self.stats["narratives_removed"] += 1
        if cleaned.get("resourceType") == "DiagnosticReport" and "presentedForm" in cleaned:
            del cleaned["presentedForm"]
            self.stats["presented_forms_removed"] += 1
        self._walk(cleaned)
        return cleaned

    def _walk(self, node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                self._walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("system") == NPI_SYSTEM and isinstance(node.get("value"), str):
            node["value"] = self.npi(node["value"])
            self.stats["npis_replaced"] += 1
        if node.get("system") in LICENSED_SYSTEMS and "display" in node:
            del node["display"]
            self.stats["licensed_displays_removed"] += 1
        codings = node.get("coding")
        # Synthea copies the coding display into CodeableConcept.text.
        if (
            isinstance(codings, list)
            and isinstance(node.get("text"), str)
            and any(isinstance(c, dict) and c.get("system") in LICENSED_SYSTEMS for c in codings)
        ):
            del node["text"]
            self.stats["licensed_texts_removed"] += 1
        reference = node.get("reference")
        if isinstance(reference, str) and f"identifier={NPI_SYSTEM}|" in reference:
            prefix, _, value = reference.rpartition("|")
            node["reference"] = f"{prefix}|{self.npi(value)}"
        for value in node.values():
            self._walk(value)


def bundle_kind(bundle: JSONObject) -> str:
    types = {entry["resource"]["resourceType"] for entry in bundle.get("entry", [])}
    if "Patient" in types:
        return "patient"
    if "Practitioner" in types:
        return "practitioner"
    if "Organization" in types:
        return "organization"
    raise SandboxError(f"unrecognised Synthea bundle with resource types {sorted(types)}")


def resource_identity(resource: JSONObject) -> str:
    return f"{resource['resourceType']}/{resource['id']}"


def build_reference_index(bundles: list[JSONObject]) -> dict[str, str]:
    """Map urn:uuid full URLs and identifier queries to literal Type/id references."""
    index: dict[str, str] = {}
    for bundle in bundles:
        for entry in bundle.get("entry", []):
            resource = entry["resource"]
            literal = resource_identity(resource)
            if entry.get("fullUrl"):
                index[entry["fullUrl"]] = literal
            for identifier in resource.get("identifier", []):
                if "system" in identifier and "value" in identifier:
                    query = f"{resource['resourceType']}?identifier={identifier['system']}|{identifier['value']}"
                    index[query] = literal
    return index


def rewrite_references(node: Any, index: dict[str, str], unresolved: Counter[str]) -> None:
    if isinstance(node, list):
        for item in node:
            rewrite_references(item, index, unresolved)
        return
    if not isinstance(node, dict):
        return
    reference = node.get("reference")
    if isinstance(reference, str) and (reference.startswith("urn:uuid:") or "?" in reference):
        literal = index.get(reference)
        if literal is None:
            # Targets of dropped types, such as Claim; the display, if any, stays.
            unresolved["urn:uuid" if reference.startswith("urn:uuid:") else reference.split("?", 1)[0]] += 1
            del node["reference"]
        else:
            node["reference"] = literal
    for value in node.values():
        rewrite_references(value, index, unresolved)


def to_transaction(bundle: JSONObject, index: dict[str, str], unresolved: Counter[str]) -> JSONObject:
    entries = []
    for entry in bundle.get("entry", []):
        resource = copy.deepcopy(entry["resource"])
        rewrite_references(resource, index, unresolved)
        entries.append(
            {
                "fullUrl": f"urn:uuid:{resource['id']}",
                "resource": resource,
                "request": {"method": "PUT", "url": resource_identity(resource)},
            }
        )
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def scrub_bundle(bundle: JSONObject, scrubber: Scrubber) -> JSONObject:
    kept = []
    for entry in bundle.get("entry", []):
        resource = entry["resource"]
        if resource["resourceType"] in DROPPED_TYPES:
            scrubber.stats[f"dropped_{resource['resourceType']}"] += 1
            continue
        kept.append({"fullUrl": entry.get("fullUrl"), "resource": scrubber.resource(resource)})
    return {"resourceType": "Bundle", "type": "transaction", "entry": kept}


def patient_of(bundle: JSONObject) -> JSONObject:
    patients = [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"]
    if len(patients) != 1:
        raise SandboxError(f"patient bundle holds {len(patients)} Patient resources")
    patient: JSONObject = patients[0]
    return patient


def check_population(patient_bundles: list[JSONObject], expected: int, state: str) -> None:
    patients = [patient_of(bundle) for bundle in patient_bundles]
    ids = {patient["id"] for patient in patients}
    if len(patients) != expected or len(ids) != expected:
        raise SandboxError(f"Synthea produced {len(patients)} patients ({len(ids)} distinct), expected {expected}")
    allowed = {state, STATE_CODES.get(state, state)}
    for patient in patients:
        states = {address.get("state") for address in patient.get("address", [])}
        if not states & allowed:
            raise SandboxError(f"Patient/{patient['id']} lives in {sorted(map(str, states))}, not {state}")


def read_synthea_tar(data: bytes) -> Iterator[tuple[str, JSONObject]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in sorted(tar.getmembers(), key=lambda m: m.name):
            if member.isfile() and member.name.endswith(".json"):
                handle = tar.extractfile(member)
                if handle is None:
                    raise SandboxError(f"unreadable Synthea output {member.name}")
                yield member.name, json.load(handle)


def synthea_arguments(patients: int, state: str, seed: int) -> list[str]:
    return [
        "-s", str(seed),
        "-cs", str(CLINICIAN_SEED),
        "-r", REFERENCE_DATE,
        "-e", REFERENCE_DATE,
        "-p", str(patients),
        "--generate.only_alive_patients=true",
        f"--exporter.years_of_history={YEARS_OF_HISTORY}",
        "--exporter.metadata.export=false",
        "--exporter.fhir.transaction_bundle=true",
        "--exporter.hospital.fhir.export=true",
        "--exporter.practitioner.fhir.export=true",
        state,
    ]


def prepare(raw_bundles: list[JSONObject], patients: int, state: str) -> tuple[list[JSONObject], Scrubber, Counter[str]]:
    grouped: dict[str, list[JSONObject]] = {"organization": [], "practitioner": [], "patient": []}
    for bundle in raw_bundles:
        grouped[bundle_kind(bundle)].append(bundle)
    check_population(grouped["patient"], patients, state)
    grouped["patient"].sort(key=lambda bundle: patient_of(bundle)["id"])
    scrubber = Scrubber()
    ordered = [scrub_bundle(b, scrubber) for kind in ("organization", "practitioner", "patient") for b in grouped[kind]]
    index = build_reference_index(ordered)
    unresolved: Counter[str] = Counter()
    transactions = [to_transaction(bundle, index, unresolved) for bundle in ordered]
    return transactions, scrubber, unresolved


def upload(transactions: list[JSONObject]) -> None:
    for number, transaction in enumerate(transactions, start=1):
        status, outcome = http("POST", HAPI_BASE, transaction, timeout=900)
        expect(status, outcome, (200,), f"transaction {number}/{len(transactions)}")
        if number % 10 == 0 or number == len(transactions):
            print(f"uploaded {number}/{len(transactions)} transaction bundles", flush=True)


def count_patients() -> int:
    status, bundle = http("GET", f"{HAPI_BASE}/Patient?_summary=count", timeout=120)
    total = expect(status, bundle, (200,), "Patient count").get("total")
    if not isinstance(total, int):
        raise SandboxError("Patient count response carries no total")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patients", type=int, required=True)
    parser.add_argument("--state", required=True, choices=sorted(STATE_CODES))
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(argv)

    started = time.monotonic()
    try:
        # The seed writes thousands of resources; prove the target is this project's HAPI first.
        verify_sandbox_target({"hapi": HAPI_BASE})
        lock = load_lock()
        wait_until_ready(f"{HAPI_BASE}/metadata", timeout=600)
        print(f"running Synthea {lock['artifacts']['synthea']['tag']} seed {args.seed}", flush=True)
        output = run_compose("run", "--rm", "-T", "synthea", *synthea_arguments(args.patients, args.state, args.seed),
                             capture=True, timeout=3600)
        generated = time.monotonic()
        raw = [bundle for _, bundle in read_synthea_tar(output)]
        transactions, scrubber, unresolved = prepare(raw, args.patients, args.state)
        content_digest = hashlib.sha256(b"".join(canonical_json(t) for t in transactions)).hexdigest()
        upload(transactions)
        uploaded = time.monotonic()
        patients = count_patients()
        if patients != args.patients:
            raise SandboxError(f"HAPI holds {patients} patients after seeding, expected {args.patients}; "
                               "remove the sandbox volumes and seed an empty server")
        types = sorted({entry["resource"]["resourceType"] for t in transactions for entry in t["entry"]})
        fingerprint, counts = server_fingerprint(HAPI_BASE, types)
        finished = time.monotonic()
    except SandboxError as error:
        print(f"seed_hapi: {error}", file=sys.stderr)
        return 1

    manifest = {
        "patients": args.patients,
        "state": args.state,
        "seed": args.seed,
        "clinician_seed": CLINICIAN_SEED,
        "reference_date": REFERENCE_DATE,
        "years_of_history": YEARS_OF_HISTORY,
        "synthea": lock["artifacts"]["synthea"]["tag"],
        "resource_types": types,
        "counts": counts,
        "content_digest": content_digest,
        "fingerprint": fingerprint,
        "scrubbing": dict(sorted(scrubber.stats.items())),
        "unresolved_references": dict(sorted(unresolved.items())),
        "seconds": {
            "synthea": round(generated - started, 1),
            "upload": round(uploaded - generated, 1),
            "fingerprint": round(finished - uploaded, 1),
        },
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEED_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("patients", "counts", "scrubbing", "seconds")}, indent=2))
    print(f"fingerprint {fingerprint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
