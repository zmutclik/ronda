#!/usr/bin/env bash
# Ronda Agent: pemasang Linux (x86_64, systemd). Dibuat server __SERVER__.
#
#   curl -fsSL __SERVER__/install.sh | sudo RONDA_KEY=<enrollment key> bash
#   curl -fsSL __SERVER__/install.sh | sudo bash -s -- --key <enrollment key> [--ca /path/ca.pem]
#   curl -fsSL __SERVER__/install.sh | sudo bash -s -- --uninstall
#
# Hasil: /opt/ronda/agent (program), /var/lib/ronda (config.toml, state.dat, buffer, logs/; hanya user `ronda`),
# service systemd ronda-agent (user `ronda`, bukan root). Instal ulang/upgrade: config.toml & identitas dipertahankan;
# --key/--ca yang diberikan menimpa nilainya. Di Linux tidak ada auto-update, screenshot, dan remote: upgrade =
# jalankan perintah ini lagi setelah admin mengunggah paket Linux baru.
set -euo pipefail
TMPDIR_RONDA=

SERVER='__SERVER__'
PACKAGE_URL='__PACKAGE_URL__'
PACKAGE_SHA256='__PACKAGE_SHA256__'
PACKAGE_VERSION='__PACKAGE_VERSION__'
SERVER_ERROR='__SERVER_ERROR__'

PREFIX=/opt/ronda
DATA_DIR=/var/lib/ronda
SERVICE=ronda-agent
UNIT=/etc/systemd/system/$SERVICE.service
RUN_USER=ronda

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mGAGAL:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  echo "Pakai: install.sh [--key KEY] [--ca FILE.pem] [--uninstall]   (atau env RONDA_KEY / RONDA_CA)"
}

uninstall() {
  say "Menghentikan dan menghapus service $SERVICE"
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl daemon-reload
  rm -rf "$PREFIX"
  # logs/ sengaja ditinggal untuk penelusuran (sama seperti uninstall di Windows)
  if [[ -d $DATA_DIR ]]; then
    find "$DATA_DIR" -mindepth 1 -maxdepth 1 ! -name logs -exec rm -rf {} +
  fi
  say "Ronda Agent dihapus. Log tersisa di $DATA_DIR/logs. PC tetap tercatat di server sampai dihapus admin."
}

