# xueli 项目架构文档

> 轻量对话内核 · 多平台适配 · 开放 API 接入

## 1. 项目概述

xueli 是一个专注于对话能力的轻量级机器人框架，不绑定任何特定平台，可接入 QQ（NapCat）、开放 API 或任何消息渠道。

### 1.1 核心设计目标

- **智能对话规划** – 判断"该不该回、什么时候回、怎么回"
- **长期记忆系统** – 三层记忆（person_fact / chat_summary / conversation_recall），支持动态遗忘和语义联想
- **自主情绪引擎** – 从对话情感密度、记忆检索失败率等内部状态自然涌现情绪
- **平台解耦** – 通过 Adapter 模式接入不同消息渠道

---

## 2. 模块架构总览

```
xueli/
├── main.py                    # 启动入口
├── xueli/
│   ├── prompts/              # 提示词模板（planner / timing / reply 等）
│   ├── config/              # 配置文件
│   ├── src/
│   │   ├── adapters/        # 平台适配器
│   │   │   ├── base.py     # PlatformAdapter / ProtocolAdapter 接口定义
│   │   │   ├── registry.py # 适配器注册表
│   │   │   ├── napcat/      # QQ/NapCat WebSocket 适配器
│   │   │   └── api/         # HTTP API 适配器
│   │   ├── core/            # 核心运行时
│   │   ├── handlers/        # 消息处理链
│   │   ├── memory/          # 记忆系统
│   │   ├── services/        # AI/图片/视觉服务
│   │   ├── emoji/           # 表情系统
│   │   └── webui/           # Web 控制台
│   └── tests/               # 单元测试
```

---

## 3. 核心模块详解

### 3.1 适配器层 (adapters/)

适配器层负责与不同消息平台交互，将平台特定协议转换为统一的事件格式。

#### 3.1.1 接口定义 (`adapters/base.py`)

```python
# PlatformAdapter - 平台连接适配器
class PlatformAdapter(ABC):
    async def run()              # 启动连接
    async def disconnect()        # 断开连接
    async def send()             # 发送原始数据
    async def send_action()      # 发送统一格式的回复动作
    def is_ready()               # 连接状态
    def attach_inbound_event()   # 归一化事件
    def normalize_inbound_payload()  # 解析平台负载

# ProtocolAdapter - 协议处理适配器
class ProtocolAdapter(ABC):
    def strip_mentions()         # 去除 @ 提及
    def extract_mentions()       # 提取 @ 提及列表
    def check_repeat_echo()      # 检测复读
```

#### 3.1.2 NapCat 适配器 (`adapters/napcat/`)

基于 WebSocket 的 QQ 消息接收与发送：

- `connection.py` - WebSocket 服务端，负责消息接收和队列管理
  - `_receive_loop` - 接收消息并分类（message/notice）
  - `_consume_loop` - 消费队列 + 50ms 节流
  - `_notice_loop` - 独立 Notice 队列（maxsize=100）
- `normalizer.py` - OneBot 协议转 InboundEvent 归一化
- `adapter.py` - PlatformAdapter 实现

#### 3.1.3 API 适配器 (`adapters/api/`)

HTTP API 接入方式，允许外部系统调用机器人能力：

- `runtime.py` - HTTP 服务端，接收 `POST /events` 事件
- `adapter.py` - 适配器实现

---

### 3.2 核心层 (core/)

#### 3.2.1 事件分发器 (`core/dispatcher.py`)

```python
class EventDispatcher:
    def register_preprocessor()    # 注册预处理器
    def on_message()               # 注册消息处理器
    def on_notice()               # 注册通知处理器
    async def dispatch()           # 分发事件到对应处理器
    async def dispatch_inbound_event()  # 分发归一化后的事件
```

事件流向：
1. 原始数据进入 `dispatch()`
2. 经过预处理器链处理
3. 根据 `post_type` 分发到对应 handlers
4. 经过后处理器链处理

#### 3.2.2 运行时主控 (`core/runtime.py`)

