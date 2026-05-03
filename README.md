
---

# xueli

> 开放 · 轻量 · 平台解耦

**xueli** 是一个专门用来聊天的轻量机器人框架。它不绑定任何特定平台，你可以把它接到 QQ（通过 NapCat）、开放 API，或者任何你想要的地方。

核心特点：

- 🧠 **智能对话规划** – 判断该不该回（TimingGate）、怎么回（Planner）— 节奏与策略分离
- 📝 **长期记忆系统** – 记住用户说过的重要信息，支持事实沉淀、会话恢复、精准召回，记忆会真实衰减和遗忘
- 🧠 **自主情绪引擎** – AI 的情绪不由正弦波驱动，而是从对话情感密度、记忆检索失败率等内部状态自然涌现
- 🔌 **平台解耦** – 同样的内核，通过不同的 adapter 接入 QQ、API 等渠道
- 🧩 **模块化设计** – 对话规划、节奏控制、记忆检索、风格策略、情绪引擎均可独立配置
- 🌐 **本地 WebUI** – 提供可视化控制台，方便调试和管理
- 📡 **开放 API 接入层** – 允许第三方服务通过 HTTP 调用机器人能力

## 主要功能

| 模块 | 能做什么 |
|------|---------|
| 会话规划 (Planner) | 决定“怎么回话”，生成一个计划，告诉后面的步骤该用哪些记忆、什么语气等 |
| 缓冲窗口调度 | 聊天消息按时间切成一段一段处理，超时自动丢弃，防止回复越堆越慢 |
| 节奏控制 (TimingGate) | 决定“要不要插话”，避免刷屏，让对话更自然 |
| 回复规划 | 控制回复的风格、情绪以及关联哪些记忆 |
| 提示词模板 | 把主要的提示词（规划、节奏、回复）抽出来放在模板文件里，方便修改和调试 |
| 结构化分段发送 | 回复默认拆成多条消息，程序负责清洗、逐条发出并加随机延迟，更像真人在打字 |
| 会话连续性 | 私聊和群聊对话永不过期，重启后自动恢复，并记住上次说话的时间用来判断对话是否还在继续 |
| 多层记忆 | 三层记忆（人物事实/对话摘要/普通回忆），Markdown + SQLite 明文存储。记忆会“用进废退”，太久不碰的会加速遗忘；带情绪的记忆更难被忘掉；还会后台自动消化近期对话，总结出新洞察 |
| 图片理解 | 会分析图片内容来更好地理解你在说什么，但机器人不会把图片当成表情包发出去 |
| 表情互动 | 只用平台自带的表情（如 QQ 的 face 表情），不再乱发本地图片当表情包 |
| WebUI | 实时看会话状态、记忆内容和日志，还能在线调配置 |
| API 接入 | 提供 `POST /events` 接口，任何外部系统都可以发消息给机器人并得到回复 |

## 快速开始

### 你需要准备

- Python 3.11 或更高版本
- 一个能用的 OpenAI 兼容接口（本地模型或云端都行）
- 一个聊天平台，比如用 NapCat 连接 QQ，或者用 API 方式接入

### 安装

```bash
# 先把代码下载下来
git clone https://github.com/Jiangsubei/xueli-qq-bot.git
cd xueli

# 创建独立的虚拟环境（是个好习惯）
python -m venv venv

# 激活虚拟环境
source venv/bin/activate      # Linux/macOS
venv\Scripts\activate         # Windows

# 安装依赖
uv pip install -r requirements.txt
```

### 配置

1. 从示例复制一份配置文件：
```bash
cp xueli/config/config.example.toml xueli/config/config.toml
```

2. 用文本编辑器打开 `xueli/config/config.toml`，至少填好这两个地方：
   - `[adapter_connection]` – 你的 NapCat WebSocket 地址或 API 地址
   - `[ai_service]` – 你用的 AI 模型的地址、密钥和模型名

### 启动

