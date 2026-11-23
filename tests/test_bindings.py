import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from medverify import ClaimLine, MedVerifyEngine, ModifierEvidence


@pytest.fixture(scope="session")
def indexes(tmp_path_factory):
    destination = tmp_path_factory.mktemp("indexes") / "synthetic"
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--locked",
            "-p",
            "medverify-core",
            "--bin",
            "medverify-index",
            "--",
            "synthetic",
            "--data",
            "data",
            "--out",
            str(destination),
        ],
        cwd=root,
        check=True,
    )
    return destination


@pytest.fixture(scope="session")
def engine(indexes):
    return MedVerifyEngine(indexes)


DAY = {
    "beneficiary": "patient-a",
    "billing_provider": "provider-a",
    "date": "2026-10-01",
    "setting": "practitioner",
}


def line(id, code, units=1, **fields):
    return ClaimLine(id=id, code=code, units=units, **(DAY | fields))


def test_synthetic_ptp_target_modifier_evidence_and_liability(engine):
    one = line(10, "Y9001")
    evidence = ModifierEvidence(10, "XS", "separate_structure")
    two = line(
        3,
        "Y9002",
        modifiers=["59"],
        modifier_evidence=[evidence],
        abn_liability_shift_attempted=True,
    )
    expected = {
        "service_day": DAY,
        "column_one_line_id": 10,
        "column_two_line_id": 3,
        "outcome": "supported_modifier_required",
        "abn_cannot_shift_liability": True,
        "modifier_issues": [
            {
                "line_id": 3,
                "modifier": "59",
                "kind": "more_specific_modifier_required",
                "required_modifier": "XS",
            }
        ],
    }
    assert engine.evaluate([two, one]) == {"ptp": [expected], "mue": []}
    two = line(3, "y9002", modifiers=("xs",), modifier_evidence=[evidence])
    assert engine.evaluate([one, two])["ptp"] == [
        expected
        | {
            "outcome": "bypassed",
            "modifier_issues": [],
            "abn_cannot_shift_liability": False,
        }
    ]
    for date, outcome in [
        ("2026-09-30", "modifier_prohibited"),
        ("2026-10-01", "supported_modifier_required"),
    ]:
        pair = [line(1, "Y9001", date=date), line(2, "Y9002", date=date)]
        assert engine.evaluate(pair)["ptp"][0]["outcome"] == outcome


def test_synthetic_mue_scopes_totals_and_group_isolation(engine):
    lines = [
        line(5, "Y9031", 2, abn_liability_shift_attempted=True),
        line(4, "Y9031", 2),
        line(2, "Y9021", 1),
        line(3, "Y9021", 2),
        line(1, "Y9011", 4),
        line(1, "Y9021", 1, beneficiary="different-patient"),
    ]
    assert engine.evaluate(lines) == {
        "ptp": [],
        "mue": [
            {
                "service_day": DAY,
                "code": code,
                "scope": scope,
                "line_id": line_id,
                "max_units": limit,
                "total_units": total,
                "abn_cannot_shift_liability": abn,
            }
            for code, scope, line_id, limit, total, abn in [
                ("Y9011", "claim_line", 1, 3, 4, False),
                ("Y9021", "absolute_service_day_limit", None, 2, 3, False),
                ("Y9031", "service_day_clinical_review", None, 3, 4, True),
            ]
        ],
    }


def test_batches_preserve_order_and_do_not_combine_service_days(engine):
    a, b = line(1, "Y9021", 2), line(2, "Y9021", 2)
    empty = {"ptp": [], "mue": []}
    combined = engine.evaluate([a, b])
    assert combined["mue"][0]["total_units"] == 4
    assert engine.evaluate_batch([]) == []
    assert engine.evaluate_batch(([a], [a, b], [], [b])) == [
        empty,
        combined,
        empty,
        empty,
    ]


def test_concurrent_calls_share_the_same_engine(engine):
    barrier = Barrier(8)

    def evaluate(i):
        claim = [line(i, "Y9011", 4)]
        barrier.wait(timeout=30)
        return [
            engine.evaluate_batch([[], claim])[1]["mue"][0]["line_id"]
            for _ in range(32)
        ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(evaluate, range(8))) == [[i] * 32 for i in range(8)]


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"code": "bad"}, ValueError),
        ({"units": 0}, ValueError),
        ({"units": -1}, OverflowError),
        ({"date": "2026-02-29"}, ValueError),
        ({"date": "2169-06-06"}, ValueError),
        ({"setting": "unknown"}, ValueError),
        ({"service_kind": "unknown"}, ValueError),
        ({"beneficiary": ""}, ValueError),
        ({"billing_provider": " "}, ValueError),
        ({"modifiers": ["XYZ"]}, ValueError),
        ({"modifier_evidence": [object()]}, TypeError),
    ],
)
def test_rejects_malformed_line(fields, error):
    with pytest.raises(error):
        ClaimLine(**(DAY | {"id": 1, "code": "Y9011", "units": 1} | fields))


def test_failed_batch_releases_engine_for_reuse(engine):
    a = line(1, "Y9021", 2**64 - 1)
    with pytest.raises(OverflowError, match="exceeds u64"):
        engine.evaluate_batch([[], [a, line(2, "Y9021")]])
    with pytest.raises(ValueError, match="duplicate claim line ID"):
        engine.evaluate([a, a])
    with pytest.raises(TypeError):
        engine.evaluate_batch([[object()]])
    with pytest.raises(ValueError, match="evidence basis"):
        ModifierEvidence(1, "XS", "unknown")
    with pytest.raises(AttributeError):
        engine.index_dir = "elsewhere"
    assert engine.evaluate([]) == {"ptp": [], "mue": []}


def test_index_errors_and_immutable_snapshot(indexes, tmp_path):
    with pytest.raises(FileNotFoundError):
        MedVerifyEngine(tmp_path)
    shutil.copytree(indexes, tmp_path, dirs_exist_ok=True)
    snapshot = MedVerifyEngine(tmp_path)
    path = tmp_path / "ncci_mue.bin"
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    with pytest.raises(OSError):
        MedVerifyEngine(tmp_path)
    assert snapshot.evaluate([line(1, "Y9011", 4)])["mue"][0]["max_units"] == 3


def test_import_preserves_interpreter_gil_mode():
    # A fresh process observes the transition even if pytest already imported the module.
    # Let CPython decide whether importing an extension requires enabling the GIL.
    env = os.environ.copy()
    env.pop("PYTHON_GIL", None)
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-Werror",
            "-c",
            """
import sys
import sysconfig
if sysconfig.get_config_var("Py_GIL_DISABLED"):
    assert not sys._is_gil_enabled()
    import medverify._medverify
    assert not sys._is_gil_enabled()
else:
    import medverify._medverify
""",
        ],
        check=True,
        env=env,
    )
