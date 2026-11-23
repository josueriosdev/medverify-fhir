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

## ICD-10-CM

Download and verify the CDC's FY2027 ICD-10-CM code list.

```sh
python -m uv run --locked medverify-ingest icd10 --release FY2027 --out .cache/data/icd10
```

## License

[Apache-2.0](LICENSE). See [LICENSING.md](LICENSING.md) for data and third-party rights.
