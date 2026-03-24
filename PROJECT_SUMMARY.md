# QQ 机器人框架 - OpenAI 兼容版

## 项目概述

一个完全通用的 **OpenAI 兼容 QQ 机器人框架**，支持连接任意遵循 OpenAI API 规范的服务，包括：

- **OpenAI** (官方 API)
- **DeepSeek**
- **OpenRouter**
- **Azure OpenAI**
- **本地 Ollama**
- **其他自定义服务**

## 核心特性

### 1. 完全通用的 OpenAI 兼容架构
- **统一配置**：只需修改 3 个核心参数即可切换任意服务
- **标准接口**：完全遵循 OpenAI API 规范
- **灵活扩展**：支持自定义请求参数、请求头、响应解析

### 2. 模块化设计
- `config.py` - 通用配置管理
- `ai_client.py` - 通用 OpenAI 兼容客户端
- `message_handler.py` - 消息处理逻辑
- `bot.py` - 机器人主类
- `connection.py` - WebSocket 连接管理
- `dispatcher.py` - 事件分发

### 3. 完整功能支持
- 私聊消息处理
- 群聊 @ 消息处理
- 多轮对话上下文
- 频率限制保护
- 长消息自动分割
- 自动重连机制
- 命令支持 (/reset, /help, /status)

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境

```bash
cp .env.example .env
```

### 3. 修改 `.env` 文件

```env
# 只需修改这三项即可切换任意服务

# 示例：DeepSeek
OPENAI_API_BASE=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_MODEL=deepseek-chat
```

### 4. 测试 API

```bash
python test_api.py
```

### 5. 启动机器人

```bash
python main.py
```

## 服务商配置示例

### OpenAI
```env
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
OPENAI_MODEL=gpt-4o
```

### DeepSeek
```env
OPENAI_API_BASE=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
OPENAI_MODEL=deepseek-chat
```

### OpenRouter
```env
OPENAI_API_BASE=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxx
OPENAI_MODEL=stepfun/step-3.5-flash:free
```

### 本地 Ollama
```env
OPENAI_API_BASE=http://localhost:11434/v1
OPENAI_API_KEY=not-needed
OPENAI_MODEL=llama2
```

### Azure OpenAI
```env
OPENAI_API_BASE=https://your-resource.openai.azure.com/openai/deployments/your-deployment
OPENAI_API_KEY=your-azure-api-key
OPENAI_MODEL=gpt-35-turbo
OPENAI_EXTRA_HEADERS={"api-version": "2023-05-15"}
```

## 高级配置

### 自定义请求参数

```env
OPENAI_EXTRA_PARAMS={"temperature": 0.7, "max_tokens": 2000, "top_p": 0.9}
```

### 自定义请求头

```env
OPENAI_EXTRA_HEADERS={"X-Custom-Header": "value"}
```

### 自定义响应解析路径

```env
# 标准 OpenAI 格式（默认）
OPENAI_RESPONSE_PATH=choices.0.message.content

# 如果服务返回格式不同
OPENAI_RESPONSE_PATH=output.choices.0.message.content
```

## 项目结构

```
.
├── config.py              # 通用配置管理
├── ai_client.py           # 通用 OpenAI 兼容客户端
├── message_handler.py     # 消息处理器
├── bot.py                 # 机器人主类
├── main.py                # 入口文件
├── connection.py          # WebSocket 连接管理
├── dispatcher.py          # 事件分发
├── models.py              # 数据模型
├── test_api.py            # API 测试工具
├── .env.example           # 环境变量模板
├── API_CONFIG_GUIDE.md    # API 配置指南
├── PROJECT_SUMMARY.md     # 项目概述（本文档）
├── requirements.txt       # 依赖列表
├── start.bat              # Windows 启动脚本
├── start.sh               # Linux/Mac 启动脚本
└── .gitignore             # Git 忽略文件
```

## 代码统计

- **总代码行数**: ~2000+ 行
- **核心模块**: 9 个 Python 文件
- **配置文件**: 3 个 (.env.example, .md 文档)
- **测试工具**: 2 个 (test_api.py 等)

## 技术栈

- **Python 3.8+** - 主语言
- **aiohttp** - 异步 HTTP 客户端
- **websockets** - WebSocket 连接
- **python-dotenv** - 环境变量管理

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

## 支持

如有问题，请查看：
1. `API_CONFIG_GUIDE.md` - 详细配置指南
2. `.env.example` - 配置模板和示例
3. `test_api.py` - API 测试工具

---

**让 AI 服务切换像修改配置文件一样简单！**