use medverify_core::encoding::{encode_code_b36, MueRecord, NcciPtpRecord};
use medverify_core::index::{write_mue, write_ptp, CatalogMode, MmapMueIndex, MmapPtpIndex};
use sha2::{Digest, Sha256};
use std::{fs, path::Path, process::Command};
use tempfile::tempdir;

fn pair(start: u16, end: u16, ccmi: u8) -> NcciPtpRecord {
    NcciPtpRecord {
        col1_b36: encode_code_b36("Y9001").unwrap(),
        col2_b36: encode_code_b36("Y9002").unwrap(),
        eff_days: start,
        del_days: end,
        ccmi,
        setting_flags: 1,
        rationale_id: 0,
        flags: 0,
    }
}

fn limit() -> MueRecord {
    MueRecord {
        code_b36: encode_code_b36("Y9120").unwrap(),
        max_units: 300,
        eff_days: 256,
        del_days: u16::MAX,
        mai: 3,
        setting_flags: 4,
        rationale_id: 0,
        flags: 0,
        reserved: 0,
    }
}

fn ptp_bytes(rows: Vec<NcciPtpRecord>) -> Vec<u8> {
    let mut bytes = Vec::new();
    write_ptp(&mut bytes, CatalogMode::Synthetic, rows).unwrap();
    bytes
}

fn sign_payload(bytes: &mut [u8]) {
    let digest = Sha256::digest(&bytes[64..]);
    bytes[32..64].copy_from_slice(&digest);
}

#[test]
fn little_endian_header_records_and_immutable_snapshots() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("edits");
    let row = pair(256, 32767, 1);
    let bytes = ptp_bytes(vec![row]);
    assert_eq!(&bytes[..16], b"MVNCCI01\x01\x00\x01\x00\x10\x00\x00\x00");
    assert_eq!(&bytes[16..24], &1u64.to_le_bytes());
    assert_eq!(&bytes[24..32], b"synth01\0");
    assert_eq!(bytes[32..64], Sha256::digest(&bytes[64..])[..]);
    assert_eq!(&bytes[64..68], &row.col1_b36.to_le_bytes());
    assert_eq!(&bytes[68..72], &row.col2_b36.to_le_bytes());
    assert_eq!(&bytes[72..], &[0, 1, 255, 127, 1, 1, 0, 0]);
    fs::write(&path, bytes).unwrap();
    let ptp = MmapPtpIndex::open(&path).unwrap();
    assert!(MmapMueIndex::open(&path).is_err());
    assert_eq!(ptp.catalog_mode(), CatalogMode::Synthetic);
    assert_eq!(ptp.release(), "synth01");

    let mut bytes = Vec::new();
    write_mue(&mut bytes, CatalogMode::Licensed, vec![limit()]).unwrap();
    assert_eq!(&bytes[68..], &[44, 1, 0, 1, 255, 255, 3, 4, 0, 0, 0, 0]);
    fs::write(&path, bytes).unwrap();
    let mue = MmapMueIndex::open(&path).unwrap();
    assert!(MmapPtpIndex::open(&path).is_err());
    assert_eq!(mue.catalog_mode(), CatalogMode::Licensed);
    assert_eq!(mue.release(), "v323r0");
    fs::write(&path, b"x").unwrap();
    fs::remove_file(&path).unwrap();
    assert_eq!(ptp.records(), &[row]);
    assert_eq!(mue.records(), &[limit()]);
}

#[test]
fn header_length_and_digest_corruption() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("edits");
    let bytes = ptp_bytes(vec![pair(0, 1, 0)]);
    for offset in [0, 8, 10, 11, 12, 16, 24, 31, 32, 64] {
        let mut broken = bytes.clone();
        broken[offset] ^= 0xff;
        fs::write(&path, broken).unwrap();
        assert!(MmapPtpIndex::open(&path).is_err(), "offset {offset}");
    }
    let mut overflow = bytes.clone();
    overflow[16..24].fill(255);
    for broken in [
        &bytes[..8],
        &bytes[..79],
        &[&bytes[..], &[0]].concat(),
        &overflow,
    ] {
        fs::write(&path, broken).unwrap();
        assert!(MmapPtpIndex::open(&path).is_err());
    }
}

