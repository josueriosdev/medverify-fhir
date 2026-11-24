#!/usr/bin/env bash
# Prove the source distribution builds on its own.
#
# Builds the sdist, extracts it into a fresh directory outside the repository, builds a wheel
# from that extracted tree with the locked maturin, installs the wheel into a new virtual
# environment and imports the compiled extension from a directory that holds no sources.
# A file the build needs but the sdist omits (such as a compile-time policy table) fails here,
# because nothing in the extracted tree can reach the original checkout.
#
#   bash scripts/check_sdist.sh [work-directory]
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work=${1:-$(mktemp -d "${TMPDIR:-/tmp}/medverify-sdist.XXXXXX")}
case "$work" in
    "$root" | "$root"/*) printf 'check_sdist: work directory must be outside %s\n' "$root" >&2; exit 2 ;;
esac
mkdir -p "$work"
if [[ -n "$(ls -A "$work")" ]]; then
    printf 'check_sdist: %s is not empty\n' "$work" >&2
    exit 2
fi

if command -v uv >/dev/null 2>&1; then
    uv=(uv)
else
    uv=(python -m uv)
fi
run=("${uv[@]}" run --locked --project "$root")

python=$(cd "$root" && "${run[@]}" python -c 'import sys; print(sys.executable)')
maturin=$(cd "$root" && "${run[@]}" python -c 'import shutil; print(shutil.which("maturin"))')

(cd "$root" && "$maturin" sdist --out "$work/dist")
sdist=$(ls "$work"/dist/*.tar.gz)
printf 'sdist: %s\n' "$(basename "$sdist")"
tar -tzf "$sdist" | sort

mkdir "$work/src"
tar -xzf "$sdist" -C "$work/src"
tree=$(ls -d "$work"/src/*/)
for table in policy/wa_wiser.toml policy/state_hitl.toml; do
    test -s "$tree/$table" || { printf 'check_sdist: sdist lacks %s\n' "$table" >&2; exit 1; }
done

(cd "$tree" && "$maturin" build --release --interpreter "$python" --out "$work/wheel")
wheel=$(ls "$work"/wheel/*.whl)
printf 'wheel: %s\n' "$(basename "$wheel")"
"$python" -c 'import sys, zipfile; print("\n".join(sorted(zipfile.ZipFile(sys.argv[1]).namelist())))' "$wheel"

"${uv[@]}" venv --quiet --python "$python" "$work/venv"
if [[ -x "$work/venv/bin/python" ]]; then
    venv_python="$work/venv/bin/python"
else
    venv_python="$work/venv/Scripts/python.exe"
fi
"${uv[@]}" pip install --quiet --python "$venv_python" "$wheel"
mkdir "$work/import"
(cd "$work/import" && "$venv_python" -c '
import pathlib
import medverify
import medverify._medverify as extension

origin = pathlib.Path(extension.__file__).resolve()
assert "site-packages" in origin.parts, origin
print(f"imported medverify {medverify.__version__} extension from {origin.name}")
')
printf 'check_sdist: sdist, extracted wheel build and clean install passed (%s)\n' "$work"
