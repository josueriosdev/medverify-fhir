//! Version 1 stores a 64-byte header followed by little-endian, 16-byte records.
//! Each setting has its own row. Date intervals are half-open.

use crate::encoding::{MueRecord, NcciPtpRecord, MAX_PROCEDURE_CODE, NO_DELETION_DATE};
use bytemuck::Pod;
use memmap2::{Mmap, MmapMut};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{self, Read, Write};
use std::marker::PhantomData;
use std::path::Path;

const HEADER: usize = 64;
const ROW: usize = 16;
const MAGIC: &[u8; 8] = b"MVNCCI01";

#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CatalogMode {
    Synthetic = 0,
    Licensed = 1,
}

impl CatalogMode {
    pub fn release(self) -> &'static str {
        match self {
            Self::Synthetic => "synth01",
            Self::Licensed => "v323r0",
        }
    }

    fn accepts(self, code: u32) -> bool {
        if self == Self::Licensed {
            return code <= MAX_PROCEDURE_CODE;
        }
        // Base 36 also places letter-suffixed codes between Y9001 and Y9120.
        let Some(bytes) = crate::encoding::decode_code_b36(code) else {
            return false;
        };
        bytes[1..].iter().all(u8::is_ascii_digit) && (*b"Y9001"..=*b"Y9120").contains(&bytes)
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

type Key = (u32, u32, u8);
type Rule = (u16, u8, u8, u8);

trait Record: Pod {
    const KIND: u8;
    fn decode(bytes: &[u8; ROW]) -> Self;
    fn key(&self) -> Key;
    fn dates(&self) -> (u16, u16);
    fn rule(&self) -> Rule;
    fn valid(&self, mode: CatalogMode) -> bool;

    fn order(&self) -> (Key, (u16, u16), Rule) {
        (self.key(), self.dates(), self.rule())
    }
}

fn word(bytes: &[u8; ROW], i: usize) -> u16 {
    u16::from_le_bytes([bytes[i], bytes[i + 1]])
}

fn code(bytes: &[u8; ROW], i: usize) -> u32 {
    u32::from_le_bytes(bytes[i..i + 4].try_into().unwrap())
}

impl Record for NcciPtpRecord {
    const KIND: u8 = 1;
    fn decode(b: &[u8; ROW]) -> Self {
        Self {
            col1_b36: code(b, 0),
            col2_b36: code(b, 4),
            eff_days: word(b, 8),
            del_days: word(b, 10),
            ccmi: b[12],
            setting_flags: b[13],
            rationale_id: b[14],
            flags: b[15],
        }
    }
    fn key(&self) -> Key {
        (self.col1_b36, self.col2_b36, self.setting_flags)
    }
    fn dates(&self) -> (u16, u16) {
        (self.eff_days, self.del_days)
    }
    fn rule(&self) -> Rule {
        (0, self.ccmi, self.rationale_id, self.flags)
    }
    fn valid(&self, mode: CatalogMode) -> bool {
        mode.accepts(self.col1_b36)
            && mode.accepts(self.col2_b36)
            && matches!(self.setting_flags, 1 | 2)
            && self.flags <= 1
            && self.eff_days != NO_DELETION_DATE
            && match self.ccmi {
                0 | 1 => self.eff_days < self.del_days,
                9 => self.eff_days == self.del_days,
                _ => false,
            }
    }
}

impl Record for MueRecord {
    const KIND: u8 = 2;
    fn decode(b: &[u8; ROW]) -> Self {
        Self {
            code_b36: code(b, 0),
            max_units: word(b, 4),
            eff_days: word(b, 6),
            del_days: word(b, 8),
            mai: b[10],
            setting_flags: b[11],
            rationale_id: b[12],
            flags: b[13],
            reserved: word(b, 14),
        }
    }
    fn key(&self) -> Key {
        (self.code_b36, 0, self.setting_flags)
    }
    fn dates(&self) -> (u16, u16) {
        (self.eff_days, self.del_days)
    }
    fn rule(&self) -> Rule {
        (self.max_units, self.mai, self.rationale_id, self.flags)
    }
    fn valid(&self, mode: CatalogMode) -> bool {
        mode.accepts(self.code_b36)
            && matches!(self.setting_flags, 1 | 2 | 4)
            && (1..=3).contains(&self.mai)
            && self.eff_days < self.del_days
            && self.flags == 0
            && self.reserved == 0
    }
}

struct Validator<R> {
    mode: CatalogMode,
    previous: Option<R>,
    active: Option<R>,
}

impl<R: Record> Validator<R> {
    fn new(mode: CatalogMode) -> Self {
        Self {
            mode,
            previous: None,
            active: None,
        }
    }

    fn check(&mut self, row: R) -> io::Result<()> {
        if !row.valid(self.mode) {
            return Err(invalid("invalid index record"));
        }
        if self.previous.is_some_and(|p| p.order() > row.order()) {
            return Err(invalid("index records out of order"));
        }
        self.previous = Some(row);
        let (start, end) = row.dates();
        if start == end {
            return Ok(());
        }
        // A shorter nested interval or an inactive edit must not hide a longer one.
        if let Some(active) = self.active {
            if active.key() == row.key() && active.dates().1 > start {
                if active.rule() != row.rule() {
                    return Err(invalid("conflicting overlapping edits"));
                }
                if active.dates().1 >= end {
                    return Ok(());
                }
            }
        }
        self.active = Some(row);
        Ok(())
    }
}

fn header<R: Record>(mode: CatalogMode, count: u64) -> [u8; HEADER] {
    let mut h = [0; HEADER];
    h[..8].copy_from_slice(MAGIC);
    h[8..10].copy_from_slice(&1_u16.to_le_bytes());
    h[10] = R::KIND;
    h[11] = mode as u8;
    h[12..16].copy_from_slice(&(ROW as u32).to_le_bytes());
    h[16..24].copy_from_slice(&count.to_le_bytes());
    let release = mode.release().as_bytes();
    h[24..24 + release.len()].copy_from_slice(release);
    h
}

fn write_index<R: Record>(
    mut output: impl Write,
    mode: CatalogMode,
    rows: &mut [R],
) -> io::Result<()> {
    rows.sort_unstable_by_key(Record::order);
    let mut validator = Validator::new(mode);
    for &row in rows.iter() {
        validator.check(row)?;
    }
    let mut h = header::<R>(mode, rows.len() as u64);
    #[cfg(target_endian = "big")]
    for row in rows.iter_mut() {
        *row = R::decode(bytemuck::bytes_of(row).try_into().unwrap());
    }
    let payload = bytemuck::cast_slice(rows);
    h[32..].copy_from_slice(&Sha256::digest(payload));
    output.write_all(&h)?;
    output.write_all(payload)
}

pub fn write_ptp(
    output: impl Write,
    mode: CatalogMode,
    mut rows: Vec<NcciPtpRecord>,
) -> io::Result<()> {
    write_index(output, mode, &mut rows)
}

pub fn write_mue(
    output: impl Write,
    mode: CatalogMode,
    mut rows: Vec<MueRecord>,
) -> io::Result<()> {
    write_index(output, mode, &mut rows)
}

pub struct MappedIndex<R> {
    map: Mmap,
    mode: CatalogMode,
    record: PhantomData<R>,
}

pub type MmapPtpIndex = MappedIndex<NcciPtpRecord>;
pub type MmapMueIndex = MappedIndex<MueRecord>;

impl<R: Pod> MappedIndex<R> {
    pub fn records(&self) -> &[R] {
        bytemuck::cast_slice(&self.map[HEADER..])
    }
    pub fn catalog_mode(&self) -> CatalogMode {
        self.mode
    }
    pub fn release(&self) -> &'static str {
        self.mode.release()
    }
}