#[test]
fn validates_intervals_fields_and_order_even_with_a_valid_digest() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("edits");
    let rows = ptp_bytes(vec![pair(1, 5, 0), pair(5, 10, 1)]);
    let mut swapped = rows.clone();
    swapped[64..].rotate_left(16);
    let mut overlap = rows.clone();
    overlap[88..90].copy_from_slice(&4u16.to_le_bytes());
    let mut reversed = rows.clone();
    reversed[74] = 0;
    let mut outside_catalog = rows.clone();
    outside_catalog[64..68].copy_from_slice(&encode_code_b36("Y900A").unwrap().to_le_bytes());
    let mut wrong_setting = rows.clone();
    wrong_setting[77] = 4;
    let mut bad_ccmi = rows;
    bad_ccmi[76] = 9;
    for mut broken in [
        swapped,
        overlap,
        reversed,
        outside_catalog,
        wrong_setting,
        bad_ccmi,
    ] {
        sign_payload(&mut broken);
        fs::write(&path, broken).unwrap();
        assert!(MmapPtpIndex::open(&path).is_err());
    }

    let next = MueRecord {
        eff_days: 300,
        ..limit()
    };
    let first = MueRecord {
        del_days: 300,
        ..limit()
    };
    let mut mue = Vec::new();
    write_mue(&mut mue, CatalogMode::Synthetic, vec![next, first]).unwrap();
    for (offset, value) in [(74, 0), (75, 3), (77, 1), (78, 1), (72, 0), (87, 0)] {
        let mut broken = mue.clone();
        broken[offset] = value;
        sign_payload(&mut broken);
        fs::write(&path, broken).unwrap();
        assert!(MmapMueIndex::open(&path).is_err(), "offset {offset}");
    }
    mue[64..].rotate_left(16);
    sign_payload(&mut mue);
    fs::write(&path, mue).unwrap();
    assert!(MmapMueIndex::open(&path).is_err());
}

#[test]
fn rejects_conflicts_hidden_by_nested_or_inactive_intervals() {
    let mut rows = vec![
        pair(1, 40, 1),
        pair(3, 7, 1),
        pair(9, 9, 9),
        pair(40, 80, 0),
    ];
    rows.push(NcciPtpRecord {
        setting_flags: 2,
        ..pair(4, 39, 0)
    });
    assert!(write_ptp(Vec::new(), CatalogMode::Synthetic, rows.clone()).is_ok());
    rows.push(pair(11, 15, 0));
    assert!(write_ptp(Vec::new(), CatalogMode::Synthetic, rows).is_err());
    let other_limit = MueRecord {
        max_units: 1,
        ..limit()
    };
    assert!(write_mue(
        Vec::new(),
        CatalogMode::Synthetic,
        vec![limit(), other_limit]
    )
    .is_err());
}

#[test]
fn empty_indexes_round_trip() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("empty");
    fs::write(&path, ptp_bytes(Vec::new())).unwrap();
    assert!(MmapPtpIndex::open(&path).unwrap().records().is_empty());
}

fn build(data: &Path, out: &Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_medverify-index"))
        .args(["synthetic", "--data"])
        .arg(data)
        .arg("--out")
        .arg(out)
        .output()
        .unwrap()
}

#[test]
fn compiler_reproduces_the_same_pair_after_input_reordering() {
    let dir = tempdir().unwrap();
    let data = dir.path().join("input");
    let first = dir.path().join("first");
    let second = dir.path().join("second");
    fs::create_dir(&data).unwrap();
    fs::create_dir(&first).unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    assert!(build(&source, &first).status.success());
    for name in [
        "procedures_synthetic.csv",
        "ncci_ptp_synthetic.csv",
        "ncci_mue_synthetic.csv",
    ] {
        let contents = fs::read_to_string(source.join(name)).unwrap();
        let mut lines = contents.lines();
        let mut shuffled = vec![lines.next().unwrap()];
        shuffled.extend(lines.rev());
        fs::write(data.join(name), shuffled.join("\n") + "\n").unwrap();
    }
    assert!(build(&data, &second).status.success());
    assert!(build(&data, &first).status.success());
    for name in ["ncci_ptp.bin", "ncci_mue.bin"] {
        assert_eq!(
            fs::read(first.join(name)).unwrap(),
            fs::read(second.join(name)).unwrap()
        );
    }
    let ptp = MmapPtpIndex::open(first.join("ncci_ptp.bin")).unwrap();
    assert!(ptp
        .records()
        .iter()
        .any(|r| r.ccmi == 9 && r.eff_days == r.del_days));
    assert!(MmapMueIndex::open(first.join("ncci_mue.bin"))
        .unwrap()
        .records()
        .contains(&MueRecord {
            code_b36: encode_code_b36("Y9011").unwrap(),
            max_units: 1,
            eff_days: 13422,
            ..limit()
        }));
    let bad = data.join("ncci_mue_synthetic.csv");
    let original = fs::read_to_string(&bad).unwrap();
    fs::write(&bad, original + "Y9011,practitioner,2026-11-01,,5,2\n").unwrap();
    let failed = build(&data, &first);
    assert!(!failed.status.success());
    assert!(String::from_utf8_lossy(&failed.stderr).contains("conflicting overlapping edits"));
    assert!(!build(&data, &dir.path().join("bad")).status.success());
    assert!(!dir.path().join("bad").exists());
    for name in ["ncci_ptp.bin", "ncci_mue.bin"] {
        assert_eq!(
            fs::read(first.join(name)).unwrap(),
            fs::read(second.join(name)).unwrap()
        );
    }
}
