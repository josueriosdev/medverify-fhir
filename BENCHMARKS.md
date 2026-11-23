# Rust evaluator benchmarks

On November 23, 2026, the Rust core evaluated 607,082.52 complete ten-line synthetic claims per second on one logical CPU. This measures repeated evaluation with the indexes and inputs already in memory. Python conversion, file I/O and index construction are outside the timed operations.

| Operation | Median time | Individual p99 |
| --- | ---: | ---: |
| One active PTP pair lookup | 11.83 ns | 100 ns |
| Ten-line PTP and MUE evaluation | 1,647.22 ns | 2,500 ns |
| Washington WISeR routing and PAS request/response validation | 21,365.59 ns | 27,800 ns |

## Workload

The benchmark compiles the repository's synthetic CSVs into fresh indexes before timing. The PTP index has seven records in 176 bytes and the MUE index has ten records in 224 bytes, including their headers. These results describe the small, cache-resident synthetic catalog.

- **Pair lookup.** Look up the active practitioner edit for Y9001 and Y9002 on October 1, 2026.
- **Ten-line claim.** Evaluate ten lines for one beneficiary, provider, date and setting. PTP checks 45 line pairs in both orientations and returns one finding with an unsupported modifier. MUE runs on the same ten lines and returns one line limit, one absolute service-day limit and one service-day clinical review finding.
- **WISeR and PAS.** Route one synthetic Washington office service through WISeR and validate both Bundles in the two-line PAS fixture, including response correlation. Policy loading and JSON parsing precede timing.

## Measurement

AMD Ryzen 9 7900X (12 cores, 24 logical CPUs), logical CPU 0, Windows 11 Pro build 26300. Cargo used its optimized bench profile with rustc 1.99.1 (58cf8f9c3), target x86_64-pc-windows-msvc and LLVM 23.1.1.

Criterion 0.8.2 warms each operation for three seconds and collects 100 samples over approximately five seconds. The gate divides one billion by Criterion's median nanoseconds per complete ten-line claim and requires a rate strictly above 100,000 claims per second. Each p99 is the nearest-rank percentile of 10,000 individually timed operations after its Criterion run. Individual timings include clock overhead, which matters most for the pair lookup.

Run from the repository root with Git Bash on Windows or Bash on Linux.

```sh
python -m uv run --locked python scripts/check_benchmark.py --bench throughput --min-claims-per-second 100000
```
