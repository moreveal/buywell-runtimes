#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

[ -x .venv/bin/python ] || {
  echo "Run ./install.sh first." >&2
  exit 1
}
[ -f config.json ] || {
  echo "config.json is missing. Run ./install.sh first." >&2
  exit 1
}
command -v systemctl >/dev/null 2>&1 || {
  echo "systemd is not available on this Linux system." >&2
  exit 1
}

runtime_dir=$(pwd -P)
runtime_user=$(id -un)
service_name="buywell-ggsel.service"
temporary=$(mktemp)
trap 'rm -f "$temporary"' EXIT HUP INT TERM

cat >"$temporary" <<EOF
[Unit]
Description=Buywell GGSel runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$runtime_user
WorkingDirectory=$runtime_dir
ExecStart="$runtime_dir/.venv/bin/python" "$runtime_dir/ggsel_runtime.py" --config "$runtime_dir/config.json"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

if [ "$(id -u)" -eq 0 ]; then
  install -m 0644 "$temporary" "/etc/systemd/system/$service_name"
  systemctl daemon-reload
  systemctl enable "$service_name"
  systemctl restart "$service_name"
  systemctl is-active --quiet "$service_name"
else
  command -v sudo >/dev/null 2>&1 || {
    echo "sudo is required to install the background service." >&2
    exit 1
  }
  sudo install -m 0644 "$temporary" "/etc/systemd/system/$service_name"
  sudo systemctl daemon-reload
  sudo systemctl enable "$service_name"
  sudo systemctl restart "$service_name"
  sudo systemctl is-active --quiet "$service_name"
fi

echo "Buywell GGSel is installed and running as $runtime_user."
echo "Status: sudo systemctl status $service_name"
echo "Logs:   sudo journalctl -u $service_name -f"
