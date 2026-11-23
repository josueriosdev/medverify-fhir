use crate::claim::{group_lines, ClaimLine, EvaluationError, ProcedureCode, ServiceDay};
use crate::encoding::MueRecord;
use crate::index::MmapMueIndex;
use std::collections::BTreeMap;

/// A resulting denial can be appealed for all three adjudication indicators.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MueScope {
    ClaimLine {
        line_id: u64,
    },
    /// MAI 2 applies an absolute service-day limit in claims processing.
    AbsoluteServiceDayLimit,
    /// MAI 3 can be reviewed using evidence of services furnished, coding and medical necessity.
    ServiceDayClinicalReview,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MueFinding<'a> {
    pub service_day: ServiceDay<'a>,
    pub code: ProcedureCode,
    pub scope: MueScope,
    pub max_units: u16,
    pub total_units: u64,
    pub abn_cannot_shift_liability: bool,
}

/// Evaluates published MUEs across all supplied lines, including lines from multiple claims.
/// An absent finding carries no coverage determination.
pub fn evaluate<'a>(
    index: &MmapMueIndex,
    lines: &[ClaimLine<'a>],
) -> Result<Vec<MueFinding<'a>>, EvaluationError<'a>> {
    let mut findings = Vec::new();
    for (service_day, group) in group_lines(lines)? {
        let mut by_code = BTreeMap::<_, Vec<_>>::new();
        for line in group {
            by_code.entry(line.code).or_default().push(line);
        }
        for (code, lines) in by_code {
            let Some(edit) = index.active_on(code.0, service_day.setting as u8, service_day.date.0)
            else {
                continue;
            };
            if edit.mai == 1 {
                for line in lines {
                    findings.extend(excess(
                        service_day,
                        edit,
                        MueScope::ClaimLine { line_id: line.id },
                        line.units.get(),
                        line.abn_liability_shift_attempted,
                    ));
                }
            } else {
                let total = lines
                    .iter()
                    .try_fold(0_u64, |total, line| total.checked_add(line.units.get()))
                    .ok_or(EvaluationError::UnitTotalOverflow { service_day, code })?;
                let scope = if edit.mai == 2 {
                    MueScope::AbsoluteServiceDayLimit
                } else {
                    MueScope::ServiceDayClinicalReview
                };
                findings.extend(excess(
                    service_day,
                    edit,
                    scope,
                    total,
                    lines.iter().any(|line| line.abn_liability_shift_attempted),
                ));
            }
        }
    }
    Ok(findings)
}

fn excess<'a>(
    service_day: ServiceDay<'a>,
    edit: &MueRecord,
    scope: MueScope,
    total_units: u64,
    abn_cannot_shift_liability: bool,
) -> Option<MueFinding<'a>> {
    (total_units > u64::from(edit.max_units)).then_some(MueFinding {
        service_day,
        code: ProcedureCode(edit.code_b36),
        scope,
        max_units: edit.max_units,
        total_units,
        abn_cannot_shift_liability,
    })
}
