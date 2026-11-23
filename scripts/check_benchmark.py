import argparse
import json
import math
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", choices=["throughput"], required=True)
    parser.add_argument("--min-claims-per-second", type=int, required=True)
    args = parser.parse_args()
    if args.min_claims_per_second <= 0:
        parser.error("minimum throughput must be positive")

    process = psutil.Process()
    cpu = min(process.cpu_affinity())
    process.cpu_affinity([cpu])
    if process.cpu_affinity() != [cpu]:
        raise SystemExit("failed to pin the benchmark process")
    print(f"Pinned to logical CPU {cpu}", flush=True)

    # A fresh Criterion directory prevents a stale estimate from satisfying the gate.
    with TemporaryDirectory(prefix="medverify-criterion-") as directory:
        subprocess.run(
            [
                "cargo",
                "bench",
                "--locked",
                "-p",
                "medverify-core",
                "--bench",
                args.bench,
                "--",
                "--noplot",
            ],
            check=True,
            env={**os.environ, "CRITERION_HOME": directory},
        )
        for name in ("pair_lookup", "ten_line_claim", "wiser_pas"):
            path = Path(directory, name, "new", "estimates.json")
            median_ns = json.loads(path.read_text(encoding="utf-8"))["median"][
                "point_estimate"
            ]
            if not math.isfinite(median_ns) or median_ns <= 0:
                raise SystemExit(f"invalid median for {name}: {median_ns}")
            print(f"{name} median: {median_ns:.2f} ns")
            if name == "ten_line_claim":
                rate = 1e9 / median_ns
                print(f"Complete ten-line claims per second: {rate:,.2f}")
                if rate <= args.min_claims_per_second:
                    raise SystemExit(
                        f"median throughput must exceed {args.min_claims_per_second}"
                    )


if __name__ == "__main__":
    main()
