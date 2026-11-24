"""Fetch and verify every pinned sandbox upstream recorded in sandbox/upstreams.lock.

Source archives and artifacts are downloaded, checked against their SHA-256 and published
atomically under sandbox/vendor/. A cached source tree is trusted only when it still
matches the tree SHA-256 in the lock; otherwise it is
extracted again. Images and git build contexts are not downloaded here: the script checks that
the compose file names exactly the locked references, that each Inferno kit tag still names the
locked commit, and, on the rendered configuration, that every Dockerfile base is replaced by a
locked digest, every published port is loopback-only and only the TLS services see CA keys.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from sandbox_common import (
    SANDBOX,
    USER_AGENT,
    JSONObject,
    SandboxError,
    check_published_ports,
    check_secret_mounts,
    load_lock,
    rendered_config,
)

CHUNK = 1 << 20
VENDOR = SANDBOX / "vendor"
DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{n}" for n in range(1, 10)} | {f"lpt{n}" for n in range(1, 10)}
)
WINDOWS_INVALID = re.compile(r'[<>:"|?*\\\x00-\x1f]')


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to HTTPS hosts that serve GitHub archives and release assets."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname not in DOWNLOAD_HOSTS:
            raise SandboxError(f"refusing redirect to {parts.scheme}://{parts.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirect)


def open_download(url: str) -> Any:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in DOWNLOAD_HOSTS:
        raise SandboxError(f"refusing download from {parts.scheme}://{parts.hostname}")
    return _OPENER.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=300)


def download(url: str, destination: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with open_download(url) as response, destination.open("wb") as handle:
        while chunk := response.read(CHUNK):
            digest.update(chunk)
            handle.write(chunk)
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise SandboxError(f"{url}: SHA-256 {actual} does not match locked {expected_sha256}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def portable_component_problem(part: str) -> str | None:
    """Reasons a path component would be unsafe or change meaning on Windows or Linux."""
    if part in ("", ".", ".."):
        return "empty or relative component"
    if WINDOWS_INVALID.search(part):
        return "character that Windows forbids or treats as a separator"
    if part.endswith((".", " ")):
        return "trailing dot or space, which Windows strips"
    if part.split(".", 1)[0].lower() in WINDOWS_RESERVED:
        return "Windows device name"
    return None


def portable_relative_path(name: str) -> PurePosixPath:
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise SandboxError(f"path {name!r} is absolute")
    path = PurePosixPath(name)
    for part in name.rstrip("/").split("/"):
        problem = portable_component_problem(part)
        if problem is not None:
            raise SandboxError(f"path {name!r} has a {problem}")
    return path


def contained(root: Path, relative: PurePosixPath) -> Path:
    """Resolve a destination and prove it stays inside root before anything is created."""
    resolved_root = root.resolve()
    target = resolved_root.joinpath(*relative.parts).resolve()
    if target == resolved_root or not target.is_relative_to(resolved_root):
        raise SandboxError(f"{relative} escapes {resolved_root}")
    return target


def vendor_destination(relative: str) -> Path:
    """A lock destination must name a path below sandbox/vendor and nothing else."""
    path = portable_relative_path(relative)
    if len(path.parts) < 2 or path.parts[0] != "vendor":
        raise SandboxError(f"lock destination {relative!r} is outside sandbox/vendor")
    return contained(VENDOR, PurePosixPath(*path.parts[1:]))


def extract_archive(archive: Path, destination: Path) -> int:
    """Extract regular files and directories only, dropping the single top-level directory.

    Every member is validated before the first write: portable names, no case-insensitive
    collisions, no links or devices, and a resolved target inside the fresh destination.
    """
    plan: list[tuple[tarfile.TarInfo, Path]] = []
    seen: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.type not in (tarfile.XGLTYPE, tarfile.XHDTYPE)]
        for member in members:
            portable_relative_path(member.name)
        prefixes = {PurePosixPath(m.name).parts[0] for m in members if m.name}
        if len(prefixes) != 1:
            raise SandboxError(f"{archive.name}: expected one top-level directory, found {sorted(prefixes)}")
        prefix = prefixes.pop()
        for member in members:
            path = portable_relative_path(member.name)
            if path.parts[0] != prefix:
                raise SandboxError(f"archive member outside {prefix}/: {member.name}")
            if len(path.parts) == 1:
                continue
            if not (member.isdir() or member.isfile()):
                raise SandboxError(f"{archive.name}: refusing non-regular member {member.name}")
            relative = PurePosixPath(*path.parts[1:])
            folded = relative.as_posix().casefold()
            if folded in seen:
                raise SandboxError(f"{archive.name}: {relative} repeats or collides on Windows with {seen[folded]}")
            seen[folded] = relative.as_posix()
            plan.append((member, contained(destination, relative)))
        written = 0
        for member, target in plan:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise SandboxError(f"{archive.name}: unreadable member {member.name}")
            with source, target.open("xb") as handle:
                shutil.copyfileobj(source, handle)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            written += 1
    return written


def tree_digest(directory: Path) -> str | None:
    """Hash portable relative paths, entry types and regular file contents."""
    digest = hashlib.sha256()
    paths = sorted((p.relative_to(directory).as_posix(), p) for p in directory.rglob("*"))
    for name, path in paths:
        metadata = path.lstat()
        if getattr(metadata, "st_reparse_tag", 0):
            return None
        if stat.S_ISDIR(metadata.st_mode):
            digest.update(b"d\0" + name.encode() + b"\n")
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"f\0" + name.encode() + b"\0" + file_sha256(path).encode() + b"\n")
        else:
            return None
    return digest.hexdigest()


def publish_directory(staging: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    staging.replace(destination)


def fetch_source(name: str, entry: JSONObject) -> str:
    destination = vendor_destination(entry["directory"])
    expected = entry["tree_sha256"]
    if destination.is_dir() and tree_digest(destination) == expected:
        return f"{name}: {entry['commit'][:12]} present, tree SHA-256 verified against the lock"
    note = "cached tree differs from the lock; extracted again" if destination.exists() else "verified"
    owner_repo = entry["repository"].removeprefix("https://github.com/")
    url = f"https://codeload.github.com/{owner_repo}/tar.gz/{entry['commit']}"
    VENDOR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=VENDOR) as scratch:
        archive = Path(scratch) / "source.tar.gz"
        download(url, archive, entry["archive_sha256"])
        staging = Path(scratch) / "tree"
        staging.mkdir()
        count = extract_archive(archive, staging)
        if tree_digest(staging) != expected:
            raise SandboxError(f"{name}: verified archive did not extract to the locked tree SHA-256")
        publish_directory(staging, destination)
    return f"{name}: {entry['commit'][:12]} {note}, {count} files"


def fetch_artifact(name: str, entry: JSONObject) -> str:
    destination = vendor_destination(entry["path"])
    if destination.is_file() and file_sha256(destination) == entry["sha256"]:
        return f"{name}: {entry['tag']} present, SHA-256 verified"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=destination.parent) as scratch:
        staged = Path(scratch) / destination.name
        download(entry["url"], staged, entry["sha256"])
        staged.replace(destination)
    return f"{name}: {entry['tag']} verified"


def compose_images(compose_text: str) -> list[str]:
    return re.findall(r"^\s*image:\s*(\S+)\s*$", compose_text, flags=re.MULTILINE)


def compose_contexts(compose_text: str) -> list[str]:
    return re.findall(r"^\s*context:\s*(\S+)\s*$", compose_text, flags=re.MULTILINE)


def pinned(entry: Mapping[str, str]) -> str:
    return f"{entry['reference']}@{entry['digest']}"


def check_compose(lock: JSONObject, compose_text: str) -> list[str]:
    problems: list[str] = []
    runtime = {name: entry for name, entry in lock["images"].items() if entry.get("usage") != "build-base"}
    bases = {name: entry for name, entry in lock["images"].items() if entry.get("usage") == "build-base"}
    allowed = {pinned(entry) for entry in runtime.values()}
    for image in compose_images(compose_text):
        if not image.startswith("medverify-sandbox/") and image not in allowed:
            problems.append(f"compose image {image} is not pinned in the lock")
    used = set(compose_images(compose_text))
    for name, entry in runtime.items():
        if pinned(entry) not in used:
            problems.append(f"locked image {name} is not used by the compose file")
    for name, entry in bases.items():
        if f"docker-image://{pinned(entry)}" not in compose_text:
            problems.append(f"locked build base {name} is not mapped by a named build context")

    contexts = compose_contexts(compose_text)
    for name, kit in lock["kits"].items():
        reference = f"{kit['repository']}.git#{kit['commit']}"
        if contexts.count(reference) != 1:
            problems.append(f"kit {name}: expected exactly one service to build from {reference}")
    # CRD and DTR copy spec/ files from the repository root; PAS builds from go/ alone.
    build_roots = {"crd-server": "", "dtr-server": "", "pas-server": "/go"}
    for name, suffix in build_roots.items():
        context = lock["sources"][name]["directory"] + suffix
        if context not in contexts:
            problems.append(f"source {name}: compose must build from {context}")
    for context in contexts:
        if context.startswith("https://") and not any(
            context == f"{kit['repository']}.git#{kit['commit']}" for kit in lock["kits"].values()
        ):
            problems.append(f"compose build context {context} is not locked")
    return problems


def dockerfile_bases(text: str) -> list[str]:
    """External images named by FROM lines, skipping references to earlier build stages."""
    stages: set[str] = set()
    bases: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", line, re.IGNORECASE)
        if not match:
            continue
        image, stage = match.group(1), match.group(2)
        if image.lower() not in stages:
            bases.append(image)
        if stage:
            stages.add(stage.lower())
    return bases


def check_build_bases(config: Mapping[str, Any], lock: JSONObject, bases_by_service: Mapping[str, list[str]]) -> list[str]:
    """Every FROM base of every built service must resolve to a locked digest."""
    locked = {entry["reference"]: f"docker-image://{pinned(entry)}" for entry in lock["images"].values()}
    problems: list[str] = []
    services = config.get("services") or {}
    for name, service in sorted(services.items()):
        if "build" not in service:
            continue
        if name not in bases_by_service:
            problems.append(f"{name} builds from a Dockerfile the lock does not cover")
            continue
        contexts = service["build"].get("additional_contexts") or {}
        for base in bases_by_service[name]:
            if base not in locked:
                problems.append(f"{name} builds FROM {base}, which the lock does not pin")
            elif contexts.get(base) != locked[base]:
                problems.append(f"{name} does not map FROM {base} to {locked[base]}")
    return problems


def remote_tag_commit(repository: str, tag: str) -> str:
    completed = subprocess.run(
        ["git", "ls-remote", repository, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise SandboxError(f"git ls-remote {repository} failed: {completed.stderr.strip()}")
    refs: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref] = sha
    # An annotated tag lists the tag object first and the commit under the peeled ^{} name.
    commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if commit is None:
        raise SandboxError(f"{repository} has no tag {tag}")
    return commit


def kit_dockerfile(kit: Mapping[str, str]) -> str:
    owner_repo = kit["repository"].removeprefix("https://github.com/")
    with open_download(f"https://raw.githubusercontent.com/{owner_repo}/{kit['commit']}/Dockerfile") as response:
        text: str = response.read().decode("utf-8")
    return text


def service_bases(lock: JSONObject) -> dict[str, list[str]]:
    bases: dict[str, list[str]] = {}
    for service, source, dockerfile in (
        ("crd", "crd-server", "go/Dockerfile"),
        ("dtr", "dtr-server", "go/Dockerfile"),
        ("pas", "pas-server", "go/Dockerfile"),
    ):
        path = vendor_destination(lock["sources"][source]["directory"]) / dockerfile
        bases[service] = dockerfile_bases(path.read_text(encoding="utf-8"))
    for key, kit in lock["kits"].items():
        bases[f"inferno-{key}"] = dockerfile_bases(kit_dockerfile(kit))
    return bases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--locked",
        action="store_true",
        required=True,
        help="fetch exactly what sandbox/upstreams.lock records; required so a run never re-pins",
    )
    parser.parse_args(argv)

    try:
        lock = load_lock()
        compose_text = (SANDBOX / "docker-compose.yaml").read_text(encoding="utf-8")
        problems = check_compose(lock, compose_text)
        for name, kit in lock["kits"].items():
            commit = remote_tag_commit(kit["repository"], kit["tag"])
            if commit != kit["commit"]:
                problems.append(f"kit {name}: tag {kit['tag']} now names {commit}, lock has {kit['commit']}")
            else:
                print(f"kit {name}: {kit['tag']} = {commit[:12]} verified")
        if problems:
            raise SandboxError("; ".join(problems))
        for name, entry in lock["sources"].items():
            print(fetch_source(name, entry))
        for name, entry in lock["artifacts"].items():
            print(fetch_artifact(name, entry))
        config = rendered_config()
        problems = check_published_ports(config) + check_secret_mounts(config)
        problems += check_build_bases(config, lock, service_bases(lock))
        if problems:
            raise SandboxError("; ".join(problems))
        runtime = sum(1 for entry in lock["images"].values() if entry.get("usage") != "build-base")
        print(f"images: {runtime} runtime images and {len(lock['images']) - runtime} build bases pinned by digest")
        print("rendered compose: loopback-only ports, CA keys confined to tls and ca-export, every FROM pinned")
    except SandboxError as error:
        print(f"fetch_sandbox: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
