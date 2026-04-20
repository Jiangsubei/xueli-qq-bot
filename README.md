# xueli

> 轻量对话内核 · 多平台适配 · 开放 API 接入

**xueli** 是一个专注于对话能力的轻量级机器人框架。它不绑定任何特定平台，你可以把它接入 QQ（NapCat）、开放 API，或者未来任何消息渠道。

核心特点：

- 🧠 **智能对话规划** – 不只会回复，还会判断“该不该回、什么时候回、怎么回”
- 📝 **长期记忆系统** – 记住用户说过的重要信息，支持事实沉淀、会话恢复、精准召回
- 🔌 **平台解耦** – 同样的内核，通过不同的 adapter 接入 QQ、API 等渠道
- 🧩 **模块化设计** – 对话规划、节奏控制、记忆检索、风格策略均可独立配置
- 🌐 **本地 WebUI** – 提供可视化控制台，方便调试和管理
- 📡 **开放 API 接入层** – 允许第三方服务通过 HTTP 调用机器人能力

---

## ✨ 主要功能

| 功能模块 | 说明 |
|---------|------|
| 会话规划 | 群聊与私聊共用一条规划链路 |
| 缓冲窗口调度 | 每会话按时间片生成窗口，顺序消费，超时窗口自动丢弃，避免延迟叠加 |
| 节奏控制 | 避免刷屏，让对话更自然 |
| 回复规划 | 控制回复风格、情感、相关记忆的拼接 |
| 语义拆分发送 | AI 生成内容按句末标点（。！？）智能拆分为多条发送，模拟猫娘自然接话感 |
| 会话连续性 | 私聊与群聊会话永不过期，重启后自动从历史存储恢复，历史消息条数受 `max_context_length` 控制 |
| 多层记忆 | ```人物事实 / 会话摘要 / 用户偏好 ``` 使用明文存储方便阅读编辑 |
| 图片理解 | 通过视觉模型分析图片内容，增强回复内容，图片描述持久化到历史记录，重启后仍可查看 |
| 表情互动 | 根据语境追发表情，增强交互感 |
| WebUI | 实时查看会话状态、记忆内容、日志，支持在线配置 |
| API 接入 | 提供 `POST /events` 接口，任何外部系统都可以发送事件并获取回复 |

---

## 🚀 快速开始

### 环境要求

- Python 3.11+
- 一个可用的 OpenAI 兼容接口（本地或云端）
- 第三方聊天平台或任何可接入聊天的应用。如使用 NapCat 接入 QQ
> 目前api接口还处于开发阶段，可能有问题
### 安装

```bash
# 克隆项目
git clone https://github.com/Jiangsubei/xueli-qq-bot.git
cd xueli

# 创建虚拟环境
python -m venv venv
source venv/bin/activate        # Linux/macOS
venv\Scripts\activate           # Windows

# 安装依赖
pip install -r requirements.txt
```

### 配置

1. 复制配置示例并编辑：

```bash
cp xueli/config/config.example.toml xueli/config/config.toml
```

2. 修改 `xueli/config/config.toml`，至少配置：

- `[adapter_connection]` – 填写你的平台连接信息（NapCat WebSocket 地址或 API 地址）
- `[ai_service]` – 填写主模型的 API 地址、Key、模型名称

> 💡 推荐从 **NapCat + QQ** 开始测试，也可以先使用 `[adapter_connection].adapter = "api"` 模式，用 `curl` 发送事件验证。

### 启动
---

- windows

直接双击 `start.bat` 或者运行 `start.ps1`

- linux

```bash
bash start.sh
```
---

启动后你会看到：

- 日志输出，显示 adapter 连接状态
- 本地 WebUI 地址（默认 `http://127.0.0.1:8080`）

### 测试运行

项目包含单元测试，可以快速验证环境：

```bash
python -m unittest discover -s xueli/tests -t xueli
```

---

## ⚙️ 主要配置说明

配置文件 `xueli/config/config.toml` 采用 TOML 格式，主要块如下：

| 配置块 | 作用 |
|--------|------|
| `[adapter_connection]` | 设置事件来源（napcat / api），以及 WebSocket 或 HTTP 地址 |
| `[ai_service]` | 主模型（对话生成）的接口、模型名、超时等 |
| `[vision_service]` | 图片理解模型的配置（可选） |
| `[group_reply]` | 群聊的回复节流、兴趣回复、复读策略 |
| `[group_reply_decision]` | 统一规划模型（可单独指定模型） |
| `[bot_behavior]` | 最大历史条数、回复长度限制、语义拆分开关等 |
| `[planning_window]` | 私聊/群聊缓冲窗口时长、窗口排队过期时间 |
| `[memory]` | 记忆开关、检索数量、半衰期等 |
| `[memory_rerank]` | 记忆重排模型配置（可选） |

几乎所有配置都提供了合理的默认值，你只需要关注 `adapter_connection` 和 `ai_service` 即可快速跑起来。

