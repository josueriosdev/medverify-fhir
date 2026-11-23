use csv::StringRecord;
use medverify_core::encoding::{
    encode_code_b36, encode_epoch_day, MueRecord, NcciPtpRecord, NO_DELETION_DATE,
};
use medverify_core::index::{write_mue, write_ptp, CatalogMode};
use std::collections::HashSet;
use std::env;
use std::error::Error;
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use time::{macros::format_description, Date};

type Result<T> = std::result::Result<T, Box<dyn Error>>;
const PTP_FILE: &str = "ncci_ptp.bin";
const MUE_FILE: &str = "ncci_mue.bin";

fn read_csv(
    path: &Path,
    headers: &[&str],
    mut consume: impl FnMut(&StringRecord) -> Result<()>,
) -> Result<()> {
    let mut reader = csv::Reader::from_path(path)?;
    if !reader.headers()?.iter().eq(headers.iter().copied()) {
        return Err(format!("unexpected columns in {}", path.display()).into());
    }
    let mut row = StringRecord::new();
    while reader.read_record(&mut row)? {
        consume(&row).map_err(|e| {
            format!(
                "{} line {}: {e}",
                path.display(),
                row.position().unwrap().line()
            )
        })?;
    }
    Ok(())
}

fn validate_catalog(data: &Path) -> Result<()> {
    let mut expected: HashSet<_> = (9001..=9120).map(|n| format!("Y{n}")).collect();
    read_csv(
        &data.join("procedures_synthetic.csv"),
        &["code", "label"],
        |row| {
            if !expected.remove(&row[0]) || row[1].trim().is_empty() {
                return Err("invalid synthetic procedure or label".into());
            }
            Ok(())
        },
    )?;
    if !expected.is_empty() {
        return Err("incomplete synthetic catalog".into());
    }
    Ok(())
}

fn procedure(code: &str) -> Result<u32> {
    encode_code_b36(code)
        .filter(|_| !code.bytes().any(|b| b.is_ascii_lowercase()))
        .ok_or_else(|| "invalid procedure code".into())
}

fn setting(value: &str) -> Result<u8> {
    match value {
        "practitioner" => Ok(1),
        "hospital" => Ok(2),
        "dme" => Ok(4),
        _ => Err("invalid setting".into()),
    }
}

fn day(value: &str) -> Result<u16> {
    let date = Date::parse(value, format_description!("[year]-[month]-[day]"))?;
    encode_epoch_day(date).ok_or_else(|| "date outside index epoch".into())
}

fn end_day(value: &str) -> Result<u16> {
    if value.is_empty() {
        Ok(NO_DELETION_DATE)
    } else {
        day(value)
    }
}

fn same_file(a: &Path, b: &Path) -> io::Result<bool> {
    let mut a = File::open(a)?;
    let mut b = File::open(b)?;
    let mut left = [0; 8192];
    let mut right = [0; 8192];
    loop {
        let n = a.read(&mut left)?;
        if n == 0 {
            return Ok(b.read(&mut right)? == 0);
        }
        b.read_exact(&mut right[..n])?;
        if left[..n] != right[..n] {
            return Ok(false);
        }
    }
}

fn build(mode: CatalogMode, data: &Path, out: &Path) -> Result<()> {
    let suffix = match mode {
        CatalogMode::Synthetic => {
            validate_catalog(data)?;
            "_synthetic"
        }
        CatalogMode::Licensed => "",
    };
    let parent = out
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    let stage = tempfile::tempdir_in(parent)?;

    let mut ptp = Vec::new();
    read_csv(
        &data.join(format!("ncci_ptp{suffix}.csv")),
        &[
            "col1",
            "col2",
            "setting",
            "effective",
            "end_exclusive",
            "ccmi",
            "prior_1996",
        ],
        |row| {
            ptp.push(NcciPtpRecord {
                col1_b36: procedure(&row[0])?,
                col2_b36: procedure(&row[1])?,
                setting_flags: setting(&row[2])?,
                eff_days: day(&row[3])?,
                del_days: end_day(&row[4])?,
                ccmi: row[5].parse()?,
                flags: row[6].parse()?,
                rationale_id: 0,
            });
            Ok(())
        },
    )?;
    let ptp_count = ptp.len();
    write_ptp(File::create(stage.path().join(PTP_FILE))?, mode, ptp)?;

    let mut mue = Vec::new();
    read_csv(
        &data.join(format!("ncci_mue{suffix}.csv")),
        &[
            "code",
            "setting",
            "effective",
            "end_exclusive",
            "max_units",
            "mai",
        ],
        |row| {
            mue.push(MueRecord {
                code_b36: procedure(&row[0])?,
                setting_flags: setting(&row[1])?,
                eff_days: day(&row[2])?,
                del_days: end_day(&row[3])?,
                max_units: row[4].parse()?,
                mai: row[5].parse()?,
                flags: 0,
                reserved: 0,
                rationale_id: 0,
            });
            Ok(())
        },
    )?;
    let mue_count = mue.len();
    write_mue(File::create(stage.path().join(MUE_FILE))?, mode, mue)?;

    // A rename publishes both files together. A concurrent identical build may win.
    if out
        .read_dir()
        .is_ok_and(|mut entries| entries.next().is_none())
    {
        if let Err(e) = fs::remove_dir(out) {
            if !matches!(
                e.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::DirectoryNotEmpty
            ) {
                return Err(e.into());
            }
        }
    }
    if let Err(e) = fs::rename(stage.path(), out) {
        if !out.is_dir() {
            return Err(e.into());
        }
        for name in [PTP_FILE, MUE_FILE] {
            if !same_file(&stage.path().join(name), &out.join(name))? {
                return Err("destination contains a different index; use a fresh directory".into());
            }
        }
    }
    println!(
        "{}: {ptp_count} PTP records, {mue_count} MUE records",
        mode.release()
    );
    Ok(())
}

fn run() -> Result<()> {
    let mut args = env::args_os().skip(1);
    let mode = match args.next().as_deref().and_then(|s| s.to_str()) {
        Some("synthetic") => CatalogMode::Synthetic,
        Some("licensed") => CatalogMode::Licensed,
        _ => return Err("usage: medverify-index <synthetic|licensed> --data DIR --out DIR".into()),
    };
    let (mut data, mut out) = (None, None);
    while let Some(option) = args.next() {
        let slot = match option.to_str() {
            Some("--data") if data.is_none() => &mut data,
            Some("--out") if out.is_none() => &mut out,
            _ => return Err("unknown or duplicate option".into()),
        };
        *slot = Some(PathBuf::from(args.next().ok_or("missing directory")?));
    }
    build(
        mode,
        &data.ok_or("--data is required")?,
        &out.ok_or("--out is required")?,
    )
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
