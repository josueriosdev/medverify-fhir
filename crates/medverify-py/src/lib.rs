mod findings;
mod line;

use findings::Findings;
use line::{ClaimLine, ModifierEvidence};
use medverify_core::claim::EvaluationError;
use medverify_core::index::{MmapMueIndex, MmapPtpIndex};
use medverify_core::{mue, ncci};
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;
use std::io;
use std::path::PathBuf;

/// Evaluates claim lines against one immutable pair of NCCI indexes.
#[pyclass(frozen, module = "medverify._medverify")]
struct MedVerifyEngine {
    ptp: MmapPtpIndex,
    mue: MmapMueIndex,
}

fn evaluation_error(error: EvaluationError<'_>) -> PyErr {
    match error {
        EvaluationError::DuplicateLineId { .. } => PyValueError::new_err(error.to_string()),
        EvaluationError::UnitTotalOverflow { .. } => PyOverflowError::new_err(error.to_string()),
    }
}

impl MedVerifyEngine {
    fn findings(&self, claim: &[ClaimLine]) -> PyResult<Findings> {
        let lines: Vec<_> = claim.iter().map(ClaimLine::as_line).collect();
        let ptp = ncci::evaluate(&self.ptp, &lines).map_err(evaluation_error)?;
        let mue = mue::evaluate(&self.mue, &lines).map_err(evaluation_error)?;
        Ok(Findings {
            ptp: ptp.into_iter().map(Into::into).collect(),
            mue: mue.into_iter().map(Into::into).collect(),
        })
    }
}

#[pymethods]
impl MedVerifyEngine {
    #[new]
    fn new(py: Python<'_>, index_dir: PathBuf) -> PyResult<Self> {
        py.detach(move || {
            let ptp = MmapPtpIndex::open(index_dir.join("ncci_ptp.bin"))?;
            let mue = MmapMueIndex::open(index_dir.join("ncci_mue.bin"))?;
            if ptp.catalog_mode() != mue.catalog_mode() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "index catalogs differ",
                ));
            }
            Ok(Self { ptp, mue })
        })
        .map_err(PyErr::from)
    }

    /// Evaluate all lines in one group of claims, including service-day totals.
    fn evaluate(&self, py: Python<'_>, lines: Vec<ClaimLine>) -> PyResult<Findings> {
        py.detach(move || self.findings(&lines))
    }

    /// Evaluate independent groups of lines and return their findings in input order.
    fn evaluate_batch(
        &self,
        py: Python<'_>,
        claims: Vec<Vec<ClaimLine>>,
    ) -> PyResult<Vec<Findings>> {
        py.detach(move || claims.iter().map(|lines| self.findings(lines)).collect())
    }
}

#[pymodule(gil_used = false)]
fn _medverify(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<MedVerifyEngine>()?;
    module.add_class::<ClaimLine>()?;
    module.add_class::<ModifierEvidence>()?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))
}
