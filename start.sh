#!/bin/bash

# ========================================
# QQ AI 机器人启动器 (Linux/Mac)
# ========================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "========================================"
echo "     QQ AI 机器人启动器"
echo "========================================"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 Python3，请先安装 Python 3.8+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "[信息] Python 版本: $PYTHON_VERSION"

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "[信息] 创建虚拟环境..."
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "[错误] 虚拟环境创建失败，请先安装 python3-venv："
        echo "  sudo apt install python3.12-venv  (Ubuntu/Debian)"
        exit 1
    fi
fi

# 激活虚拟环境
echo "[信息] 激活虚拟环境..."
source .venv/bin/activate

# 确保 pip 已安装
if ! .venv/bin/python -m pip --version &> /dev/null; then
    echo "[信息] 安装 pip..."
    .venv/bin/python -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '/tmp/get-pip.py'); exec(open('/tmp/get-pip.py').read())"
fi

# 检查依赖
echo "[信息] 检查依赖..."
.venv/bin/python -m pip install -q -r requirements.txt

# 检查配置文件
if [ ! -f "xueli/config/.env" ]; then
    echo ""
    echo "[警告] 未找到 .env 配置文件！"
    echo "[信息] 正在从示例创建..."
    cp xueli/config/.env.example xueli/config/.env
    echo ""
    echo "========================================"
    echo "请编辑 xueli/config/.env 文件，填入你的配置后重新运行"
    echo "========================================"
    echo ""

    # 尝试使用默认编辑器打开
    if command -v nano &> /dev/null; then
        nano xueli/config/.env
    elif command -v vim &> /dev/null; then
        vim xueli/config/.env
    fi

    exit 1
fi

# 启动机器人
echo ""
echo "========================================"
echo "正在启动机器人..."
echo "按 Ctrl+C 停止"
echo "========================================"
echo ""

python3 main.py

# 退出虚拟环境
deactivate

echo ""
echo "机器人已停止"