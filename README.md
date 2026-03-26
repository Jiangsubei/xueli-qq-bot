# Claude QQ Bot

一个基于 NapCat 与 OpenAI 兼容 API 的 QQ 机器人项目。当前入口采用 `config.json` 驱动，运行时由 `QQBot -> BotBootstrapper -> NapCatConnection/MessageHandler/MemoryManager` 组成，目标是让启动、消息编排、AI 调用、图片理解和记忆系统都保持可测试、可关闭、可扩展。

## 当前架构

- `main.py`
  启动入口，只负责事件循环策略与 `QQBot.run()`。
- `src/core/config.py`
  强类型配置入口。`Config.validate()` 会在启动时聚合所有配置错误；模块级 `config` 仍保留兼容访问方式。
- `src/core/bot.py`
  运行时协调器，负责信号处理、运行循环、消息任务管理、状态统计和统一关闭。
- `src/core/bootstrap.py`
  生命周期装配器，负责配置校验、依赖构造、记忆初始化、消息处理器装配和连接创建。
- `src/core/lifecycle.py`
  统一资源关闭与后台任务取消辅助函数。
- `src/handlers/`
  `message_handler.py` 只做编排；命令、会话、群聊规划、回复流水线分别下沉到独立协作者。
- `src/services/ai_client.py`
  AI facade。请求构造、响应解析、错误映射、HTTP session 生命周期拆在 `src/services/ai/` 内部模块。
- `src/services/vision_client.py`
  独立视觉模型门面，负责把图片转换成短文本描述，再交给 planner 或主回复模型复用。
- `src/memory/memory_manager.py`
  Memory facade。存储访问、检索编排、抽取调度、索引维护、后台任务管理拆在 `src/memory/internal/` 内部协作者。
- `tests/`
  基于 `unittest` 的模块化测试，覆盖 handler、config、bot lifecycle、AI client、vision client、memory manager 等关键路径。

## 启动流程

1. `main.py` 创建 `QQBot` 并调用 `QQBot.run()`。
2. `QQBot.initialize()` 调用 `BotBootstrapper.build()`。
3. `Config.validate()` 加载并校验 `config.json`，一次性返回全部配置错误。
4. Bootstrapper 按顺序构造 `MemoryManager`、`MessageHandler`、`NapCatConnection`，并按配置决定是否启用 `VisionClient`。
5. `QQBot` 启动 WebSocket 运行循环，并在关闭时统一回收 connection、handler、memory、vision 和后台任务。

## 配置说明

项目默认读取仓库根目录下的 `config.json`，不再依赖 `.env`。当前结构保持兼容，重点分区如下：

- `napcat`
  NapCat WebSocket 与 HTTP 地址。
- `ai_service`
  主 AI 服务配置，包括 `api_base`、`api_key`、`model`、`extra_params`、`extra_headers`、`response_path`。
- `vision_service`
  独立视觉模型配置。结构与 `ai_service` 对齐，只有在 `enabled=true` 且 `api_base`、`api_key`、`model` 都完整时才会真正启用；否则进入 text-only 模式。
- `bot_behavior`
  上下文长度、超时、消息长度、限流等运行参数。
- `assistant_profile`
  助手名称与别名。
- `group_reply`
  群聊触发策略、burst 聚合窗口、planner 并发限制。
- `group_reply_decision`
  群聊规划器可选的独立模型配置；为空时自动回退到 `ai_service`。
- `personality` / `dialogue_style` / `behavior`
  系统提示词片段。
- `memory`
  memory 开关、读范围、检索参数、抽取参数、衰减参数与独立 extraction client 配置；为空时自动回退到 `ai_service`。

### 推荐读取方式

新代码优先使用强类型入口：

```python
from src.core.config import config, get_vision_service_status

app = config.app
model = app.ai_service.model
vision_status = get_vision_service_status(app)
burst_window = app.group_reply.burst_window_seconds
read_scope = app.memory.read_scope
```

模块级 `config` 的扁平属性和 `__getattr__` 兼容层仍然保留，但只用于过渡兼容，不建议在新代码里继续新增 `config.OPENAI_MODEL`、`getattr(config, ...)` 或 `hasattr(config, ...)` 这类访问方式。

### 配置校验

启动时会集中校验：

- 必填字段缺失
- 字段类型错误
- 非法枚举值
- 数值越界
- 跨字段约束，例如 `memory.rerank_top_k` 不能大于 `memory.bm25_top_k`

