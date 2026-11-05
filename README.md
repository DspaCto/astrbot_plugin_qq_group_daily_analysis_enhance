# 群总结扩展补丁

AstrBot 插件 ID：`astrbot_plugin_qq_group_daily_analysis_enhance`

给 `astrbot_plugin_qq_group_daily_analysis`（群分析总结插件）加四层扩展：

1. **按群定向**：不同群使用不同的关注方向，分析前注入本群关键词，分析后按标签过滤话题。
2. **内容审核自动回退**：分析 LLM 被上游内容审核拒绝时，自动识别错误并切换备用 Provider（默认官方 `deepseek-flash`）重试，避免无人值守时整份报告失败。
3. **增量触发解耦**：定时分析名单为空时，增量名单里的群仍然可以按 900 条自动生成静默批次，不会因为关闭定时报告而停止积累历史。
4. **历史报告路由**：原 `astrbot_plugin_group_history_bridge` 的功能已并入本插件；管理员私聊说“分析历史”时，按“接收者<-分析群号”把每个群的增量报告发到对应私聊或群，并强制同步当前未满 900 条的尾巴。

## 安装

- 在 AstrBot 插件市场搜索“群总结扩展补丁”，或按插件 ID `astrbot_plugin_qq_group_daily_analysis_enhance` 安装。
- 也可以把本仓库目录放到 `AstrBot/data/plugins/astrbot_plugin_qq_group_daily_analysis_enhance/` 后重启 AstrBot。
- 依赖 `astrbot_plugin_qq_group_daily_analysis` 已安装并加载。

## 按群定向

- **分析前**：只把当前群的关键词及其解释拼进话题分析提示词，不让模型看到别的群的方向。
- **分析后**：只保留 `[关键词]` 前缀在白名单内的话题；没有标签时用关键词/解释兜底。可关闭过滤，只保留提示词引导。

## 内容审核自动回退

- 拦截群分析插件 `llm_utils` 的内容风控判断，补充识别 `content_check_error`、`content_check_rejected`、`sensitive content` 等错误。
- 被识别后，`call_provider_with_retry` 原有的降级逻辑会改用 `fallback_provider_id` 重试。
- 只影响群分析插件的 LLM 调用；聊天 Agent 的 Provider 不受影响。
- 备用 Provider 也可能有自己的内容审核；它再次拒绝时不会无限重试。

## 历史报告路由

- 只有 AstrBot 管理员可以私聊触发；群聊里调用会提示改私聊。
- 管理员说“分析历史 / 群分析 / 生成群报告 / 看看群最近聊了什么 / 总结最近的群聊”时，插件按“接收者<-分析群号”逐条调用群分析插件强制同步一次尾巴，然后把发送游标之后的全部增量批次按 Markdown 发到对应接收者。
- 接收者可以是私聊 UMO 或群 UMO；例如 `[napcat_qq:FriendMessage:123456]<-[419566332]` 表示把 419566332 群的报告发到私聊 123456，`[napcat_qq:GroupMessage:419566332]<-[543991797]` 表示把 543991797 群的报告发到 419566332 群。
- 每份报告单独一条消息发送，消息头带 `（i/N）`；不再把所有批次拼成一条长消息。
- 单份报告如果本身超过 3500 字，仍会按平台长度限制再拆；正常单份不会。
- 不再在话题后面列出参与者括号；正文里第一次出现某个群员时写 `[显示名-QQ号]`，之后同一份报告里再出现写 `[显示名]`。
- 显示名规则：昵称宽度不超过 4 个汉字时显示完整昵称；超过 4 个汉字时只取前 3 个字。一个英文字母/半角字符按 0.5 个汉字计算。
- 发送游标按“分析群 + 接收者”保存在插件 KV `delivery_cursors` 里；本次先发 `max_batches_per_reply` 份，剩余的下次继续。
- 已从独立的历史桥接插件合并进来；不再需要安装 `astrbot_plugin_group_history_bridge`。

## 配置

- `receiver_routes`（接收者<-分析群号）：每行 `[接收者的UMO]<-[分析群号数字]`。
  - 接收者 UMO 可以是私聊（`napcat_qq:FriendMessage:QQ号`）或群聊（`napcat_qq:GroupMessage:群号`）。
  - 示例：`[napcat_qq:FriendMessage:123456]<-[419566332]`
  - 一行一条路由，可多行；同一群可以配多个接收者。
- `group_focus_map`（分析群号==关键词）：每行格式 `分析群号==关键词1|关键词2`。
  - **只写上面定义过的关键词**，不要写解释；多个关键词只用 `|` 分隔。
  - 群号可写纯数字（如 `419566332`）或完整 UMO。
  - 未定义的关键词会被跳过并在日志里提示。
- `enable_label_filter`：是否按 `[关键词]` 白名单硬过滤话题。关闭后只做提示词引导。
- `focus_keywords`（关键词--具体内容）：每行格式 `关键词--具体内容`。
  - 示例：`健康--和健康有关，着重于饮食和睡眠`
  - 关键词可以含单个 `-`，但不要含 `--`、`|`、`[`、`]`、`【`、`】`、换行；解释里可以含中文标点，但不要含换行、`|`、`[`、`]`、`【`、`】`。
  - 只按第一个 `--` 分割关键词和解释。
- `fallback_on_content_reject`：内容审核拒绝时是否自动换备用 Provider，默认开启。
- `fallback_provider_id`：备用 Provider ID，默认 `deepseek-flash`；必须与 AstrBot 提供商列表里的 ID 完全一致。
- `initial_lookback_days`：第一次请求（还没有发送游标）时的回看天数，范围 1-7，默认 7。
- `max_batches_per_reply`：一次最多发送的报告份数，范围 1-100，默认 30。
- `max_force_loops`：强制增量同步最多循环几次，范围 1-20，默认 10。

插件页的总开关优先级最高：关掉插件就不会加载，也不会给原插件打补丁。

## 依赖与兼容

- 依赖 `astrbot_plugin_qq_group_daily_analysis` 已加载。
- 按群定向通过包装 `TopicAnalyzer` 实例工作；内容回退通过包装群分析插件的 `llm_utils` 函数工作，不修改其源码。
- 如果原插件更新后 `llm_analyzer.topic_analyzer`、`analyze` / `build_prompt_with_override`，或 `llm_utils` 的 `_is_content_risk_error` / `get_provider_id_with_fallback` 接口变化，补丁可能应用失败；启动日志会提示 `[group-focus]`，此时需要适配新接口。
- 按群定向只作用于“话题分析”；内容审核回退作用于群分析插件的所有 LLM 分析区域（话题、金句、质量等）。

## 说明

- 提示词引导是软约束；硬过滤靠 `[关键词]` 前缀 + 白名单，模型乱打标签时会被过滤。
- 关键词兜底只用于模型忘记加标签的情况，可能误留或误杀，建议保持 `enable_label_filter` 开启。
- 内容审核回退只解决 Provider 层面的拒绝，不改变消息本身；如果备用 Provider 也拒绝，报告仍可能失败。