`BotRuntime` 是整个系统的主协调器：

**主要职责：**
- 初始化和管理所有组件
- 消息处理主循环（最多 6 轮/消息）
- 回复发送协调
- 状态同步到 WebUI

**消息处理主循环：**
```
for round_index in range(6):
    1. 节奏判断 (TimingGate) → decide_timing_first
    2. 规划 (ConversationPlanner) → plan_message
    3. 速率限制检查 → check_rate_limit
    4. 回复生成 (ReplyPipeline) → get_ai_response
    5. 发送回复 + 表情追评 → _send_response
    6. 分发下一窗口 → _try_dispatch_next_window
```

**背压机制：**
| 层级 | 机制 | 参数 | 效果 |
|------|------|------|------|
| Adapter | 消费节流 | 50ms/条 | 限制接收速率 |
| Adapter | 有界通知队列 | maxsize=100 | Notice 事件背压 |
| Session | 触发阈值 | 1~N 条消息 | 消息聚合 |
| Session | 静默期 | 50ms | 等待消息平静 |

#### 3.2.3 引导程序 (`core/bootstrap.py`)

`BotBootstrapper` 负责：
- 配置验证
- 依赖组件构建
- 适配器创建
- 记忆管理器初始化

#### 3.2.4 消息流水线 (`core/session_message_pipeline.py`)

`SessionMessagePipeline` 实现：
- **per-user 串行执行** - 同一用户消息顺序处理
- **per-group 并发控制** - 群聊消息并发处理限制

```python
async def submit(execution_key, trace_id, event, handler):
    # 消息入队
    # 创建/复用 worker
    # worker 串行处理同一会话的消息
```

#### 3.2.5 平台模型 (`core/platform_models.py`)

定义平台无关的数据模型：

```python
@dataclass
class InboundEvent:        # 平台无关的入站事件
    platform: str
    adapter: str
    event_type: EventType
    message_kind: MessageKind
    session: SessionRef    # 会话引用
    sender: SenderRef      # 发送者引用
    text: str
    attachments: Tuple[AttachmentRef, ...]

@dataclass
class ReplyAction:         # 回复动作
    session: SessionRef
    text: str
    segments: Tuple[Dict, ...]

@dataclass
class SessionRef:          # 会话标识
    platform: str
    scope: SessionScope    # private / shared / channel
    conversation_id: str
```

---

### 3.3 处理器层 (handlers/)

#### 3.3.1 消息处理器 (`handlers/message_handler.py`)

`MessageHandler` 是高层消息编排层，协调所有处理组件：

**组件初始化：**
```python
self.conversation_planner = ConversationPlanner(...)
self.timing_gate_service = TimingGateService(...)
self.reply_pipeline = ReplyPipeline(self)
self.planning_window_service = PlanningWindowService(...)
self.memory_flow_service = MemoryFlowService(...)
self.emoji_manager = EmojiManager(...)
self.character_card_service = CharacterCardService(...)
```

**核心方法：**
- `plan_message()` - 消息规划入口
- `get_ai_response()` - 获取 AI 回复
- `build_message_context()` - 构建消息上下文
- `decide_timing_first()` - 首次节奏判断

#### 3.3.2 规划窗口服务 (`handlers/planning_window_service.py`)

`PlanningWindowService` 管理会话缓冲窗口：

```python
async def submit_event(event, trace_id)  # 提交消息到窗口
async def mark_window_complete(key, seq)  # 标记窗口完成
async def cleanup(active_keys)            # 清理过期窗口
```

#### 3.3.3 会话窗口调度器 (`handlers/conversation_window_scheduler.py`)

`ConversationWindowScheduler` 实现滚动窗口管理：

```python
class ConversationWindowScheduler:
    # 每个会话维护一个 active_buffer 和一个 queued_windows 队列
    # 窗口触发条件：min_messages 条消息 或 窗口超时
    # 窗口完成后自动分发下一窗口
```

