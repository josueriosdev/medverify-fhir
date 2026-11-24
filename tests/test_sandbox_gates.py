import datetime as dt
import http.server
import io
import json
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_reset
import fetch_sandbox
import inspect_release
import run_inferno
import sandbox_common
import seed_hapi
from sandbox_common import SandboxError, load_lock

LOCK = load_lock()
TOKEN = run_inferno.PAS_NOTIFICATION_TOKEN


class Recorder:
    """HTTP double: answers from a route table and records every call it receives."""

    def __init__(self, routes=None):
        self.routes = routes or []
        self.calls = []

    def __call__(self, method, url, body=None, **kwargs):
        self.calls.append((method, url, body))
        for route_method, fragment, answer in self.routes:
            if route_method == method and fragment in url:
                return answer(url, body) if callable(answer) else answer
        return 404, None

    def writes(self):
        return [call for call in self.calls if call[0] in ("POST", "PUT", "DELETE", "PATCH")]


# ---------------------------------------------------------------- archive extraction


def tar_archive(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = b"content\n"
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)


def test_archive_extraction_strips_the_top_directory(tmp_path):
    archive = tmp_path / "source.tar.gz"
    tar_archive(archive, [("repo-abc", "dir"), ("repo-abc/go", "dir"), ("repo-abc/go/main.go", "file")])
    destination = tmp_path / "out"
    destination.mkdir()
    assert fetch_sandbox.extract_archive(archive, destination) == 1
    assert (destination / "go" / "main.go").read_bytes() == b"content\n"


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([("repo-abc/../escape", "file")], "relative component"),
        ([("/repo-abc/abs", "file")], "absolute"),
        ([("repo-abc/C:/drive", "file")], "Windows forbids"),
        ([("repo-abc/a\\..\\..\\escape", "file")], "Windows forbids"),
        ([("repo-abc/odd?.json", "file")], "Windows forbids"),
        ([("repo-abc/trailing.", "file")], "trailing dot"),
        ([("repo-abc/nul.txt", "file")], "device name"),
        ([("repo-abc/Readme", "file"), ("repo-abc/README", "file")], "collides"),
        ([("repo-abc/link", "symlink")], "non-regular"),
        ([("repo-abc/a", "file"), ("other/b", "file")], "one top-level directory"),
    ],
)
def test_archive_extraction_rejects_unsafe_members_before_writing(tmp_path, entries, message):
    archive = tmp_path / "source.tar.gz"
    tar_archive(archive, [("repo-abc/first", "file"), *entries])
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(SandboxError, match=message):
        fetch_sandbox.extract_archive(archive, destination)
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "destination", ["../escape", "vendor", "vendor/../../etc", "/abs/vendor/x", "C:/vendor/x", "other/x", "vendor/a\\b"]
)
def test_lock_destinations_must_stay_inside_the_vendor_tree(destination):
    with pytest.raises(SandboxError):
        fetch_sandbox.vendor_destination(destination)


def test_lock_destination_resolves_below_vendor():
    target = fetch_sandbox.vendor_destination("vendor/davinci-pas-server")
    assert target.parent == fetch_sandbox.VENDOR.resolve()


@pytest.fixture
def cached_source(tmp_path, monkeypatch):
    """A locked source served from a local archive, fetched into a temporary vendor tree."""
    archive = tmp_path / "upstream.tar.gz"
    tar_archive(archive, [("repo-abc", "dir"), ("repo-abc/go", "dir"), ("repo-abc/go/main.go", "file"),
                          ("repo-abc/spec.json", "file")])
    digest = fetch_sandbox.file_sha256(archive)
    downloads = []

    def fake_download(url, destination, expected):
        downloads.append(url)
        shutil.copyfile(archive, destination)
        assert fetch_sandbox.file_sha256(destination) == expected

    monkeypatch.setattr(fetch_sandbox, "VENDOR", tmp_path / "sandbox" / "vendor")
    monkeypatch.setattr(fetch_sandbox, "download", fake_download)
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    fetch_sandbox.extract_archive(archive, pristine)
    entry = {"repository": "https://github.com/example/repo", "commit": "a" * 40, "archive_sha256": digest,
             "tree_sha256": fetch_sandbox.tree_digest(pristine), "directory": "vendor/repo"}
    return entry, downloads, tmp_path / "sandbox" / "vendor" / "repo"


