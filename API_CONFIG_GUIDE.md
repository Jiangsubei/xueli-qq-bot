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

## 群聊判断模型配置

统一会话规划模型配置位于 `group_reply_decision`。

只有当 `group_reply_decision.api_base` 和 `group_reply_decision.model` 完整时，`ConversationPlanner` / `TimingGateService` 使用的规划模型才会启用。

如果未完整配置，不会自动回退到 `ai_service` 充当 planner，而是退回规则路径：群聊通常只在被 `@` 时回复，或使用规则型兜底行为。

当前回复主链里，`group_reply_decision` 负责的是：

- `ConversationPlanner` 的 `reply / wait / ignore`
- `TimingGateService` 的 `continue / wait / no_reply`

它不负责生成最终用户可见回复；最终回复仍然由 `ai_service` 主模型负责。

## 记忆提取模型配置

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

### 群聊规划模型没有生效

检查：

- `group_reply_decision.api_base`
- `group_reply_decision.model`

如果未完整配置，群聊不会启用统一 planner / timing gate 模型，而会退回规则路径。
