# WebUI Django Preview

`src/webui` 已改造成一个可独立运行的 Django 子项目，用来承载后续接入主项目的控制台界面。

## 运行方式

```bash
pip install -r requirements.txt
python src/webui/manage.py runserver
```

打开 `http://127.0.0.1:8000/` 即可预览。

## 当前状态

- 当前版本是控制台静态占位页，使用 Django `templates + static` 组织页面。
- 页面结构保留 7 个主要分区，后续可以逐块替换成真实业务数据。
- 当前不会读取 `config.json`，也不会把 API key、token 等敏感信息渲染到前端。
- 旧的 React/Vite 方案不再作为默认运行方式。

## 目录说明

- `manage.py`: Django 运行入口
- `webui_site/`: Django project 配置
- `console/`: 控制台 app，包含视图、上下文、模板、静态资源和测试

## 后续扩展建议

- 在 `console/context.py` 中逐步替换占位数据为真实项目状态摘要。
- 如果需要实时数据，可在 `console` app 下新增只读接口，而不破坏现有模板结构。
- 如果要接入你的主项目，可直接复用 `console` app 或把模板、静态资源迁入现有 Django 工程。
