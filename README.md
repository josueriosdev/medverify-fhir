# MedVerify-FHIR

MedVerify-FHIR is a Rust library for checking Medicare coding edits, Washington WISeR policy, and Da Vinci Prior Authorization Support (PAS) transactions.

- **NCCI** evaluates procedure-to-procedure edits and medically unlikely edits using immutable indexes. The included dataset contains 120 synthetic procedures.
- **Washington WISeR** checks prior authorization and pre-payment review routing, epidural injection and cervical fusion criteria, tracking numbers, and provider exemptions. The bundled [Washington policy](policy/wa_wiser.toml) uses synthetic procedure mappings. For actual codes, load a locally authorized table with `wiser::Policy::from_toml`.
- **Da Vinci PAS 2.2.1** checks R4 JSON request and response Bundles, references, line decisions, and subscription notifications. It evaluates item-level CMS-0057-F timing and the dated [reviewer rules](policy/state_hitl.toml) for Arizona, California, Maryland, Texas, and Washington.

## Quickstart

With a current stable Rust toolchain and a native linker, run these commands from the repository root.

```sh
cargo run --locked -p medverify-core --bin medverify-index -- synthetic --data data --out target/synthetic
cargo test --locked -p medverify-core
```

Open the generated `ncci_ptp.bin` and `ncci_mue.bin` with `MmapPtpIndex::open` and `MmapMueIndex::open`. Pass `claim::ClaimLine` records to `ncci::evaluate` and `mue::evaluate`. Include lines from separate claims when they share a beneficiary, billing provider, service date, and setting.

## ICD-10-CM

With Python 3.11 or later and uv installed as a Python module, download and verify the CDC's FY2027 ICD-10-CM code list.

```sh
python -m uv run --locked medverify-ingest icd10 --release FY2027 --out .cache/data/icd10
```

## License

[Apache-2.0](LICENSE). See [LICENSING.md](LICENSING.md) for data and third-party rights.