def test_cached_source_is_reused_only_while_every_file_matches(cached_source):
    entry, downloads, _tree = cached_source
    assert "verified" in fetch_sandbox.fetch_source("repo", entry)
    assert "present" in fetch_sandbox.fetch_source("repo", entry)
    assert len(downloads) == 1


def test_changed_cached_file_is_detected_and_repaired(cached_source):
    entry, downloads, tree = cached_source
    fetch_sandbox.fetch_source("repo", entry)
    (tree / "go" / "main.go").write_bytes(b"tampered\n")
    assert "extracted again" in fetch_sandbox.fetch_source("repo", entry)
    assert (tree / "go" / "main.go").read_bytes() == b"content\n"
    assert len(downloads) == 2


def test_verified_archive_must_also_match_the_pinned_tree(cached_source):
    entry, _downloads, tree = cached_source
    entry["tree_sha256"] = "0" * 64
    with pytest.raises(SandboxError, match="did not extract to the locked tree"):
        fetch_sandbox.fetch_source("repo", entry)
    assert not tree.exists()


def test_missing_or_extra_cached_file_is_detected_and_repaired(cached_source):
    entry, _downloads, tree = cached_source
    fetch_sandbox.fetch_source("repo", entry)
    (tree / "spec.json").unlink()
    assert "extracted again" in fetch_sandbox.fetch_source("repo", entry)
    assert (tree / "spec.json").is_file()
    (tree / "planted.go").write_bytes(b"extra\n")
    assert "extracted again" in fetch_sandbox.fetch_source("repo", entry)
    assert not (tree / "planted.go").exists()


# ---------------------------------------------------------------- compose pins and rendering


COMPOSE_TEXT = (ROOT / "sandbox" / "docker-compose.yaml").read_text(encoding="utf-8")


def test_compose_file_uses_exactly_the_locked_pins():
    assert fetch_sandbox.check_compose(LOCK, COMPOSE_TEXT) == []


def test_compose_check_reports_unpinned_images_and_bases():
    hapi = fetch_sandbox.pinned(LOCK["images"]["hapi"])
    golang = fetch_sandbox.pinned(LOCK["images"]["golang"])
    changed = COMPOSE_TEXT.replace(hapi, "hapiproject/hapi:latest").replace(golang, "golang:1.26-alpine")
    problems = fetch_sandbox.check_compose(LOCK, changed)
    assert "compose image hapiproject/hapi:latest is not pinned in the lock" in problems
    assert "locked image hapi is not used by the compose file" in problems
    assert "locked build base golang is not mapped by a named build context" in problems


def test_dockerfile_bases_skip_build_stage_references():
    text = "FROM golang:1.26-alpine AS build\nRUN go build\nFROM build AS test\nFROM --platform=linux/amd64 gcr.io/x:y\n"
    assert fetch_sandbox.dockerfile_bases(text) == ["golang:1.26-alpine", "gcr.io/x:y"]


def rendered(service):
    return {"services": {"svc": service}}


def test_build_bases_must_map_to_locked_digests():
    golang = f"docker-image://{fetch_sandbox.pinned(LOCK['images']['golang'])}"
    good = rendered({"build": {"additional_contexts": {"golang:1.26-alpine": golang}}})
    assert fetch_sandbox.check_build_bases(good, LOCK, {"svc": ["golang:1.26-alpine"]}) == []
    unmapped = rendered({"build": {}})
    assert fetch_sandbox.check_build_bases(unmapped, LOCK, {"svc": ["golang:1.26-alpine"]})
    unknown = fetch_sandbox.check_build_bases(good, LOCK, {"svc": ["alpine:latest"]})
    assert unknown == ["svc builds FROM alpine:latest, which the lock does not pin"]
    assert fetch_sandbox.check_build_bases(good, LOCK, {}) == ["svc builds from a Dockerfile the lock does not cover"]


