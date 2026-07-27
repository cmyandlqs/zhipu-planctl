#!/bin/bash
set -e

INSTALL_DIR="${HOME}/zhipu-coding-plan"
SERVICE_NAME="zhipu-plan"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "=== 智谱 Coding Plan 管理工具 安装脚本 ==="

# Python 检查
if ! command -v python3 &>/dev/null; then
    echo "错误: 请先安装 Python 3.8+"
    exit 1
fi

# 拷贝源码
mkdir -p "${INSTALL_DIR}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp -r "${SCRIPT_DIR}/src" "${INSTALL_DIR}/"
find "${INSTALL_DIR}/src" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "${INSTALL_DIR}/src" -name '*.pyc' -delete 2>/dev/null || true
cp "${SCRIPT_DIR}/pyproject.toml" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"

if [ ! -f "${INSTALL_DIR}/config.yaml" ]; then
    cp "${SCRIPT_DIR}/config.yaml.example" "${INSTALL_DIR}/config.yaml"
    echo "已创建配置模板: ${INSTALL_DIR}/config.yaml"
    echo "请编辑此文件填入你的 API Key 和飞书配置"
fi

# 安装依赖
cd "${INSTALL_DIR}"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e .

echo "依赖安装完成"

# 创建 systemd user 服务
mkdir -p "${USER_SYSTEMD_DIR}"

cat > "${USER_SYSTEMD_DIR}/${SERVICE_NAME}.service" << SERVICEOF
[Unit]
Description=Zhipu Coding Plan Manager (最新版)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
Environment=PATH=${INSTALL_DIR}/venv/bin:${PATH}
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=${INSTALL_DIR}/venv/bin/python -m zhipu_planctl --config ${INSTALL_DIR}/config.yaml --log-dir ${INSTALL_DIR}/logs --log-retention-hours 48
Restart=always
RestartSec=30
KillMode=control-group
SuccessExitStatus=143
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SERVICEOF

# 创建定时重启 oneshot
cat > "${USER_SYSTEMD_DIR}/${SERVICE_NAME}-restart.service" << RESTARTEOF
[Unit]
Description=Restart Zhipu Plan service

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl --user restart ${SERVICE_NAME}.service
RESTARTEOF

# 创建每日 05:55 重启 timer
cat > "${USER_SYSTEMD_DIR}/${SERVICE_NAME}-restart.timer" << TIMEREOF
[Unit]
Description=Daily 05:55 restart for Zhipu Plan service

[Timer]
OnCalendar=*-*-* 05:55:00
Persistent=true

[Install]
WantedBy=timers.target
TIMEREOF

systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}.service"
systemctl --user enable --now "${SERVICE_NAME}-restart.timer"

echo ""
echo "=== 安装完成 ==="
echo ""
echo "下一步:"
echo "  1. 编辑配置文件: vi ${INSTALL_DIR}/config.yaml"
echo "  2. 启动服务:     systemctl --user start ${SERVICE_NAME}"
echo "  3. 查看日志:     journalctl --user -u ${SERVICE_NAME} -f"
echo "  4. 文件日志:     tail -f ${INSTALL_DIR}/logs/zhipu-planctl.log"
echo "  5. 自动重启:     每天 05:55 timer 自动重启服务"
echo "  6. 若需退出 SSH 后继续运行: loginctl enable-linger \"$(whoami)\""
echo ""
echo "快速测试:"
echo "  cd ${INSTALL_DIR} && source venv/bin/activate && python -m zhipu_planctl --query"
echo "  systemctl --user restart ${SERVICE_NAME}"
