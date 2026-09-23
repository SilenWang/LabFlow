#!/usr/bin/env bash
# 安装 / 更新 LabFlow 的 systemd 服务并设为开机自启（替换旧 Windows 自启脚本）。
#
# 用法：sudo pixi run service-install
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 权限，请用：sudo pixi run service-install" >&2
  exit 1
fi

# 服务以哪个用户跑：优先 sudo 发起者，其次仓库属主
SERVICE_USER="${SUDO_USER:-$(stat -c '%U' "$REPO_DIR")}"
PIXI_BIN="$(command -v pixi || true)"
if [ -z "$PIXI_BIN" ]; then
  echo "找不到 pixi，请先安装 pixi 并确保在 PATH 中。" >&2
  exit 1
fi

if [ ! -f deploy/labflow.env ]; then
  echo "缺少 deploy/labflow.env，无法确定后端 / 端口配置。" >&2
  exit 1
fi

echo "仓库目录：$REPO_DIR"
echo "运行用户：$SERVICE_USER"
echo "pixi：    $PIXI_BIN"

# 先确保环境已就绪（依赖按 pixi.lock 安装），避免服务起来才发现缺包
sudo -u "$SERVICE_USER" "$PIXI_BIN" install --frozen

UNIT=/etc/systemd/system/labflow.service
sed \
  -e "s#__LABFLOW_DIR__#$REPO_DIR#g" \
  -e "s#__LABFLOW_USER__#$SERVICE_USER#g" \
  -e "s#__PIXI__#$PIXI_BIN#g" \
  deploy/labflow.service.template > "$UNIT"

systemctl daemon-reload
systemctl enable labflow.service
systemctl restart labflow.service

echo
systemctl --no-pager --lines=0 status labflow.service || true
echo
echo "已安装并启动。查看状态：systemctl status labflow"