**Windows 用户**  
直接双击 `start.bat`，或者运行 `start.ps1`。

**Linux / macOS 用户**  
在终端执行：
```bash
bash start.sh
```

启动后你会看到：
- 日志输出，告诉你连接状态
- 本地网页控制台的地址（默认 `http://127.0.0.1:8080`）

### 快速测试

项目自带了一些测试。在项目根目录下，运行前确保虚拟环境已激活：
```bash
python -m unittest discover -s xueli/tests -t xueli
```

## 主要配置说明

配置文件 `config.toml` 里这些部分你可能需要关心：

| 配置块 | 用途 |
|--------|---------|
| `[adapter_connection]` | 消息来源：napcat 还是 api，以及地址 |
| `[ai_service]` | 主要聊天模型的设置 |
| `[vision_service]` | 识图模型（可选） |
| `[emoji]` | 是否回复表情 |
| `[group_reply]` | 群聊回复频率、复读策略 |
| `[group_reply_decision]` | 群聊策略可以使用单独的模型（可选） |
| `[bot_behavior]` | 回复长度、回复拆分、延时等行为 |
| `[planning_window]` | 私聊/群聊的消息缓冲时间和过期设置 |
| `[memory]` | 记忆开关、上限、遗忘速度等 |
| `[memory_rerank]` | 记忆重排序的模型（可选） |

如果你觉得机器人回复的节奏不舒服，可以调整 `[bot_behavior]` 里的这些参数：
- `segmented_reply_enabled` – 是否开启消息分段
- `max_segments` – 最多拆分成几条
- `first_segment_delay_min_ms` / `first_segment_delay_max_ms` – 第一条发出前的等待时间
- `followup_delay_min_seconds` / `followup_delay_max_seconds` – 后续每条之间的间隔

## 开放 API 接入

如果你想让其他程序（比如网站、定时脚本）也能跟机器人聊天，可以打开 API 服务。

在启动前设置这几个环境变量：
```bash
export API_RUNTIME_ENABLED=true
export API_RUNTIME_HOST=127.0.0.1
export API_RUNTIME_PORT=8765
```

然后启动机器人，用 `POST http://127.0.0.1:8765/events` 发 JSON 格式的消息就行了。消息格式参考代码里的 `InboundEvent` 定义。

## 如果你是来改代码的

项目目录结构：

```
.
├── data/                # 运行产生的数据（记忆、缓存、网页资源）
├── xueli/
│   ├── prompts/         # 提示词模板文件
│   ├── config/          # 配置文件
│   ├── src/
│   │   ├── adapters/    # 平台转接器（目前有 napcat 和 api）
│   │   ├── core/        # 核心运行时、事件分发、情绪引擎等
│   │   ├── handlers/    # 消息处理流程：规划、节奏、上下文、回复生成
│   │   ├── memory/      # 记忆系统（存储、搜索、总结）
│   │   ├── services/    # 调用 AI、图片处理等
│   │   ├── webui/       # 网页控制台后端
│   │   └── emoji/       # 表情相关
│   ├── tests/           # 单元测试
│   └── tools/           # 工具脚本
├── main.py              # 启动入口
├── requirements.txt
├── start.bat / start.ps1 / start.sh
├── README.md
└── .docs/               # 设计文档
```

### 代码地图

**核心运行时**
- `xueli/src/core/runtime.py` – 主循环
- `xueli/src/core/bootstrap.py` – 启动初始化
- `xueli/src/core/runtime_supervisor.py` – 运行时
- `xueli/src/core/dispatcher.py` – 事件分发
- `xueli/src/core/mood_engine.py` – 情绪
- `xueli/src/core/model_invocation_router.py` – 模型调用路由
- `xueli/src/core/prompt_templates.py` – 提示词管理
- `xueli/src/core/reply_send_orchestrator.py` – 回复发送编排
- `xueli/src/core/platform_models.py` / `platform_normalizers.py` / `platform_bridge.py` – 平台抽象相关