**窗口状态机：**
```
buffer_opened → messages accumulating → close_after_window
                                        ↓
                                 queued_windows
                                        ↓
                              dispatch_next_window
                                        ↓
                                 processing
                                        ↓
                              mark_window_complete
```

#### 3.3.4 对话规划器 (`handlers/conversation_planner.py`)

`ConversationPlanner` 调用专门的规划模型决定 reply/wait/ignore：

**核心流程：**
1. 构建 system prompt（从 `planner.prompt` 模板加载）
2. 构建 user prompt（包含窗口消息、历史、上下文）
3. 调用规划模型获取决策
4. 解析 `PromptPlan`（上下文策略、记忆策略、语气策略）

**PromptPlan 结构：**
```python
@dataclass
class PromptPlan:
    context_policy: ContextPolicy   # 上下文开关
    memory_policy: MemoryPolicy     # 记忆开关
    tone_policy: TonePolicy         # 语气策略
    reply_goal: str                # 回复目标
    constraints: List[str]          # 约束条件
```

#### 3.3.5 节奏门控服务 (`handlers/timing_gate_service.py`)

`TimingGateService` 是第二层节奏控制：

**职责：**
- 在规划后、可见回复前做最终节奏判断
- `decide()` - 规划后调用
- `decide_timing_only()` - 首次节奏判断

**决策结果：**
- `continue` - 继续生成回复
- `wait` - 等待更多消息
- `no_reply` / `ignore` - 不回复

**回退逻辑：**
```python
def _fallback_decision():
    if signals.get("_force_timing_continue"):  # @打断
        return CONTINUE
    if signals.get("has_image_without_text"):
        return WAIT
    return CONTINUE
```

#### 3.3.6 回复管道 (`handlers/reply_pipeline.py`)

`ReplyPipeline` 是 Prompt 编译器和回复生成协调器：

**执行流程：**
```python
async def execute(event, user_message, plan, context):
    1. prepare_request()    # 准备请求
       - 构建 MessageContext
       - 处理图片
       - 检索记忆
       - 渲染 prompt
    2. generate_reply()     # 调用 AI 生成
    3. _persist_reply_result()  # 记忆写入
    4. 返回 ReplyResult
```

**组件协作：**
- `ReplyPromptRenderer` - 渲染 prompt
- `ReplyGenerationService` - 调用 AI
- `MoodEngine` - 情绪引擎
- `MemoryFlowService` - 记忆写入

#### 3.3.7 回复提示渲染器 (`handlers/reply_prompt_renderer.py`)

`ReplyPromptRenderer` 负责组装最终发给 AI 的 prompt：

- system prompt 拼接（人设、对话风格、行为规范）
- 记忆区块注入
- 时间上下文
- 风格策略

#### 3.3.8 回复生成服务 (`handlers/reply_generation_service.py`)

`ReplyGenerationService` 处理 AI 调用：

- 模型路由选择
- 重试逻辑
- 响应解析（支持 JSON 数组分段格式）
- 错误处理与兜底

#### 3.3.9 上下文构建器 (`handlers/conversation_context_builder.py`)

`ConversationContextBuilder` 构建消息处理所需的完整上下文：

```python
@dataclass
class MessageContext:
    trace_id: str
    execution_key: str
    conversation_key: str
    user_message: str
    current_sender_label: str
    temporal_context: TemporalContext
    recent_history_text: str
    planning_signals: Dict
    conversation: Conversation
    # 记忆上下文
    person_fact_context: str
    persistent_memory_context: str
    session_restore_context: str
    precise_recall_context: str
    dynamic_memory_context: str
```

#### 3.3.10 时间上下文 (`handlers/temporal_context.py`)

`TemporalContext` 包含时间相关的上下文：

```python
@dataclass
class TemporalContext:
    current_event_time: float
    previous_message_time: float
    conversation_last_time: float
    previous_session_time: float
    recent_gap_bucket: str       # 最近消息间隔桶
    conversation_gap_bucket: str  # 会话间隔桶
    session_gap_bucket: str       # 跨会话间隔桶
    continuity_hint: str          # 连续性提示
```

