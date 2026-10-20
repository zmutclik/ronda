#!/usr/bin/env sh
# Unduh library frontend dengan versi di-pin ke app/static/js/vendor/ (commit hasilnya ke repo).
# Alpine memakai build CSP agar CSP bisa tanpa 'unsafe-eval'. noVNC (halaman remote) diambil dari source rilis
# GitHub: modul ES core/ + vendor/pako apa adanya (paket npm-nya berisi build CommonJS yang tidak bisa dimuat browser).
# Checksum SHA-256 dicek: file dari CDN yang berubah/diganti tidak ikut masuk repo.
# Naik versi: ubah VERSION, jalankan dengan UPDATE_CHECKSUMS=1, review diff-nya, lalu salin hash baru ke sini.
set -eu
HTMX_VERSION=2.0.4
HTMX_SHA256=e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447
ALPINE_VERSION=3.14.8
ALPINE_SHA256=c98f46291087f0f93c9cdefb682d120fe1d2b3dcb649de83cfc1ad5b1fac58e4
NOVNC_VERSION=1.6.0
NOVNC_SHA256=5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa
DEST="$(dirname "$0")/../app/static/js/vendor"
mkdir -p "$DEST"

fetch() {   # url, file, sha256
    curl -fsSL "$1" -o "$DEST/$2.tmp"
    actual=$(sha256sum "$DEST/$2.tmp" | cut -d' ' -f1)
    if [ "${UPDATE_CHECKSUMS:-0}" = 1 ]; then
        echo "$2 sha256: $actual"
    elif [ "$actual" != "$3" ]; then
        rm -f "$DEST/$2.tmp"
        echo "CHECKSUM TIDAK COCOK: $2 (diharapkan $3, didapat $actual)" >&2
        exit 1
    fi
    mv "$DEST/$2.tmp" "$DEST/$2"
}

fetch "https://cdn.jsdelivr.net/npm/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" htmx.min.js "$HTMX_SHA256"
fetch "https://cdn.jsdelivr.net/npm/@alpinejs/csp@${ALPINE_VERSION}/dist/cdn.min.js" alpine-csp.min.js "$ALPINE_SHA256"
fetch "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" novnc.tar.gz "$NOVNC_SHA256"
rm -rf "$DEST/novnc"
mkdir -p "$DEST/novnc/vendor"
tar xzf "$DEST/novnc.tar.gz" -C "$DEST/novnc" --strip-components=1 \
    "noVNC-${NOVNC_VERSION}/core" "noVNC-${NOVNC_VERSION}/vendor/pako" "noVNC-${NOVNC_VERSION}/LICENSE.txt"
rm -f "$DEST/novnc.tar.gz" "$DEST/novnc/vendor/pako/README.md"
echo "htmx ${HTMX_VERSION}, alpine-csp ${ALPINE_VERSION}, noVNC ${NOVNC_VERSION} -> $DEST"