# Ganti (atau tambah) satu baris `key = "value"` di config.toml.
set_config() {
  local file=$1 key=$2 value=$3 tmp
  tmp=$(mktemp "$DATA_DIR/.config.XXXXXX")
  grep -v "^[[:space:]]*$key[[:space:]]*=" "$file" > "$tmp" || true
  printf '%s = "%s"\n' "$key" "$value" >> "$tmp"
  chown "$RUN_USER:$RUN_USER" "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

main() {
  local key=${RONDA_KEY:-} ca=${RONDA_CA:-} action=install
  while [[ $# -gt 0 ]]; do
    case $1 in
      --key) key=${2:-}; shift 2 ;;
      --key=*) key=${1#*=}; shift ;;
      --ca) ca=${2:-}; shift 2 ;;
      --ca=*) ca=${1#*=}; shift ;;
      --uninstall) action=uninstall; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; fail "argumen tidak dikenal: $1" ;;
    esac
  done

  [[ $(id -u) -eq 0 ]] || fail "jalankan sebagai root: ... | sudo bash"
  command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || fail "butuh systemd"
  if [[ $action == uninstall ]]; then uninstall; return; fi

  [[ -z $SERVER_ERROR ]] || fail "$SERVER_ERROR"
  [[ $(uname -m) == x86_64 ]] || fail "hanya untuk x86_64 (PC ini $(uname -m))"
  for tool in curl tar sha256sum useradd; do
    command -v "$tool" >/dev/null || fail "perintah $tool tidak ada"
  done
  if [[ -n $key && ! $key =~ ^[A-Za-z0-9-]{4,64}$ ]]; then fail "enrollment key berisi karakter tidak valid"; fi
  if [[ -n $ca ]]; then
    [[ -f $ca ]] || fail "file CA $ca tidak ada"
    grep -q "BEGIN CERTIFICATE" "$ca" || fail "file CA $ca bukan sertifikat PEM"
  fi
  local fresh=1
  [[ -f $DATA_DIR/config.toml ]] && fresh=0
  if [[ $fresh == 1 && -z $key ]]; then
    fail "instalasi baru butuh enrollment key: ... | sudo RONDA_KEY=<key> bash  (key dari menu Pendaftaran Agent)"
  fi
  if [[ $SERVER == http://* ]]; then
    echo "PERINGATAN: server memakai http:// (tanpa TLS); hanya untuk uji." >&2
  fi

  TMPDIR_RONDA=$(mktemp -d)
  local tmpdir=$TMPDIR_RONDA
  trap 'rm -rf "$TMPDIR_RONDA"' EXIT
  say "Mengunduh Ronda Agent $PACKAGE_VERSION"
  local curl_ca=()
  if [[ -n $ca ]]; then curl_ca=(--cacert "$ca"); fi
  curl -fsSL ${curl_ca[@]+"${curl_ca[@]}"} -o "$tmpdir/agent.tar.gz" "$PACKAGE_URL" || fail "unduhan gagal: $PACKAGE_URL"
  echo "$PACKAGE_SHA256  $tmpdir/agent.tar.gz" | sha256sum -c --quiet - \
    || fail "SHA-256 paket tidak cocok (paket diganti saat mengunduh?); jalankan lagi"
  mkdir -p "$tmpdir/x"
  tar -xzf "$tmpdir/agent.tar.gz" -C "$tmpdir/x" --no-same-owner
  [[ -x $tmpdir/x/ronda-agent/ronda-agent ]] || fail "isi paket tidak dikenal (tidak ada ronda-agent/ronda-agent)"

  if ! id "$RUN_USER" >/dev/null 2>&1; then
    say "Membuat user sistem $RUN_USER"
    useradd --system --user-group --no-create-home --home-dir "$DATA_DIR" \
      --shell "$(command -v nologin || echo /bin/false)" "$RUN_USER"
  fi

  say "Memasang ke $PREFIX/agent"
  systemctl stop "$SERVICE" 2>/dev/null || true
  install -d -m 755 -o root -g root "$PREFIX"
  rm -rf "$PREFIX/agent.new" "$PREFIX/agent.old"
  mv "$tmpdir/x/ronda-agent" "$PREFIX/agent.new"
  chown -R root:root "$PREFIX/agent.new"
  chmod -R u=rwX,go=rX "$PREFIX/agent.new"
  if [[ -d $PREFIX/agent ]]; then mv "$PREFIX/agent" "$PREFIX/agent.old"; fi
  mv "$PREFIX/agent.new" "$PREFIX/agent"
  rm -rf "$PREFIX/agent.old"

  # Folder data hanya untuk user ronda: state.dat (token agent) tidak dienkripsi di Linux.
  install -d -m 700 -o "$RUN_USER" -g "$RUN_USER" "$DATA_DIR"
  local config=$DATA_DIR/config.toml
  if [[ $fresh == 1 ]]; then
    say "Menulis $config"
    install -m 600 -o "$RUN_USER" -g "$RUN_USER" /dev/null "$config"
    cat > "$config" <<EOF
# Dibuat install.sh dari $SERVER. Linux: tanpa auto-update, screenshot, dan remote.
server_url = "$SERVER"
screenshot = false
remote = false
auto_update = false
EOF
  fi
  if [[ -n $key ]]; then set_config "$config" enrollment_key "$key"; fi
  if [[ -n $ca ]]; then
    install -m 644 -o root -g root "$ca" "$DATA_DIR/ca.pem"
    set_config "$config" ca_bundle "$DATA_DIR/ca.pem"
  fi
  chown -R "$RUN_USER:$RUN_USER" "$DATA_DIR"

  say "Memasang service $SERVICE"
  cat > "$UNIT" <<EOF
[Unit]
Description=Ronda Agent (monitoring PC)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
Group=$RUN_USER
ExecStart=$PREFIX/agent/ronda-agent --data-dir $DATA_DIR
Restart=always
RestartSec=10
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA_DIR
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
  systemctl restart "$SERVICE"
  sleep 3
  systemctl is-active --quiet "$SERVICE" \
    || fail "service tidak jalan; cek: journalctl -u $SERVICE dan $DATA_DIR/logs/agent.log"

  say "Selesai: Ronda Agent $PACKAGE_VERSION jalan (service $SERVICE)."
  if [[ $fresh == 1 ]]; then
    echo "    PC ini akan muncul di Pendaftaran Agent > Persetujuan PC baru; setujui di sana."
  fi
  echo "    Log: $DATA_DIR/logs/agent.log   Status: systemctl status $SERVICE"
}

main "$@"