---

### 3.4 记忆系统 (memory/)

#### 3.4.1 架构概览

```
MemoryManager
├── storage/
│   ├── MarkdownMemoryStore      # 普通记忆（Markdown）
│   ├── ImportantMemoryStore     # 重要记忆
│   ├── PersonFactStore         # 人物事实
│   └── SQLiteConversationStore  # 对话历史
├── retrieval/
│   ├── BM25Index               # BM25 全文检索
│   ├── VectorIndex             # 轻量向量索引（n-gram 余弦）
│   └── TwoStageRetriever       # 两阶段检索（BM25 + 向量）
├── extraction/
│   └── MemoryExtractor         # LLM 记忆提取
└── internal/
    ├── MemoryBackgroundCoordinator  # 后台协调
    ├── MemoryRetrievalCoordinator   # 检索协调
    └── MemoryIndexCoordinator       # 索引协调
```

#### 3.4.2 三层记忆

| 层级 | 说明 | 存储 |
|------|------|------|
| person_fact | 人物事实、偏好、关系 | JSON + Markdown |
| chat_summary | 聊天摘要、模式、趋势 | Markdown |
| conversation_recall | 对话历史 | SQLite |

#### 3.4.3 拟人化特性

**动态遗忘（用进废退）：**
- 普通记忆按指数衰减：`importance = base * (0.5 ^ (days / half_life))`
- `core_fact` 3x 半衰期，`important` 1.5x，`casual` 0.7x
- 检索命中的记忆自动强化

**冷记忆加速衰减：**
- 超过 `cold_memory_threshold_days`（默认 90 天）触发额外衰减

**软遗忘（归档动态折扣）：**
- 归档记忆仍可被检索，但分数打折
- `[归档激活]` 日志记录唤醒

**情绪标记：**
- LLM 推断对话情绪 `emotional_tone`
- 带情绪的记忆衰减时获得 +0.2 留存加成

**离线消化：**
- 自动扫描近期记忆
- LLM 归纳模式/趋势/变化
- 生成 insight 存入重要记忆

#### 3.4.4 检索流程

```python
async def retrieve():
    # 第一阶段：BM25 初排
    bm25_results = bm25_index.search(query, top_k)

    # 第二阶段：向量联想
    if rerank_enabled:
        vector_scores = vector_index.score(query, candidates)
        combined_scores = bm25 * bm25_weight + vector * vector_weight
        rerank(top_k)

    # 归档惩罚
    for item in results:
        if item.is_archived:
            score *= archive_penalty
```

---

### 3.5 服务层 (services/)

#### 3.5.1 AI 客户端 (`services/ai_client.py`)

`AIClient` 封装 OpenAI 兼容 API 调用：

```python
class AIClient:
    async def chat_completion(messages, temperature, model)
    def build_text_message(role, content)
    def build_image_message(role, image_url_or_base64)
```

#### 3.5.2 视觉客户端 (`services/vision_client.py`)

`VisionClient` 处理图片理解：

```python
async def analyze_images(base64_images, user_text, trace_id):
    # 调用视觉模型
    # 返回 ImageAnalysisResult
    # - per_image_descriptions
    # - merged_description
    # - failure_count
```

#### 3.5.3 图片客户端 (`services/image_client.py`)

`ImageClient` 处理图片下载和 base64 编码：

```python
async def process_image_segment(segment) -> base64:
    # 从 QQ CDN 下载图片
    # 转为 base64
```

---

### 3.6 表情系统 (emoji/)

#### 3.6.1 模块结构

```
emoji/
├── manager.py      # EmojiManager - 总协调
├── repository.py    # EmojiDatabase - 持久化存储
├── reply_service.py # EmojiReplyService - 表情追评
├── database.py      # SQLite 数据库操作
└── models.py       # EmojiItem, EmojiEmotionResult
```

#### 3.6.2 EmojiManager

