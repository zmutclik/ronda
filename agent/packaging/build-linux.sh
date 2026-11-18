#!/usr/bin/env bash
# Build agent Linux (x86_64): PyInstaller onedir → dist/ronda-agent-linux-x86_64-<versi>.tar.gz
# Paket ini diunggah di Pendaftaran Agent › Rilis agent › Installer (platform Linux) dan dipasang oleh install.sh.
#
#   ./packaging/build-linux.sh 1.2.0
#
# Build berjalan di container Docker python:3.13-slim-bullseye (glibc 2.31): binary PyInstaller hanya jalan di
# distro dengan glibc ≥ glibc mesin build, jadi build langsung di mesin baru menghasilkan paket yang gagal di
# distro lama ("GLIBC_2.38 not found"). Hasilnya jalan di Debian 11+, Ubuntu 20.04+, RHEL/Rocky 9+.
# Butuh: docker. Linux hanya membawa agent (tanpa launcher/helper): tanpa auto-update, screenshot, dan remote;
# upgrade = jalankan install.sh lagi setelah paket baru diunggah.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=${1:-}
IMAGE=python:3.13-slim-bullseye
[[ $VERSION =~ ^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$ ]] || { echo "Pakai: $0 <versi x.y.z>" >&2; exit 2; }
CODE_VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' ronda_agent/__init__.py)
[[ $CODE_VERSION == "$VERSION" ]] || { echo "Versi $VERSION tidak sama dengan ronda_agent.__version__ ($CODE_VERSION)" >&2; exit 1; }

if [[ ${RONDA_BUILD_INNER:-} != 1 ]]; then
  command -v docker >/dev/null || { echo "Butuh docker." >&2; exit 1; }
  mkdir -p dist
  # Sumber dibaca-saja; build di salinan dalam container, jadi .venv/build host tidak tersentuh.
  docker run --rm --platform linux/amd64 -e RONDA_BUILD_INNER=1 -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    -v "$PWD:/src:ro" -v "$PWD/../LICENSE:/LICENSE:ro" -v "$PWD/dist:/out" "$IMAGE" bash /src/packaging/build-linux.sh "$VERSION"
  TARBALL="dist/ronda-agent-linux-x86_64-$VERSION.tar.gz"
  echo "Selesai: $TARBALL"
  echo "  SHA-256 $(sha256sum "$TARBALL" | cut -d' ' -f1)"
  echo "  Unggah di Pendaftaran Agent › Rilis agent › Installer, platform Linux, versi $VERSION."
  exit 0
fi

# ---- di dalam container ----
apt-get update -qq && apt-get install -y -qq binutils >/dev/null      # PyInstaller butuh objdump
pip install -q --root-user-action=ignore uv
mkdir -p /work/agent && cp -r /src/. /work/agent/ && cp /LICENSE /work/LICENSE && cd /work/agent   # licenses.py: ../LICENSE
rm -rf .venv build dist
export UV_PYTHON_PREFERENCE=only-system UV_LINK_MODE=copy
uv sync -q --frozen --group build
OUT=dist/linux
uv run pyinstaller --noconfirm --clean --onedir --console --name ronda-agent \
  --distpath "$OUT" --workpath build/linux --specpath build/linux \
  --paths . --exclude-module tkinter --exclude-module unittest --exclude-module pydoc \
  packaging/entry/agent.py
uv run python packaging/licenses.py --pyinstaller --out "$OUT/ronda-agent/THIRD_PARTY_LICENSES.txt"
echo "$VERSION" > "$OUT/ronda-agent/VERSION"
"$OUT/ronda-agent/ronda-agent" --help >/dev/null      # binary bisa dijalankan (import lengkap)
NAME="ronda-agent-linux-x86_64-$VERSION.tar.gz"
tar -C "$OUT" --owner=0 --group=0 --numeric-owner -czf "/out/$NAME" ronda-agent
chown "$HOST_UID:$HOST_GID" "/out/$NAME"
