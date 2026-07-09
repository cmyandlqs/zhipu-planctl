#!/bin/bash
set -e

INSTALL_DIR="${HOME}/zhipu-coding-plan"
SERVICE_NAME="zhipu-plan"

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
cp "${SCRIPT_DIR}/pyproject.toml" "${INSTALL_DIR}/"

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

# 创建 systemd 服务
read -r -d '' SERVICE << EOF || true
[Unit]
Description=Zhipu Coding Plan Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python -m zhipu_planctl
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

echo "${SERVICE}" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null
sudo systemctl daemon-reload

echo ""
echo "=== 安装完成 ==="
echo ""
echo "下一步:"
echo "  1. 编辑配置文件: vi ${INSTALL_DIR}/config.yaml"
echo "  2. 启动服务:     sudo systemctl start ${SERVICE_NAME}"
echo "  3. 开机自启:     sudo systemctl enable ${SERVICE_NAME}"
echo "  4. 查看日志:     sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "快速测试:"
echo "  cd ${INSTALL_DIR} && source venv/bin/activate && python -m zhipu_planctl --query"
