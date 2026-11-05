# Changelog

## v1.7.0

- 历史报告不再在话题后追加参与者括号；参与者信息改为融入正文。
- 正文首次提到群员时写 `[显示名-QQ号]`，同一份报告后续写 `[显示名]`；昵称宽度不超过 4 个汉字时显示完整昵称，超过时取前 3 个字（半角字符按 0.5 个汉字计）。
- 每份增量分析单独发一条消息，消息头带 `（i/N）`；不再把所有报告拼成一条长消息再被自动切块。

## v1.6.0

- 新增“接收者<-分析群号”历史报告路由：每行 `[接收者UMO]<-[分析群号]`，可把不同群的报告发到私聊或群。
- 删除“历史报告目标群号”单群配置；历史报告改为按路由逐条发送，发送游标按“分析群 + 接收者”保存。
- 删除“启用分群总结定向”配置项；插件加载即启用，总开关使用 AstrBot 上级插件页。
- “群号=关注方向”改名为“分析群号==关键词”，文本格式改用 `群号==关键词1|关键词2`。
- “内容方向关键词”改名为“关键词--具体内容”，文本格式改用 `关键词--具体内容`；兼容旧单 `-` 配置。
- 配置顺序调整为：接收者<-分析群号、分析群号==关键词、按方向标签硬过滤、关键词--具体内容，其余配置在后。

## v1.5.0

- 将独立插件 `astrbot_plugin_group_history_bridge`（群历史分析）并入本插件。
- 新增管理员私聊工具 `request_group_history_analysis`：强制同步尾巴、按发送游标发送增量报告、报告内昵称/QQ 格式化。
- 发送游标统一使用本插件 KV `delivery_cursors`；迁移时从旧桥接插件复制，避免重复发送。
- 新增配置项：`group_id`、`initial_lookback_days`、`max_batches_per_reply`、`max_force_loops`。
- 移除独立历史桥接插件后，插件列表只保留本插件。

## v1.4.0

- 增量分析触发与定时分析名单解耦：即使 `scheduled_group_list` 为空，`incremental_group_list` 里的群仍会按 900 条静默生成批次。
- 新增配置项 `decouple_incremental_from_scheduled`（默认开启）。
- 文档补充：`max_messages` 调高用于回填，桥接强制循环上限提高。

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