**职责：**
- 采集原生表情引用（face/mface）
- 空闲时分类（emotion_labels）
- 表情数据管理

#### 3.6.3 EmojiReplyService

**职责：**
- 回复后决定是否发送表情追评
- 基于 `emoji_reply.prompt` 模板决策
- `plan_follow_up()` - 规划表情追评
- `build_follow_up_action()` - 构建发送动作

---

### 3.7 WebUI 系统 (webui/)

#### 3.7.1 组件

```
webui/
├── runtime_server.py   # WebUI HTTP 服务
├── console/            # 控制台后端
│   ├── handlers/      # API 处理器
│   └── commands/       # 命令处理
└── webui_site/        # 前端静态文件
```

#### 3.7.2 功能

- 实时查看会话状态
- 记忆内容查看与编辑
- 日志查看
- 在线配置
- 运行时控制（启停）

---

## 4. 业务流程

### 4.1 消息接收流程

```
NapCat WebSocket / API HTTP
        ↓
EventDispatcher.dispatch()
        ↓
预处理器链（日志、归一化）
        ↓
MessageHandler.handle_message()
        ↓
SessionMessagePipeline.submit()
        ↓
per-session worker 串行处理
```

### 4.2 消息处理主流程

```
收到消息
    ↓
┌─────────────────────────────────────────────┐
│ 主循环 (最多 6 轮)                          │
├─────────────────────────────────────────────┤
│ 1. 节奏判断 (TimingGateService)             │
│    - @提及 → 直接 continue                  │
│    - 其他 → 调用模型判断 continue/wait/ignore │
│    ↓                                       │
│ 2. 规划 (ConversationPlanner)               │
│    - 构建 PromptPlan                        │
│    - 决定 reply/wait/ignore                │
│    ↓                                       │
│ 3. 速率限制检查                              │
│    ↓                                       │
│ 4. 回复生成 (ReplyPipeline)                  │
│    - 加载记忆上下文                          │
│    - 渲染 prompt                           │
│    - 调用 AI                               │
│    ↓                                       │
│ 5. 发送回复 + 表情追评                       │
│    - 分条发送                               │
│    - 随机延迟                               │
│    - emoji follow-up                       │
│    ↓                                       │
│ 6. 分发下一窗口                              │
│    - mark_window_complete                  │
│    - dispatch_next_window                  │
└─────────────────────────────────────────────┘
    ↓
记忆写入 (MemoryFlowService)
    ↓
清理过期状态
```

### 4.3 记忆流程

```
消息处理完成
    ↓
MemoryFlowService.enqueue()
    ↓
后台队列处理
    ↓
┌────────────────────────────────────┐
│ 1. 事实提取 (MemoryExtractor)        │
│    - 调用 LLM 提取人物事实           │
│    - 保存到 PersonFactStore         │
├────────────────────────────────────┤
│ 2. 聊天摘要 (ChatSummaryService)     │
│    - 定期汇总聊天内容                │
│    - 保存到 MarkdownMemoryStore     │
├────────────────────────────────────┤
│ 3. 离线消化 (BackgroundCoordinator)  │
│    - 归纳模式/趋势/变化              │
│    - 生成 insight                   │
├────────────────────────────────────┤
│ 4. 索引更新                         │
│    - BM25 索引更新                  │
│    - 向量索引更新                   │
└────────────────────────────────────┘
```

---

## 5. 配置架构

### 5.1 配置结构 (`config.toml`)

```toml
[adapter_connection]      # 平台连接配置
adapter = "napcat"        # napcat / api
ws_url = "..."
http_url = "..."

[ai_service]              # 主模型配置
api_base = "..."
api_key = "..."
model = "..."
response_timeout = 60

[vision_service]           # 视觉模型配置
enabled = false
api_base = "..."

[emoji]                   # 表情配置
enabled = false
storage_path = "data/emojis"
capture_enabled = true

[group_reply]             # 群聊回复配置
only_reply_when_at = true
interest_reply_enabled = false
repeat_echo_enabled = false

[group_reply_decision]    # 规划模型配置
api_base = "..."
model = "..."

[planning_window]         # 窗口配置
private_window_seconds = 1.2
group_proactive_window_seconds = 0.45
group_max_concurrent = 3

[memory]                  # 记忆配置
enabled = true
storage_path = "data/memories"
auto_extract = true
bm25_top_k = 10

[character_growth]        # 角色成长配置
mood_fluctuation_enabled = false
mood_volatility = 0.3
```

