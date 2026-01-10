#!/bin/bash
# qBit Smart Web Manager 启动脚本

echo "==================================="
echo "  qBit Smart Web Manager v1.6"
echo "==================================="

# 检查Python版本
python3 --version 2>/dev/null || {
    echo "❌ 未找到Python3，请先安装Python3"
    exit 1
}

# 检查并安装依赖
echo "📦 检查依赖..."
pip3 install -r requirements.txt -q 2>/dev/null || pip install -r requirements.txt -q

# 启动应用
echo "🚀 启动应用..."
echo ""
python3 app.py
