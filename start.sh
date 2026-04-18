#!/bin/bash

# ========================================
# QQ AI 机器人启动器 (Linux/Mac)
# ========================================

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
if [ ! -d "venv" ]; then
    echo "[信息] 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
echo "[信息] 激活虚拟环境..."
source venv/bin/activate

# 检查依赖
echo "[信息] 检查依赖..."
pip install -q -r requirements.txt

# 检查配置文件
if [ ! -f ".env" ]; then
    echo ""
    echo "[警告] 未找到 .env 配置文件！"
    echo "[信息] 正在从示例创建..."
    cp .env.example .env
    echo ""
    echo "========================================"
    echo "请编辑 .env 文件，填入你的配置后重新运行"
    echo "========================================"
    echo ""

    # 尝试使用默认编辑器打开
    if command -v nano &> /dev/null; then
        nano .env
    elif command -v vim &> /dev/null; then
        vim .env
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