### 5.2 配置验证

`BotBootstrapper._log_runtime_config()` 启动时输出配置摘要：

```
运行配置：助手=xxx，回复模型=xxx，地址=xxx
视觉服务：状态=xxx，模型=xxx
群聊规划：已配置=xxx，模型=xxx，仅@回复=xxx，兴趣回复=xxx
记忆配置：自动提取=xxx，每xxx轮提取一次
```

---

## 6. 三大提示词模板

### 6.1 planner.prompt

会话规划模板，决定 reply/wait/ignore：

```markdown
你是一个对话规划助手...
决策输出格式：
{
    "action": "reply|wait|ignore",
    "reason": "...",
    "context_policy": {...},
    "memory_policy": {...},
    "tone_policy": {...},
    "reply_goal": "..."
}
```

### 6.2 timing_gate.prompt

节奏门控模板，决定 continue/wait/no_reply：

```markdown
你是一个节奏控制助手...
根据当前对话节奏判断：
- continue: 当前适合回复
- wait: 等待更多消息
- no_reply: 不回复
```

### 6.3 reply.prompt

回复生成模板，生成最终回复内容：

```markdown
你是一个对话助手...
根据以下信息生成回复：
- 身份设定
- 对话历史
- 相关记忆
- 风格策略
```

---

## 7. 关键数据流

### 7.1 InboundEvent 归一化流

```
OneBot Event (NapCat)
    ↓
normalize_onebot_message_event()
    ↓
InboundEvent (平台无关)
    ↓
attach_inbound_event(event)
    ↓
MessageEvent._inbound_event = inbound_event
```

### 7.2 回复发送流

```
ReplyResult (text, segments)
    ↓
ReplySendOrchestrator.build_part_plan()
    ↓
分段计划 List[ReplyPartPlan]
    ↓
BotRuntime._send_response()
    ↓
适配器 send_action(ReplyAction)
    ↓
平台特定协议发送
```

---

## 8. 状态管理

### 8.1 运行时状态

```python
BotRuntime.status = {
    "connected": bool,
    "ready": bool,
    "messages_received": int,
    "messages_sent": int,
    "errors": int,
}
```

### 8.2 会话状态

```python
ConversationWindowState = {
    "processing": BufferedWindow,      # 当前处理窗口
    "active_buffer": BufferedWindow,   # 活跃缓冲
    "queued_windows": List[BufferedWindow],  # 队列
    "next_seq": int,
    "last_activity_at": float,
}
```

### 8.3 窗口状态

```python
BufferedWindow = {
    "conversation_key": str,
    "seq": int,
    "chat_mode": str,
    "opened_at": float,
    "messages": List[Dict],
    "latest_event": MessageEvent,
    "min_messages": int,
    "expires_at": float,
    "planning_signals": Dict,
}
```

---

## 9. 扩展指南

### 9.1 新增适配器

1. 在 `adapters/` 下创建新目录，如 `myplatform/`
2. 实现 `PlatformAdapter` 接口
3. 在 `adapters/registry.py` 注册

```python
# adapters/myplatform/adapter.py
class MyPlatformAdapter(PlatformAdapter):
    async def run(self): ...
    async def disconnect(self): ...
    async def send(self, data): ...
    async def send_action(self, action): ...
    def is_ready(self): ...
```

### 9.2 新增记忆类型

1. 在 `memory/storage/` 实现存储类
2. 在 `MemoryManager.__init__()` 初始化
3. 在 `MemoryRetrievalCoordinator` 添加检索逻辑

