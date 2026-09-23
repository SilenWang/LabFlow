#!/usr/bin/env bash
# 卸载 LabFlow 的 systemd 服务（不动数据）。
#
# 用法：sudo pixi run service-uninstall
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 权限，请用：sudo pixi run service-uninstall" >&2
  exit 1
fi

systemctl disable --now labflow.service || true
rm -f /etc/systemd/system/labflow.service
systemctl daemon-reload
systemctl reset-failed labflow.service || true

echo "已卸载 labflow.service；data/ 与 uploads/ 未改动。"
