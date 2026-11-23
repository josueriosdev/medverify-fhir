# MedVerify-FHIR

MedVerify-FHIR builds and reads immutable indexes for National Correct Coding Initiative (NCCI) procedure-to-procedure (PTP) edits and medically unlikely edits (MUEs). The repository includes a catalog of 120 synthetic procedures and a Python command for downloading the CDC FY2027 ICD-10-CM code list.

## Quickstart

Use a stable Rust toolchain with a native linker, Python 3.11 or later, and uv installed as a Python module. Run the commands from the repository root.

Build both synthetic indexes and run the Rust tests.

```sh
cargo run --locked -p medverify-core --bin medverify-index -- synthetic --data data --out target/synthetic
cargo test --locked -p medverify-core
```

The files `target/synthetic/ncci_ptp.bin` and `target/synthetic/ncci_mue.bin` open with `MmapPtpIndex::open` and `MmapMueIndex::open` in `medverify_core::index`. Both readers validate the file before exposing its records. Use a fresh output directory when changing the input edits.

Install the CDC code list. The importer verifies the archive's SHA-256 and checks for 74,879 unique codes before publishing the file under `.cache/data/icd10/`.

```sh
python -m uv sync --locked
python -m uv run --locked medverify-ingest icd10 --release FY2027 --out .cache/data/icd10
```

## License

Project code is available under [Apache-2.0](LICENSE). See [LICENSING.md](LICENSING.md) for data and third-party rights.
