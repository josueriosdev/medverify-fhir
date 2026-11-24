"""Run the official Inferno server suites against the sandbox payer servers over test TLS.

Each kit runs in its own Inferno container built from the commit pinned in
sandbox/upstreams.lock. The payer servers are reached only through https://*.medverify.test,
which the sandbox Caddy proxy serves with a test CA that the Inferno containers trust, so the
suites' TLS checks run against a real handshake. Test inputs come from the synthetic testdata
published with each Nirmitee server at its pinned commit.

The verdict fails closed. Every test in a selected group needs a result object with a known
verdict, and every group needs one too. Inferno's "omit" (the test does not apply) and a "skip"
of a test or group the kit marks optional are the only non-pass outcomes accepted; a missing,
null or unknown verdict, fail, error, cancel, wait, a skipped required test or group, a timed-out
or unresolved run, or a harness error fails the suite. Reports go to artifacts/inferno/ and the
summary is rendered into docs/CONFORMANCE.md, including partial results when a later suite fails.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sandbox_common import (
    PORTS,
    RESET_TIMINGS,
    ROOT,
    SANDBOX,
    SEED_MANIFEST,
    JSONObject,
    SandboxError,
    http,
    inferno_base,
    load_lock,
    pas_control,
    run_compose,
)

REPORT_ROOT = ROOT / "artifacts" / "inferno"
CONFORMANCE_DOC = ROOT / "docs" / "CONFORMANCE.md"
POLL_SECONDS = 3.0
HANDSHAKE_SECONDS = 90.0
SELF_RESUME_SECONDS = 600.0
KNOWN_VERDICTS = frozenset({"pass", "fail", "error", "skip", "omit", "cancel", "wait"})
# Inferno core result semantics: "omit" marks a test that does not apply to the system under
# test and never counts against it; "skip" means a test could not run. A skip is accepted only
# where the kit declares the test or group optional.
ACCEPTED = frozenset({"pass", "omit"})
# The token Inferno expects on the server's subscription notifications; fresh per run.
PAS_NOTIFICATION_TOKEN = secrets.token_urlsafe(24)

HttpFunction = Callable[..., tuple[int, Any]]


@dataclass(frozen=True)
class Kit:
    key: str
    title: str
    service: str
    internal_host: str
    source: str
    testdata: str
    group_order: tuple[str, ...]
    resolve_inputs: Callable[[Kit, Path], dict[str, str]]
    # Waits the kit ends by itself: the test parks while an Inferno job does the work, then the
    # job calls the run's resume URL. They get time to finish but are never resolved from here.
    self_resuming_waits: tuple[str, ...] = ()

    @property
    def base(self) -> str:
        return inferno_base(self.service)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def crd_inputs(kit: Kit, testdata: Path) -> dict[str, str]:
    spec = json.loads(read_text(testdata / "inputs.json"))
    # The Nirmitee hook bodies name a local Inferno at http://localhost; here Inferno is a
    # compose service, so the FHIR server and issuer URLs point at it by name.
    local = "http://localhost/custom/"
    internal = f"{kit.internal_host}/custom/"
    values: dict[str, str] = {}
    for name, value in spec["inputs"].items():
        if isinstance(value, str):
            text = value
        elif "file" in value:
            text = json.dumps(json.loads(read_text(testdata / value["file"])))
        else:
            text = json.dumps([json.loads(read_text(testdata / item)) for item in value["files"]])
        values[name] = text.replace(local, internal)
    values["base_url"] = "https://crd.medverify.test/v221"
    values["encryption_method"] = "ES384"
    return values


def dtr_inputs(kit: Kit, testdata: Path) -> dict[str, str]:
    spec = json.loads(read_text(testdata / "inputs.json"))
    server_base = "https://dtr.medverify.test"
    values: dict[str, str] = {}
    for name, value in spec["inputs"].items():
        if isinstance(value, dict):
            parsed = json.loads(read_text(testdata / value["file"]).replace("{{SERVER_BASE}}", server_base))
            values[name] = json.dumps(parsed) if value.get("serialize") else json.dumps(parsed, indent=2)
        else:
            values[name] = str(value).replace("{{SERVER_BASE}}", server_base)
    return values


def pas_inputs(kit: Kit, testdata: Path) -> dict[str, str]:
    spec = json.loads(read_text(testdata / "inputs.json"))
    variables = {"FHIR_BASE": "https://pas.medverify.test/v221/fhir", "NOTIFICATION_TOKEN": PAS_NOTIFICATION_TOKEN}
    values: dict[str, str] = {}
    for item in spec["inputs"]:
        if "file" in item:
            text = read_text(testdata / item["file"])
        else:
            text = item.get("value", "") if isinstance(item.get("value", ""), str) else json.dumps(item["value"])
        for key, value in variables.items():
            text = text.replace("${" + key + "}", value)
        values[item["name"]] = json.dumps(json.loads(text), indent=2) if "file" in item else text
    values["server_endpoint"] = variables["FHIR_BASE"]
    values["access_token"] = PAS_NOTIFICATION_TOKEN
    return values


KITS = {
    "crd": Kit("crd", "Da Vinci CRD Server", "inferno-crd", "http://inferno-crd:4567", "crd-server",
               "testdata/v221",
               # The hook groups run before the hook-response demonstration, as the Nirmitee
               # harness does, so group 2 can draw on the services exercised in group 3.
               ("1", "3", "2", "4"), crd_inputs,
               # The pinned kit's invoke-hook tests wait while a job sends the hook requests and,
               # with manual_continuation "no", resume as soon as the requests complete.
               ("_invoke_hook_test", "_invoke_hook_single_request_test")),
    "dtr": Kit("dtr", "Da Vinci DTR Payer Server", "inferno-dtr", "http://inferno-dtr:4567", "dtr-server",
               "testdata/v220", (), dtr_inputs),
    "pas": Kit("pas", "Da Vinci PAS Server", "inferno-pas", "http://inferno-pas:4567", "pas-server",
               "testdata/v221", (), pas_inputs),
}


@dataclass
class SuiteRun:
    kit: Kit
    suite_id: str
    kit_version: str
    ig_version: str = ""
    reported_version: str = ""
    session_id: str = ""
    started: str = ""
    finished: str = ""
    groups: list[JSONObject] = field(default_factory=list)
    tests: list[JSONObject] = field(default_factory=list)
    group_results: list[JSONObject] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    group_counts: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems and bool(self.tests)


class Inferno:
    def __init__(self, kit: Kit, http_fn: HttpFunction) -> None:
        self.kit = kit
        self.base = kit.base
        self.http = http_fn

    def api(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201)) -> Any:
        status, payload = self.http(method, f"{self.base}/api{path}", body,
                                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                                    timeout=120)
        if status not in ok:
            raise SandboxError(f"{self.kit.key}: {method} /api{path} returned HTTP {status}: {str(payload)[:400]}")
        return payload

    def local_callback(self, url: str) -> str:
        """Map a callback URL Inferno built from its compose host name onto the loopback port.

        Only this kit's own host and port, plain HTTP and a /custom/ path are accepted, so an
        output that names any other destination is refused instead of fetched.
        """
        parts = urllib.parse.urlsplit(url)
        internal = urllib.parse.urlsplit(self.kit.internal_host)
        if (parts.scheme, parts.hostname, parts.port) != (internal.scheme, internal.hostname, internal.port):
            raise SandboxError(f"{self.kit.key}: callback {parts.scheme}://{parts.hostname}:{parts.port} is not this kit")
        if not parts.path.startswith("/custom/") or parts.username or parts.password or parts.fragment:
            raise SandboxError(f"{self.kit.key}: callback path {parts.path} is not an Inferno custom route")
        local = urllib.parse.urlsplit(self.base)
        return urllib.parse.urlunsplit((local.scheme, local.netloc, parts.path, parts.query, ""))


def index_suite(suite: JSONObject) -> dict[str, JSONObject]:
    index: dict[str, JSONObject] = {}

    def walk(group: JSONObject, top: str, optional: bool) -> None:
        group_optional = optional or bool(group.get("optional"))
        index[group["id"]] = {"kind": "group", "top": top, "short_id": group.get("short_id"),
                              "title": group.get("title"), "optional": group_optional}
        for test in group.get("tests") or []:
            index[test["id"]] = {"kind": "test", "top": top, "short_id": test.get("short_id"),
                                 "title": test.get("title"), "optional": group_optional or bool(test.get("optional"))}
        for child in group.get("test_groups") or []:
            walk(child, top, group_optional)

    for group in suite.get("test_groups") or []:
        walk(group, group["id"], False)
    return index


def ordered_groups(suite: JSONObject, order: tuple[str, ...]) -> list[JSONObject]:
    groups: list[JSONObject] = list(suite.get("test_groups") or [])
    if not order:
        return groups
    by_short = {str(group.get("short_id")): group for group in groups}
    if set(order) != set(by_short):
        raise SandboxError(f"suite groups {sorted(by_short)} do not match the configured order {list(order)}")
    return [by_short[short_id] for short_id in order]


def group_inputs(inferno: Inferno, session_id: str, group: JSONObject, testdata: dict[str, str]) -> list[JSONObject]:
    """Prefer values already in the session, which hold outputs of earlier groups such as tokens."""
    session = {item["name"]: item.get("value")
               for item in inferno.api("GET", f"/test_sessions/{session_id}/session_data") or []}
    inputs = []
    for name in [item["name"] for item in group.get("inputs") or []]:
        value = session.get(name)
        if value in (None, "", "null"):
            value = testdata.get(name)
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        if value not in (None, ""):
            inputs.append({"name": name, "value": value})
    return inputs


def waiting_result(run: JSONObject) -> JSONObject | None:
    waits = [result for result in run.get("results") or [] if result.get("result") == "wait"]
    tests = [result for result in waits if result.get("test_id")]
    if tests:
        first: JSONObject = tests[0]
        return first
    groups = [result for result in waits if result.get("test_group_id")]
    if not groups:
        return None
    deepest = dict(max(groups, key=lambda result: len(result["test_group_id"])))
    deepest["test_id"] = deepest["test_group_id"]
    return deepest


def parse_instant(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


class WaitResolver:
    """Resolves the two waits a PAS server suite run reaches without a person at the browser.

    The subscription-setup wait is confirmed only after the payer's own delivery log shows a
    successful handshake that carries this run's notification token and was queued after this
    run began. Without that evidence, or when the confirmation is not accepted, the wait is
    reported as a failure and no confirmation is sent.
    """

    def __init__(
        self,
        inferno: Inferno,
        notification_timeout: float,
        *,
        started: dt.datetime,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        control: str | None = None,
        handshake_timeout: float = HANDSHAKE_SECONDS,
        self_resume_timeout: float = SELF_RESUME_SECONDS,
    ) -> None:
        self.inferno = inferno
        self.http = inferno.http
        self.notification_timeout = notification_timeout
        self.handshake_timeout = handshake_timeout
        self.self_resume_timeout = self_resume_timeout
        self.started = started
        self.clock = clock
        self.sleep = sleep
        self.control = control or pas_control()
        self.first_seen: dict[str, float] = {}
        self.evidence: dict[str, Any] = {}

    def resolve(self, run: JSONObject) -> str | None:
        """Return None while the run can keep going, or the reason it cannot."""
        result = waiting_result(run)
        if result is None:
            return None
        test_id = str(result.get("test_id") or "")
        key = str(result.get("id") or test_id)
        first = key not in self.first_seen
        since = self.first_seen.setdefault(key, self.clock())
        if test_id.endswith(self.inferno.kit.self_resuming_waits):
            if self.clock() - since > self.self_resume_timeout:
                return f"the kit did not resume {test_id} within {self.self_resume_timeout:.0f} s"
            return None
        if self.inferno.kit.key != "pas":
            return f"unexpected wait at {test_id}: {str(result.get('result_message') or '')[:300]}"
        if test_id.endswith(("subscriptions_r4_server_notification_delivery", "subscription_setup")):
            if first:
                return self.confirm_handshake(result)
            # Inferno answers resume_pass with a redirect to its session page, so the proof that
            # the confirmation worked is the wait clearing, not the status code.
            if self.clock() - since > self.handshake_timeout * 2:
                return "the subscription setup confirmation was sent but the wait did not resume"
            return None
        if "subscription_notification_wait" in test_id or "pended_use_case" in test_id:
            if first:
                status, _ = self.http("POST", f"{self.control}/admin/pended/finalize", {"version": "v221"},
                                      headers={"Content-Type": "application/json"})
                self.evidence.setdefault("pended_finalize_http", []).append(status)
                print(f"  pended finalize requested: HTTP {status}", flush=True)
            if self.clock() - since > self.notification_timeout:
                return f"no notification resumed {test_id} within {self.notification_timeout:.0f} s"
            return None
        return f"unexpected wait at {test_id}: {str(result.get('result_message') or '')[:300]}"

    def handshake_evidence(self) -> JSONObject | None:
        status, body = self.http("GET", f"{self.control}/admin/notifications")
        if status != 200 or not isinstance(body, dict):
            return None
        floor = self.started - dt.timedelta(seconds=5)
        for item in body.get("notifications") or []:
            if not isinstance(item, dict):
                continue
            delivered_status = item.get("status")
            queued = parse_instant(item.get("queued_at"))
            if (
                item.get("kind") == "handshake"
                and item.get("delivered") is True
                and isinstance(delivered_status, int)
                and 200 <= delivered_status < 300
                and item.get("endpoint_token") == PAS_NOTIFICATION_TOKEN
                and item.get("version") == "v221"
                and isinstance(item.get("subscription_id"), str)
                and item["subscription_id"]
                and queued is not None
                and queued >= floor
            ):
                return item
        return None

    def confirm_handshake(self, result: JSONObject) -> str | None:
        deadline = self.clock() + self.handshake_timeout
        evidence = self.handshake_evidence()
        while evidence is None and self.clock() < deadline:
            self.sleep(POLL_SECONDS)
            evidence = self.handshake_evidence()
        if evidence is None:
            return (f"no delivered handshake for this run's subscription within {self.handshake_timeout:.0f} s; "
                    "the subscription setup wait was not confirmed")
        url = next((o.get("value") for o in result.get("outputs") or [] if o.get("name") == "confirmation_url"), None)
        if not isinstance(url, str) or not url.startswith("http"):
            token = urllib.parse.quote(f"notification {PAS_NOTIFICATION_TOKEN}")
            url = f"{self.inferno.kit.internal_host}/custom/subscriptions_r5_backport_r4_server/resume_pass?token={token}"
        try:
            local = self.inferno.local_callback(url)
        except SandboxError as error:
            return str(error)
        status, _ = self.http("GET", local, headers={"Accept": "*/*"})
        self.evidence["handshake"] = {
            "subscription_id": evidence["subscription_id"],
            "queued_at": evidence.get("queued_at"),
            "delivery_http": evidence.get("status"),
            "confirmation_http": status,
        }
        if not 200 <= status < 400:
            return f"the subscription setup confirmation returned HTTP {status}"
        print(f"  handshake for subscription {evidence['subscription_id']} confirmed", flush=True)
        return None


def run_group(inferno: Inferno, session_id: str, group: JSONObject, testdata: dict[str, str],
              timeout: float, resolver: WaitResolver, clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> JSONObject:
    body = {"test_session_id": session_id, "test_group_id": group["id"],
            "inputs": group_inputs(inferno, session_id, group, testdata)}
    started = clock()
    while True:
        status, run = inferno.http("POST", f"{inferno.base}/api/test_runs", body,
                                   headers={"Accept": "application/json", "Content-Type": "application/json"})
        if status == 409 and clock() - started < 120:
            sleep(POLL_SECONDS)
            continue
        if status not in (200, 201) or not isinstance(run, dict):
            raise SandboxError(f"starting {group['id']} returned HTTP {status}: {str(run)[:400]}")
        break
    run_id = run["id"]
    outcome = "done"
    while True:
        run = inferno.api("GET", f"/test_runs/{run_id}?include_results=true")
        if run.get("status") == "done":
            break
        if run.get("status") == "waiting":
            problem = resolver.resolve(run)
            if problem is not None:
                outcome = problem
                break
        if clock() - started > timeout:
            outcome = f"timed out after {timeout:.0f} s"
            break
        sleep(POLL_SECONDS)
    if outcome != "done":
        inferno.api("DELETE", f"/test_runs/{run_id}", ok=(200, 204))
    return {"short_id": group.get("short_id"), "id": group["id"], "title": group.get("title"),
            "run_id": run_id, "completed": outcome == "done", "outcome": outcome,
            "seconds": round(clock() - started, 1)}


def classify(run: SuiteRun, index: dict[str, JSONObject], results: list[JSONObject], selected: set[str]) -> None:
    """Record a verdict for every expected test and group; anything not clearly acceptable fails."""
    latest: dict[str, JSONObject] = {}
    for result in sorted(results, key=lambda r: str(r.get("updated_at") or r.get("created_at") or "")):
        runnable = result.get("test_id") or result.get("test_group_id")
        if runnable:
            latest[runnable] = result
    tests: Counter[str] = Counter()
    groups: Counter[str] = Counter()
    for runnable, meta in index.items():
        if meta["top"] not in selected:
            continue
        found = latest.get(runnable)
        verdict = found.get("result") if found else None
        if found is None:
            outcome, reason = "missing", "no result recorded"
        elif verdict is None or verdict == "":
            outcome, reason = "no-verdict", "result has no verdict"
        elif verdict not in KNOWN_VERDICTS:
            outcome, reason = "unknown", f"unknown verdict {verdict!r}"
        else:
            outcome, reason = str(verdict), ""
        if outcome in ACCEPTED or (outcome == "skip" and meta["optional"]):
            accepted = True
        else:
            accepted = False
            if outcome == "skip":
                reason = f"required {meta['kind']} skipped"
            elif not reason:
                reason = outcome
        entry = {"id": runnable, "short_id": meta["short_id"], "title": meta["title"], "result": outcome,
                 "optional": meta["optional"], "accepted": accepted,
                 "message": (found or {}).get("result_message")}
        if meta["kind"] == "test":
            tests[outcome] += 1
            run.tests.append(entry)
        else:
            groups[outcome] += 1
            run.group_results.append(entry)
        if not accepted:
            label = "group" if meta["kind"] == "group" else "test"
            run.problems.append(f"{label} {meta['short_id']} {meta['title']}: {reason}")
    run.counts = dict(sorted(tests.items()))
    run.group_counts = dict(sorted(groups.items()))


def run_suite(kit: Kit, lock: JSONObject, timeout: float, notification_timeout: float, http_fn: HttpFunction,
              clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
              *, serial_compose: bool = False) -> SuiteRun:
    locked = lock["kits"][kit.key]
    run = SuiteRun(kit=kit, suite_id=locked["suite"], kit_version=locked["tag"],
                   ig_version=locked["implementation_guide_version"])
    services = (f"validator-{kit.key}", kit.service, f"{kit.service}-worker")
    try:
        if serial_compose:
            print(f"{kit.key}: starting Inferno and its validator", flush=True)
            run_compose("up", "--build", "-d", "--wait", "--wait-timeout", "1800", *services, timeout=2100)
        inferno = Inferno(kit, http_fn)
        suite = inferno.api("GET", f"/test_suites/{run.suite_id}")
        run.reported_version = str(suite.get("version") or "")
        if run.reported_version and f"v{run.reported_version.removeprefix('v')}" != locked["tag"]:
            raise SandboxError(f"Inferno reports kit {run.reported_version}, lock pins {locked['tag']}")
        testdata_dir = SANDBOX / lock["sources"][kit.source]["directory"] / kit.testdata
        testdata = kit.resolve_inputs(kit, testdata_dir)
        index = index_suite(suite)
        groups = ordered_groups(suite, kit.group_order)
        session = inferno.api("POST", "/test_sessions", {"test_suite_id": run.suite_id})
        run.session_id = session["id"]
        started = dt.datetime.now(dt.UTC)
        run.started = started.isoformat(timespec="seconds")
        resolver = WaitResolver(inferno, notification_timeout, started=started, clock=clock, sleep=sleep)
        print(f"{kit.key}: session {run.session_id} on {run.suite_id} ({locked['tag']})", flush=True)
        for group in groups:
            print(f"  group {group.get('short_id')} {group.get('title')}", flush=True)
            record = run_group(inferno, run.session_id, group, testdata, timeout, resolver, clock, sleep)
            print(f"    {record['outcome']} in {record['seconds']} s", flush=True)
            run.groups.append(record)
            if not record["completed"]:
                run.problems.append(f"run of group {record['short_id']} {record['title']}: {record['outcome']}")
        run.evidence = resolver.evidence
        results = inferno.api("GET", f"/test_sessions/{run.session_id}/results") or []
        if not isinstance(results, list):
            raise SandboxError("session results are not a list")
        classify(run, index, results, {group["id"] for group in groups})
    except (SandboxError, subprocess.TimeoutExpired) as error:
        run.problems.append(f"harness: {error}")
    finally:
        if serial_compose:
            try:
                run_compose("stop", *services, timeout=120)
            except (SandboxError, subprocess.TimeoutExpired) as error:
                run.problems.append(f"harness: could not stop {kit.key} services: {error}")
    run.finished = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    return run


SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),
)


def redact(value: Any) -> Any:
    """Strip this run's notification token, bearer tokens and JWTs from anything written out."""
    if isinstance(value, str):
        text = value.replace(PAS_NOTIFICATION_TOKEN, "[redacted]")
        text = SECRET_PATTERNS[0].sub(r"\1[redacted]", text)
        return SECRET_PATTERNS[1].sub("[redacted-jwt]", text)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def docker_engine() -> JSONObject:
    docker = shutil.which("docker")
    if docker is None:
        return {}
    completed = subprocess.run([docker, "info", "--format", "{{json .}}"], capture_output=True, text=True,
                               timeout=60, check=False)
    if completed.returncode != 0:
        return {}
    info: JSONObject = json.loads(completed.stdout)
    return info


def cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in read_text(cpuinfo).splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    if sys.platform == "win32":
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    return platform.processor() or platform.machine()


def hardware() -> dict[str, str]:
    engine = docker_engine()
    memory = engine.get("MemTotal")
    return {
        "CPU": cpu_model(),
        "Docker engine CPUs": str(engine.get("NCPU", os.cpu_count())),
        "Docker engine memory": f"{int(memory) / 2**30:.1f} GiB" if isinstance(memory, int) else "unknown",
        "Docker engine OS": str(engine.get("OperatingSystem", "unknown")),
        "Kernel": str(engine.get("KernelVersion", platform.release())),
        "Docker": str(engine.get("ServerVersion", "unknown")),
        "Runner OS": platform.platform(),
    }


def candidate_identity() -> dict[str, str]:
    """Identify the tree under test: its base commit and a digest of every candidate file."""

    def git(*args: str) -> str:
        completed = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
        return completed.stdout.strip() if completed.returncode == 0 else ""

    files = sorted(
        path for path in git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        if path != "docs/CONFORMANCE.md" and (ROOT / path).is_file()
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.encode() + b"\0" + hashlib.sha256((ROOT / path).read_bytes()).hexdigest().encode() + b"\n")
    return {
        "base commit": os.environ.get("MEDVERIFY_CANDIDATE_BASE") or git("rev-parse", "HEAD") or "unknown",
        "working tree": "clean"
        if not git("status", "--porcelain", "--", ".", ":(exclude)docs/CONFORMANCE.md")
        else "uncommitted changes present",
        "files": str(len(files)),
        "content digest": digest.hexdigest() if files else "unknown",
    }


