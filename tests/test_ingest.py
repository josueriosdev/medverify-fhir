import csv
import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from medverify import ingest


def archive_with(member, content):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, content)
    return buffer.getvalue()


def serve(monkeypatch, archive, count=2):
    monkeypatch.setattr(
        ingest, "_download", lambda destination: destination.write_bytes(archive)
    )
    monkeypatch.setattr(ingest, "SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(ingest, "CODE_COUNT", count)


def test_verified_release_is_repeatable_and_preserves_cdc_bytes(tmp_path, monkeypatch):
    content = b"A00     First fixture\r\nQA00101 Second fixture\r\n"
    serve(monkeypatch, archive_with(ingest.CODES_MEMBER, content))
    first = ingest.ingest_icd10(tmp_path)
    assert first.read_bytes() == content
    assert ingest.ingest_icd10(tmp_path) == first
    assert list(tmp_path.iterdir()) == [first]
    assert first.read_bytes() == content


@pytest.mark.parametrize(
    ("member", "content", "error"),
    [
        ("other.txt", b"", "one FY2027 billable-code list"),
        (ingest.CODES_MEMBER, b"A00 first\n", "expected 2 ICD-10-CM codes"),
        (ingest.CODES_MEMBER, b"A00 first\nA00 duplicate\n", "duplicate CDC code"),
        (ingest.CODES_MEMBER, b"A.00 malformed\n", "invalid CDC record"),
        (ingest.CODES_MEMBER, b"A00    \n", "invalid CDC record"),
    ],
)
def test_invalid_release_leaves_previous_publication_intact(
    tmp_path, monkeypatch, member, content, error
):
    published = tmp_path / "icd10cm-codes-FY2027.txt"
    published.write_bytes(b"previous verified release\n")
    serve(monkeypatch, archive_with(member, content))
    with pytest.raises(ValueError, match=error):
        ingest.ingest_icd10(tmp_path)
    assert list(tmp_path.iterdir()) == [published]
    assert published.read_bytes() == b"previous verified release\n"


def test_checks_digest_before_reading_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ingest, "_download", lambda path: path.write_bytes(b"incomplete zip")
    )
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        ingest.ingest_icd10(tmp_path)
    assert not list(tmp_path.iterdir())


def test_interrupted_download_preserves_existing_file(tmp_path, monkeypatch):
    def interrupted(path):
        path.write_bytes(b"partial")
        raise ConnectionResetError("download interrupted")

    published = tmp_path / "icd10cm-codes-FY2027.txt"
    published.write_bytes(b"previous")
    monkeypatch.setattr(ingest, "_download", interrupted)
    with pytest.raises(ConnectionResetError):
        ingest.ingest_icd10(tmp_path)
    assert list(tmp_path.iterdir()) == [published]
    assert published.read_bytes() == b"previous"


def test_synthetic_catalog_and_edit_coverage():
    data = Path(__file__).resolve().parents[1] / "data"

    def rows(name):
        with (data / f"{name}_synthetic.csv").open(
            newline="", encoding="ascii"
        ) as source:
            return list(csv.DictReader(source))

    catalog = rows("procedures")
    codes = {row["code"] for row in catalog}
    assert codes == {f"Y{n}" for n in range(9001, 9121)}
    assert len({row["label"].strip() for row in catalog} - {""}) == len(catalog) == 120
    ptp, mue = rows("ncci_ptp"), rows("ncci_mue")
    assert all(row["col1"] in codes and row["col2"] in codes for row in ptp)
    assert all(row["code"] in codes for row in mue)
    assert {(row["ccmi"], row["setting"]) for row in ptp} == {
        (ccmi, setting) for ccmi in "019" for setting in ("practitioner", "hospital")
    }
    assert {(row["mai"], row["setting"]) for row in mue} == {
        (mai, setting)
        for mai in "123"
        for setting in ("practitioner", "hospital", "dme")
    }
