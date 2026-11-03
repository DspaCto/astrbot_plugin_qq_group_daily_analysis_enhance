# Changelog

## v1.3.0

- 内部插件 ID 改为 `astrbot_plugin_qq_group_daily_analysis_enhance`，显示名改为「群总结扩展补丁」。
- 新增内容审核自动回退：识别 `content_check_error`、`content_check_rejected`、`sensitive content` 等错误后，自动切换备用 Provider（默认 `deepseek-flash`）。
- 保留按群定向和话题标签过滤能力。
- 新增配置：`fallback_on_content_reject`、`fallback_provider_id`。
- 增加内容审核回退自检。

## v1.2.0

- 增加内容审核自动回退。
- 显示名改为「群总结扩展补丁」。

## v1.1.0

- 按群定向、话题标签过滤。
