# OpenAI 兼容接口配置指南

本文档说明当前项目如何通过本地 `xueli/config/config.toml` 配置 OpenAI 兼容服务。

## 当前主配置入口

当前项目以 `xueli/config/config.toml` 作为主配置文件，不再以 `.env` 作为默认启动配置入口。

仓库内建议把：

- `xueli/config/config.example.toml` 作为模板
- `xueli/config/config.toml` 作为本地私有配置

也就是说，后续分享仓库时应优先同步模板，而不是同步你的真实 `xueli/config/config.toml`。

主模型配置位于：

- `ai_service.api_base`
- `ai_service.api_key`
- `ai_service.model`

## 最小主模型配置

```toml
[ai_service]
api_base = "https://api.openai.com/v1"
api_key = "sk-xxxx"
model = "gpt-4o"
response_path = "choices.0.message.content"

[ai_service.extra_params]

[ai_service.extra_headers]
```

## 常见服务商示例

### OpenAI

```toml
[ai_service]
api_base = "https://api.openai.com/v1"
api_key = "sk-xxxx"
model = "gpt-4o"
```

### DeepSeek

```toml
[ai_service]
api_base = "https://api.deepseek.com/v1"
api_key = "sk-xxxx"
model = "deepseek-chat"
```

### OpenRouter

```toml
[ai_service]
api_base = "https://openrouter.ai/api/v1"
api_key = "sk-or-v1-xxxx"
model = "stepfun/step-3.5-flash:free"
```

### 本地 Ollama

```toml
[ai_service]
api_base = "http://127.0.0.1:11434/v1"
api_key = "not-needed"
model = "llama2"
```

## 额外参数与请求头

### 自定义请求参数

```toml
[ai_service.extra_params]
temperature = 0.7
max_tokens = 2000
top_p = 0.9
```

### 自定义请求头

```toml
[ai_service.extra_headers]
"X-Custom-Header" = "value"
```

### 自定义响应提取路径

```toml
[ai_service]
response_path = "choices.0.message.content"
```

如果服务返回结构不同，也可以改成例如：

```toml
[ai_service]
response_path = "output.choices.0.message.content"
```

## 机器人行为配置

`bot_behavior` 控制机器人的核心行为。

## 图片与表情发送边界

当前版本里，普通图片和表情包已经彻底分流：

- 普通图片：
  - 只参与视觉理解 / OCR / 多图分析
  - 不写入 emoji 仓库
  - 机器人不会主动通过 `image` 路径重新发送

- 表情回复：
  - 只使用平台原生表情能力
  - 当前目标平台为 OneBot / NapCat 的 `face` 和 `mface`
  - emoji 仓库存的是原生表情引用与元数据，不再存本地图片文件

这意味着如果当前平台拿不到可用的原生 `face / mface` 资源，机器人会直接退回文本，而不是再用普通图片兜底。

## 提示词模板结构

当前版本的主提示词不再全部硬编码在 Python 里，而是拆成 11 个模板文件：

- `xueli/prompts/zh-CN/planner.prompt`
- `xueli/prompts/zh-CN/timing_gate.prompt`
- `xueli/prompts/zh-CN/reply.prompt`
- `xueli/prompts/zh-CN/reply_constraint.prompt`
- `xueli/prompts/zh-CN/vision.prompt`
- `xueli/prompts/zh-CN/vision_emotion.prompt`
- `xueli/prompts/zh-CN/emoji_reply.prompt`
- `xueli/prompts/zh-CN/relationship_tone.prompt`
- `xueli/prompts/zh-CN/rerank.prompt`
- `xueli/prompts/zh-CN/reflection.prompt`
- `xueli/prompts/zh-CN/insight_digestion.prompt`

运行时通过 `xueli/src/core/prompt_templates.py` 加载，并由各自服务补充动态 section。

目前的职责划分是：

- planner：不再做 `reply / wait / ignore` 决策，而是输出"怎么回"的策略（`PromptPlan`：上下文策略、记忆策略、语气策略等）
- timing gate：统一负责"是否回复"的节奏判断（`continue / wait / no_reply`）
- reply：根据 `PromptPlan + MessageContext + reply_reference + style guide` 生成最终回复数组

其中 `reply_reference` 是 planner 给 reply 的自然语言方向提示，只作为软指导，不参与程序硬执行。

### 结构化分段发送

```toml
[bot_behavior]
segmented_reply_enabled = true
max_segments = 3
first_segment_delay_min_ms = 0
first_segment_delay_max_ms = 600
followup_delay_min_seconds = 3
followup_delay_max_seconds = 10
```

开启后，回复模型会被要求直接输出 JSON 字符串数组，例如：

```json
["刚在发呆呢喵~", "顺便刷手机呢喵~", "你呢喵？"]
```

程序会负责：

- 清洗空段和重复段
- 按段逐条发送
- 第一段近即时发送
- 后续段按随机延迟发送

如果模型没有按协议输出字符串数组，则会退回普通单条文本；`sentence_split_enabled` 保留为最后的标点分句兜底，不再是主路径。

