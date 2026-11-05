"""群总结扩展补丁（群分析总结插件的扩展集合）

作用：
- 在“内容方向关键词”里定义 关键词-具体内容的解释；
- 在“群号=关注方向”里只写关键词（例如 群号=关键词1|关键词2）；
- 分析前按当前 umo 只注入本群关键词及其解释；
- 分析后按 [关键词] 前缀白名单过滤话题（可开关）；
- 分析 LLM 遇到内容审核拒绝时，自动识别并临时切换到备用 Provider
  （默认 deepseek-flash），避免无人值守时整份报告失败；
- 管理员私聊说“分析历史”时，按“接收者<-分析群号”把增量报告发送到指定私聊或群；
- 插件页总开关关闭时，恢复原插件行为，不修改原插件源码。

依赖：astrbot_plugin_qq_group_daily_analysis
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime
import importlib
import re
import sys
import time
import unicodedata
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

ANALYSIS_PLUGIN = "astrbot_plugin_qq_group_daily_analysis"
LABEL_RE = re.compile(r"^\s*[\[【]([^\]】]+)[\]】]\s*[:：]?\s*")
HISTORY_CURSOR_KEY = "delivery_cursors"
# 作用：路由格式 [接收者UMO]<-[群号]；群号可带方括号。
ROUTE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*<-\s*\[?(\d+)\]?\s*$")

# 作用：在同一次话题分析调用里传递“本群关键词+解释”，避免并发分析串群
_CURRENT_FOCUS: contextvars.ContextVar[list[tuple[str, str]] | None] = (
    contextvars.ContextVar("group_focus_directions", default=None)
)


@register(
    "astrbot_plugin_qq_group_daily_analysis_enhance",
    "DspaCto",
    "群总结扩展补丁：按群定向 + 内容审核自动回退 + 历史报告路由",
    "1.7.0",
)
class GroupDailyFocus(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._patched = None
        self._original_analyze = None
        self._original_build = None
        self._llm_module = None
        self._incremental_cm = None
        self._incremental_original = None
        self._active = False
        self._history_tasks: set[asyncio.Task] = set()
        self._history_run_lock = asyncio.Lock()
        self._apply_patch()

    # ---------- 配置读取 ----------
    @property
    def _enabled(self) -> bool:
        # 作用：插件加载即启用；总开关在 AstrBot 的上级插件页
        return True

    @property
    def _fallback_enabled(self) -> bool:
        # 作用：内容审核自动回退总开关；插件关闭时也自动关闭
        return self._enabled and bool(
            self.config.get("fallback_on_content_reject", True)
        )

    @property
    def _fallback_provider_id(self) -> str:
        # 作用：内容审核被拒时使用的备用 Provider ID
        return str(self.config.get("fallback_provider_id", "deepseek-flash")).strip()

    @property
    def _initial_lookback_days(self) -> int:
        # 作用：还没有发送游标时，从几天前开始找报告
        return max(1, min(int(self.config.get("initial_lookback_days", 7)), 7))

    @property
    def _max_history_batches(self) -> int:
        # 作用：一次最多发送多少份报告，避免单次刷屏
        return max(1, min(int(self.config.get("max_batches_per_reply", 30)), 100))

    @property
    def _max_force_loops(self) -> int:
        # 作用：强制增量同步最多循环几次（每次最多 900 条）
        return max(1, min(int(self.config.get("max_force_loops", 10)), 20))

    def _receiver_routes(self) -> list[tuple[str, str]]:
        """作用：解析“接收者<-分析群号”，返回 (分析群号, 接收者 UMO) 列表。"""
        raw = self.config.get("receiver_routes", "")
        if not isinstance(raw, str) or not raw.strip():
            return []
        routes: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 作用：容忍全角括号/箭头和中英文输入法混用
            line = (
                line.replace("【", "[")
                .replace("】", "]")
                .replace("＜", "<")
                .replace("＞", ">")
                .replace("－", "-")
            )
            match = ROUTE_RE.match(line)
            if not match:
                logger.warning(
                    f"[group-focus] 忽略格式不正确的接收者路由：{line[:80]}"
                )
                continue
            umo = match.group(1).strip()
            group_id = match.group(2).strip()
            if not umo or not group_id:
                continue
            key = (group_id, umo)
            if key in seen:
                continue
            seen.add(key)
            routes.append(key)
        return routes

    def _keyword_definitions(self) -> dict[str, str]:
        """作用：解析“关键词--具体内容”，一行一个；优先按 -- 分割，兼容旧的单 - 格式。"""
        raw = self.config.get("focus_keywords", "")
        if not isinstance(raw, str) or not raw.strip():
            return {}
        definitions: dict[str, str] = {}
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            separator = "--" if "--" in line else "-"
            if separator not in line:
                logger.debug(f"[group-focus] 忽略没有 -- 的关键词行：{line[:40]}")
                continue
            if separator == "-":
                logger.warning(
                    "[group-focus] 检测到旧的关键词格式（单 -），建议改成 --"
                )
            keyword, explanation = line.split(separator, 1)
            keyword = keyword.strip()
            explanation = explanation.strip()
            if not keyword or not explanation:
                continue
            definitions[keyword] = explanation
        return definitions

    def _focus_for_umo(self, umo: str | None) -> list[tuple[str, str]]:
        """作用：从 group_focus_map 取当前群的关键词，并解析成 (关键词, 解释)。"""
        raw = self.config.get("group_focus_map", "")
        if not isinstance(raw, str) or not raw.strip():
            return []
        definitions = self._keyword_definitions()
        if not definitions:
            logger.warning("[group-focus] focus_keywords 为空，group_focus_map 不会生效")
            return []
        target = str(umo or "").strip()
        target_id = target.rsplit(":", 1)[-1] if target else ""
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").replace("＝", "=").split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if "==" in line:
                key, value = line.split("==", 1)
            else:
                key, value = line.split("=", 1)
                logger.warning(
                    "[group-focus] 检测到旧的分析群号格式（单 =），建议改成 =="
                )
            key = key.strip()
            if not key:
                continue
            if key == target or key == target_id or (target and target.endswith(":" + key)):
                resolved: list[tuple[str, str]] = []
                for keyword in (part.strip() for part in value.split("|")):
                    if not keyword:
                        continue
                    if keyword in definitions:
                        resolved.append((keyword, definitions[keyword]))
                    else:
                        logger.warning(
                            f"[group-focus] 群 {key} 使用了未定义关键词：{keyword}"
                        )
                return resolved
        return []

    # ---------- 补丁 ----------
    def _topic_analyzer(self):
        # 作用：找到群分析插件里的 TopicAnalyzer 实例
        meta = self.context.get_registered_star(ANALYSIS_PLUGIN)
        star = getattr(meta, "star_cls", None) if meta else None
        llm = getattr(star, "llm_analyzer", None) if star else None
        return getattr(llm, "topic_analyzer", None) if llm else None

    def _llm_utils_module(self):
        """作用：找到群分析插件的 llm_utils 模块，用于给内容审核错误加回退。"""
        if self.context.get_registered_star(ANALYSIS_PLUGIN) is None:
            return None
        module_name = (
            f"data.plugins.{ANALYSIS_PLUGIN}"
            ".src.infrastructure.analysis.utils.llm_utils"
        )
        module = sys.modules.get(module_name)
        if module is not None:
            return module
        try:
            return importlib.import_module(module_name)
        except Exception as exc:
            logger.debug(f"[group-focus] llm_utils 尚未可导入: {exc}")
            return None

    def _analysis_config_manager(self):
        """作用：找到群分析插件的 ConfigManager，用于解耦增量名单和定时名单。"""
        meta = self.context.get_registered_star(ANALYSIS_PLUGIN)
        star = getattr(meta, "star_cls", None) if meta else None
        return getattr(star, "config_manager", None) if star else None

    def _apply_incremental_decouple_patch(self) -> None:
        """作用：让增量分析只按基础群权限 + 增量名单判断，不被空的定时名单连带关闭。"""
        if not self._enabled:
            return
        if not bool(self.config.get("decouple_incremental_from_scheduled", True)):
            return
        config_manager = self._analysis_config_manager()
        if config_manager is None:
            return

        # 重复加载时先拆掉旧闭包，避免旧插件实例继续生效。
        if getattr(config_manager, "_enhance_incremental_patched", False):
            current = getattr(config_manager, "is_incremental_group_allowed", None)
            original = getattr(current, "_group_focus_original", None)
            if original is None:
                return
            config_manager.is_incremental_group_allowed = original
            config_manager._enhance_incremental_patched = False

        original = config_manager.is_incremental_group_allowed
        plugin = self

        def patched_is_incremental_group_allowed(group_umo_or_id):
            if not plugin._enabled or not bool(
                plugin.config.get("decouple_incremental_from_scheduled", True)
            ):
                return original(group_umo_or_id)
            try:
                if not config_manager.is_group_allowed(group_umo_or_id):
                    return False
                mode = config_manager.get_incremental_group_list_mode()
                if mode == "inherit":
                    return True
                return config_manager.is_group_in_filtered_list(
                    group_umo_or_id,
                    mode,
                    config_manager.get_incremental_group_list(),
                )
            except Exception as exc:
                logger.debug(f"[group-focus] 增量名单解耦判断失败，回退原逻辑: {exc}")
                return original(group_umo_or_id)

        patched_is_incremental_group_allowed._group_focus_patched = True
        patched_is_incremental_group_allowed._group_focus_original = original
        config_manager.is_incremental_group_allowed = (
            patched_is_incremental_group_allowed
        )
        config_manager._enhance_incremental_patched = True
        config_manager._enhance_incremental_original = original
        self._incremental_cm = config_manager
        self._incremental_original = original
        logger.info("[group-focus] 已应用增量触发解耦补丁")

    def _unpatch_incremental_decouple(self) -> None:
        """作用：恢复增量名单判断原方法。"""
        config_manager = self._incremental_cm
        if config_manager is None:
            return
        try:
            if getattr(config_manager, "_enhance_incremental_patched", False):
                original = getattr(
                    config_manager, "_enhance_incremental_original", None
                )
                if original is not None:
                    config_manager.is_incremental_group_allowed = original
                config_manager._enhance_incremental_patched = False
                logger.info("[group-focus] 已恢复增量触发解耦补丁")
        except Exception as exc:
            logger.warning(f"[group-focus] 恢复增量解耦补丁失败: {exc}")
        finally:
            self._incremental_cm = None
            self._incremental_original = None

    def _apply_llm_fallback_patch(self) -> None:
        """作用：让群分析插件把 content_check_error 识别为内容风控，并在失败时切备用 Provider。"""
        module = self._llm_utils_module()
        if module is None:
            return

        current_is_content = getattr(module, "_is_content_risk_error", None)
        current_get_provider = getattr(module, "get_provider_id_with_fallback", None)
        if not callable(current_is_content) or not callable(current_get_provider):
            logger.warning("[group-focus] llm_utils 缺少回退所需函数，跳过")
            return
        if (
            getattr(module, "_group_focus_llm_patched", False)
            and getattr(current_is_content, "_group_focus_patched", False)
            and getattr(current_get_provider, "_group_focus_patched", False)
        ):
            return

        # 若当前函数已经是我们上一轮的包装，先取它保存的原函数，避免套娃。
        original_is_content = getattr(
            current_is_content, "_group_focus_original", current_is_content
        )
        original_get_provider = getattr(
            current_get_provider, "_group_focus_original", current_get_provider
        )
        plugin = self

        def patched_is_content_risk(error):
            try:
                if original_is_content(error):
                    return True
            except Exception:
                pass
            text = str(error).lower()
            markers = (
                "content_check_error",
                "content_check_rejected",
                "sensitive content",
                "content risk",
                "content_filter",
                "moderation",
            )
            if any(marker in text for marker in markers):
                logger.warning(
                    "[group-focus] 已识别内容审核拒绝，准备切换备用 Provider"
                )
                return True
            return False

        async def patched_get_provider_id(
            context, config_manager, provider_id_key, umo=None
        ):
            # provider_id_key=None 是 call_provider_with_retry 解析备用 Provider 的调用。
            if provider_id_key is None and plugin._fallback_enabled:
                fallback_id = plugin._fallback_provider_id
                if fallback_id:
                    try:
                        if context.get_provider_by_id(provider_id=fallback_id) is not None:
                            logger.info(
                                f"[group-focus] 内容审核回退到 Provider: {fallback_id}"
                            )
                            return fallback_id
                    except Exception as exc:
                        logger.debug(
                            f"[group-focus] 备用 Provider 检查失败: {exc}"
                        )
            return await original_get_provider(
                context, config_manager, provider_id_key, umo
            )

        patched_is_content_risk._group_focus_patched = True
        patched_is_content_risk._group_focus_original = original_is_content
        patched_get_provider_id._group_focus_patched = True
        patched_get_provider_id._group_focus_original = original_get_provider

        module._is_content_risk_error = patched_is_content_risk
        module.get_provider_id_with_fallback = patched_get_provider_id
        module._group_focus_llm_patched = True
        module._group_focus_llm_original_is_content = original_is_content
        module._group_focus_llm_original_get_provider = original_get_provider
        self._llm_module = module
        logger.info("[group-focus] 已应用内容审核自动回退补丁")

    def _unpatch_llm_fallback(self) -> None:
        """作用：恢复 llm_utils 原始函数，避免插件关闭后仍改变原插件行为。"""
        module = self._llm_module
        if module is None:
            return
        try:
            if getattr(module, "_group_focus_llm_patched", False):
                original_is_content = getattr(
                    module, "_group_focus_llm_original_is_content", None
                )
                original_get_provider = getattr(
                    module, "_group_focus_llm_original_get_provider", None
                )
                if original_is_content is not None:
                    module._is_content_risk_error = original_is_content
                if original_get_provider is not None:
                    module.get_provider_id_with_fallback = original_get_provider
                module._group_focus_llm_patched = False
                logger.info("[group-focus] 已恢复内容审核回退补丁")
        except Exception as exc:
            logger.warning(f"[group-focus] 恢复内容审核回退补丁失败: {exc}")
        finally:
            self._llm_module = None

    def _apply_patch(self) -> None:
        # 作用：把按群定向的包装套到 TopicAnalyzer 实例上，并安装内容审核回退/增量解耦
        self._apply_llm_fallback_patch()
        self._apply_incremental_decouple_patch()
        analyzer = self._topic_analyzer()
        if analyzer is None:
            logger.debug("[group-focus] 群分析插件未就绪，等待 on_plugin_loaded")
            return

        # 如果已被旧实例 patch 过，先恢复，避免旧闭包继续生效
        if getattr(analyzer, "_group_focus_patched", False):
            old_analyze = getattr(analyzer, "_group_focus_original_analyze", None)
            old_build = getattr(analyzer, "_group_focus_original_build", None)
            if old_analyze and old_build:
                analyzer.analyze = old_analyze
                analyzer.build_prompt_with_override = old_build
                analyzer._group_focus_patched = False
            else:
                logger.warning("[group-focus] 检测到旧补丁但找不到原方法，跳过重打")
                return

        original_analyze = analyzer.analyze
        original_build = analyzer.build_prompt_with_override
        plugin = self

        async def patched_analyze(data, umo=None, persona_id=None, prompt_override=None):
            # 作用：分析前设置本群方向；分析后按方向白名单过滤话题
            directions = plugin._focus_for_umo(umo) if plugin._enabled else []
            token = _CURRENT_FOCUS.set(directions or None)
            try:
                topics, usage = await original_analyze(
                    data, umo, persona_id, prompt_override
                )
            finally:
                _CURRENT_FOCUS.reset(token)

            if (
                directions
                and plugin._enabled
                and bool(plugin.config.get("enable_label_filter", True))
            ):
                topics = plugin._filter_topics(topics, directions)
            return topics, usage

        def patched_build(data, prompt_override=None):
            # 作用：把本群方向追加到话题分析提示词
            prompt = original_build(data, prompt_override)
            directions = _CURRENT_FOCUS.get()
            if directions and plugin._enabled:
                direction_lines = "\n".join(
                    f"{index}. 关键词：{keyword}\n   说明：{explanation}"
                    for index, (keyword, explanation) in enumerate(directions, 1)
                )
                block = (
                    "\n\n【本群内容方向（必须遵守）】\n"
                    f"本群只关注以下关键词：\n{direction_lines}\n"
                    "1. 每个话题的 topic 必须以 [关键词] 开头，关键词必须与上面某一条"
                    "完全一致；判断话题是否属于该方向时，参考对应说明。\n"
                    "2. 不属于上述关键词的话题不要输出。\n"
                    "3. 仍然只输出原来的 JSON，不要加额外文字。\n"
                )
                prompt = (prompt or "") + block
            return prompt

        analyzer.analyze = patched_analyze
        analyzer.build_prompt_with_override = patched_build
        analyzer._group_focus_patched = True
        analyzer._group_focus_original_analyze = original_analyze
        analyzer._group_focus_original_build = original_build
        self._patched = analyzer
        self._original_analyze = original_analyze
        self._original_build = original_build
        self._active = True
        logger.info("[group-focus] 已应用分群总结定向补丁")

    def _unpatch(self) -> None:
        # 作用：插件关闭/卸载时恢复原方法，保证总开关真的能关掉
        self._unpatch_llm_fallback()
        self._unpatch_incremental_decouple()
        analyzer = self._patched
        if analyzer is None:
            return
        try:
            if getattr(analyzer, "_group_focus_patched", False):
                analyzer.analyze = self._original_analyze
                analyzer.build_prompt_with_override = self._original_build
                analyzer._group_focus_patched = False
                logger.info("[group-focus] 已恢复群分析插件原方法")
        except Exception as exc:
            logger.warning(f"[group-focus] 恢复原方法失败: {exc}")
        finally:
            self._patched = None
            self._active = False

    @staticmethod
    def _normalize_label(text: str) -> str:
        """作用：比较方向标签时忽略空格和常见中英文标点差异。"""
        value = re.sub(r"\s+", "", str(text or ""))
        table = str.maketrans(
            {
                "，": ",",
                "、": ",",
                "。": ".",
                "；": ";",
                "：": ":",
                "！": "!",
                "？": "?",
                "（": "(",
                "）": ")",
                "【": "[",
                "】": "]",
            }
        )
        return value.translate(table).lower()

    def _filter_topics(self, topics, directions: list[tuple[str, str]]):
        """作用：只保留关键词白名单内的话题；[关键词] 前缀会被去掉。"""
        allowed = {self._normalize_label(keyword) for keyword, _ in directions}
        kept = []
        dropped_labels: list[str] = []
        for topic in topics or []:
            raw = str(getattr(topic, "topic", "") or "").strip()
            match = LABEL_RE.match(raw)
            if match:
                label = match.group(1).strip()
                if self._normalize_label(label) in allowed:
                    topic.topic = LABEL_RE.sub("", raw, count=1).strip()
                    kept.append(topic)
                else:
                    dropped_labels.append(label or "空标签")
                continue

            # 没有标签时兜底：话题名或详情里出现关键词/解释就保留
            haystack = raw + " " + str(getattr(topic, "detail", "") or "")
            if any(
                keyword in haystack or (explanation and explanation in haystack)
                for keyword, explanation in directions
            ):
                kept.append(topic)
            else:
                dropped_labels.append("无标签且不含关键词")

        if dropped_labels:
            logger.info(
                f"[group-focus] 过滤掉 {len(dropped_labels)} 条无关话题；"
                f"原因标签: {sorted(set(dropped_labels))[:10]}"
            )
        return kept

    # ---------- 私聊群历史报告（原 group_history_bridge 功能） ----------
    @filter.llm_tool("request_group_history_analysis")
    async def request_group_history_analysis(
        self,
        event: AstrMessageEvent,
        days: int = 7,
        analysis_sections: str = "全部",
    ) -> str:
        """当管理员在私聊中要求“分析历史 / 群分析 / 生成群报告 / 看看群最近聊了什么 / 总结最近的群聊”时调用。

        作用：按“接收者<-分析群号”路由，把每个分析群上次发送之后的增量批次报告发到对应接收者，
        并强制同步一次每个群当前未满 900 条的尾巴。每条报告都带时间戳（该批最后一条消息的时间）。

        使用规则：
        - 只有 AstrBot 管理员可以使用；普通群友调用会被拒绝。
        - 只在私聊中使用；如果在群聊中调用，会提示用户私聊。
        - 不需要向用户索要群号、文件路径或脚本；群号和接收者来自插件配置。
        - 当前私聊只负责触发；报告会发到“接收者<-分析群号”里配置的 UMO。
        - 本工具立即返回“已开始”，报告由后台任务在分析完成后单独发送，不要编造分析结果。
        - days 只用于第一次请求的初始回看范围（1-7 天，默认 7）；之后按“上次发送游标”继续。
        """
        if not event.is_admin():
            return "[权限不足] 群历史分析仅管理员可用。"
        if event.get_group_id():
            return "[提示] 请在私聊里对我说“分析历史”，我会按配置把报告发到对应接收者。"
        star = self._analysis_plugin()
        if star is None:
            return "[错误] 群分析插件未加载，暂时无法生成报告。"
        routes = self._receiver_routes()
        if not routes:
            return "[未配置] 请先在插件配置的“接收者<-分析群号”里填写至少一条路由。"
        try:
            days_int = max(1, min(int(days), 7))
        except (TypeError, ValueError):
            days_int = self._initial_lookback_days
        task = asyncio.create_task(
            self._run_routes(
                event, star, routes, days_int, str(analysis_sections or "全部")
            )
        )
        self._history_tasks.add(task)
        task.add_done_callback(self._history_tasks.discard)
        return (
            f"[已开始] 正在按 {len(routes)} 条路由整理报告并同步尾巴；"
            "完成后会发到对应接收者。"
        )

    @filter.on_llm_request(priority=100)
    async def inject_history_hint(self, event: AstrMessageEvent, req):
        """作用：在管理员私聊时注入一句规则，避免模型改去要群号/路径/脚本。"""
        try:
            if not event.is_admin() or event.get_group_id():
                return
            req.system_prompt = (req.system_prompt or "") + (
                "\n\n[群历史分析规则]\n"
                "当管理员说“分析历史 / 群分析 / 生成群报告 / 看看群最近聊了什么 / 总结最近的群聊”时，"
                "必须优先调用 request_group_history_analysis 工具，不要向用户索要群号、路径或脚本；"
                "该工具会按“接收者<-分析群号”路由发送增量报告，并强制同步当前未满 900 条的尾巴。\n"
            )
        except Exception:
            return

    def _analysis_plugin(self):
        """作用：按插件名找到已加载的群分析插件实例。"""
        meta = self.context.get_registered_star(ANALYSIS_PLUGIN)
        return getattr(meta, "star_cls", None) if meta else None

    async def _run_routes(self, event, star, routes, days: int, sections: str):
        """作用：逐条路由发送；某条失败不影响其他路由，最后把状态发回触发者。"""
        platform_id = str(event.get_platform_id() or "")
        requester_umo = event.unified_msg_origin
        delivered = 0
        errors: list[str] = []
        for group_id, target_umo in routes:
            try:
                delivered += await self._run_and_deliver(
                    star, group_id, target_umo, platform_id, days, sections
                )
            except Exception as exc:
                logger.error(
                    f"[group-focus] 群历史分析或发送失败（群 {group_id}）: {exc}",
                    exc_info=True,
                )
                errors.append(f"{group_id}: {exc}")
        if errors:
            detail = "；".join(errors[:3])
            prefix = "[部分失败]" if delivered else "[分析失败]"
            await self._send(requester_umo, f"{prefix} {detail}")
        elif delivered == 0:
            await self._send(requester_umo, "没有新的群消息需要总结。")

    async def _run_and_deliver(
        self,
        star,
        group_id: str,
        target_umo: str,
        platform_id: str,
        days: int,
        sections: str,
    ) -> int:
        """作用：强制同步尾巴 → 取出游标之后的批次 → 格式化发到目标 → 推进游标。"""
        async with self._history_run_lock:
            cursor = await self._load_cursor(target_umo, group_id)
            if cursor <= 0:
                lookback_days = max(days, self._initial_lookback_days)
                cursor = int(time.time() - lookback_days * 86400)

            # 1) 手动触发一次增量同步，把未满 900 条的尾巴也总结掉
            await self._force_tail(star, group_id, platform_id)

            # 2) 取游标之后的所有批次（自动 900 条批次 + 刚同步的尾巴）
            batches = await self._list_batches_after(star, group_id, cursor)
            if not batches:
                return 0

            truncated = len(batches) > self._max_history_batches
            batches = batches[: self._max_history_batches]
            texts = self._format_batches(group_id, batches, cursor, sections)
            for text in texts:
                await self._send(target_umo, text)

            new_cursor = max(
                int(getattr(b, "last_message_timestamp", 0) or 0) for b in batches
            )
            await self._save_cursor(target_umo, group_id, new_cursor)
            if truncated:
                await self._send(
                    target_umo,
                    "（本次先发这么多，还有更多报告；再问一次“分析历史”继续。）",
                )
            return len(batches)

    async def _force_tail(self, star, group_id: str, platform_id: str):
        """作用：调用带 force 的增量分析，把未满阈值的尾巴也生成批次。"""
        limit = int(star.config_manager.get_incremental_min_messages())
        for _ in range(self._max_force_loops):
            try:
                result = await star.analysis_service.execute_incremental_analysis(
                    group_id=group_id,
                    platform_id=platform_id,
                    force=True,
                )
            except TypeError:
                logger.error(
                    "[group-focus] 群分析插件缺少 force 补丁，无法手动同步尾巴"
                )
                return
            if not isinstance(result, dict) or not result.get("success"):
                return
            count = int(result.get("messages_count", 0) or 0)
            if count <= 0 or count < limit:
                return
        logger.info("[group-focus] 强制增量达到循环上限，剩余消息留到下次")

    async def _list_batches_after(self, star, group_id: str, cursor: int):
        """作用：取 last_message_timestamp 大于游标的批次，按时间升序。"""
        store = getattr(star.analysis_service, "incremental_store", None)
        if store is None:
            return []
        batches = await store.query_batches(group_id, 0, time.time() + 60)
        batches = [
            b
            for b in batches
            if int(getattr(b, "last_message_timestamp", 0) or 0) > cursor
        ]
        batches.sort(
            key=lambda b: (
                int(getattr(b, "last_message_timestamp", 0) or 0),
                float(getattr(b, "timestamp", 0) or 0),
            )
        )
        return batches

    @staticmethod
    def _display_nickname(name: str) -> str:
        """作用：昵称宽度不超过4个汉字时全显示，否则取前3个字；半角字符按0.5个汉字计。"""
        value = str(name or "").strip()
        if not value:
            return ""
        units = 0.0
        for char in value:
            if unicodedata.east_asian_width(char) in {"W", "F"}:
                units += 1.0
            else:
                units += 0.5
        return value if units <= 4.0 else value[:3]

    def _format_mentions(
        self,
        text: str,
        uid_to_name: dict[str, str],
        name_to_uid: dict[str, str],
        seen_uids: set[str],
    ) -> str:
        """作用：正文里的 QQ 号/昵称统一成 [显示名-QQ] 或 [显示名]；每份报告只带一次 QQ。"""
        text = str(text or "")
        if not text or not uid_to_name:
            return text
        alternatives = [
            re.escape(name)
            for name in sorted(name_to_uid, key=len, reverse=True)
            if name
        ]
        if uid_to_name:
            alternatives.append(r"(?<![:\d])\d{5,12}(?![\d:])")
        pattern = re.compile(r"\[?(?:" + "|".join(alternatives) + r")\]?")

        def repl(match: re.Match) -> str:
            raw = match.group(0)
            token = raw.strip("[]").strip()
            uid = name_to_uid.get(token)
            if uid is None and token in uid_to_name:
                uid = token
            if uid is None:
                return raw
            display = self._display_nickname(uid_to_name.get(uid, ""))
            if not display:
                return raw
            if uid in seen_uids:
                return f"[{display}]"
            seen_uids.add(uid)
            return f"[{display}-{uid}]"

        return pattern.sub(repl, text)

    def _format_batches(
        self, group_id: str, batches: list, cursor: int, sections: str
    ) -> list[str]:
        """作用：把增量批次逐份格式化成独立 Markdown 消息，返回消息列表。"""
        include_topics, include_quotes, include_quality = self._parse_sections(
            sections
        )
        texts: list[str] = []
        prev_ts = int(cursor)
        total = len(batches)
        for index, batch in enumerate(batches, 1):
            text, prev_ts = self._format_one_batch(
                group_id,
                batch,
                index,
                total,
                prev_ts,
                include_topics,
                include_quotes,
                include_quality,
            )
            texts.append(text)
        return texts

    def _format_one_batch(
        self,
        group_id: str,
        batch,
        index: int,
        total: int,
        start_ts: int,
        include_topics: bool,
        include_quotes: bool,
        include_quality: bool,
    ) -> tuple[str, int]:
        """作用：格式化单份报告；首次提及群员带 QQ，后续只写显示名。"""
        user_stats = getattr(batch, "user_stats", None) or {}
        uid_to_name: dict[str, str] = {}
        name_to_uid: dict[str, str] = {}
        if isinstance(user_stats, dict):
            for uid, info in user_stats.items():
                if not isinstance(info, dict):
                    continue
                name = str(info.get("nickname") or info.get("name") or "").strip()
                if not name:
                    continue
                uid_str = str(uid)
                uid_to_name[uid_str] = name
                name_to_uid.setdefault(name, uid_str)

        seen_uids: set[str] = set()
        last_ts = int(getattr(batch, "last_message_timestamp", 0) or 0)
        start_text = self._fmt_ts(start_ts) if start_ts > 0 else "上次发送之后"
        end_text = self._fmt_ts(last_ts)
        lines = [
            f"### 群聊增量报告（{index}/{total}）",
            f"- 群: {group_id}",
            f"- 覆盖时间: {start_text} ~ {end_text}",
            f"- 消息数: {int(getattr(batch, 'messages_count', 0) or 0)}",
        ]

        participants = getattr(batch, "participant_ids", None) or getattr(
            batch, "user_stats", None
        )
        if participants:
            try:
                lines.append(f"- 参与人数: {len(participants)}")
            except Exception:
                pass

        topics = getattr(batch, "topics", None) or []
        if include_topics and topics:
            lines.append("- 话题:")
            for topic in topics[:5]:
                if not isinstance(topic, dict):
                    continue
                name = self._format_mentions(
                    str(topic.get("topic", "")).strip(),
                    uid_to_name,
                    name_to_uid,
                    seen_uids,
                )
                detail = self._format_mentions(
                    str(topic.get("detail", "")).strip(),
                    uid_to_name,
                    name_to_uid,
                    seen_uids,
                )
                lines.append(f"  - {name}" + (f"：{detail}" if detail else ""))

        quotes = getattr(batch, "golden_quotes", None) or []
        if include_quotes and quotes:
            lines.append("- 金句:")
            for quote in quotes[:5]:
                if not isinstance(quote, dict):
                    continue
                content = self._format_mentions(
                    str(quote.get("content", "")).strip(),
                    uid_to_name,
                    name_to_uid,
                    seen_uids,
                )
                sender = self._format_mentions(
                    str(quote.get("sender", "")).strip(),
                    uid_to_name,
                    name_to_uid,
                    seen_uids,
                )
                reason = self._format_mentions(
                    str(quote.get("reason", "")).strip(),
                    uid_to_name,
                    name_to_uid,
                    seen_uids,
                )
                lines.append(
                    f"  - “{content}” — {sender}"
                    + (f"（{reason}）" if reason else "")
                )

        review = getattr(batch, "chat_quality_review", None)
        if include_quality and isinstance(review, dict):
            summary = self._format_mentions(
                str(review.get("summary", "")).strip(),
                uid_to_name,
                name_to_uid,
                seen_uids,
            )
            if summary:
                lines.append(f"- 聊天质量: {summary}")

        return "\n".join(lines).strip(), last_ts

    @staticmethod
    def _parse_sections(sections: str) -> tuple[bool, bool, bool]:
        """作用：解析要显示的报告模块；默认全部显示。"""
        raw = str(sections or "全部").strip()
        lower = raw.lower()
        if not raw or raw in {"全部", "all"} or lower == "all":
            return True, True, True
        include_topics = "话题" in raw or "topic" in lower
        include_quotes = "金句" in raw or "quote" in lower
        include_quality = "质量" in raw or "quality" in lower
        if not (include_topics or include_quotes or include_quality):
            return True, True, True
        return include_topics, include_quotes, include_quality

    @staticmethod
    def _fmt_ts(timestamp: int) -> str:
        """作用：把 epoch 转成 UTC+8 的“年月日 时:分”。"""
        try:
            return datetime.datetime.fromtimestamp(int(timestamp)).strftime(
                "%Y-%m-%d %H:%M"
            )
        except Exception:
            return str(timestamp)

    async def _load_cursor(self, umo: str, group_id: str) -> int:
        """作用：读取某条“接收者 + 分析群”路由上次收到报告的最后一条消息时间。"""
        data = await self.get_kv_data(HISTORY_CURSOR_KEY, {})
        if isinstance(data, dict):
            try:
                return int(data.get(f"{group_id}|{umo}", 0) or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    async def _save_cursor(self, umo: str, group_id: str, timestamp: int) -> None:
        """作用：保存某条路由已经发到哪条消息。"""
        data = await self.get_kv_data(HISTORY_CURSOR_KEY, {})
        if not isinstance(data, dict):
            data = {}
        data[f"{group_id}|{umo}"] = int(timestamp)
        await self.put_kv_data(HISTORY_CURSOR_KEY, data)

    async def _send(self, umo: str, text: str):
        """作用：把报告分段发到指定会话，避免单条消息过长。"""
        text = (text or "").strip() or "[没有可发送的内容]"
        limit = 3500
        for start in range(0, len(text), limit):
            await self.context.send_message(
                umo, MessageChain([Plain(text[start : start + limit])])
            )
            await asyncio.sleep(0.3)

    # ---------- 生命周期 ----------
    @filter.on_plugin_loaded()
    async def _on_plugin_loaded(self, metadata: Any, *args: Any, **kwargs: Any) -> None:
        # 作用：等群分析插件加载/重载后，把补丁套到新实例上
        if getattr(metadata, "name", "") == ANALYSIS_PLUGIN:
            self._apply_patch()

    async def terminate(self) -> None:
        # 作用：插件被关闭/卸载时停止后台报告任务并撤销补丁
        for task in list(self._history_tasks):
            task.cancel()
        self._history_tasks.clear()
        self._unpatch()
