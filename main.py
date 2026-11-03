"""群总结扩展补丁（群分析总结插件的按群定向 + 内容审核回退扩展）

作用：
- 在“内容方向关键词”里定义 关键词-具体内容的解释；
- 在“群号=关注方向”里只写关键词（例如 群号=关键词1|关键词2）；
- 分析前按当前 umo 只注入本群关键词及其解释；
- 分析后按 [关键词] 前缀白名单过滤话题（可开关）；
- 分析 LLM 遇到内容审核拒绝时，自动识别并临时切换到备用 Provider
  （默认 deepseek-flash），避免无人值守时整份报告失败；
- 插件页总开关关掉时，恢复原插件行为，不修改原插件源码。

依赖：astrbot_plugin_qq_group_daily_analysis
"""

from __future__ import annotations

import contextvars
import importlib
import re
import sys
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

ANALYSIS_PLUGIN = "astrbot_plugin_qq_group_daily_analysis"
LABEL_RE = re.compile(r"^\s*[\[【]([^\]】]+)[\]】]\s*[:：]?\s*")

# 作用：在同一次话题分析调用里传递“本群关键词+解释”，避免并发分析串群
_CURRENT_FOCUS: contextvars.ContextVar[list[tuple[str, str]] | None] = (
    contextvars.ContextVar("group_focus_directions", default=None)
)


@register(
    "astrbot_plugin_qq_group_daily_analysis_enhance",
    "DspaCto",
    "群总结扩展补丁：按群定向 + 内容审核自动回退",
    "1.3.0",
)
class GroupDailyFocus(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._patched = None
        self._original_analyze = None
        self._original_build = None
        self._llm_module = None
        self._active = False
        self._apply_patch()

    # ---------- 配置读取 ----------
    @property
    def _enabled(self) -> bool:
        # 作用：插件自身开关；关掉后只做原插件行为
        return bool(self.config.get("enabled", True))

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

    def _keyword_definitions(self) -> dict[str, str]:
        """作用：解析“关键词-具体内容的解释”，一行一个；只按第一个 - 分割。"""
        raw = self.config.get("focus_keywords", "")
        if not isinstance(raw, str) or not raw.strip():
            return {}
        definitions: dict[str, str] = {}
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "-" not in line:
                logger.debug(f"[group-focus] 忽略没有 - 的关键词行：{line[:40]}")
                continue
            keyword, explanation = line.split("-", 1)
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
            key, value = line.split("=", 1)
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
        # 作用：把按群定向的包装套到 TopicAnalyzer 实例上，并安装内容审核回退
        self._apply_llm_fallback_patch()
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

    # ---------- 生命周期 ----------
    @filter.on_plugin_loaded()
    async def _on_plugin_loaded(self, metadata: Any, *args: Any, **kwargs: Any) -> None:
        # 作用：等群分析插件加载/重载后，把补丁套到新实例上
        if getattr(metadata, "name", "") == ANALYSIS_PLUGIN:
            self._apply_patch()

    async def terminate(self) -> None:
        # 作用：插件被关闭/卸载时撤销补丁
        self._unpatch()