### 9.3 新增提示词模板

1. 在 `xueli/prompts/zh-CN/` 创建 `.prompt` 文件
2. 在 `PromptTemplateLoader` 添加加载逻辑
3. 在对应 Handler 中调用

---

## 10. 目录结构

```
xueli/
├── main.py                      # 启动入口
├── requirements.txt             # 依赖
├── start.bat / start.ps1 / start.sh  # 启动脚本
├── xueli/
│   ├── prompts/
│   │   └── zh-CN/
│   │       ├── planner.prompt
│   │       ├── timing_gate.prompt
│   │       ├── reply.prompt
│   │       ├── vision.prompt
│   │       ├── emoji_reply.prompt
│   │       └── ...
│   ├── config/
│   │   ├── config.example.toml
│   │   └── config.toml
│   ├── src/
│   │   ├── adapters/
│   │   │   ├── base.py
│   │   │   ├── registry.py
│   │   │   ├── napcat/
│   │   │   │   ├── adapter.py
│   │   │   │   ├── connection.py
│   │   │   │   └── normalizer.py
│   │   │   └── api/
│   │   │       ├── adapter.py
│   │   │       └── runtime.py
│   │   ├── core/
│   │   │   ├── bootstrap.py
│   │   │   ├── config.py
│   │   │   ├── dispatcher.py
│   │   │   ├── models.py
│   │   │   ├── mood_engine.py
│   │   │   ├── platform_models.py
│   │   │   ├── platform_normalizers.py
│   │   │   ├── platform_bridge.py
│   │   │   ├── runtime.py
│   │   │   ├── runtime_metrics.py
│   │   │   ├── runtime_supervisor.py
│   │   │   ├── session_message_pipeline.py
│   │   │   └── ...
│   │   ├── handlers/
│   │   │   ├── message_handler.py
│   │   │   ├── planning_window_service.py
│   │   │   ├── conversation_planner.py
│   │   │   ├── timing_gate_service.py
│   │   │   ├── reply_pipeline.py
│   │   │   ├── reply_prompt_renderer.py
│   │   │   ├── reply_generation_service.py
│   │   │   ├── conversation_window_scheduler.py
│   │   │   ├── conversation_context_builder.py
│   │   │   └── ...
│   │   ├── memory/
│   │   │   ├── memory_manager.py
│   │   │   ├── memory_flow_service.py
│   │   │   ├── memory_dispute_resolver.py
│   │   │   ├── person_fact_service.py
│   │   │   ├── chat_summary_service.py
│   │   │   ├── session_restore_service.py
│   │   │   ├── conversation_recall_service.py
│   │   │   ├── storage/
│   │   │   │   ├── markdown_store.py
│   │   │   │   ├── important_memory_store.py
│   │   │   │   ├── person_fact_store.py
│   │   │   │   └── sqlite_conversation_store.py
│   │   │   ├── retrieval/
│   │   │   │   ├── bm25_index.py
│   │   │   │   ├── vector_index.py
│   │   │   │   └── two_stage_retriever.py
│   │   │   ├── extraction/
│   │   │   │   └── memory_extractor.py
│   │   │   └── internal/
│   │   │       ├── background_coordinator.py
│   │   │       └── ...
│   │   ├── services/
│   │   │   ├── ai_client.py
│   │   │   ├── image_client.py
│   │   │   └── vision_client.py
│   │   ├── emoji/
│   │   │   ├── manager.py
│   │   │   ├── repository.py
│   │   │   ├── reply_service.py
│   │   │   ├── database.py
│   │   │   └── models.py
│   │   └── webui/
│   │       ├── runtime_server.py
│   │       ├── console/
│   │       └── webui_site/
│   └── tests/
│       ├── test_*.py
│       └── ...
└── data/                       # 运行时数据（gitignore）
    ├── memories/
    ├── emojis/
    ├── conversations/
    └── webui/
```

---

*文档版本：2026-05-04*
*项目版本：xueli*