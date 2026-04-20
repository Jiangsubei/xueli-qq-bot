# WebUI Django Console

`src/webui` 已经是项目当前在用的 Django 控制台，而不只是早期演示页面。

## 运行方式

```bash
pip install -r requirements.txt
python src/webui/manage.py runserver
```

打开 `http://127.0.0.1:8000/` 即可进入控制台。

## 当前状态

- 当前控制台已经能读取真实运行快照、配置和记忆数据。
- 已支持运行状态展示、网络/模型/助手/表情/记忆设置保存、记忆管理、头像上传和后端 runtime 重启。
- 页面仍采用 Django `templates + static` 和原生 JavaScript，而不是 React/Vite。
- 敏感字段会在服务层做掩码，不直接把密钥原文渲染到前端。

## 目录说明

- `manage.py`: Django 运行入口
- `webui_site/`: Django project 配置
- `console/`: 控制台 app，包含视图、service facade、模板、静态资源和测试

当前 `console/views.py` 已优先通过这些 service facade 组织依赖：

- `runtime_service`
- `config_service`
- `memory_service`
- `avatar_service`

不过大部分核心业务逻辑仍然集中在 `console/services.py`，服务拆分还在继续推进。

## 后续扩展建议

- 继续把 `console/services.py` 按 runtime / config / memory / avatar 拆成真正独立的服务模块。
- 如果需要更实时的数据，可以在现有只读接口基础上继续扩展，而不破坏当前单页结构。
- 如果要接入更多运行时能力，优先延续现在的 snapshot + runtime registry 边界。