def write_reports(runs: list[SuiteRun], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for run in runs:
        report = {
            "kit": run.kit.key, "suite_id": run.suite_id, "kit_version": run.kit_version,
            "implementation_guide_version": run.ig_version, "reported_version": run.reported_version,
            "session_id": run.session_id, "started": run.started, "finished": run.finished,
            "passed": run.passed, "counts": run.counts, "group_counts": run.group_counts, "groups": run.groups,
            "evidence": run.evidence, "problems": run.problems, "group_results": run.group_results,
            "tests": run.tests,
        }
        (directory / f"{run.kit.key}.json").write_text(json.dumps(redact(report), indent=2) + "\n", encoding="utf-8")


def optional_json(path: Path) -> JSONObject:
    if not path.is_file():
        return {}
    value: JSONObject = json.loads(read_text(path))
    return value


def render_conformance(runs: list[SuiteRun], lock: JSONObject, stamp: str, report_dir: Path,
                       identity: dict[str, str], machine: dict[str, str]) -> str:
    seed = optional_json(SEED_MANIFEST)
    resets = optional_json(RESET_TIMINGS)
    verdict = "pass" if runs and all(run.passed for run in runs) else "fail"
    try:
        reports = report_dir.relative_to(ROOT).as_posix()
    except ValueError:
        reports = report_dir.as_posix()
    lines = [
        "# Conformance",
        "",
        "Results of the official Inferno server test suites against the sandbox payer servers, written by",
        "`scripts/run_inferno.py` from an actual local run. The suites exercise the Nirmitee Go CRD, DTR and",
        "PAS servers built from the commits below; they do not exercise MedVerify's Rust PAS validator, which",
        "has its own gate in `cargo test` (crates/medverify-core/tests/pas.rs). The suites reach each server",
        "over TLS signed by a sandbox test CA. Passing Inferno is a conformance signal for these pinned",
        "builds, not a certification.",
        "",
        f"Overall verdict: **{verdict}**. Run finished {stamp} (UTC). Detailed reports: `{reports}/`.",
        "",
        "A suite passes only when every test and group has a known verdict that is pass, omit (does not",
        "apply), or a skip of something the kit marks optional. A missing or unknown verdict, failure,",
        "error, cancellation, timeout, unresolved wait or required skip fails it.",
        "",
        "## Candidate",
        "",
        "| Item | Value |",
        "|---|---|",
    ]
    lines += [f"| {key.capitalize()} | {value} |" for key, value in identity.items()]
    lines += [
        "",
        "## Results",
        "",
        "| Suite | IG | Kit | Server commit | Tests | Pass | Omit | Skip | Fail | Error | Other | Groups | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        counts = run.counts
        other = sum(v for k, v in counts.items() if k not in ("pass", "omit", "skip", "fail", "error"))
        commit = lock["sources"][run.kit.source]["commit"][:12]
        lines.append(
            f"| {run.suite_id} | {run.ig_version} | {run.kit_version} | {commit} | {sum(counts.values())} | "
            f"{counts.get('pass', 0)} | {counts.get('omit', 0)} | {counts.get('skip', 0)} | {counts.get('fail', 0)} | "
            f"{counts.get('error', 0)} | {other} | {sum(run.group_counts.values())} | "
            f"{'pass' if run.passed else 'fail'} |"
        )
    for run in runs:
        omitted = [t for t in run.tests if t["result"] == "omit"]
        skipped = [t for t in run.tests + run.group_results if t["result"] == "skip" and t["accepted"]]
        if run.problems or omitted or skipped:
            lines += ["", f"### {run.kit.title} details", ""]
            lines += [f"- Problem: {problem}" for problem in run.problems]
            lines += [f"- Omitted (does not apply): {t['short_id']} {t['title']}" for t in omitted]
            lines += [f"- Optional, skipped: {t['short_id']} {t['title']}" for t in skipped]
    lines += ["", "## Timings", "", "| Step | Seconds |", "|---|---|"]
    for key, seconds in (seed.get("seconds") or {}).items():
        lines.append(f"| Seed: {key} | {seconds} |")
    for cycle in resets.get("cycles") or []:
        lines.append(f"| Reset cycle {cycle['cycle']} | {cycle['reset_seconds']} |")
    for run in runs:
        for group in run.groups:
            lines.append(f"| {run.kit.key.upper()} group {group['short_id']} | {group['seconds']} |")
    lines += ["", "## Hardware", "", "| Item | Value |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in machine.items()]
    if seed:
        lines += [
            "",
            "## Seed data",
            "",
            (
                f"{seed['patients']} synthetic {seed['state']} patients from Synthea {seed['synthea']}, seed "
                f"{seed['seed']}, clinician seed {seed['clinician_seed']}, simulation date {seed['reference_date']}."
            ),
            f"Server fingerprint `{seed['fingerprint']}`.",
        ]
        if resets.get("cycles"):
            lines.append(
                f"{len(resets['cycles'])} reset cycles each removed planted mutations and restored that fingerprint "
                f"and {resets['expected_patients']} patients."
            )
    return str(redact("\n".join(lines) + "\n"))


def main(argv: list[str] | None = None, http_fn: HttpFunction = http) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--crd", required=True, help="CRD implementation guide version")
    parser.add_argument("--dtr", required=True, help="DTR implementation guide version")
    parser.add_argument("--pas", required=True, help="PAS implementation guide version")
    parser.add_argument("--require-pass", action="store_true", help="exit 1 unless every suite passes")
    parser.add_argument("--serial-compose", action="store_true", help="start and stop each kit's Compose services")
    parser.add_argument("--group-timeout", type=float, default=1800.0)
    parser.add_argument("--notification-timeout", type=float, default=240.0)
    parser.add_argument("--report-dir", type=Path, default=None, help="default: artifacts/inferno/<UTC stamp>")
    parser.add_argument("--conformance-doc", type=Path, default=CONFORMANCE_DOC)
    parser.add_argument("--no-conformance-doc", action="store_true", help="leave the conformance document unchanged")
    args = parser.parse_args(argv)

    try:
        lock = load_lock()
        requested = {"crd": args.crd, "dtr": args.dtr, "pas": args.pas}
        for key, version in requested.items():
            locked = lock["kits"][key]["implementation_guide_version"]
            if version != locked:
                raise SandboxError(f"{key.upper()} {version} requested; the sandbox pins {locked}")
        for key in requested:
            if KITS[key].service not in PORTS:
                raise SandboxError(f"no published port for {KITS[key].service}")
    except SandboxError as error:
        print(f"run_inferno: {error}", file=sys.stderr)
        return 2

    started = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    report_dir = args.report_dir or REPORT_ROOT / started
    runs: list[SuiteRun] = []
    for key in requested:
        run = run_suite(KITS[key], lock, args.group_timeout, args.notification_timeout, http_fn,
                        serial_compose=args.serial_compose)
        runs.append(run)
        write_reports([run], report_dir)
        print(f"{run.suite_id}: {'pass' if run.passed else 'fail'} tests {run.counts} groups {run.group_counts}",
              flush=True)
        for problem in run.problems:
            print(f"  {redact(problem)}", flush=True)
        # After a Compose or harness error, a previous kit may still be using the runner.
        if args.serial_compose and any(problem.startswith("harness: ") for problem in run.problems):
            break
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    if not args.no_conformance_doc:
        args.conformance_doc.parent.mkdir(parents=True, exist_ok=True)
        document = render_conformance(runs, lock, stamp, report_dir, candidate_identity(), hardware())
        args.conformance_doc.write_text(document, encoding="utf-8")
    print(f"reports: {report_dir}")
    if any(problem.startswith("harness: ") for run in runs for problem in run.problems):
        return 2
    if args.require_pass and not all(run.passed for run in runs):
        return 1
    return 0


def replace_kits(**changes: Any) -> dict[str, Kit]:
    """Copies of the kit table with fields replaced; used by tests to stub inputs."""
    return {key: dataclasses.replace(kit, **changes) for key, kit in KITS.items()}


if __name__ == "__main__":
    sys.exit(main())