@pytest.mark.parametrize(
    ("binding", "accepted"),
    [
        ({"host_ip": "127.0.0.1", "published": "18080", "target": 8080}, True),
        ({"host_ip": "::1", "published": "18080", "target": 8080}, True),
        ({"published": "18080", "target": 8080}, False),
        ({"host_ip": "0.0.0.0", "published": "18080", "target": 8080}, False),
        ({"host_ip": "::", "published": "18080", "target": 8080}, False),
        ({"host_ip": "192.168.1.5", "published": "18080", "target": 8080}, False),
        ({"host_ip": "127.0.0.1", "target": 8080}, False),
        ("18080:8080", False),
    ],
)
def test_published_ports_must_name_a_loopback_host(binding, accepted):
    assert (sandbox_common.check_published_ports(rendered({"ports": [binding]})) == []) is accepted


def test_host_networking_is_rejected():
    assert sandbox_common.check_published_ports(rendered({"network_mode": "host"})) == ["svc uses host networking"]


def test_only_the_tls_services_may_mount_the_ca_keys():
    mount = {"type": "volume", "source": "caddy-data", "target": "/x", "read_only": True}
    config = {"services": {"tls": {"volumes": [mount]}, "ca-export": {"volumes": [mount]},
                           "inferno-pas": {"volumes": [mount]}}}
    assert sandbox_common.check_secret_mounts(config) == ["inferno-pas mounts caddy-data, which holds the CA signing keys"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_rendered_candidate_binds_loopback_and_confines_ca_keys():
    config = sandbox_common.rendered_config()
    assert sandbox_common.check_published_ports(config) == []
    assert sandbox_common.check_secret_mounts(config) == []
    published = [p for s in config["services"].values() for p in s.get("ports") or []]
    assert len(published) == 6
    assert {p["host_ip"] for p in published} == {"127.0.0.1"}


# ---------------------------------------------------------------- destinations and endpoints


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.5:18080/fhir", "http://127.0.0.1:8080/fhir", "file:///etc/passwd",
     "http://user:pw@127.0.0.1:18080/fhir", "http://example.org:18080/"],
)
def test_host_side_requests_are_confined_to_sandbox_ports(url):
    with pytest.raises(SandboxError, match="refusing"):
        sandbox_common.http("GET", url)


def test_redirects_are_returned_not_followed(monkeypatch):
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.org/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("MEDVERIFY_HAPI_PORT", str(server.server_address[1]))
        status, _ = sandbox_common.http("GET", f"http://127.0.0.1:{server.server_address[1]}/fhir/metadata")
    finally:
        server.shutdown()
    assert status == 302


def test_sandbox_target_must_be_this_projects_published_port():
    published = {"hapi": "127.0.0.1:18080", "pas": "127.0.0.1:18085"}.__getitem__
    sandbox_common.verify_sandbox_target(
        {"hapi": "http://127.0.0.1:18080/fhir", "pas": "http://127.0.0.1:18085"}, published
    )
    with pytest.raises(SandboxError, match="not the sandbox port"):
        sandbox_common.verify_sandbox_target({"hapi": "http://10.1.2.3:18080/fhir"}, published)
    with pytest.raises(SandboxError, match="publishes hapi at"):
        sandbox_common.verify_sandbox_target({"hapi": "http://127.0.0.1:18080/fhir"}, lambda s: "0.0.0.0:18080")


