# MedVerify-FHIR

MedVerify-FHIR provides Rust primitives for working with Medicare NCCI edit data. It encodes five-character procedure codes and service dates and defines 16-byte record layouts for procedure-to-procedure edits and medically unlikely edits. The Python package exposes the installed version through a PyO3 extension.

## Quickstart

From the repository root, with a stable Rust toolchain, a native linker, Python 3.11 or later, and uv installed as a Python module, run:

```sh
cargo test --locked -p medverify-core
python -m uv sync --locked
python -m uv run --locked python -c "from medverify import __version__; print(__version__)"
```

## License

Project code is available under [Apache-2.0](LICENSE). See [LICENSING.md](LICENSING.md) for data and third-party rights.