如果配置存在问题，程序会在启动期一次性输出错误列表，而不是在运行过程中零散失败。

## 运行项目

### 1. 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 准备 `config.json`

直接编辑仓库根目录下的 `config.json`。至少需要确认下面几项可用：

```json
{
  "napcat": {
    "ws_url": "ws://127.0.0.1:8095",
    "http_url": "http://127.0.0.1:6700"
  },
  "ai_service": {
    "api_base": "https://your-openai-compatible-endpoint/v1",
    "api_key": "sk-...",
    "model": "your-model",
    "extra_params": {},
    "extra_headers": {},
    "response_path": "choices.0.message.content"
  },
  "vision_service": {
    "enabled": false,
    "api_base": null,
    "api_key": null,
    "model": null,
    "extra_params": null,
    "extra_headers": null,
    "response_path": null
  }
}
```

当 `vision_service` 处于 `enabled` 状态时，机器人会走“图片下载 -> 视觉模型 -> 文本描述 -> planner / 主回复模型”的文本化图片链路。

如果 `vision_service` 是 `disabled` 或 `unconfigured`，系统会进入 text-only 模式：

- 私聊图文：只保留文字，忽略图片。
- 私聊纯图片：直接返回“这次没看清图片”一类自然降级回复，不调用主模型。
- 群聊图文：planner 和 reply 都只看文字部分。
- 群聊纯图片：直接忽略，不触发回复。

### 3. 启动

```bash
python main.py
```

如果启动成功，日志中会看到配置校验完成、memory 初始化状态、视觉状态摘要（`disabled / unconfigured / enabled`）和 NapCat 连接状态。

## 模块职责

### Handler 分层

- `MessageHandler`
  负责 plan、prepare、dispatch、限流和生命周期入口。
- `CommandHandler`
  负责命令识别与状态文案，内部通过 `CommandRegistry` 注册命令。
- `ConversationSessionManager`
  负责会话 key、上下文缓存和过期清理。
- `GroupPlanCoordinator`
  负责群聊 burst 聚合、窗口内图片预分析和 planner 调度。
- `group_reply.burst_merge_enabled`
  控制是否启用群消息合并窗口；关闭时群消息仍会进入 planner，但按单条独立判断。
- `ReplyPipeline`
  负责 prompt 组装、视觉结果复用、AI 调用、对话登记、记忆后置动作。

### 图片理解链路

当 `vision_service` 状态为 `enabled` 时，图片消息会先经过独立视觉模型，再把文本化结果交给后续链路：

- 私聊无图：保持现状。
- 私聊纯图片：先识图，再把“图片描述”交给主模型回复。
- 私聊图文混合：把“用户原文 + 每图描述 + 合并摘要”拼成增强后的文本消息交给主模型。
- 群聊消息：进入 burst buffer 后，在 flush 执行 planner 前先补图片描述；planner 基于“文字 + 图片描述”共同决策。
- planner 阶段产出的图片描述会挂在 `reply_context.window_messages` 上，reply 阶段优先复用，不重复识图。
- 视觉结果会写入会话历史，但不会直接写入长期 memory，也不会直接进入 shared/private 记忆判定。

当视觉模型未启用或未完整配置时，会进入 text-only 降级：

- 私聊图文：只保留文字，忽略图片。
- 私聊纯图：返回自然的“没看清图”降级回复，不调用主模型。
- 群聊图文：按文字继续判断。
- 群聊纯图：直接 `ignore`。

当视觉模型已启用但本次识图失败时：

- 私聊图文：按文字继续。
- 私聊纯图：返回自然的“没看清图”降级回复。
- 群聊图文：按文字继续判断。
- 群聊纯图：优先 `wait`。

### AI 服务层

- `AIClient` 保持对外入口稳定。
- `request_builder` 负责请求参数合并规则。
- `response_parser` 负责 `response_path`、回退提取和 `tool_calls` 解析。
- `error_mapper` 负责 HTTP、超时、JSON 解析、客户端异常映射。
- `session_manager` 负责 `aiohttp` session 生命周期。

### Memory 边界