**平台适配**
- `xueli/src/adapters/base.py` – 适配器基类
- `xueli/src/adapters/registry.py` – 适配器注册
- `xueli/src/adapters/napcat/` – NapCat 适配（WebSocket 连接、OneBot 消息归一化）
- `xueli/src/adapters/api/` – API 适配与运行时

**消息处理**
- `xueli/src/handlers/conversation_planner.py` – 对话规划 (怎么回)
- `xueli/src/handlers/timing_gate_service.py` – 节奏控制 (要不要回)
- `xueli/src/handlers/conversation_context_builder.py` – 上下文构造
- `xueli/src/handlers/reply_pipeline.py` – 回复生成流水线
- `xueli/src/handlers/reply_prompt_renderer.py` – 回复提示词渲染
- `xueli/src/handlers/reply_style_policy.py` – 回复风格策略
- `xueli/src/handlers/character_card_service.py` – 角色卡与关系追踪
- `xueli/src/handlers/planning_window_service.py` – 规划窗口服务
- `xueli/src/handlers/conversation_window_scheduler.py` – 缓冲窗口调度

**记忆系统**
- `xueli/src/memory/memory_manager.py` 
- `xueli/src/memory/memory_flow_service.py` – 记忆流处理
- `xueli/src/memory/memory_dispute_resolver.py` – 记忆冲突裁决
- `xueli/src/memory/person_fact_service.py` – 人物事实提取
- `xueli/src/memory/chat_summary_service.py` – 对话摘要
- `xueli/src/memory/session_restore_service.py` – 会话恢复
- `xueli/src/memory/conversation_recall_service.py` – 普通记忆
- `xueli/src/memory/storage/` – 各种存储实现（SQLite、Markdown、事实、重要记忆等）
- `xueli/src/memory/retrieval/vector_index.py` – 轻量语义索引
- `xueli/src/memory/internal/background_coordinator.py` – 后台提取

## 图片和表情

- **普通图片**：只用来“看懂”（视觉理解、OCR 等），不会存储。
- **原生表情**：只采集和存储平台自带的 `mface` 表情。
- 表情功能目前仅为 `QQ` 适配

### 提示词都在这里
所有提示词模板文件都放在 `xueli/prompts/zh-CN/` 下，看名字就能知道大概用途。实际运行时采用“主模板 + 代码动态拼接”的方式，方便改小段文案而不用动整个提示词。

planner 不再负责判断“回不回”，只输出“怎么回”的策略；节奏的决策完全由 `TimingGateService` 统一负责。

### 记忆系统

记忆用 Markdown 文件 + SQLite 数据库保存，分三个层次：人物事实、重要记忆、日常回忆。它模仿人的记忆特点：

- **越用越记得**：被回想起来的记忆会变得更加牢固，太久不碰就慢慢减弱。
- **冷门记忆忘得快**：超过 90 天没再提起的事，遗忘速度会加快。
- **归档不是真的忘**：被归档的记忆仍然可以被检索到，只是分数会打折扣；如果被重新唤醒，还能“激活”回来。
- **情绪加分**：带有情绪色彩的记忆更难被遗忘。
- **自动消化总结**：后台会定期用 AI 扫描近期记忆，归纳出一些趋势或洞察，作为重要记忆存下来。
- **语义联想**：用一个极轻量的字符 n-gram 向量做相似记忆推荐，不依赖任何外部向量数据库。

## 贡献与反馈

项目目前由个人维护，欢迎：
- 报告 Bug 或提出新功能建议（直接提 Issue）
- 提交 Pull Request 改进代码或文档
- 分享你的使用场景和配置经验

> 提交代码前，请确保现有测试都能通过，并且为新功能补充了测试。

## 许可证

本项目采用 **MIT 许可证**。你可以自由使用、修改、分发，甚至是商用，只需保留原始版权声明。

---

**有任何问题，欢迎提交 Issue**