def test_seed_refuses_a_misconfigured_hapi_before_any_request(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(seed_hapi, "HAPI_BASE", "http://10.0.0.7:8080/fhir")
    monkeypatch.setattr(seed_hapi, "http", recorder)
    monkeypatch.setattr(seed_hapi, "run_compose", lambda *a, **k: pytest.fail("compose must not run"))
    assert seed_hapi.main(["--patients", "50", "--state", "Washington", "--seed", "42"]) == 1
    assert recorder.calls == []


def test_reset_check_refuses_a_misconfigured_pas_before_any_write(monkeypatch, tmp_path):
    recorder = Recorder()
    monkeypatch.setattr(check_reset, "PAS_CONTROL", "http://192.168.0.9:18085")
    monkeypatch.setattr(check_reset, "http", recorder)
    monkeypatch.setattr(check_reset, "RESET_TIMINGS", tmp_path / "reset.json")
    monkeypatch.setattr(sandbox_common, "published_address", lambda service: f"127.0.0.1:{sandbox_common.port(service)}")
    assert check_reset.main(["--cycles", "2", "--expected-patients", "50"]) == 1
    assert recorder.writes() == []
    assert not (tmp_path / "reset.json").exists()


def test_reset_check_deletes_the_first_unreferenced_resource(monkeypatch):
    listing = {"entry": [{"resource": {"id": "obs-a"}}, {"resource": {"id": "obs-b"}}]}
    recorder = Recorder([
        ("GET", "/Observation?", (200, listing)),
        ("DELETE", "/Observation/obs-a", (409, {"resourceType": "OperationOutcome"})),
        ("DELETE", "/Observation/obs-b", (204, None)),
    ])
    monkeypatch.setattr(check_reset, "http", recorder)
    assert check_reset.delete_unreferenced("Observation") == "obs-b"
    assert [call[1].rsplit("/", 1)[-1] for call in recorder.writes()] == ["obs-a", "obs-b"]


@pytest.mark.parametrize("cycles", ["0", "-1", "two"])
def test_reset_check_rejects_non_positive_cycles_before_network(monkeypatch, tmp_path, cycles):
    recorder = Recorder()
    record = tmp_path / "reset.json"
    record.write_text("{}")
    monkeypatch.setattr(check_reset, "http", recorder)
    monkeypatch.setattr(check_reset, "RESET_TIMINGS", record)
    with pytest.raises(SystemExit) as exit_info:
        check_reset.main(["--cycles", cycles, "--expected-patients", "50"])
    assert exit_info.value.code == 2
    assert recorder.calls == []
    assert record.read_text() == "{}"


def test_compose_env_forwards_sandbox_settings_into_wsl(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "medverify-verify")
    monkeypatch.setenv("MEDVERIFY_HAPI_PORT", "28080")
    monkeypatch.setenv("WSLENV", "USERPROFILE/p")
    env = sandbox_common.compose_env()
    parts = env["WSLENV"].split(":")
    assert parts[0] == "USERPROFILE/p"
    assert {"COMPOSE_PROJECT_NAME", "MEDVERIFY_HAPI_PORT"} <= set(parts)


# ---------------------------------------------------------------- Inferno verdicts


SUITE = {
    "id": "suite",
    "version": "0.15.0",
    "test_groups": [
        {"id": "g1", "short_id": "1", "inputs": [{"name": "base_url"}],
         "tests": [{"id": "t1", "short_id": "1.01"}, {"id": "t2", "short_id": "1.02", "optional": True}]},
        {"id": "g2", "short_id": "2", "optional": True,
         "test_groups": [{"id": "g21", "short_id": "2.1", "tests": [{"id": "t3", "short_id": "2.1.01"}]}]},
    ],
}


def results(tests, groups=None):
    out = [{"test_id": test_id, "result": verdict} for test_id, verdict in tests.items()]
    for group_id, verdict in (groups or {"g1": "pass", "g2": "pass", "g21": "pass"}).items():
        out.append({"test_group_id": group_id, "result": verdict})
    return out


def classify(found):
    run = run_inferno.SuiteRun(kit=run_inferno.KITS["crd"], suite_id="s", kit_version="v1")
    run_inferno.classify(run, run_inferno.index_suite(SUITE), found, {"g1", "g2"})
    return run


def test_passes_omits_and_optional_skips_pass():
    run = classify(results({"t1": "pass", "t2": "skip", "t3": "omit"}))
    assert run.passed, run.problems
    assert run.counts == {"omit": 1, "pass": 1, "skip": 1}
    assert run.group_counts == {"pass": 3}


def test_required_leaf_without_a_verdict_fails():
    run = classify(results({"t1": None, "t2": "pass", "t3": "pass"}))
    assert not run.passed
    assert run.counts["no-verdict"] == 1
    assert any("1.01" in problem and "no verdict" in problem for problem in run.problems)


def test_failed_group_with_a_passing_child_fails():
    run = classify(results({"t1": "pass", "t2": "pass", "t3": "pass"}, {"g1": "fail", "g2": "pass", "g21": "pass"}))
    assert not run.passed
    assert run.problems == ["group 1 None: fail"]


def test_unknown_verdicts_and_missing_results_fail():
    run = classify(results({"t1": "passed", "t2": "pass"}))
    assert not run.passed
    assert run.counts == {"missing": 1, "pass": 1, "unknown": 1}


def test_required_skip_fails_but_optional_group_skip_passes():
    run = classify(results({"t1": "skip", "t2": "pass", "t3": "skip"}, {"g1": "skip", "g2": "skip", "g21": "skip"}))
    assert run.problems == ["group 1 None: required group skipped", "test 1.01 None: required test skipped"]


def test_group_order_must_cover_every_group():
    assert [g["id"] for g in run_inferno.ordered_groups(SUITE, ("2", "1"))] == ["g2", "g1"]
    assert [g["id"] for g in run_inferno.ordered_groups(SUITE, ())] == ["g1", "g2"]
    with pytest.raises(SandboxError, match="do not match"):
        run_inferno.ordered_groups(SUITE, ("1",))


def test_waiting_result_falls_back_to_the_deepest_waiting_group():
    run = {"results": [{"test_group_id": "a", "result": "wait"}, {"test_group_id": "a-b", "result": "wait"}]}
    assert run_inferno.waiting_result(run)["test_id"] == "a-b"
    assert run_inferno.waiting_result({"results": [{"test_id": "x", "result": "pass"}]}) is None


def test_reports_redact_tokens():
    text = f"token {TOKEN} Authorization: Bearer abc.def-ghi jwt eyJhbGciOi.eyJzdWIi.sig"
    redacted = run_inferno.redact({"message": text})["message"]
    assert TOKEN not in redacted and "abc.def-ghi" not in redacted and "eyJzdWIi" not in redacted


class FakeInferno:
    """The slice of the Inferno JSON API run_inferno.py uses, for all three kits at once."""

    def __init__(self, session_results):
        self.session_results = session_results
        self.runs = 0

    def __call__(self, method, url, body=None, **kwargs):
        path = url.split("/api", 1)[1] if "/api" in url else url
        if method == "GET" and path.startswith("/test_suites/"):
            kit = next(k for k in run_inferno.KITS.values() if url.startswith(k.base))
            return 200, dict(SUITE, version=LOCK["kits"][kit.key]["tag"].lstrip("v"))
        if method == "POST" and path == "/test_sessions":
            return 201, {"id": "session-1"}
        if method == "GET" and path.endswith("/session_data"):
            return 200, []
        if method == "POST" and path == "/test_runs":
            self.runs += 1
            return 201, {"id": f"run-{self.runs}"}
        if method == "GET" and path.startswith("/test_runs/"):
            return 200, {"status": "done", "results": []}
        if method == "GET" and path.endswith("/results"):
            return 200, self.session_results
        return 404, None


@pytest.fixture
def stubbed_kits(monkeypatch):
    monkeypatch.setattr(run_inferno, "KITS", run_inferno.replace_kits(group_order=(), resolve_inputs=lambda k, p: {}))
    monkeypatch.setattr(run_inferno, "hardware", lambda: {"CPU": "test"})
    monkeypatch.setattr(run_inferno, "candidate_identity", lambda: {"base commit": "test"})


def run_cli(tmp_path, session_results):
    doc = tmp_path / "absent" / "docs" / "CONFORMANCE.md"
    code = run_inferno.main(
        ["--crd", "2.2.1", "--dtr", "2.2.0", "--pas", "2.2.1", "--require-pass",
         "--report-dir", str(tmp_path / "reports"), "--conformance-doc", str(doc)],
        http_fn=FakeInferno(session_results),
    )
    return code, doc


def test_cli_passes_a_clean_run_and_creates_the_document(tmp_path, stubbed_kits):
    code, doc = run_cli(tmp_path, results({"t1": "pass", "t2": "skip", "t3": "omit"}))
    assert code == 0
    assert "Overall verdict: **pass**" in doc.read_text(encoding="utf-8")
    report = json.loads((tmp_path / "reports" / "pas.json").read_text(encoding="utf-8"))
    assert report["passed"] and report["counts"] == {"omit": 1, "pass": 1, "skip": 1}


@pytest.mark.parametrize(
    "session_results",
    [
        results({"t1": None, "t2": "pass", "t3": "pass"}),
        results({"t1": "pass", "t2": "pass", "t3": "pass"}, {"g1": "fail", "g2": "pass", "g21": "pass"}),
        results({"t1": "unexpected", "t2": "pass", "t3": "pass"}),
    ],
    ids=["leaf-without-verdict", "failed-group-passing-child", "unknown-verdict"],
)
def test_cli_exits_nonzero_on_false_pass_shapes(tmp_path, stubbed_kits, session_results):
    code, doc = run_cli(tmp_path, session_results)
    assert code == 1
    assert "Overall verdict: **fail**" in doc.read_text(encoding="utf-8")


def test_harness_error_keeps_partial_reports(tmp_path, stubbed_kits, monkeypatch):
    original = run_inferno.run_suite

    def flaky(kit, *args, **kwargs):
        if kit.key == "pas":

            def broken(*a, **k):
                return 500, "down"

            return original(kit, args[0], args[1], args[2], broken)
        return original(kit, *args, **kwargs)

    monkeypatch.setattr(run_inferno, "run_suite", flaky)
    code, doc = run_cli(tmp_path, results({"t1": "pass", "t2": "pass", "t3": "pass"}))
    assert code == 2
    crd = json.loads((tmp_path / "reports" / "crd.json").read_text(encoding="utf-8"))
    pas = json.loads((tmp_path / "reports" / "pas.json").read_text(encoding="utf-8"))
    assert crd["passed"] and not pas["passed"]
    assert pas["problems"][0].startswith("harness: ")
    assert "Overall verdict: **fail**" in doc.read_text(encoding="utf-8")


# ---------------------------------------------------------------- PAS handshake evidence


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def handshake(**changes):
    item = {"kind": "handshake", "delivered": True, "status": 200, "endpoint_token": TOKEN, "version": "v221",
            "subscription_id": "sub-1", "queued_at": "2026-11-23T12:00:05.000Z"}
    item.update(changes)
    return item


WAIT = {"status": "waiting", "results": [{
    "id": "r1", "result": "wait", "test_id": "pas_v221-subscriptions_r4_server_notification_delivery",
    "outputs": [{"name": "confirmation_url",
                 "value": "http://inferno-pas:4567/custom/subscriptions_r5_backport_r4_server/resume_pass?token=x"}],
}]}


def resolver(notifications, confirmation=(200, None)):
    recorder = Recorder([
        ("GET", "/admin/notifications", (200, {"notifications": notifications})),
        ("GET", "/custom/", confirmation),
    ])
    clock = Clock()
    inferno = run_inferno.Inferno(run_inferno.KITS["pas"], recorder)
    started = dt.datetime(2026, 11, 23, 12, 0, 0, tzinfo=dt.UTC)
    wait = run_inferno.WaitResolver(inferno, 240, started=started, clock=clock, sleep=clock.sleep,
                                    control="http://127.0.0.1:18085")
    return wait, recorder, clock


def confirmations(recorder):
    return [call for call in recorder.calls if "/custom/" in call[1]]


@pytest.mark.parametrize(
    "notifications",
    [
        [],
        [handshake(endpoint_token="token-of-an-earlier-run")],
        [handshake(queued_at="2026-11-23T11:00:00.000Z")],
        [handshake(delivered=False, status=None)],
        [handshake(status=500)],
        [handshake(kind="event-notification")],
    ],
    ids=["none", "other-run-token", "stale", "undelivered", "rejected", "not-handshake"],
)
def test_handshake_without_evidence_times_out_without_confirming(notifications):
    wait, recorder, clock = resolver(notifications)
    problem = wait.resolve(WAIT)
    assert problem is not None and "not confirmed" in problem
    assert confirmations(recorder) == []
    assert clock.now >= run_inferno.HANDSHAKE_SECONDS


def test_handshake_with_evidence_sends_exactly_one_confirmation():
    wait, recorder, _ = resolver([handshake(endpoint_token="other"), handshake()])
    assert wait.resolve(WAIT) is None
    assert wait.resolve(WAIT) is None
    calls = confirmations(recorder)
    assert len(calls) == 1
    assert calls[0][1].startswith("http://127.0.0.1:14569/custom/subscriptions_r5_backport_r4_server/resume_pass")
    assert wait.evidence["handshake"]["subscription_id"] == "sub-1"


def test_unsuccessful_confirmation_fails_the_wait():
    wait, recorder, _ = resolver([handshake()], confirmation=(500, "error"))
    problem = wait.resolve(WAIT)
    assert problem == "the subscription setup confirmation returned HTTP 500"
    assert len(confirmations(recorder)) == 1


def test_redirected_confirmation_passes_only_if_the_wait_clears():
    wait, recorder, clock = resolver([handshake()], confirmation=(302, None))
    assert wait.resolve(WAIT) is None
    assert len(confirmations(recorder)) == 1
    clock.now += run_inferno.HANDSHAKE_SECONDS * 2 + 1
    assert wait.resolve(WAIT) == "the subscription setup confirmation was sent but the wait did not resume"
    assert len(confirmations(recorder)) == 1


def test_confirmation_url_for_another_host_is_refused():
    run = json.loads(json.dumps(WAIT))
    run["results"][0]["outputs"][0]["value"] = "http://evil.example:4567/custom/x"
    wait, recorder, _ = resolver([handshake()])
    assert "is not this kit" in wait.resolve(run)
    assert confirmations(recorder) == []


def test_crd_hook_waits_resume_themselves_within_a_bound():
    clock = Clock()
    inferno = run_inferno.Inferno(run_inferno.KITS["crd"], Recorder())
    wait = run_inferno.WaitResolver(inferno, 240, started=dt.datetime.now(dt.UTC), clock=clock, sleep=clock.sleep)
    parked = {"status": "waiting", "results": [{
        "id": "r2", "result": "wait", "test_id": "crd_v221-Group01-crd_v221_server_invoke_hook_single_request_test"}]}
    assert wait.resolve(parked) is None
    clock.now += run_inferno.SELF_RESUME_SECONDS + 1
    assert "did not resume" in wait.resolve(parked)
    other = {"status": "waiting", "results": [{"id": "r3", "result": "wait", "test_id": "crd_v221-attestation"}]}
    assert wait.resolve(other).startswith("unexpected wait")
    assert inferno.http.calls == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_reset_script_parses_and_rejects_unknown_arguments():
    script = "scripts/reset_sandbox.sh"
    subprocess.run([shutil.which("bash"), "-n", script], cwd=ROOT, check=True)
    completed = subprocess.run(
        [shutil.which("bash"), script, "--unknown"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 2
    assert "usage" in completed.stderr


# ---------------------------------------------------------------- release inspection

# Built at run time so this file never contains the patterns the inspection looks for.
AGENT_NOTES = "memory" + "-bank"
CPT_URL = "http://www.ama-assn.org/go/" + "cpt"


def issuable_npi():
    body = "100300012"
    check = next(d for d in "0123456789" if inspect_release.luhn_npi(body + d))
    return body + check


def test_release_inspection_reports_locations_not_values(tmp_path, capsys):
    tree = tmp_path / "candidate"
    (tree / AGENT_NOTES).mkdir(parents=True)
    (tree / AGENT_NOTES / "notes.md").write_text("internal\n")
    npi = issuable_npi()
    fixture = {"npi": npi, "example": "1234567893", "synthetic": "9000000001",
               "code": {"system": CPT_URL, "code": "Y9001", "display": "descriptor"}}
    (tree / "fixture.json").write_text(json.dumps(fixture) + "\n")
    (tree / "clean.md").write_text("synthetic data only\n")
    assert inspect_release.main([str(tree)]) == 1
    output = capsys.readouterr().out
    assert f"{AGENT_NOTES}/notes.md: forbidden path" in output
    assert "fixture.json:1: check-digit-valid NPI" in output
    assert "fixture.json: CPT coding with a display" in output
    assert npi not in output and "descriptor" not in output
    assert "clean.md" not in output


def test_release_inspection_passes_clean_material(tmp_path):
    (tmp_path / "README.md").write_text("Synthetic procedures Y9001 to Y9120.\n")
    assert inspect_release.main([str(tmp_path)]) == 0
