# MedVerify-FHIR

MedVerify-FHIR is a Rust library for checking claims against National Correct Coding Initiative (NCCI) procedure-to-procedure (PTP) edits and medically unlikely edits (MUEs). It builds and reads immutable edit indexes. The public dataset contains 120 synthetic procedures.

## Quickstart

From the repository root, use a current stable Rust toolchain and a native linker to build the synthetic indexes and run the core tests.

```sh
cargo run --locked -p medverify-core --bin medverify-index -- synthetic --data data --out target/synthetic
cargo test --locked -p medverify-core
```

Open `target/synthetic/ncci_ptp.bin` with `MmapPtpIndex::open` and `target/synthetic/ncci_mue.bin` with `MmapMueIndex::open`. Use a fresh output directory if you change the input data.

Use `ncci::evaluate` to check procedure pairs and audit documented modifier evidence. Use `mue::evaluate` to check units per line or across a service day. Both functions take `claim::ClaimLine` values. Include lines from separate claims when they share a beneficiary, billing provider, service date, and setting. Evaluate coverage and payment separately.

## CDC ICD-10-CM

With Python 3.11 or later and uv installed as a Python module, download the FY2027 code list from the CDC. The importer verifies the archive's SHA-256 and checks for 74,879 unique codes before writing the list locally.

```sh
python -m uv sync --locked
python -m uv run --locked medverify-ingest icd10 --release FY2027 --out .cache/data/icd10
```

## License

Project code is available under [Apache-2.0](LICENSE). See [LICENSING.md](LICENSING.md) for data and third-party rights.
