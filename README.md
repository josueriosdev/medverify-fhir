# MedVerify-FHIR

MedVerify-FHIR checks Medicare coding edits, Washington WISeR policy, and Da Vinci Prior Authorization Support (PAS) transactions. The Rust core provides all three checks. The typed Python API exposes the Medicare coding edit evaluators on standard and free-threaded CPython.

- **NCCI** evaluates procedure-to-procedure (PTP) and medically unlikely edits (MUE) against immutable indexes. The included dataset has 120 synthetic procedures.
- **Washington WISeR** checks prior authorization and pre-payment review routing, epidural injection and cervical fusion criteria, tracking numbers, and provider exemptions. The bundled policy uses synthetic procedure mappings. Load an authorized table for actual codes with `wiser::Policy::from_toml`. [Bundled policy](policy/wa_wiser.toml)
- **Da Vinci PAS 2.2.1** validates FHIR R4 request and response Bundles, internal references, line decisions, and subscription notifications. The Rust core also evaluates CMS-0057-F deadlines and dated reviewer requirements for Arizona, California, Maryland, Texas, and Washington. [Reviewer rules](policy/state_hitl.toml)

## Quickstart

From the repository root, use stable Rust with a native linker, CPython 3.11 or later, and uv installed as a Python module. Run these commands in Bash, or Git Bash on Windows, to build the synthetic indexes and check one line against its unit limit.

```sh
cargo run --locked -p medverify-core --bin medverify-index -- synthetic --data data --out target/synthetic
python -m uv run --locked python - <<'PY'
from medverify import ClaimLine, MedVerifyEngine

engine = MedVerifyEngine("target/synthetic")
line = ClaimLine(
    id=1, beneficiary="patient-a", billing_provider="provider-a",
    date="2026-10-01", setting="practitioner", code="Y9011", units=4,
)
finding = engine.evaluate([line])["mue"][0]
print(f"{finding['code']}: {finding['total_units']} billed units, limit {finding['max_units']}")
PY
```

Include lines from separate claims in one `evaluate` call when they share a beneficiary, billing provider, service date, and setting. Use `evaluate_batch` for independent groups. It returns results in input order, and threads can share one engine.

Rust callers can open the generated files with `MmapPtpIndex::open` and `MmapMueIndex::open` and pass `claim::ClaimLine` records to `ncci::evaluate` and `mue::evaluate`.

## Tests and benchmarks

```sh
cargo test --locked --workspace
python -m uv run --locked pytest -q
```

The Rust benchmarks measure pair lookup, ten-line claim evaluation, and WISeR routing with PAS validation on synthetic inputs. [Workloads and measured results](BENCHMARKS.md)

Open a pull request against `main` and wait for the applicable CI jobs to pass. Documentation-only PRs run repository hygiene and secret scanning. Code changes also run the Rust and Python jobs. Changes to FHIR or payer policy, sandbox files, CI, build manifests, or lockfiles also run the sandbox and Inferno suites.

Before tagging a release, use **Actions → CI → Run workflow** on `main`. Confirm that the run tested the commit you intend to tag and that all jobs passed.

## ICD-10-CM

Download and verify the CDC's FY2027 ICD-10-CM code list.

```sh
python -m uv run --locked medverify-ingest icd10 --release FY2027 --out .cache/data/icd10
```

## FHIR sandbox

A Docker Compose sandbox runs HAPI FHIR R4 8.12.0-2 on PostgreSQL 16, the Nirmitee Go payer servers for Da Vinci CRD 2.2.1, DTR 2.2.0 and PAS 2.2.1 behind a test TLS proxy, and the official Inferno server suites from the CRD 0.15.0, DTR 0.18.1 and PAS 0.16.0 test kits. Images are pinned by digest and sources by commit in [sandbox/upstreams.lock](sandbox/upstreams.lock). It needs Docker Engine with Compose and buildx, Bash 4 or later (macOS ships Bash 3.2), and network access while building and validating.

```sh
python -m uv run --locked python scripts/fetch_sandbox.py --locked
docker compose -f sandbox/docker-compose.yaml up --build -d --wait
python -m uv run --locked python scripts/seed_hapi.py --patients 50 --state Washington --seed 42
bash scripts/reset_sandbox.sh --create-template
python -m uv run --locked python scripts/check_reset.py --cycles 2 --expected-patients 50
python -m uv run --locked python scripts/run_inferno.py --crd 2.2.1 --dtr 2.2.0 --pas 2.2.1 --require-pass
```

The seed loads 50 synthetic Washington patients from Synthea with fixed seeds and a fixed simulation date, replaces NPIs with synthetic values, removes licensed code displays, and records a fingerprint of the server content. `bash scripts/reset_sandbox.sh` restores that state: it stops HAPI and the payer servers, drains database connections, recreates the HAPI database from the snapshot, and restarts the payer servers with empty memory. A refreshed snapshot replaces the old one only once it is complete, and a restore interrupted at any step can simply be run again. The seed and reset scripts refuse to write unless their target is the port this compose project publishes. [Latest Inferno results](docs/CONFORMANCE.md)

| Service | Host address | Port variable |
|---|---|---|
| HAPI FHIR R4 | `http://127.0.0.1:18080/fhir` | `MEDVERIFY_HAPI_PORT` |
| CRD, DTR and PAS over test TLS | `https://127.0.0.1:18443` with Host `crd.medverify.test`, `dtr.medverify.test` or `pas.medverify.test` | `MEDVERIFY_TLS_PORT` |
| PAS test-only control routes | `http://127.0.0.1:18085/admin/` | `MEDVERIFY_PAS_PORT` |
| Inferno CRD, DTR and PAS APIs | `http://127.0.0.1:14567`, `14568` and `14569` | `MEDVERIFY_INFERNO_CRD_PORT`, `_DTR_PORT`, `_PAS_PORT` |

Every port binds to 127.0.0.1. The PAS control routes exist only for unattended Inferno runs; the server refuses them through the proxy. To run a second, disposable stack beside an existing one, set `COMPOSE_PROJECT_NAME` and a different value for every port variable before running the commands above; seed and reset records are kept per project under `.cache/sandbox/`.

These suites exercise the sandbox payer servers. MedVerify's own PAS 2.2.1 validator is tested by `cargo test`. `bash scripts/check_sdist.sh` proves the source distribution builds and installs on its own.

## License

[Apache-2.0](LICENSE). See [LICENSING.md](LICENSING.md) for data and third-party rights.