---

## 🌐 开放 API 接入

如果你希望其他程序（例如一个 Web 应用、一个定时任务）调用机器人的对话能力，可以启用 **API runtime**。

设置环境变量：

```bash
export API_RUNTIME_ENABLED=true
export API_RUNTIME_HOST=127.0.0.1
export API_RUNTIME_PORT=8765
```

启动后，向 `POST http://127.0.0.1:8765/events` 发送 JSON 格式的事件即可。事件结构参考 `InboundEvent` 定义（见文档或源码）。

---

## 🧩 如果你是来修改代码的

这里是项目目前的结构：

```
.
├── data/                # 运行时数据（记忆、缓存、webui 资源）
├── xueli/
│   ├── config/          # 配置文件
│   ├── src/
│   │   ├── adapters/    # 平台适配器（napcat, api, ...）
│   │   ├── core/        # 核心运行时、事件分发
│   │   ├── handlers/    # 规划、节奏、上下文、回复生成
│   │   ├── memory/      # 记忆系统（存储、检索、摘要）
│   │   ├── services/    # AI 调用、图片服务等
│   │   ├── webui/       # 本地控制台后端
│   │   └── emoji/       # 表情相关逻辑
│   ├── tests/           # 单元测试
│   ├── tools/           # 工具脚本
│   └── main.py          # 启动入口
├── requirements.txt     # 依赖
├── start.bat
├── start.ps1
├── start.sh
├── README.md
├── AGENTS.md
└── API_CONFIG_GUIDE.md

```

## 🔍 当前关键模块

### 核心

- `xueli/src/core/runtime.py`
- `xueli/src/core/bootstrap.py`
- `xueli/src/core/runtime_supervisor.py`
- `xueli/src/core/dispatcher.py`
- `xueli/src/core/config.py`
- `xueli/src/core/models.py`
- `xueli/src/core/platform_models.py`
- `xueli/src/core/platform_normalizers.py`
- `xueli/src/core/platform_bridge.py`

### adapter

- `xueli/src/adapters/base.py`
- `xueli/src/adapters/registry.py`
- `xueli/src/adapters/napcat/adapter.py`
- `xueli/src/adapters/napcat/connection.py`
- `xueli/src/adapters/api/adapter.py`
- `xueli/src/adapters/api/runtime.py`

### 消息处理链

- `xueli/src/handlers/message_handler.py`
- `xueli/src/handlers/planning_window_service.py`  # 规划窗口服务
- `xueli/src/handlers/conversation_window_scheduler.py`  # 会话缓冲窗口调度器
- `xueli/src/handlers/conversation_window_models.py`  # 窗口批次与调度状态模型
- `xueli/src/handlers/conversation_planner.py`
- `xueli/src/handlers/timing_gate_service.py`
- `xueli/src/handlers/conversation_context_builder.py`
- `xueli/src/handlers/conversation_session_manager.py`
- `xueli/src/handlers/conversation_timeline_formatter.py`
- `xueli/src/handlers/message_context.py`
- `xueli/src/handlers/reply_pipeline.py`
- `xueli/src/handlers/reply_prompt_renderer.py`
- `xueli/src/handlers/reply_generation_service.py`
- `xueli/src/handlers/reply_style_policy.py`
- `xueli/src/handlers/character_card_service.py`  # 角色卡服务
- `xueli/src/handlers/conversation_engagement.py`
- `xueli/src/handlers/conversation_plan_coordinator.py`
- `xueli/src/handlers/prompt_planner.py`
- `xueli/src/handlers/temporal_context.py`

### 记忆系统

- `xueli/src/memory/memory_manager.py`
- `xueli/src/memory/memory_flow_service.py`
- `xueli/src/memory/memory_dispute_resolver.py`  # 记忆纠错裁决
- `xueli/src/memory/person_fact_service.py`
- `xueli/src/memory/chat_summary_service.py`
- `xueli/src/memory/session_restore_service.py`
- `xueli/src/memory/conversation_recall_service.py`
- `xueli/src/memory/storage/fact_evidence_store.py`  # 事实证据存储
- `xueli/src/memory/storage/conversation_store.py`
- `xueli/src/memory/storage/important_memory_store.py`
- `xueli/src/memory/storage/markdown_store.py`
- `xueli/src/memory/storage/person_fact_store.py`

---

## 🤝 贡献与反馈

项目目前由个人维护，但非常欢迎你：

- 报告 Bug 或提出新功能建议（提交 Issue）
- 提交 Pull Request 改进代码或文档
- 分享你的使用场景和配置经验

> 在提交代码前，请确保已通过现有测试，并对新增功能补充测试。

---

## 📄 许可证

本项目采用 **MIT 许可证**。你可以自由使用、修改、分发，甚至用于商业项目，只需保留原始版权声明。

---

**如果你有任何问题，欢迎提 Issue 或直接联系作者。**
