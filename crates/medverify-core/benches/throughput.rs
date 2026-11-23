use criterion::{criterion_group, criterion_main, Criterion};
use medverify_core::claim::{
    ClaimLine, Modifier, ProcedureCode, ServiceDate, ServiceDay, ServiceKind, Setting,
};
use medverify_core::encoding::{encode_code_b36, encode_epoch_day};
use medverify_core::fhir_pas::{validate_request, validate_response, ResponseContext};
use medverify_core::index::{MmapMueIndex, MmapPtpIndex};
use medverify_core::{mue, ncci, wiser};
use std::hint::black_box;
use std::num::NonZeroU64;
use std::path::Path;
use std::process::Command;
use std::time::Instant;
use time::macros::date;

fn measure<T>(criterion: &mut Criterion, name: &str, mut run: impl FnMut() -> T) {
    criterion.bench_function(name, |b| b.iter(&mut run));
    // Criterion times batches. Time individual operations separately for tail latency.
    let mut latencies = Vec::with_capacity(10_000);
    for _ in 0..10_000 {
        let start = Instant::now();
        drop(black_box(run()));
        latencies.push(start.elapsed().as_nanos());
    }
    latencies.sort_unstable();
    println!("{name} individual p99: {} ns", latencies[9899]);
}

fn throughput(c: &mut Criterion) {
    let dir = tempfile::tempdir().unwrap();
    let output = dir.path().join("indexes");
    let data = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    assert!(Command::new(env!("CARGO_BIN_EXE_medverify-index"))
        .arg("synthetic")
        .arg("--data")
        .arg(data)
        .arg("--out")
        .arg(&output)
        .status()
        .unwrap()
        .success());
    let ptp = MmapPtpIndex::open(output.join("ncci_ptp.bin")).unwrap();
    let mue = MmapMueIndex::open(output.join("ncci_mue.bin")).unwrap();
    let date = date!(2026 - 10 - 01);
    let day = encode_epoch_day(date).unwrap();
    let pair = (
        encode_code_b36("Y9001").unwrap(),
        encode_code_b36("Y9002").unwrap(),
    );
    let lookup = || {
        ptp.active_on(
            black_box(pair.0),
            black_box(pair.1),
            Setting::Practitioner as u8,
            black_box(day),
        )
    };
    assert_eq!(lookup().unwrap().ccmi, 1);
    measure(c, "pair_lookup", lookup);

    let service_day = ServiceDay {
        beneficiary: "patient",
        billing_provider: "provider",
        date: ServiceDate::new(date).unwrap(),
        setting: Setting::Practitioner,
    };
    let modifiers = [Modifier::new("59").unwrap()];
    let lines: Vec<_> = [
        "Y9002", "Y9001", "Y9011", "Y9021", "Y9021", "Y9031", "Y9031", "Y9004", "Y9005", "Y9006",
    ]
    .into_iter()
    .enumerate()
    .map(|(i, code)| ClaimLine {
        id: i as u64 + 1,
        service_day,
        code: ProcedureCode::new(code).unwrap(),
        units: NonZeroU64::new(if i == 2 { 4 } else { 2 }).unwrap(),
        service_kind: ServiceKind::Other,
        modifiers: if i == 0 { &modifiers } else { &[] },
        modifier_evidence: &[],
        abn_liability_shift_attempted: i == 0,
    })
    .collect();
    let claim = || {
        (
            ncci::evaluate(&ptp, black_box(&lines)).unwrap(),
            mue::evaluate(&mue, black_box(&lines)).unwrap(),
        )
    };
    let (pairs, limits) = claim();
    assert_eq!((pairs.len(), limits.len()), (1, 3));
    measure(c, "ten_line_claim", claim);

    let policy = wiser::Policy::washington_synthetic();
    let service = wiser::Service {
        state: "WA",
        plan: wiser::Plan::OriginalMedicare,
        site: wiser::Site::Office,
        date,
        emergency: false,
        inpatient_only: false,
        subject_to_other_cms_prior_authorization: false,
        provider_under_upic_review: false,
        facility_claim: false,
        code: ProcedureCode::new("Y9001").unwrap(),
        diagnoses: &[],
        same_claim_codes: &[],
        individual_npi: None,
        exemption: None,
    };
    let request = serde_json::from_str(include_str!("../tests/fixtures/pas/request.json")).unwrap();
    let response =
        serde_json::from_str(include_str!("../tests/fixtures/pas/response.json")).unwrap();
    let review = || {
        let route = policy.route(black_box(&service), wiser::Stage::Claim(None));
        let parsed = validate_request(black_box(&request)).unwrap();
        (
            route,
            validate_response(black_box(&response), &parsed, ResponseContext::default()).unwrap(),
        )
    };
    assert_eq!(
        review().0,
        wiser::Route::PrePaymentReview(wiser::Category::KneeArthroscopy)
    );
    measure(c, "wiser_pas", review);
}

criterion_group!(benches, throughput);
criterion_main!(benches);