- `MemoryManager` 作为稳定门面。
- `MemoryIndexCoordinator` 负责索引初始化与刷新。
- `MemoryRetrievalCoordinator` 负责检索、important memory、上下文拼装。
- `MemoryBackgroundCoordinator` 负责会话保存、抽取触发与关闭收尾。
- `MemoryTaskManager` 负责后台任务登记、取消与 flush。

当前 memory 不再只用“private/shared”一个维度判断是否可读，而是同时看：

- `visibility`
  这条记忆能不能跨边界读取。
- `applicability_scope`
  这条记忆只在哪个场景生效。

共享判定规则如下：

- 私聊来源不是自动全部 private；只有规则允许共享的公共规则才会 shared。
- 群聊来源也不是自动 shared。
- 个人信息、偏好、禁忌、计划、背景默认都按 `private`。
- 只有 `group_rule`、`bot_rule`、明确授权共享的公共规则，才允许 `shared`。
- 判不清时默认按 `private`。
- conversation 历史始终私有，不参与 shared 召回。

称呼要求单独建模为 `addressing_preference`，不混进普通偏好，也不当作公共 shared 规则：

- 私聊称呼要求只在该用户私聊里生效。
- 群聊称呼要求按 `group_id + user_id` 生效。
- 私聊和群聊称呼互不继承。

### Prompt 注入与控长

Memory prompt 注入顺序固定为：

1. 助手身份
2. 群聊窗口上下文
3. 当前用户重要记忆
4. 当前场景称呼要求
5. 当前场景共享规则 / 共享重要记忆
6. 动态相关普通记忆
7. 基础系统提示词

为了避免 prompt 越来越长，memory 使用三层控长机制：

- 写入时做去重和合并，尽量保留更稳定、更短的表达。
- 读取时按预算裁剪，不再只按条数限制。
- 后台定期整理压缩，优先整理 shared 规则和重复的场景规则。

读取阶段会分别给“用户重要记忆 / 场景称呼要求 / shared 规则 / 动态相关记忆”分配预算；如果记忆存在压缩摘要，会优先注入摘要版。

### 命令注册表

内建命令通过 `CommandRegistry` 维护，当前已注册：

- `/help`
- `/status`
- `/reset`

`/help` 会根据注册表自动生成；后续新增命令时只需要注册 `CommandSpec`，不再继续堆 `if/else`。

### 运行指标

运行态指标统一由 `RuntimeMetrics` 维护，`QQBot.get_status()` 和 `/status` 读取同一份 snapshot。当前覆盖：

- 生命周期: `ready`、`connected`、`uptime_seconds`、`last_error_at`
- 消息: `messages_received`、`messages_replied`、`reply_parts_sent`、`message_errors`
- 命令: `command_hits`、`command_hits_by_name`
- 群规划: `planner_reply`、`planner_wait`、`planner_ignore`、`planner_burst_merge`
- 图片理解: `vision_requests`、`vision_images_processed`、`vision_failures`、`vision_reused_from_plan`
- Memory: `memory_reads`、`memory_shared_reads`、`memory_scene_rule_hits`、`memory_access_denied`、`memory_writes`、`memory_migrations`、`memory_compactions`、`background_tasks`
- 运行态: `active_message_tasks`、`active_conversations`

## 测试

运行全部测试：

```bash
python -m unittest discover -s tests -v
```

常见测试范围：

- handler 编排与协作者边界
- 配置注释 JSON、fallback client config、聚合报错
- bot 生命周期正常启动、初始化失败、重复关闭
- AI 请求构造、响应解析、错误映射
- vision client 解析结构化结果、文本回退与错误分支
- memory 初始化、检索、抽取、后台任务回收

## 故障排查

- 启动即失败
  先看 `ConfigValidationError` 输出，它会列出全部配置问题。
- 能启动但连不上 NapCat
  检查 `napcat.ws_url` 是否与 NapCat 实际监听地址一致。
- 图片消息没有走视觉模型
  检查 `vision_service.enabled` 是否开启，以及 `vision_service` 的 `api_base`、`api_key`、`model` 是否完整；未完整配置时状态会显示为 `unconfigured`，系统会进入 text-only 模式，不会回退到 `ai_service` 或主模型多模态。
- AI 返回异常
  检查 `ai_service.api_base`、`api_key`、`model` 与 `response_path` 是否匹配当前上游。
- 关闭后仍有悬挂任务
  先执行生命周期测试；当前关闭链路应统一回收 connection、message handler、memory manager、vision client 和 memory 后台任务。

