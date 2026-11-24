"""Check that scripts/reset_sandbox.sh removes planted mutations and restores the seed.

Each cycle plants four changes: a new Patient, an edited Patient, a deleted Observation and a
PAS Subscription held in payer memory. The check confirms the changes are visible, runs the
reset, then requires the seeded fingerprint, the expected patient count, and that the planted
Patient and Subscription are gone. The record of every cycle is written only after all pass.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import time

from sandbox_common import (
    RESET_TIMINGS,
    ROOT,
    SANDBOX,
    STATE_DIR,
    JSONObject,
    SandboxError,
    bash_command,
    compose_env,
    expect,
    hapi_base,
    http,
    pas_control,
    read_manifest,
    server_fingerprint,
    verify_sandbox_target,
    wait_until_ready,
)

HAPI_BASE = hapi_base()
PAS_CONTROL = pas_control()
PAS_FHIR = f"{PAS_CONTROL}/v221/fhir"
PLANT_SYSTEM = "urn:medverify:sandbox:reset-check"
SUBSCRIPTION_FIXTURE = SANDBOX / "vendor/davinci-pas-server/testdata/v221/subscription.json"


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value} is not a positive count")
    return number


def first_id(resource_type: str) -> str:
    status, bundle = http("GET", f"{HAPI_BASE}/{resource_type}?_count=1&_sort=_id&_elements=id")
    entries = expect(status, bundle, (200,), f"first {resource_type}").get("entry") or []
    if not entries:
        raise SandboxError(f"the seeded server holds no {resource_type}")
    identity: str = entries[0]["resource"]["id"]
    return identity


def delete_unreferenced(resource_type: str, candidates: int = 50) -> str:
    """Delete the first resource, in _id order, that nothing else references.

    HAPI refuses with 409 to delete a resource another one still points at, such as an
    Observation listed in DiagnosticReport.result; a refused delete changes nothing.
    """
    status, bundle = http("GET", f"{HAPI_BASE}/{resource_type}?_count={candidates}&_sort=_id&_elements=id")
    for entry in expect(status, bundle, (200,), f"list {resource_type}").get("entry") or []:
        identity: str = entry["resource"]["id"]
        status, outcome = http("DELETE", f"{HAPI_BASE}/{resource_type}/{identity}")
        if status in (200, 204):
            return identity
        if status != 409:
            raise SandboxError(f"delete {resource_type}/{identity} returned HTTP {status}: {str(outcome)[:300]}")
    raise SandboxError(f"none of the first {candidates} {resource_type} resources can be deleted")


def patient_count() -> int:
    status, bundle = http("GET", f"{HAPI_BASE}/Patient?_summary=count")
    total = expect(status, bundle, (200,), "Patient count").get("total")
    if not isinstance(total, int):
        raise SandboxError("Patient count response carries no total")
    return total


def created_id(status: int, payload: object, what: str) -> str:
    body = expect(status, payload, (201,), what)
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise SandboxError(f"{what} returned no resource id")
    identity: str = body["id"]
    return identity


def plant(cycle: int) -> dict[str, str]:
    new_patient = {
        "resourceType": "Patient",
        "identifier": [{"system": PLANT_SYSTEM, "value": f"cycle-{cycle}"}],
        "name": [{"family": "Planted", "given": [f"Cycle{cycle}"]}],
        "address": [{"state": "WA"}],
    }
    created = http("POST", f"{HAPI_BASE}/Patient", new_patient, headers={"Prefer": "return=representation"})
    planted = {"patient": created_id(*created, "create Patient")}

    edited = first_id("Patient")
    status, patient = http("GET", f"{HAPI_BASE}/Patient/{edited}")
    patient = copy.deepcopy(expect(status, patient, (200,), f"read Patient/{edited}"))
    patient.setdefault("name", []).append({"family": "Mutated", "given": [f"Cycle{cycle}"]})
    status, outcome = http("PUT", f"{HAPI_BASE}/Patient/{edited}", patient)
    expect(status, outcome, (200,), f"update Patient/{edited}")
    planted["edited_patient"] = edited

    planted["deleted_observation"] = delete_unreferenced("Observation")

    subscription: JSONObject = json.loads(SUBSCRIPTION_FIXTURE.read_text(encoding="utf-8"))
    # Port 9 (discard) refuses connections, so the server's handshake retries stay local.
    subscription["channel"]["endpoint"] = "http://127.0.0.1:9/medverify-reset-check"
    planted["subscription"] = created_id(*http("POST", f"{PAS_FHIR}/Subscription", subscription), "create Subscription")
    status, _ = http("GET", f"{PAS_FHIR}/Subscription/{planted['subscription']}")
    if status != 200:
        raise SandboxError(f"planted Subscription is not readable (HTTP {status})")
    return planted


def run_reset() -> float:
    started = time.monotonic()
    completed = subprocess.run(
        bash_command(ROOT / "scripts" / "reset_sandbox.sh"), cwd=ROOT, env=compose_env(), check=False
    )
    if completed.returncode != 0:
        raise SandboxError(f"reset_sandbox.sh exited with {completed.returncode}")
    return time.monotonic() - started


def verify_clean(planted: dict[str, str], baseline: str, types: list[str], expected_patients: int) -> JSONObject:
    wait_until_ready(f"{HAPI_BASE}/metadata", timeout=600)
    fingerprint, _ = server_fingerprint(HAPI_BASE, types)
    patient_status, _ = http("GET", f"{HAPI_BASE}/Patient/{planted['patient']}")
    subscription_status, _ = http("GET", f"{PAS_FHIR}/Subscription/{planted['subscription']}")
    count = patient_count()
    result = {
        "fingerprint": fingerprint,
        "patients": count,
        "planted_patient_http": patient_status,
        "planted_subscription_http": subscription_status,
    }
    if fingerprint != baseline:
        raise SandboxError(f"fingerprint after reset is {fingerprint}, seed recorded {baseline}")
    if count != expected_patients:
        raise SandboxError(f"{count} patients after reset, expected {expected_patients}")
    if patient_status not in (404, 410):
        raise SandboxError(f"planted Patient/{planted['patient']} survived the reset (HTTP {patient_status})")
    if subscription_status != 404:
        raise SandboxError(f"planted Subscription survived the reset (HTTP {subscription_status})")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=positive_int, required=True)
    parser.add_argument("--expected-patients", type=positive_int, required=True)
    args = parser.parse_args(argv)

    # A record from an earlier run must never stand in for this one if it fails.
    RESET_TIMINGS.unlink(missing_ok=True)
    cycles: list[JSONObject] = []
    try:
        verify_sandbox_target({"hapi": HAPI_BASE, "pas": PAS_CONTROL})
        manifest = read_manifest()
        baseline: str = manifest["fingerprint"]
        types: list[str] = manifest["resource_types"]
        wait_until_ready(f"{HAPI_BASE}/metadata", timeout=600)
        wait_until_ready(f"{PAS_CONTROL}/health", timeout=120)
        current, _ = server_fingerprint(HAPI_BASE, types)
        if current != baseline:
            raise SandboxError("the server does not match the seeded fingerprint; reset it before checking")
        if patient_count() != args.expected_patients:
            raise SandboxError(f"the seeded server does not hold {args.expected_patients} patients")
        for cycle in range(1, args.cycles + 1):
            planted = plant(cycle)
            mutated, _ = server_fingerprint(HAPI_BASE, types)
            if mutated == baseline:
                raise SandboxError(f"cycle {cycle}: planted mutations did not change the fingerprint")
            seconds = run_reset()
            after = verify_clean(planted, baseline, types, args.expected_patients)
            cycles.append({
                "cycle": cycle,
                "planted": planted,
                "mutated_fingerprint": mutated,
                "reset_seconds": round(seconds, 1),
                "after_reset": after,
            })
            print(f"cycle {cycle}: planted {json.dumps(planted, sort_keys=True)}; mutated fingerprint {mutated[:16]}; "
                  f"reset in {seconds:.1f} s; after reset {json.dumps(after, sort_keys=True)}", flush=True)
    except SandboxError as error:
        print(f"check_reset: {error}", file=sys.stderr)
        return 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"fingerprint": baseline, "expected_patients": args.expected_patients, "cycles": cycles}
    RESET_TIMINGS.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"check_reset: {args.cycles} cycles restored the seeded fingerprint {baseline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
