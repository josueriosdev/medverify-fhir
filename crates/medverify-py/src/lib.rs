use pyo3::prelude::*;

#[pymodule(gil_used = false)]
fn _medverify(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))
}
