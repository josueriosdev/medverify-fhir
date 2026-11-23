"""Install the billable-code list from the CDC FY2027 ICD-10-CM release."""

import argparse
import hashlib
import http.client
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HOST = "ftp.cdc.gov"
ARCHIVE_PATH = "/pub/Health_Statistics/NCHS/Publications/ICD10CM/2027/icd10cm-code-descriptions-2027.zip"
CODES_MEMBER = "icd10cm-code-descriptions-2027/icd10cm-codes-2027.txt"
SHA256 = "93e3ad6004badf470c55bfe679b748ae88fd9b2b421851e409eec382c7713b9a"
CODE_COUNT = 74_879
CODE = re.compile(rb"[A-Z][A-Z0-9]{2,6}")


def _download(destination: Path) -> None:
    connection = http.client.HTTPSConnection(HOST, timeout=60)
    try:
        connection.request("GET", ARCHIVE_PATH)
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"CDC returned HTTP {response.status}")
            with destination.open("wb") as stream:
                shutil.copyfileobj(response, stream)
    finally:
        connection.close()


def ingest_icd10(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    published = out / "icd10cm-codes-FY2027.txt"
    with tempfile.TemporaryDirectory(dir=out) as directory:
        archive = Path(directory) / "release.zip"
        _download(archive)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
                raise ValueError("CDC archive SHA-256 mismatch")

        staged = Path(directory) / "codes.txt"
        seen: set[bytes] = set()
        with zipfile.ZipFile(archive) as bundle:
            if bundle.namelist().count(CODES_MEMBER) != 1:
                raise ValueError("expected one FY2027 billable-code list")
            with bundle.open(CODES_MEMBER) as codes, staged.open("wb") as output:
                for line_number, line in enumerate(codes, 1):
                    fields = line.split(maxsplit=1)
                    if (
                        len(fields) != 2
                        or not CODE.fullmatch(fields[0])
                        or not fields[1].strip()
                    ):
                        raise ValueError(f"invalid CDC record at line {line_number}")
                    if fields[0] in seen:
                        raise ValueError(f"duplicate CDC code at line {line_number}")
                    seen.add(fields[0])
                    output.write(line)
        if len(seen) != CODE_COUNT:
            raise ValueError(
                f"expected {CODE_COUNT} ICD-10-CM codes, found {len(seen)}"
            )
        staged.replace(published)
    return published


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    icd = commands.add_parser("icd10")
    icd.add_argument("--release", choices=["FY2027"], required=True)
    icd.add_argument("--out", type=Path, default=Path(".cache/data/icd10"))
    args = parser.parse_args()
    try:
        published = ingest_icd10(args.out)
    except (
        OSError,
        ValueError,
        http.client.HTTPException,
        zipfile.BadZipFile,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"FY2027: {CODE_COUNT} ICD-10-CM codes; archive SHA-256 {SHA256}")
    print(published.as_posix())
    return 0
