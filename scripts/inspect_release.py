"""Inspect release material for content that must not be published.

Accepts directories, single files, source distributions (.tar.gz) and wheels (.whl). Every member
path is checked against the repository's publication rules, and every text member is scanned
for internal material, licensed CPT descriptors, real-looking NPIs and SSNs, and private keys.
Findings are reported by location and rule only; matched values are never printed.

    python scripts/inspect_release.py <path> [<path> ...]

Exit status is 0 when nothing is found and 1 otherwise. Secrets are covered separately by
Gitleaks; this inspection adds the project-specific rules Gitleaks does not know.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

# Mirrors check_path in scripts/check_repo_hygiene.sh.
FORBIDDEN_PATHS = (
    "agents.md", "*/agents.md", "claude.md", "*/claude.md", "gemini.md", "*/gemini.md",
    "docs/internal/*", "*/docs/internal/*", "scripts/ingest_cms.py", "*/scripts/ingest_cms.py",
    "*.zip", "*.bin", ".cache/*", "*/.cache/*", "*.local.md", ".claude/*", "*/.claude/*",
    ".codex/*", "*/.codex/*", "memory-bank/*", "*/memory-bank/*", "sandbox/vendor/*", "*/sandbox/vendor/*",
    "artifacts/*", "*/artifacts/*", "*.tar", "*.tar.gz", "*.tgz", "*.jar", "*.whl", "*.db", "*.sqlite",
)
# 1234567893 is the worked example in the CMS NPI check-digit standard, used by the synthetic
# test fixtures; it is not assigned to a provider.
DOCUMENTED_EXAMPLE_NPIS = frozenset({"1234567893"})
# Lockfiles hold sizes and hashes, not people.
NO_IDENTIFIER_SCAN = frozenset({"uv.lock", "Cargo.lock"})
INTERNAL_MARKERS = re.compile(r"docs/internal|memory-bank|AGENTS\.md|AgentHub", re.IGNORECASE)
NPI_SHAPE = re.compile(r"(?<![\d.])[12]\d{9}(?![\d.])")
SSN_SHAPE = re.compile(r"(?<!\d)(?!9\d\d|000|666)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)")
CPT_DISPLAY = re.compile(
    r'"system"\s*:\s*"(?:http://www\.ama-assn\.org/go/cpt|urn:oid:2\.16\.840\.1\.113883\.6\.12)"[^{}]*"display"'
    r'|"display"[^{}]*"system"\s*:\s*"(?:http://www\.ama-assn\.org/go/cpt|urn:oid:2\.16\.840\.1\.113883\.6\.12)"'
)
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# These files define or exclude the very paths and markers being looked for.
RULE_SOURCES = frozenset({"scripts/inspect_release.py", "scripts/check_repo_hygiene.sh", ".gitignore"})


def luhn_npi(value: str) -> bool:
    total = 0
    for index, char in enumerate(reversed("80840" + value)):
        digit = int(char)
        if index % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def members(path: Path) -> Iterator[tuple[str, bytes | None]]:
    """Yield (relative member path, content or None for directories) for any supported input."""
    if path.is_dir():
        for item in sorted(path.rglob("*")):
            # A scratch repository's own metadata is not candidate content.
            if item.is_file() and item.relative_to(path).parts[0] != ".git":
                yield item.relative_to(path).as_posix(), item.read_bytes()
    elif path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                yield name, None if name.endswith("/") else archive.read(name)
    elif path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                handle = archive.extractfile(member) if member.isfile() else None
                parts = PurePosixPath(member.name).parts
                # Drop the sdist's single top-level directory so rules see project paths.
                name = PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else member.name
                yield name, handle.read() if handle else None
    else:
        yield path.name, path.read_bytes()


def inspect(path: Path) -> tuple[int, list[str]]:
    findings: list[str] = []
    count = 0
    for name, content in members(path):
        count += 1
        lowered = name.lower()
        if any(fnmatch.fnmatchcase(lowered, pattern) for pattern in FORBIDDEN_PATHS):
            findings.append(f"{path.name}:{name}: forbidden path")
        if content is None or b"\0" in content[:8192]:
            continue
        text = content.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            where = f"{path.name}:{name}:{number}"
            if name not in RULE_SOURCES and INTERNAL_MARKERS.search(line):
                findings.append(f"{where}: internal material marker")
            if PRIVATE_KEY.search(line):
                findings.append(f"{where}: private key block")
            if PurePosixPath(name).name not in NO_IDENTIFIER_SCAN:
                if any(luhn_npi(m) and m not in DOCUMENTED_EXAMPLE_NPIS for m in NPI_SHAPE.findall(line)):
                    findings.append(f"{where}: check-digit-valid NPI with an issuable prefix")
                if SSN_SHAPE.search(line):
                    findings.append(f"{where}: SSN-shaped value")
        if CPT_DISPLAY.search(text):
            findings.append(f"{path.name}:{name}: CPT coding with a display")
    return count, findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path in args.paths:
        if not path.exists():
            print(f"inspect_release: {path} does not exist", file=sys.stderr)
            return 2
        count, findings = inspect(path)
        print(f"{path.name}: {count} members inspected, {len(findings)} findings")
        for finding in findings:
            print(f"  {finding}")
        failed = failed or bool(findings)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
