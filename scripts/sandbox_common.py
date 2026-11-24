"""Shared helpers for the sandbox scripts: paths, ports, the upstream lock, HTTP, Docker and FHIR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SANDBOX = ROOT / "sandbox"
# Relative on purpose: Docker may run in another environment (WSL, a CI runner) that only
# shares the working directory with this process, never a Windows absolute path.
COMPOSE_FILE = "sandbox/docker-compose.yaml"
LOCK_FILE = SANDBOX / "upstreams.lock"
USER_AGENT = "medverify-sandbox/0.1"
DEFAULT_PROJECT = "medverify-sandbox"

JSONObject = dict[str, Any]

# Host port variables, their defaults, and the container port each one publishes. The compose
# file reads the same variables, so a disposable stack moves every binding together.
PORTS: dict[str, tuple[str, int, int]] = {
    "hapi": ("MEDVERIFY_HAPI_PORT", 18080, 8080),
    "pas": ("MEDVERIFY_PAS_PORT", 18085, 8085),
    "tls": ("MEDVERIFY_TLS_PORT", 18443, 443),
    "inferno-crd": ("MEDVERIFY_INFERNO_CRD_PORT", 14567, 4567),
    "inferno-dtr": ("MEDVERIFY_INFERNO_DTR_PORT", 14568, 4567),
    "inferno-pas": ("MEDVERIFY_INFERNO_PAS_PORT", 14569, 4567),
}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOOPBACK_BINDINGS = frozenset({"127.0.0.1", "::1"})
# Only these services may mount Caddy's data volume, which holds the CA signing keys.
CA_KEY_HOLDERS = frozenset({"tls", "ca-export"})


class SandboxError(RuntimeError):
    """A sandbox step failed in a way the caller should report and exit on."""


def port(service: str) -> int:
    variable, default, _ = PORTS[service]
    raw = os.environ.get(variable)
    if raw is None or raw == "":
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise SandboxError(f"{variable}={raw!r} is not a TCP port")
    return int(raw)


def project_name() -> str:
    name = os.environ.get("COMPOSE_PROJECT_NAME") or DEFAULT_PROJECT
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise SandboxError(f"COMPOSE_PROJECT_NAME={name!r} is not a valid compose project name")
    return name


# Seed and reset records belong to one compose project, so a disposable stack never reads or
# overwrites the records of a developer stack in the same checkout.
STATE_DIR = ROOT / ".cache" / "sandbox" / project_name()
SEED_MANIFEST = STATE_DIR / "seed.json"
RESET_TIMINGS = STATE_DIR / "reset.json"


def hapi_base() -> str:
    return os.environ.get("MEDVERIFY_HAPI_BASE") or f"http://127.0.0.1:{port('hapi')}/fhir"


def pas_control() -> str:
    return os.environ.get("MEDVERIFY_PAS_CONTROL") or f"http://127.0.0.1:{port('pas')}"


def inferno_base(service: str) -> str:
    return f"http://127.0.0.1:{port(service)}"


def load_lock() -> JSONObject:
    with LOCK_FILE.open("rb") as handle:
        lock = tomllib.load(handle)
    if lock.get("version") != 1:
        raise SandboxError(f"{LOCK_FILE}: unsupported lock version {lock.get('version')!r}")
    return lock


def compose_env() -> dict[str, str]:
    """Environment for Docker calls, forwarding sandbox settings through WSLENV.

    When docker is a shim into a WSL distro, variables reach the Linux side only if WSLENV
    names them; without that a disposable stack's project name and ports would be dropped.
    """
    env = dict(os.environ)
    env["COMPOSE_PROJECT_NAME"] = project_name()
    forwarded = [name for name in env if name == "COMPOSE_PROJECT_NAME" or name.startswith("MEDVERIFY_")]
    current = [part for part in env.get("WSLENV", "").split(":") if part]
    names = {part.split("/", 1)[0] for part in current}
    env["WSLENV"] = ":".join(current + [name for name in sorted(forwarded) if name not in names])
    return env


def compose_command(*args: str) -> list[str]:
    docker = shutil.which("docker")
    if docker is None:
        raise SandboxError("docker is not on PATH")
    return [docker, "compose", "-p", project_name(), "-f", COMPOSE_FILE, *args]


def run_compose(*args: str, capture: bool = False, timeout: float | None = None) -> bytes:
    """Run docker compose from the repository root and return stdout when capture is set."""
    completed = subprocess.run(
        compose_command(*args),
        cwd=ROOT,
        env=compose_env(),
        stdout=subprocess.PIPE if capture else None,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise SandboxError(f"docker compose {' '.join(args)} exited with {completed.returncode}")
    return completed.stdout if capture else b""


def bash_command(script: Path, *args: str) -> list[str]:
    # On Windows, a bare "bash" can resolve to the WSL launcher in System32 before Git Bash;
    # shutil.which follows PATH order the way an interactive shell does.
    bash = shutil.which("bash")
    if bash is None:
        raise SandboxError("bash is not on PATH")
    return [bash, str(script.relative_to(ROOT)).replace(os.sep, "/"), *args]


def check_destination(url: str) -> None:
    """Refuse any host-side request that is not for a published sandbox port on loopback."""
    parts = urllib.parse.urlsplit(url)
    allowed = {port(service) for service in PORTS}
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise SandboxError(f"refusing request to {parts.scheme}://{parts.hostname}: not a plain sandbox URL")
    if parts.hostname not in LOOPBACK_HOSTS or parts.port not in allowed:
        raise SandboxError(f"refusing request to {parts.hostname}:{parts.port}: not a sandbox port on loopback")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Return 3xx responses to the caller instead of following them anywhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def http(
    method: str,
    url: str,
    body: Any = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, Any]:
    """Send one request and return the status with the JSON body, the raw text, or None.

    HTTP error statuses, including unfollowed redirects, are returned, not raised; transport
    failures and destinations outside the sandbox raise SandboxError.
    """
    check_destination(url)
    data: bytes | None = None
    request_headers = {"Accept": "application/fhir+json, application/json", "User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/fhir+json")
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    except (urllib.error.URLError, OSError) as error:
        raise SandboxError(f"{method} {url} failed: {error}") from error
    if not raw:
        return status, None
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def expect(status: int, payload: Any, wanted: Iterable[int], what: str) -> Any:
    if status not in set(wanted):
        raise SandboxError(f"{what} returned HTTP {status}: {str(payload)[:400]}")
    return payload


def wait_until_ready(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            status, _ = http("GET", url, timeout=10)
        except SandboxError as error:
            last = str(error)
        else:
            if status == 200:
                return
            last = f"HTTP {status}"
        time.sleep(3)
    raise SandboxError(f"{url} not ready after {timeout:.0f} s ({last})")


def published_address(service: str) -> str:
    _, _, container_port = PORTS[service]
    output = run_compose("port", service, str(container_port), capture=True, timeout=120).decode().strip()
    if not output:
        raise SandboxError(f"{service} publishes no port in compose project {project_name()}")
    return output.splitlines()[0]


def verify_sandbox_target(
    targets: Mapping[str, str],
    published: Callable[[str], str] | None = None,
) -> None:
    """Prove every endpoint about to be written is this compose project's published port.

    The URL must be loopback on the port configured for the service, and Docker must report
    that the service of this project publishes exactly that address. A stray base URL, or a
    second stack that happens to share a port, fails here before the first write.
    """
    for service, url in targets.items():
        parts = urllib.parse.urlsplit(url)
        expected = port(service)
        if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS or parts.port != expected:
            raise SandboxError(f"{service} endpoint {url} is not the sandbox port 127.0.0.1:{expected}")
        address = (published or published_address)(service)
        host, _, published_port = address.rpartition(":")
        if host.strip("[]") not in LOOPBACK_BINDINGS or published_port != str(expected):
            raise SandboxError(
                f"compose project {project_name()} publishes {service} at {address}, not 127.0.0.1:{expected}"
            )


def rendered_config() -> JSONObject:
    config: JSONObject = json.loads(run_compose("config", "--format", "json", capture=True, timeout=120))
    return config


def check_published_ports(config: Mapping[str, Any]) -> list[str]:
    """Every published binding in the rendered config must name a loopback host address."""
    problems: list[str] = []
    for name, service in sorted((config.get("services") or {}).items()):
        if service.get("network_mode") == "host":
            problems.append(f"{name} uses host networking")
        for binding in service.get("ports") or []:
            if not isinstance(binding, dict):
                problems.append(f"{name} has an unrendered port binding {binding!r}")
                continue
            host_ip = binding.get("host_ip")
            if binding.get("published") in (None, ""):
                problems.append(f"{name} publishes container port {binding.get('target')} on a random host port")
            if host_ip not in LOOPBACK_BINDINGS:
                problems.append(f"{name} publishes {binding.get('published')} on {host_ip or 'every interface'}")
    return problems


def check_secret_mounts(config: Mapping[str, Any]) -> list[str]:
    """Only the TLS proxy and the CA exporter may mount the volume holding the CA keys."""
    problems: list[str] = []
    for name, service in sorted((config.get("services") or {}).items()):
        for volume in service.get("volumes") or []:
            if isinstance(volume, dict) and volume.get("source") == "caddy-data" and name not in CA_KEY_HOLDERS:
                problems.append(f"{name} mounts caddy-data, which holds the CA signing keys")
    return problems


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def search_all(base: str, resource_type: str, params: Sequence[tuple[str, str]] = ()) -> Iterator[JSONObject]:
    """Yield every match of a search, following next links rebased onto the given base."""
    query = urllib.parse.urlencode([("_count", "1000"), ("_sort", "_id"), *params])
    url: str | None = f"{base}/{resource_type}?{query}"
    while url is not None:
        status, bundle = http("GET", url, timeout=120)
        expect(status, bundle, (200,), f"search {resource_type}")
        for entry in bundle.get("entry") or []:
            resource = entry.get("resource") or {}
            if resource.get("resourceType") == resource_type:
                yield resource
        url = None
        for link in bundle.get("link") or []:
            if link.get("relation") == "next":
                # HAPI builds paging links from the Host it saw, which differs when the
                # server sits behind a forwarded port.
                url = f"{base}?{urllib.parse.urlsplit(link['url']).query}"


def server_fingerprint(base: str, resource_types: Iterable[str]) -> tuple[str, dict[str, int]]:
    """Hash server content by type and id, ignoring meta, which the server rewrites on load."""
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    for resource_type in sorted(set(resource_types)):
        count = 0
        for resource in search_all(base, resource_type):
            content = {key: value for key, value in resource.items() if key != "meta"}
            digest.update(f"{resource_type}/{resource['id']}\n".encode())
            digest.update(canonical_json(content))
            digest.update(b"\n")
            count += 1
        counts[resource_type] = count
    return digest.hexdigest(), counts


def read_manifest() -> JSONObject:
    if not SEED_MANIFEST.is_file():
        raise SandboxError(f"{SEED_MANIFEST.relative_to(ROOT)} is missing; run scripts/seed_hapi.py first")
    manifest: JSONObject = json.loads(SEED_MANIFEST.read_text(encoding="utf-8"))
    return manifest
