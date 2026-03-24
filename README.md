# QQ AI 机器人框架

一个基于 Python 使用AI构建的 QQ 机器人框架，连接 NapCat（OneBot v11 协议），支持openai兼容的api。

## ✨ 特性

- 🔌 **WebSocket 连接**：与 NapCat 建立稳定的 WebSocket 长连接
- 🧠 **AI 对话**：集成 DeepSeek API，支持多轮上下文对话
- 💬 **私聊支持**：直接私聊机器人进行对话
- 👥 **群聊支持**：群聊中 @ 机器人即可触发对话
- 📝 **长消息分割**：自动处理超长消息，分段发送
- 🔄 **自动重连**：连接断开后自动重连
- ⚡ **频率限制**：防止刷屏，保护账号安全
- 🗑️ **对话管理**：支持清空历史、查看状态等命令

## 📁 项目结构

```
.
├── main.py              # 程序入口
├── bot.py              # 机器人主类
├── config.py           # 配置管理
├── connection.py       # WebSocket 连接管理
├── dispatcher.py       # 事件分发
├── message_handler.py  # 消息处理
├── ai_client.py        # AI API 客户端
├── models.py           # 数据模型
├── requirements.txt    # 依赖列表
├── .env.example        # 环境变量示例
└── README.md           # 使用说明
```

## 🚀 快速开始

### 1. 安装 NapCat

按照 [NapCat 官方文档](https://github.com/NapNeko/NapCatQQ) 安装并配置 NapCat。

确保 NapCat 的 WebSocket 服务已启用，默认地址为 `ws://127.0.0.1:6700`。

### 2. 安装依赖

```bash
# 创建虚拟环境（推荐）
python -m venv venv

# 激活虚拟环境
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
# 复制示例配置文件
cp .env.example .env

# 编辑 .env 文件，填入你的配置
```

主要配置项：

```env
# NapCat WebSocket 地址
NAPCAT_WS_URL=ws://127.0.0.1:6700

# DeepSeek API Key（从 https://platform.deepseek.com/ 获取）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 机器人名字
BOT_NAME=AI助手
```

### 4. 启动机器人

```bash
python main.py
```

看到以下日志表示启动成功：

```
==================================================
✅ 机器人已连接到 NapCat
==================================================
```

## 💬 使用方法

### 私聊

直接给机器人发送消息即可开始对话。

### 群聊

在群聊中 @机器人 并发送消息，机器人会回复你。

### 可用命令

| 命令 | 说明 |
|------|------|
| `/reset` 或 `/清除` | 清空当前对话历史 |
| `/help` 或 `/帮助` | 显示帮助信息 |
| `/status` 或 `/状态` | 查看机器人状态 |

## ⚙️ 高级配置

### 修改系统提示词

在 `.env` 文件中修改 `SYSTEM_PROMPT`：

```env
SYSTEM_PROMPT=你是一个专业的编程助手，擅长 Python 编程...
```

### 调整对话上下文长度

```env
MAX_CONTEXT_LENGTH=20  # 保留最近 20 轮对话
```

### 使用其他 AI 服务

支持任何 OpenAI 兼容的 API：

```env
# 使用 OpenAI
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_API_URL=https://api.openai.com/v1/chat/completions
OPENAI_MODEL=gpt-3.5-turbo
```

## 🔧 故障排除

### 无法连接到 NapCat

1. 检查 NapCat 是否已启动
2. 检查 WebSocket 地址和端口是否正确
3. 检查防火墙设置

### AI 无响应

1. 检查 API Key 是否正确
2. 检查 API 账户余额
3. 查看日志中的错误信息

### 消息发送失败

1. 检查 QQ 账号是否被风控
2. 降低发送频率
3. 减少消息长度

## 📜 许可证

MIT License

## 🙏 致谢

- [NapCat](https://github.com/NapNeko/NapCatQQ) - OneBot 11 协议实现
- [DeepSeek](https://deepseek.com/) - AI 模型服务