### 会话连续性

私聊与群聊的会话在运行期间永不过期，始终保持连续对话。每次对话结束后，相关消息会自动存入历史存储，重启时可从历史存储恢复上一轮会话的全部消息，并保留原始消息时间与上一轮会话关闭时间。

`max_context_length` 控制内存中保留的最大历史消息条数。

这意味着重启后的第一条新消息不再被错误判成“刚刚接上”，planner 能同时看到：

- 最近一条恢复历史消息的真实时间
- 上一轮已关闭会话的时间分层

### 其他行为配置

```toml
[bot_behavior]
max_context_length = 10          # 对话历史最大条数
max_message_length = 4000         # 单条消息最大长度（字符），超长自动截断
response_timeout = 60             # AI 响应超时时间（秒）
rate_limit_interval = 1.0         # 同一目标发送间隔（秒）
private_quote_reply_enabled = false  # 私聊是否启用引用回复
private_batch_window_seconds = 1.2   # 私聊消息合批窗口（秒）
sentence_split_enabled = true        # 仅作兜底标点分句
segmented_reply_enabled = true       # 主回复路径使用字符串数组分段
max_segments = 3                     # 单轮回复最多保留几段
first_segment_delay_min_ms = 0       # 第一段最小延迟（毫秒）
first_segment_delay_max_ms = 600     # 第一段最大延迟（毫秒）
followup_delay_min_seconds = 3.0     # 后续段最小延迟（秒）
followup_delay_max_seconds = 10.0    # 后续段最大延迟（秒）
log_full_prompt = false               # 是否打印完整提示词（仅调试用）
```

## 规划缓冲窗口配置

`planning_window` 控制会话前置缓冲调度器。

当前实现不是“每条消息各自 sleep 一轮”，而是：

- 每个会话持续按时间片生成缓冲窗口
- 窗口按顺序进入 `planner -> timing gate -> reply`
- 后续排队窗口如果等待过久会被直接丢弃，避免延迟叠加

示例：

```toml
[planning_window]
enabled = true
private_window_seconds = 1.2
group_proactive_window_seconds = 0.45
queue_expire_seconds = 60.0
```

字段说明：

- `private_window_seconds`：私聊封窗时长
- `group_proactive_window_seconds`：群聊主动接话路径的封窗时长
- `queue_expire_seconds`：排队窗口的最大等待时间，超时后整窗丢弃

## 视觉模型配置

视觉功能配置位于 `vision_service`。

只有同时满足以下条件时，视觉功能才会真正可用：

1. `vision_service.enabled = true`
2. `vision_service.api_base` 非空
3. `vision_service.model` 非空

说明：`vision_service.api_key` 可以为空，是否必需取决于你接入的服务提供方。

示例：

```toml
[vision_service]
enabled = true
api_base = "https://your-vision-endpoint/v1"
api_key = "sk-xxxx"
model = "your-vision-model"
response_path = "choices.0.message.content"

[vision_service.extra_params]

[vision_service.extra_headers]
```

## 群聊规划模型配置

统一会话规划模型配置位于 `group_reply_decision`。

只有当 `group_reply_decision.api_base` 和 `group_reply_decision.model` 完整时，`ConversationPlanner` / `TimingGateService` 使用的规划模型才会启用。

如果未完整配置，不会自动回退到 `ai_service` 充当 planner，而是退回规则路径：群聊通常只在被 `@` 时回复，或使用规则型兜底行为。

当前回复主链里，`group_reply_decision` 负责的是：

- `ConversationPlanner` 的"怎么回"策略规划（`PromptPlan`：上下文/记忆/语气策略）
- `TimingGateService` 的节奏判断（`continue / wait / no_reply`）

它不负责生成最终用户可见回复；最终回复仍然由 `ai_service` 主模型负责。

## 记忆冲突裁决配置

`memory_dispute` 控制新记忆与旧记忆发生冲突时的裁决策略：

```toml
[memory_dispute]
enabled = true
high_confidence_threshold = 0.75   # 高置信度阈值：超过则直接采纳新记忆
normal_confidence_threshold = 0.45  # 普通置信度阈值
signal_ttl_hours = 168.0          # 裁决信号有效期（小时）
```

## 角色人设成长与关系追踪

`character_growth` 控制基于用户反馈的渐进式角色调整和关系追踪：

```toml
[character_growth]
enabled = true
explicit_feedback_threshold = 2    # 明确反馈次数阈值
stable_signal_threshold = 6        # 稳定信号阈值
core_trait_threshold = 5          # 核心特质阈值
tone_preference_threshold = 3     # 语气偏好阈值
behavior_habit_threshold = 2      # 行为习惯阈值
# 自主情绪引擎（关闭时保持镜像用户情绪行为）
mood_fluctuation_enabled = false
mood_volatility = 0.3
mood_independence_ratio = 0.7
mood_energy_decay_per_turn = 0.05
mood_energy_recovery_night = 0.2
mood_show_in_reply = false
# 关系追踪（默认开启）
relationship_tracking_enabled = true
intimacy_acquaintance_threshold = 0.2
intimacy_friend_threshold = 0.5
intimacy_close_friend_threshold = 0.8
intimacy_gain_per_high_quality = 0.01
intimacy_loss_per_low_quality = 0.005
intimacy_loss_per_friction = 0.02
friction_signals_caution_threshold = 2
```

