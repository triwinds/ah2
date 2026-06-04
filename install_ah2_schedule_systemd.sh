#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME=ah2-schedule.service
REPO_DIR=/root/ah2
SERVICE_SRC="${REPO_DIR}/systemd/${SERVICE_NAME}"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This installer must run as root." >&2
  exit 1
fi

if [[ ! -f "${SERVICE_SRC}" ]]; then
  echo "Missing service file: ${SERVICE_SRC}" >&2
  exit 1
fi

install -m 0644 "${SERVICE_SRC}" "${SERVICE_DST}"
systemctl daemon-reload
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

if command -v screen >/dev/null 2>&1; then
  screen -S ah2 -X quit 2>/dev/null || true
fi

pkill -f '[p]ython linux_schedule.py' 2>/dev/null || true

systemctl enable --now "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}"
