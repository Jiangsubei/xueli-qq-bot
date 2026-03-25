# OpenAI 兼容 API 配置指南

本文档说明如何配置 QQ 机器人以连接任意 OpenAI 兼容的 AI 服务。

## 核心概念

本框架采用**通用 OpenAI 兼容架构**，通过标准化配置即可连接任意遵循 OpenAI API 规范的服务。

核心配置只有三项：
1. `OPENAI_API_BASE` - API 端点地址
2. `OPENAI_API_KEY` - API 密钥
3. `OPENAI_MODEL` - 模型名称

## 快速配置

### 1. 复制环境配置文件

```bash
cp .env.example .env
```

### 2. 根据你的服务商修改以下三项

```env
# ============================================
# 只需修改这三项即可切换任意服务
# ============================================
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_MODEL=gpt-3.5-turbo
```

## 常见服务商配置示例

### OpenAI 官方
```env
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_MODEL=gpt-4o
```

### DeepSeek
```env
OPENAI_API_BASE=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_MODEL=deepseek-chat
```

### OpenRouter
```env
OPENAI_API_BASE=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
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

# Azure 需要额外请求头
OPENAI_EXTRA_HEADERS={"api-version": "2023-05-15"}
```

## 高级配置

### 自定义请求参数

添加额外的请求参数（如 temperature、max_tokens）：

```env
OPENAI_EXTRA_PARAMS={"temperature": 0.7, "max_tokens": 2000, "top_p": 0.9}
```

### 自定义请求头

添加自定义 HTTP 请求头：

```env
OPENAI_EXTRA_HEADERS={"X-Custom-Header": "value"}
```

### 自定义响应提取路径

如果服务返回格式与标准 OpenAI 格式不同，可配置响应提取路径：

```env
# 标准路径（默认）
OPENAI_RESPONSE_PATH=choices.0.message.content

# 如果服务返回格式为 {output: {choices: [...]}}
# OPENAI_RESPONSE_PATH=output.choices.0.message.content
```

## 验证配置

### 1. 测试 API 连接

```bash
python test_api.py
```

脚本会自动读取 `.env` 中的配置并测试连接。

### 2. 启动机器人

```bash
python main.py
```

## 故障排除

### API 连接失败

1. **检查 API Base URL**
   - 确保 URL 以 `/v1` 结尾（大多数服务）
   - 确保 URL 没有多余的斜杠

2. **检查 API Key**
   - 确认 Key 没有包含多余的空格
   - 确认 Key 在服务商处有效

3. **检查网络连接**
   - 测试能否 ping 通服务商域名
   - 检查防火墙设置

### 模型不存在错误

确认 `OPENAI_MODEL` 填写正确：
- 不同服务商的模型名称不同
- 检查服务商的模型列表文档

### 响应格式错误

如果服务返回格式与标准 OpenAI 不同：
1. 设置 `OPENAI_RESPONSE_PATH` 指定正确的提取路径
2. 检查服务商文档了解响应格式

## 贡献

如果你成功配置了新的服务商，欢迎分享配置示例！

## 参考

- [OpenAI API 文档](https://platform.openai.com/docs)
- [DeepSeek API 文档](https://platform.deepseek.com/)
- [OpenRouter 文档](https://openrouter.ai/docs)
- [Ollama OpenAI 兼容](https://ollama.com/blog/openai-compatibility)
