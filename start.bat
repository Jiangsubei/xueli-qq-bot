@echo off
chcp 65001 >nul
echo.
echo ========================================
echo     QQ AI 机器人启动器
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

:: 检查虚拟环境
if not exist "venv" (
    echo [信息] 创建虚拟环境...
    python -m venv venv
)

:: 激活虚拟环境
echo [信息] 激活虚拟环境...
call venv\Scripts\activate.bat

:: 检查依赖
echo [信息] 检查依赖...
pip install -q -r requirements.txt

:: 检查配置文件
if not exist ".env" (
    echo.
    echo [警告] 未找到 .env 配置文件！
    echo [信息] 正在从示例创建...
    copy .env.example .env
    echo.
    echo ========================================
    echo 请编辑 .env 文件，填入你的配置后重新运行
    echo ========================================
    echo.
    notepad .env
    pause
    exit /b 1
)

:: 启动机器人
echo.
echo ========================================
echo 正在启动机器人...
echo 按 Ctrl+C 停止
echo ========================================
echo.

python main.py

:: 退出虚拟环境
call venv\Scripts\deactivate.bat

echo.
echo 机器人已停止
pause