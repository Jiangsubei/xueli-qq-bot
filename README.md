# Claude QQ Bot

这是一个基于 NapCat 和 OpenAI 兼容接口的 QQ 机器人项目。

你可以把它理解成一个桥接层：NapCat 负责把 QQ 消息送进来，这个项目负责处理消息、调用 AI，再把回复发回 QQ。除了基础聊天，它还支持群聊回复、图片理解、长期记忆，以及一个本地 WebUI 控制台。

## 这个项目是做什么的

这个项目的用途很直接：把你的 QQ 账号接到 AI 上，让它像一个真正能聊天的 QQ 助手一样工作。

它能做的事情包括：

- 在私聊里自动回复消息
- 在群聊里按规则判断要不要回复
- 保留一定上下文，让对话更连贯
- 在启用视觉模型后理解图片内容
- 在启用记忆模块后记录和召回长期记忆
- 提供一个网页控制台查看运行状态

如果你已经有：

- 一个能正常工作的 NapCat
- 一个能用的 OpenAI 兼容接口

那这个项目就是把这两边接起来的那一层。

## 你该怎么用它

当前项目主要使用仓库根目录下的 `config.json` 作为配置文件。仓库里如果还有旧的 `.env` 示例或旧脚本，请优先以 `config.json` 和这份 README 为准。

### 1. 先准备运行环境

请先准备好这些东西：

- Python 3.10 或更高版本
- 一个已经登录并可正常工作的 NapCat
- 一个可调用的 OpenAI 兼容接口

兼容接口可以是：

- OpenAI
- DeepSeek
- OpenRouter
- Ollama
- 其他兼容 `/v1/chat/completions` 的服务

### 2. 安装依赖

Windows：

```bash
python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
```

Linux / macOS：

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
```

### 3. 修改 `config.json`

你至少需要确认下面这些配置是正确的：

- `napcat.ws_url`
- `napcat.http_url`
- `ai_service.api_base`
- `ai_service.api_key`
- `ai_service.model`

一个最小可运行示例：

```json
{
  "napcat": {
    "ws_url": "ws://127.0.0.1:8095",
    "http_url": "http://127.0.0.1:6700"
  },
  "ai_service": {
    "api_base": "https://your-openai-compatible-endpoint/v1",
    "api_key": "sk-xxxx",
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
    "response_path": "choices.0.message.content"
  }
}
```

常见 `api_base` 示例：

- OpenAI: `https://api.openai.com/v1`
- DeepSeek: `https://api.deepseek.com/v1`
- OpenRouter: `https://openrouter.ai/api/v1`
- Ollama: `http://127.0.0.1:11434/v1`

你可以这样理解这几个配置块：

- `napcat`：告诉程序去哪里连接 QQ
- `ai_service`：告诉程序去哪里调用主模型
- `vision_service`：如果你想让它看图，就配置这里
- `memory`：如果你想让它记住长期信息，就配置这里

如果你只是第一次上手，我建议先只配好 `napcat` 和 `ai_service`，先把最基础的聊天跑通。

### 4. 启动项目

直接运行：

```bash
python main.py
```

程序启动后会按这个顺序工作：

1. 读取并校验 `config.json`
2. 初始化机器人运行组件
3. 连接 NapCat
4. 启动 WebUI

如果 WebUI 启动成功，默认地址通常是：

```text
http://127.0.0.1:8000/
```

### 5. 实际使用方式

启动完成后，你主要从两个地方使用它：

- QQ：直接私聊机器人，或者在群里触发它回复
- 浏览器：打开 WebUI 查看状态和做一些控制操作

如果你是第一次部署，我建议按这个顺序来：

1. 先只测试私聊回复
2. 再测试群聊回复
3. 最后再开启图片理解和记忆功能

这样比较容易排查问题，也不会一开始就把变量堆太多。

## 它的整体架构是什么样

这个项目从结构上看，可以分成四层。

### 1. 入口层

- `main.py`

这是程序入口。你运行 `python main.py` 时，整个项目就是从这里开始启动的。

### 2. 核心运行层

- `src/core/`

这一层负责整个项目的主流程控制，主要包括：

- 读取和校验配置
- 组装运行时依赖
- 管理机器人生命周期
- 控制启动、关闭和重启

如果你把这个项目看成一个系统，这一层就是“总控”。

### 3. 消息处理层

- `src/handlers/`
- `src/services/`
- `src/emoji/`

这一层负责真正处理消息逻辑。收到 QQ 消息后，大部分“机器人到底怎么想、怎么回”的事情都发生在这里，比如：

- 这条消息要不要回复
- 要不要读取上下文
- 要不要调用 AI
- 要不要先做图片理解
- 最后把什么内容发回去

### 4. 数据与界面层

- `src/memory/`
- `src/webui/`
- `data/`
- `memories/`

这一层负责两类事情：

- 存和读数据
- 展示运行状态

具体来说：

- `memory` 负责长期记忆
- `webui` 负责网页控制台
- `data` 和 `memories` 负责本地运行数据

## 一条消息在项目里怎么流动

如果你想快速理解整个项目，可以直接看这条链路：

1. NapCat 收到一条 QQ 消息
2. NapCat 把消息转给这个项目
3. 项目判断这条消息要不要回复
4. 如果要回复，就调用 AI
5. 如果开启了视觉或记忆，也会在这个过程中参与
6. 最后项目再通过 NapCat 把回复发回 QQ

换成一句更直白的话就是：

`QQ 消息 -> 项目处理 -> AI 生成回复 -> 发回 QQ`

## 目录怎么快速看懂

如果你是第一次看这个仓库，先认这几个位置就够了：

```text
main.py                     启动入口
config.json                 主配置文件
src/core/                   核心运行逻辑
src/handlers/               消息处理逻辑
src/services/               AI、视觉等服务封装
src/memory/                 记忆系统
src/webui/                  WebUI 控制台
tests/                      测试
```

如果你的目标只是把项目跑起来，重点看：

- `config.json`
- `main.py`

如果你的目标是继续开发，再去看 `src/` 下的各个模块。
