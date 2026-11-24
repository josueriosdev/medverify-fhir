import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import seed_hapi
from sandbox_common import SandboxError

CPT = "http://www.ama-assn.org/go/cpt"
SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
NPI = seed_hapi.NPI_SYSTEM


def luhn_valid(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def test_luhn_check_digit_matches_the_published_npi_example():
    # CMS's worked example: 123456789 with the 80840 prefix has check digit 3.
    assert seed_hapi.luhn_check_digit("80840123456789") == "3"


def test_synthetic_npis_are_valid_deterministic_and_never_issued():
    taken = set()
    first = seed_hapi.synthetic_npi("1234567893", taken)
    again = seed_hapi.synthetic_npi("1234567893", set())
    other = seed_hapi.synthetic_npi("1234567893", taken)
    assert first == again
    assert other != first
    for value in (first, other):
        assert len(value) == 10 and value.startswith("9")
        assert luhn_valid("80840" + value)


def coding(system, code, display):
    return {"system": system, "code": code, "display": display}


def test_scrubber_strips_licensed_text_and_keeps_open_displays():
    resource = {
        "resourceType": "Procedure",
        "id": "p1",
        "text": {"status": "generated", "div": "<div>narrative</div>"},
        "code": {"coding": [coding(CPT, "Y9001", "Licensed descriptor")], "text": "Licensed descriptor"},
        "reasonCode": [{"coding": [coding(SNOMED, "1", "Licensed term")], "text": "Licensed term"}],
        "note": [{"text": "kept"}],
        "category": {"coding": [coding(LOINC, "2", "Open display")], "text": "Open display"},
    }
    scrubber = seed_hapi.Scrubber()
    cleaned = scrubber.resource(resource)
    assert "text" not in cleaned
    assert cleaned["code"] == {"coding": [{"system": CPT, "code": "Y9001"}]}
    assert cleaned["reasonCode"] == [{"coding": [{"system": SNOMED, "code": "1"}]}]
    assert cleaned["category"]["coding"][0]["display"] == "Open display"
    assert cleaned["category"]["text"] == "Open display"
    assert cleaned["note"] == [{"text": "kept"}]
    assert resource["code"]["coding"][0]["display"] == "Licensed descriptor"
    assert scrubber.stats["licensed_displays_removed"] == 2


def test_scrubber_replaces_npis_consistently_in_identifiers_and_references():
    scrubber = seed_hapi.Scrubber()
    practitioner = scrubber.resource(
        {"resourceType": "Practitioner", "id": "d1", "identifier": [{"system": NPI, "value": "9999999999"}]}
    )
    encounter = scrubber.resource(
        {
            "resourceType": "Encounter",
            "id": "e1",
            "participant": [{"individual": {"reference": f"Practitioner?identifier={NPI}|9999999999"}}],
        }
    )
    replaced = practitioner["identifier"][0]["value"]
    assert replaced != "9999999999"
    assert encounter["participant"][0]["individual"]["reference"] == f"Practitioner?identifier={NPI}|{replaced}"


def synthea_output(state="Washington", patients=2):
    organization = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "fullUrl": "urn:uuid:org-1",
                "resource": {
                    "resourceType": "Organization",
                    "id": "org-1",
                    "identifier": [{"system": "https://github.com/synthetichealth/synthea", "value": "org-1"}],
                },
            }
        ],
    }
    practitioner = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "fullUrl": "urn:uuid:doc-1",
                "resource": {
                    "resourceType": "Practitioner",
                    "id": "doc-1",
                    "identifier": [{"system": NPI, "value": "9999999991"}],
                },
            }
        ],
    }
    bundles = [practitioner, organization]
    for number in range(patients, 0, -1):
        patient_id = f"pat-{number}"
        bundles.append(
            {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {
                        "fullUrl": f"urn:uuid:{patient_id}",
                        "resource": {"resourceType": "Patient", "id": patient_id, "address": [{"state": state}]},
                    },
                    {
                        "fullUrl": f"urn:uuid:enc-{number}",
                        "resource": {
                            "resourceType": "Encounter",
                            "id": f"enc-{number}",
                            "subject": {"reference": f"urn:uuid:{patient_id}"},
                            "serviceProvider": {
                                "reference": "Organization?identifier=https://github.com/synthetichealth/synthea|org-1"
                            },
                            "participant": [
                                {"individual": {"reference": f"Practitioner?identifier={NPI}|9999999991"}}
                            ],
                        },
                    },
                    {
                        "fullUrl": f"urn:uuid:claim-{number}",
                        "resource": {"resourceType": "Claim", "id": f"claim-{number}"},
                    },
                    {
                        "fullUrl": f"urn:uuid:eob-{number}",
                        "resource": {
                            "resourceType": "ExplanationOfBenefit",
                            "id": f"eob-{number}",
                            "claim": {"reference": f"urn:uuid:claim-{number}"},
                        },
                    },
                    {
                        "fullUrl": f"urn:uuid:obs-{number}",
                        "resource": {
                            "resourceType": "Observation",
                            "id": f"obs-{number}",
                            "subject": {"reference": f"urn:uuid:{patient_id}"},
                            "derivedFrom": [{"reference": f"urn:uuid:claim-{number}", "display": "kept"}],
                        },
                    },
                ],
            }
        )
    return bundles


def test_prepare_orders_bundles_and_uses_literal_put_references():
    transactions, scrubber, unresolved = seed_hapi.prepare(synthea_output(), patients=2, state="Washington")
    first_urls = [t["entry"][0]["request"]["url"] for t in transactions]
    assert first_urls == ["Organization/org-1", "Practitioner/doc-1", "Patient/pat-1", "Patient/pat-2"]
    encounter = transactions[2]["entry"][1]
    assert encounter["request"] == {"method": "PUT", "url": "Encounter/enc-1"}
    resource = encounter["resource"]
    assert resource["subject"] == {"reference": "Patient/pat-1"}
    assert resource["serviceProvider"] == {"reference": "Organization/org-1"}
    assert resource["participant"][0]["individual"] == {"reference": "Practitioner/doc-1"}
    types = {entry["resource"]["resourceType"] for t in transactions for entry in t["entry"]}
    assert not types & seed_hapi.DROPPED_TYPES
    assert transactions[2]["entry"][2]["resource"]["derivedFrom"] == [{"display": "kept"}]
    assert unresolved == {"urn:uuid": 2}
    assert scrubber.stats["dropped_Claim"] == 2


@pytest.mark.parametrize(
    ("state", "patients", "message"),
    [("Oregon", 2, "not Washington"), ("Washington", 3, "expected 2")],
)
def test_prepare_rejects_the_wrong_population(state, patients, message):
    with pytest.raises(SandboxError, match=message):
        seed_hapi.prepare(synthea_output(state=state, patients=patients), patients=2, state="Washington")


def test_prepare_is_reproducible():
    first = seed_hapi.prepare(synthea_output(), patients=2, state="Washington")[0]
    second = seed_hapi.prepare(list(reversed(synthea_output())), patients=2, state="Washington")[0]
    assert first == second