情绪引擎 (`mood_engine`) 不再使用正弦波驱动，而是由多因素叠加涌现：
- `user_emotion_valence`：用户当前情感倾向
- `recent_negative_density`：近期对话负面情感密度
- `retrieval_failure_rate`：记忆调取失败率
- `conversation_gap_hours`：对话间隔时长

日志输出格式：`[情绪] valence=... energy=... 原因: 负面对话密度=80%; 检索失败率=35%`

关系追踪支持 6 个阶段：`stranger → met_before → acquaintance → friend → close_friend → intimate`，阶段变更时输出 `[关系] 阶段变更` 日志。

## 记忆系统配置

记忆系统支持以下核心特性：

- **动态遗忘（用进废退）**：普通记忆按指数衰减，按 `core_fact`（3x半衰期）/ `important`（1.5x）/ `casual`（0.7x）分类差异化衰减
- **冷记忆加速衰减**：超过 `cold_memory_threshold_days`（默认90天）的记忆额外加速衰减
- **软遗忘（归档动态折扣）**：归档记忆可被检索但分数打折，折扣按 `archive_penalty_base` + 归档时长动态调整，命中召回后折扣递减
- **情绪标记加成**：带 `emotional_tone` 的记忆衰减时获得 +0.2 留存加成

关键配置字段：

```toml
[memory]
ordinary_decay_enabled = true
ordinary_half_life_days = 30.0     # 半衰期（天）
ordinary_forget_threshold = 0.5    # 遗忘阈值
cold_memory_threshold_days = 90.0  # 冷记忆阈值（超过此天数加速衰减）
cold_decay_multiplier = 1.5        # 冷记忆衰减倍率
archive_penalty_base = 0.5         # 归档召回基础折扣
# 检索权重
local_bm25_weight = 1.0
local_importance_weight = 0.35
local_mention_weight = 0.2
local_recency_weight = 0.15
local_scene_weight = 0.3
# 场景匹配子权重
scene_same_group_weight = 1.5
scene_same_type_weight = 1.0
scene_same_user_weight = 0.8
```

记忆提取配置位于 `memory`：

- `memory.extraction_api_base`
- `memory.extraction_api_key`
- `memory.extraction_model`
- `memory.extraction_extra_params`
- `memory.extraction_extra_headers`
- `memory.extraction_response_path`

如果这些提取专用字段为空，代码会自动回退到 `ai_service`。

也就是说：

- 不单独配置时，记忆提取默认复用主模型
- 单独配置后，可使用独立提取模型

## 记忆重排配置

记忆重排配置位于 `memory_rerank`。

只有当以下字段完整时，才视为已配置：

- `memory_rerank.api_base`
- `memory_rerank.model`

未完整配置时，记忆重排视为未启用。

## 配置验证建议

启动前至少确认以下字段：

- `adapter_connection.adapter`
- `adapter_connection.platform`
- `adapter_connection.ws_url`
- `adapter_connection.http_url`
- `ai_service.api_base`
- `ai_service.api_key`
- `ai_service.model`

如启用视觉，还要确认：

- `vision_service.enabled`
- `vision_service.api_base`
- `vision_service.model`

如启用记忆提取，还要确认：

- `memory.enabled`
- `memory.auto_extract`
- `memory.extract_every_n_turns`

如需要情绪引擎/关系追踪等高级特性，还要确认：

- `character_growth.enabled`
- `character_growth.relationship_tracking_enabled`
- `character_growth.mood_fluctuation_enabled`

## 故障排查

### 主模型请求失败

检查：

- `ai_service.api_base` 是否正确
- `ai_service.api_key` 是否有效
- `ai_service.model` 是否存在
- `response_path` 是否与服务返回格式一致

### 视觉功能不可用

检查：

- `vision_service.enabled` 是否为 `true`
- `vision_service.api_base` 和 `vision_service.model` 是否都已填写

### 记忆提取没有单独走提取模型

检查：

- `memory.extraction_api_base`
- `memory.extraction_api_key`
- `memory.extraction_model`

如果为空，就会自动回退到 `ai_service`。

### 群聊规划/节奏模型没有生效

检查：

- `group_reply_decision.api_base`
- `group_reply_decision.model`

如果未完整配置，planner 和 timing gate 会退回规则路径（planner 输出默认策略，timing gate 使用规则判断）。

### 图片描述没有持久化到历史

检查：

- `vision_service.enabled` 是否为 `true`
- `vision_service.api_base` 和 `vision_service.model` 是否都已填写
- 视觉模型是否成功返回了 `merged_description`（可查看日志中 `vision_success_count`）

图片描述在视觉分析成功后会自动随对话轮次存入历史存储，重启后仍可在历史消息中显示为 `[图片描述：xxx]`。
