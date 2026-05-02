# AGENTS.md

## 常用命令

```bash
uv pip install -r requirements.txt

# 全量测试（使用虚拟环境 Python）
.venv/bin/python -m unittest discover -s xueli/tests -t xueli

# 单测试文件
.venv/bin/python -m unittest discover -s xueli/tests -t xueli -p test_conversation_planner.py

# 启动
start.bat / start.ps1  # Windows
bash start.sh         # Linux/macOS
```

- 无 lint/typecheck/formatter 配置，勿运行 ruff/mypy/pylint
- 测试框架是 `unittest`（非 pytest）
- 测试以 `-t xueli` 设定包根，import 路径为 `src.*`（如 `from src.core.models`），**不是** `xueli.src.*`
- `xueli/config/config.toml` 为本地私有配置（gitignore），首次运行需从 `config.example.toml` 复制

---

## 核心架构

### 主消息处理链

```
MessageHandler
  └── PlanningWindowService (缓冲窗口调度)
        └── ConversationPlanner (reply/wait/ignore + PromptPlan)
              └── TimingGateService (continue/wait/no_reply)
                    └── ConversationContextBuilder
                          └── ReplyPipeline (PromptPlan + 上下文 → 最终回复)
                                └── ReplyGenerationService (AI 生成)
                                      └── MemoryFlowService (记忆写入)
```

### 关键契约

**PromptPlan**：planner 输出给 reply pipeline 的核心结构，含 `action` (reply/wait/ignore) 和各上下文层开关。改动它必须同步更新所有调用方、测试和文档。

### 三大提示词模板

- `xueli/prompts/zh-CN/planner.prompt` — reply/wait/ignore 决策
- `xueli/prompts/zh-CN/timing_gate.prompt` — continue/wait/no_reply 决策
- `xueli/prompts/zh-CN/reply.prompt` — 最终回复生成

主模板文件 + 代码内 section 注入。改 reply prompt 优先改 section/renderer/style policy，不退回大段字符串硬拼。

### Adapter 隔离

- 平台适配器在 `src/adapters/` 下：`napcat/`（QQ/NapCat WebSocket）、`api/`（HTTP API runtime）
- 新 adapter 必须实现 `attach_inbound_event()`，由 `dispatcher.py` 优先调用
- OneBot 归一化在 `adapters/napcat/normalizer.py`，**不在** core
- core 不应被 QQ/NapCat 细节污染

---

## 记忆系统

- 记忆写入统一经由 `MemoryFlowService`（**不在** ReplyPipeline 内）
- 三层记忆：person_fact / chat_summary / conversation_recall
- 存储：Markdown 明文 + SQLite；检索：BM25 初排 + 向量联想（n-gram 余弦，零外部依赖）
- 拟人化特性：动态遗忘（用进废退）、软遗忘（归档记忆打折召回）、情绪标记、离线消化、语义联想

---

## 关键约束

1. 私聊和群聊共用一条 conversation 主链，**不要**分裂两套逻辑
2. `ReplyPipeline` 定位是 prompt compiler，回复后副作用走 `MemoryFlowService`
3. 命名用 `conversation_*`、`adapter_*`、`platform_*` 等中性命名，**不要**扩散 `group_*`、`napcat_*`
4. 会话永不过期，重启后从历史存储恢复并保留原始时间信息
5. 结构化分段发送是主路径（模型输出 JSON 数组）；正则分句仅作兜底
6. 普通图片只做视觉理解，**不**入 emoji 仓库；原生表情只存 `face / mface` 引用
7. `data/` 目录是运行时产物，已 gitignore，不提交
8. `group_reply_decision` 配置未填写时，群聊退回规则路径（通常只在被 @ 时回复）
9. **用户侧异常提醒原则**：处理失败时不要发送解释性文字给用户（QQ 场景不需要"抱歉处理失败了"这类消息），静默失败即可；原则是判断这个提醒是否会让用户觉得突兀

---

## 日志规范

### 格式要求
- `[模块]` 格式日志仅输出简单内容，**禁止**结构化参数（如 `extra={}`）
- 日志级别：`INFO` 用于关键节点，`DEBUG` 用于详细调试

### 必须保留的日志
- `[FULL PROMPT]` — 完整提示词
- `[PROMPT SUMMARY]` / `[系统提示词]` — 提示词摘要
- HTTP 访问日志（标准格式）
- AI 重试日志（重试次数/延迟）
- `[启动]` 启动信息（adapter 名称、记忆模块状态等）

### 禁止出现的日志
- 规划原始 DEBUG 日志（包含 `plan.action` / `plan.reason` 等）
- 非关键路径的 DEBUG/INFO 心跳日志
- 用户侧异常解释性文字

---

## 提示词模板规范

所有提示词必须抽象到 `xueli/prompts/` 目录下的 `.prompt` 模板文件，**禁止**在 Python 代码中硬编码提示词字符串。

已模板化的提示词（通过 `PromptTemplateLoader` 加载）：
- `planner.prompt` — 对话规划器
- `timing_gate.prompt` — 节奏门控
- `reply.prompt` — 回复生成
- `reply_constraint.prompt` — 回复格式约束
- `insight_digestion.prompt` — 离线消化
- `vision.prompt` — 图片理解
- `vision_emotion.prompt` — 表情分类
- `emoji_reply.prompt` — 表情追评决策
- `rerank.prompt` — 记忆重排

涉及提示词生成的模块（`VisionClient`、`EmojiReplyService`、`APIReranker`、`ReplyPromptRenderer` 等）必须使用 `PromptTemplateLoader` 加载模板，不得内嵌字符串。

---

## 高风险改动

以下模块改动必须连带检查测试和所有调用点：

- `PromptPlan` / `ConversationPlanner` / `TimingGateService`
- `ReplyPipeline` / `ReplyPromptRenderer`
- `MessageHandler`
- `MemoryManager` / `MemoryFlowService`
- `BotRuntime` 主消息处理链
- 三大 prompt 模板文件

---

## 编码陷阱（必读）

- **async `except Exception` 前必须先 `except asyncio.CancelledError: raise`**：Python 3.9+ 中 `CancelledError` 继承自 `Exception`，不加守卫会破坏 asyncio 取消协议
- **禁止用 `asyncio.CancelledError` 作业务流程控制**：用自定义异常（如 `StaleWindowError`）
- **文件写入必须原子化**：所有持久化（Markdown/JSON）先写 `.tmp` 再 `os.replace()`，禁止直接覆写目标文件
- **`Future.set_result()` 不要在持有 `asyncio.Lock` 时调用**：回调可能尝试获取同一把锁导致死锁，应收集 waiter → 锁外 resolve
- **禁止在 async 上下文中使用同步阻塞 I/O**：`Path.read_text()` / `Path.write_text()` 通过 `asyncio.to_thread()` 包裹或用 `aiofiles`

---

## 标签常量

中文字符串标签统一在 `src/handlers/label_constants.py` 管理：

- `SESSION_TYPE_LABEL` — 私聊/群聊标签
- `SENDER_LABEL_USER` / `SENDER_LABEL_ASSISTANT` — 用户/助手标签
- `DISPLAY_NAME_FALLBACK` — 显示名称兜底值

---

## 依赖协议

MIT 许可证。引入新依赖必须 permissive 协议（MIT、BSD-3-Clause、Apache-2.0、ISC、PSF、HPND）。**禁止** GPL-3.0/AGPL-3.0/LGPL-3.0。
