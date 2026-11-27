#!/usr/bin/env bash
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
if [ -x "$APP_DIR/venv/bin/python" ]; then PY="$APP_DIR/venv/bin/python"; else PY="$(command -v python3)"; fi

sudo usermod -aG dialout,video,audio "$USER_NAME"
sed -e "s|__USER__|$USER_NAME|g" -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__PYTHON__|$PY|g" \
    "$APP_DIR/deploy/roaddamage.service" | sudo tee /etc/systemd/system/roaddamage.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now roaddamage.service
echo "Installed. Logs: journalctl -u roaddamage -f"