impl MmapPtpIndex {
    pub fn open(path: impl AsRef<Path>) -> io::Result<Self> {
        open(path.as_ref())
    }
}

impl MmapMueIndex {
    pub fn open(path: impl AsRef<Path>) -> io::Result<Self> {
        open(path.as_ref())
    }
}

fn open<R: Record>(path: &Path) -> io::Result<MappedIndex<R>> {
    let mut file = File::open(path)?;
    let length = file.metadata()?.len();
    let mut h = [0; HEADER];
    file.read_exact(&mut h)?;
    let mode = match h[11] {
        0 => CatalogMode::Synthetic,
        1 => CatalogMode::Licensed,
        _ => return Err(invalid("unknown catalog mode")),
    };
    let count = u64::from_le_bytes(h[16..24].try_into().unwrap());
    if h[..32] != header::<R>(mode, count)[..32] {
        return Err(invalid("invalid index header"));
    }
    if count
        .checked_mul(ROW as u64)
        .and_then(|n| n.checked_add(HEADER as u64))
        != Some(length)
    {
        return Err(invalid("index length differs from record count"));
    }
    let size = usize::try_from(length).map_err(|_| invalid("index exceeds address space"))?;
    let mut map = MmapMut::map_anon(size)?;
    map[..HEADER].copy_from_slice(&h);
    file.read_exact(&mut map[HEADER..])?;
    if file.read(&mut [0])? != 0 {
        return Err(invalid("index grew during read"));
    }
    let payload = &mut map[HEADER..];
    if !payload.as_ptr().cast::<R>().is_aligned() {
        return Err(invalid("unaligned index payload"));
    }
    if Sha256::digest(&*payload)[..] != h[32..] {
        return Err(invalid("index payload SHA-256 mismatch"));
    }
    let mut validator = Validator::new(mode);
    for chunk in payload.as_chunks_mut::<ROW>().0 {
        let row = R::decode(chunk);
        validator.check(row)?;
        #[cfg(target_endian = "big")]
        chunk.copy_from_slice(bytemuck::bytes_of(&row));
    }
    Ok(MappedIndex {
        map: map.make_read_only()?,
        mode,
        record: PhantomData,
    })
}
