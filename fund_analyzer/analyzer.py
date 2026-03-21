"""
AI 基金分析器核心模块
提供基于大模型的智能分析功能，整合量化分析数据
"""

import asyncio
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any
import re
from urllib.parse import urlsplit

import aiohttp

from astrbot.api import logger

from .factors import FundInfluenceFactors
from .prompts import AnalysisPromptBuilder
from .quant import QuantAnalyzer

if TYPE_CHECKING:
    from astrbot.api.provider import Provider
    from astrbot.api.star import Context


class AIFundAnalyzer:
    """AI 智能基金分析器（含量化分析）"""

    ANALYSIS_TARGET_CHARS = 1400
    ANALYSIS_MAX_CHARS = 1800

    def __init__(self, context: "Context"):
        """
        初始化 AI 分析器

        Args:
            context: AstrBot 上下文
        """
        self.context = context
        self.factors = FundInfluenceFactors()
        self.prompt_builder = AnalysisPromptBuilder()
        self.quant = QuantAnalyzer()  # 量化分析器

    @classmethod
    def _normalize_text_blocks(cls, text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").strip()
        normalized = re.sub(r"\n[ \t]+\n", "\n\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized

    @classmethod
    def _limit_analysis_output(
        cls,
        text: str,
        *,
        target_chars: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        normalized = cls._normalize_text_blocks(text)
        if not normalized:
            return ""

        target = max(int(target_chars or cls.ANALYSIS_TARGET_CHARS), 200)
        maximum = max(int(max_chars or cls.ANALYSIS_MAX_CHARS), target)
        if len(normalized) <= maximum:
            return normalized

        suffix = "\n\n（内容已按系统字数上限压缩，如需展开某一部分，请继续追问对应主题。）"
        budget = max(maximum - len(suffix), 120)
        preferred_budget = min(target, budget)

        blocks = [block.strip() for block in re.split(r"\n\s*\n", normalized) if block.strip()]
        if not blocks:
            return normalized[:budget].rstrip() + suffix

        selected: list[str] = []
        current_length = 0
        stop = False
        for block in blocks:
            addition = len(block) if not selected else len(block) + 2
            if current_length + addition > budget:
                if current_length >= preferred_budget:
                    stop = True
                    break
                remaining = budget - current_length - (0 if not selected else 2)
                if remaining > 40:
                    trimmed = block[:remaining].rstrip("，,；;：:、 \n")
                    if trimmed:
                        selected.append(trimmed)
                stop = True
                break
            selected.append(block)
            current_length += addition
            if current_length >= preferred_budget:
                stop = True
                break

        if not selected:
            return normalized[:budget].rstrip() + suffix

        limited = "\n\n".join(selected).strip()
        if stop and len(limited) < len(normalized):
            return limited + suffix
        return limited

    def _get_provider(self) -> "Provider | None":
        """获取 LLM 提供商"""
        return self.context.get_using_provider()

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        url = (url or "").strip().rstrip("/")
        if not url:
            return ""
        if not url.endswith("/v1"):
            url = url + "/v1" if not re.search(r"/v\d+$", url) else url
        return url

    @staticmethod
    def _normalize_api_keys(api_keys: Any) -> list[str]:
        if api_keys is None:
            return []
        if isinstance(api_keys, list):
            return [str(item).strip() for item in api_keys if str(item).strip()]
        if isinstance(api_keys, tuple | set):
            return [str(item).strip() for item in api_keys if str(item).strip()]
        if isinstance(api_keys, str) and api_keys.strip():
            return [api_keys.strip()]
        if isinstance(api_keys, dict):
            return [str(item).strip() for item in api_keys.values() if str(item).strip()]
        return []

    @classmethod
    def _unwrap_config_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key in ("value", "default"):
                if key in value:
                    return value[key]
        return value

    @classmethod
    def _coerce_mapping(cls, item: Any) -> dict[str, Any] | None:
        if item is None:
            return None
        if isinstance(item, dict):
            return item
        if hasattr(item, "__dict__"):
            return vars(item)
        return None

    @classmethod
    def _unwrap_provider_item(cls, item: Any) -> Any:
        mapping = cls._coerce_mapping(item)
        if mapping is None:
            return item

        if any(key in mapping for key in ("base_url", "api_keys", "model")):
            return mapping

        for key in ("config", "data", "payload", "provider", "provider_config", "value", "items"):
            child = mapping.get(key)
            child_mapping = cls._coerce_mapping(child)
            if not child_mapping:
                continue
            if any(field in child_mapping for field in ("base_url", "api_keys", "model")):
                return child_mapping
            nested = cls._unwrap_provider_item(child_mapping)
            nested_mapping = cls._coerce_mapping(nested)
            if nested_mapping and any(field in nested_mapping for field in ("base_url", "api_keys", "model")):
                return nested_mapping

        if len(mapping) == 1:
            only_value = next(iter(mapping.values()))
            only_mapping = cls._coerce_mapping(only_value)
            if only_mapping:
                return cls._unwrap_provider_item(only_mapping)

        return mapping

    @classmethod
    def _provider_item_to_dict(cls, item: Any) -> dict[str, str | list[str]] | None:
        if item is None:
            return None
        unwrapped = cls._unwrap_provider_item(item)
        if isinstance(unwrapped, dict):
            base_url = str(cls._unwrap_config_value(unwrapped.get("base_url")) or "").strip()
            api_keys = cls._normalize_api_keys(cls._unwrap_config_value(unwrapped.get("api_keys")))
            model = str(cls._unwrap_config_value(unwrapped.get("model")) or "").strip()
        else:
            base_url = str(cls._unwrap_config_value(getattr(unwrapped, "base_url", None)) or "").strip()
            api_keys = cls._normalize_api_keys(
                cls._unwrap_config_value(getattr(unwrapped, "api_keys", None))
            )
            model = str(cls._unwrap_config_value(getattr(unwrapped, "model", None)) or "").strip()
        if not base_url or not api_keys:
            return None
        return {"base_url": base_url, "api_keys": api_keys, "model": model}

    @classmethod
    def _iter_provider_items(cls, raw: Any):
        queue: list[Any] = [raw]
        seen_ids: set[int] = set()
        while queue:
            current = queue.pop(0)
            if current is None:
                continue

            is_container = isinstance(current, (list, tuple, set, dict)) or hasattr(current, "__dict__")
            if is_container:
                current_id = id(current)
                if current_id in seen_ids:
                    continue
                seen_ids.add(current_id)

            if isinstance(current, (list, tuple, set)):
                queue.extend(list(current))
                continue

            mapping = cls._coerce_mapping(current)
            if mapping is None:
                yield current
                continue

            yield mapping
            for child in mapping.values():
                if isinstance(child, (list, tuple, set, dict)) or hasattr(child, "__dict__"):
                    queue.append(child)

    @classmethod
    def _normalize_provider_configs(cls, raw: Any) -> list[dict[str, str | list[str]]]:
        out: list[dict[str, str | list[str]]] = []
        seen: set[tuple[str, tuple[str, ...], str]] = set()
        for item in cls._iter_provider_items(raw):
            normalized = cls._provider_item_to_dict(item)
            if normalized:
                base_url = str(normalized.get("base_url") or "").strip()
                model = str(normalized.get("model") or "").strip()
                api_keys_raw = normalized.get("api_keys") or []
                if isinstance(api_keys_raw, list):
                    api_keys = tuple(str(v).strip() for v in api_keys_raw if str(v).strip())
                elif isinstance(api_keys_raw, str):
                    api_keys = (api_keys_raw.strip(),) if api_keys_raw.strip() else tuple()
                else:
                    api_keys = tuple()
                dedupe_key = (base_url, api_keys, model)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                out.append(normalized)
        return out

    @staticmethod
    def _first_api_key(provider_config: dict[str, str | list[str]]) -> str:
        api_keys = provider_config.get("api_keys") or []
        if isinstance(api_keys, list) and api_keys:
            return str(api_keys[0]).strip()
        if isinstance(api_keys, str):
            return api_keys.strip()
        return ""

    def normalize_provider_configs(self, raw: Any) -> list[dict[str, str | list[str]]]:
        return self._normalize_provider_configs(raw)

    async def _generate_with_openai_compatible(
        self,
        provider_config: dict[str, str | list[str]],
        prompt: str,
        timeout_seconds: int,
    ) -> str:
        base_url = self._normalize_base_url(str(provider_config.get("base_url") or ""))
        api_key = self._first_api_key(provider_config)
        model = str(provider_config.get("model") or "gpt-4o-mini").strip()
        if not base_url or not api_key:
            raise ValueError("服务商未填完整（需 API 地址、API Key、模型名称）")

        post_url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "max_tokens": 4096,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                post_url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=max(int(timeout_seconds), 1)),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"OpenAI 兼容接口返回 {resp.status}: {text[:300]}")
                data = await resp.json()
                choices = (data or {}).get("choices") or []
                if not choices:
                    raise RuntimeError("OpenAI 兼容接口未返回 choices")
                message = choices[0].get("message") or {}
                content = message.get("content") or ""
                if isinstance(content, list):
                    joined = []
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text")
                            if text:
                                joined.append(str(text))
                    content = "\n".join(joined)
                return str(content).strip()

    async def _generate_with_provider(
        self,
        prompt: str,
        session_id: str,
        provider_id: str = "",
        timeout_seconds: int = 90,
        provider_configs: Any = None,
    ) -> str:
        """统一封装文本生成，支持显式 provider_id 或回退当前会话 provider。"""
        provider_id = str(provider_id or "").strip()
        timeout = max(int(timeout_seconds), 1)
        normalized_provider_configs = self._normalize_provider_configs(provider_configs)

        if normalized_provider_configs:
            last_error: Exception | None = None
            for provider_config in normalized_provider_configs:
                try:
                    return await self._generate_with_openai_compatible(
                        provider_config=provider_config,
                        prompt=prompt,
                        timeout_seconds=timeout,
                    )
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "OpenAI 兼容智能分析服务商调用失败: model=%s, base_url=%s, error=%s",
                        str(provider_config.get("model") or ""),
                        self._normalize_base_url(str(provider_config.get("base_url") or "")),
                        exc,
                    )
            if last_error:
                raise last_error

        if provider_id:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                ),
                timeout=timeout,
            )
            return (getattr(response, "completion_text", None) or "").strip()

        provider = self._get_provider()
        if not provider:
            raise ValueError("未配置大模型提供商")

        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                session_id=session_id,
                persist=False,
            ),
            timeout=timeout,
        )
        return (getattr(response, "completion_text", None) or "").strip()

    @staticmethod
    def _provider_debug_info(provider: Any) -> dict[str, str]:
        """提取可安全打印的 provider 调试信息，不包含密钥。"""
        def first_attr(obj: Any, names: tuple[str, ...]) -> str:
            for name in names:
                value = getattr(obj, name, None)
                if value:
                    return str(value)
            return ""

        def mask_url(raw: str) -> str:
            text = (raw or "").strip()
            if not text:
                return ""
            try:
                parsed = urlsplit(text)
                if parsed.scheme and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            except Exception:
                pass
            return text

        return {
            "provider_class": f"{provider.__class__.__module__}.{provider.__class__.__name__}",
            "provider_name": first_attr(provider, ("name", "provider_name", "id")),
            "model": first_attr(provider, ("model", "model_name", "default_model", "_model")),
            "base_url": mask_url(first_attr(provider, ("base_url", "api_base", "endpoint", "host"))),
        }

    async def get_news_summary(
        self,
        fund_name: str,
        fund_code: str,
        provider_id: str = "",
        timeout_seconds: int = 90,
        provider_configs: Any = None,
    ) -> str:
        """
        获取基金相关新闻摘要（增强版，含季节性因素和国际形势）

        Args:
            fund_name: 基金名称
            fund_code: 基金代码

        Returns:
            新闻摘要文本
        """
        provider = self._get_provider()
        normalized_provider_configs = self._normalize_provider_configs(provider_configs)
        if not provider and not str(provider_id or "").strip() and not normalized_provider_configs:
            return "暂无法获取新闻资讯（未配置大模型）"
        provider_info = (
            {
                "provider_class": "openai_compatible_config",
                "provider_name": "",
                "model": str(normalized_provider_configs[0].get("model") or ""),
                "base_url": self._normalize_base_url(
                    str(normalized_provider_configs[0].get("base_url") or "")
                ),
            }
            if normalized_provider_configs
            else self._provider_debug_info(provider)
            if provider
            else {
                "provider_class": "configured_provider_id",
                "provider_name": str(provider_id or "").strip(),
                "model": "",
                "base_url": "",
            }
        )

        # 获取影响因素
        factors = self.factors.get_factors(fund_name)

        # 获取季节性背景
        seasonal_context = self.factors.get_current_seasonal_context(fund_name)

        # 获取增强版搜索关键词
        search_keywords = self.factors.get_news_search_keywords(fund_name)

        # 获取国际形势分析文本
        global_situation_text = self.factors.format_global_situation_text(fund_name)

        # 构建提示词（使用增强版，含国际形势）
        prompt = self.prompt_builder.build_news_prompt(
            fund_name=fund_name,
            underlying=factors["underlying"],
            seasonal_context=seasonal_context,
            search_keywords=search_keywords,
            global_situation_text=global_situation_text,
        )

        try:
            started_at = perf_counter()
            logger.info(
                "开始获取新闻摘要: %s(%s), prompt_length=%s, provider=%s, model=%s, base_url=%s, configured_provider_id=%s, timeout=%ss",
                fund_name,
                fund_code,
                len(prompt),
                provider_info["provider_class"],
                provider_info["model"] or provider_info["provider_name"] or "unknown",
                provider_info["base_url"] or "unknown",
                str(provider_id or "").strip() or "<session-default>",
                timeout_seconds,
            )
            output = await self._generate_with_provider(
                prompt=prompt,
                session_id=f"fund_news_{fund_code}_{datetime.now().strftime('%Y%m%d')}",
                provider_id=provider_id,
                timeout_seconds=timeout_seconds,
                provider_configs=provider_configs,
            )
            logger.info(
                "新闻摘要获取完成: %s(%s), elapsed=%.2fs, output_length=%s",
                fund_name,
                fund_code,
                perf_counter() - started_at,
                len(output),
            )
            return output
        except Exception as e:
            logger.warning(f"获取新闻摘要失败: {e}")
            return "暂无法获取最新新闻资讯"

    async def analyze(
        self,
        fund_info: Any,  # FundInfo 类型
        history_data: list[dict],
        technical_indicators: dict[str, Any],
        user_id: str,
        fund_flow_text: str = "",
        provider_id: str = "",
        timeout_seconds: int = 90,
        provider_configs: Any = None,
    ) -> str:
        """
        执行 AI 智能分析（含量化数据和资金流向）

        Args:
            fund_info: 基金信息对象
            history_data: 历史数据列表
            technical_indicators: 技术指标（旧版，保留兼容性）
            user_id: 用户 ID
            fund_flow_text: 资金流向数据文本

        Returns:
            分析结果文本
        """
        provider = self._get_provider()
        normalized_provider_configs = self._normalize_provider_configs(provider_configs)
        if not provider and not str(provider_id or "").strip() and not normalized_provider_configs:
            raise ValueError("未配置大模型提供商")
        provider_info = (
            {
                "provider_class": "openai_compatible_config",
                "provider_name": "",
                "model": str(normalized_provider_configs[0].get("model") or ""),
                "base_url": self._normalize_base_url(
                    str(normalized_provider_configs[0].get("base_url") or "")
                ),
            }
            if normalized_provider_configs
            else self._provider_debug_info(provider)
            if provider
            else {
                "provider_class": "configured_provider_id",
                "provider_name": str(provider_id or "").strip(),
                "model": "",
                "base_url": "",
            }
        )

        started_at = perf_counter()
        logger.info(
            "开始 AI 智能分析: %s(%s), history_count=%s, flow_text_length=%s, provider=%s, model=%s, base_url=%s, configured_provider_id=%s, timeout=%ss",
            fund_info.name,
            fund_info.code,
            len(history_data),
            len(fund_flow_text or ""),
            provider_info["provider_class"],
            provider_info["model"] or provider_info["provider_name"] or "unknown",
            provider_info["base_url"] or "unknown",
            str(provider_id or "").strip() or "<session-default>",
            timeout_seconds,
        )

        # 1. 计算量化绩效指标
        performance = self.quant.calculate_performance(history_data)
        performance_summary = (
            self.quant.format_performance_text(performance)
            if performance
            else "历史数据不足，无法计算绩效指标"
        )
        logger.info("AI 智能分析阶段完成: 绩效指标已计算 - %s(%s)", fund_info.name, fund_info.code)

        # 2. 计算全部技术指标
        tech_indicators = self.quant.calculate_all_indicators(history_data)
        tech_indicators_text = self.quant.format_indicators_text(tech_indicators)
        logger.info("AI 智能分析阶段完成: 技术指标已计算 - %s(%s)", fund_info.name, fund_info.code)

        # 3. 运行策略回测
        backtest_results = self.quant.run_all_backtests(history_data)
        backtest_summary = self.quant.format_backtest_text(backtest_results)
        logger.info("AI 智能分析阶段完成: 策略回测已完成 - %s(%s)", fund_info.name, fund_info.code)

        # 4. 获取影响因素文本
        factors_text = self.factors.format_factors_text(fund_info.name)

        # 5. 获取国际形势分析文本
        global_situation_text = self.factors.format_global_situation_text(fund_info.name)

        # 6. 格式化历史数据
        history_summary = self.prompt_builder.format_history_summary(history_data)

        # 7. 获取新闻摘要（含国际形势）
        news_summary = await self.get_news_summary(
            fund_name=fund_info.name,
            fund_code=fund_info.code,
            provider_id=provider_id,
            timeout_seconds=timeout_seconds,
            provider_configs=provider_configs,
        )
        logger.info("AI 智能分析阶段完成: 新闻摘要已就绪 - %s(%s)", fund_info.name, fund_info.code)

        # 8. 构建分析提示词（使用新模板，含国际形势和资金流向）
        analysis_prompt = self._build_quant_analysis_prompt(
            fund_info=fund_info,
            performance_summary=performance_summary,
            tech_indicators_text=tech_indicators_text,
            backtest_summary=backtest_summary,
            factors_text=factors_text,
            history_summary=history_summary,
            news_summary=news_summary,
            global_situation_text=global_situation_text,
            fund_flow_text=fund_flow_text,
        )
        logger.info(
            "AI 智能分析阶段完成: 主分析提示词已构建 - %s(%s), prompt_length=%s",
            fund_info.name,
            fund_info.code,
            len(analysis_prompt),
        )

        # 9. 调用大模型分析
        logger.info("开始调用主分析模型: %s(%s)", fund_info.name, fund_info.code)
        output = await self._generate_with_provider(
            prompt=analysis_prompt,
            session_id=f"fund_analysis_{fund_info.code}_{user_id}",
            provider_id=provider_id,
            timeout_seconds=timeout_seconds,
            provider_configs=provider_configs,
        )
        logger.info(
            "主分析模型返回成功: %s(%s), elapsed=%.2fs, output_length=%s",
            fund_info.name,
            fund_info.code,
            perf_counter() - started_at,
            len(output),
        )

        limited_output = self._limit_analysis_output(output)
        if len(limited_output) != len(output):
            logger.info(
                "主分析模型输出已压缩: %s(%s), raw_length=%s, limited_length=%s",
                fund_info.name,
                fund_info.code,
                len(output),
                len(limited_output),
            )

        return limited_output

    def _build_quant_analysis_prompt(
        self,
        fund_info: Any,
        performance_summary: str,
        tech_indicators_text: str,
        backtest_summary: str,
        factors_text: str,
        history_summary: str,
        news_summary: str,
        global_situation_text: str = "",
        fund_flow_text: str = "",
    ) -> str:
        """构建包含量化数据、国际形势和资金流向的分析提示词"""
        from .prompts import ANALYSIS_PROMPT_TEMPLATE

        return ANALYSIS_PROMPT_TEMPLATE.format(
            fund_name=fund_info.name,
            fund_code=fund_info.code,
            latest_price=fund_info.latest_price,
            change_rate=fund_info.change_rate,
            amount=fund_info.amount,
            current_date=datetime.now().strftime("%Y年%m月%d日"),
            performance_summary=performance_summary
            if performance_summary
            else "暂无数据",
            tech_indicators=tech_indicators_text
            if tech_indicators_text
            else "暂无数据",
            backtest_summary=backtest_summary
            if backtest_summary
            else "历史数据不足，无法回测",
            factors_text=factors_text,
            global_situation_text=global_situation_text
            if global_situation_text
            else "暂无国际形势分析",
            fund_flow_text=fund_flow_text
            if fund_flow_text
            else "暂无资金流向数据",
            history_summary=history_summary if history_summary else "暂无数据",
            news_summary=news_summary if news_summary else "暂无相关新闻",
        )

    async def quick_analyze(
        self,
        fund_info: Any,  # FundInfo 类型
        trend: str,
    ) -> str:
        """
        快速分析（简化版）

        Args:
            fund_info: 基金信息对象
            trend: 技术趋势判断

        Returns:
            快速分析结果
        """
        provider = self._get_provider()
        if not provider:
            raise ValueError("未配置大模型提供商")

        prompt = self.prompt_builder.build_quick_prompt(
            fund_name=fund_info.name,
            fund_code=fund_info.code,
            latest_price=fund_info.latest_price,
            change_rate=fund_info.change_rate,
            trend=trend,
        )

        response = await provider.text_chat(
            prompt=prompt,
            session_id=f"fund_quick_{fund_info.code}",
            persist=False,
        )

        return response.completion_text

    async def assess_risk(
        self,
        fund_info: Any,  # FundInfo 类型
        technical_indicators: dict[str, Any],
    ) -> str:
        """
        风险评估

        Args:
            fund_info: 基金信息对象
            technical_indicators: 技术指标

        Returns:
            风险评估结果
        """
        provider = self._get_provider()
        if not provider:
            raise ValueError("未配置大模型提供商")

        factors = self.factors.get_factors(fund_info.name)

        prompt = self.prompt_builder.build_risk_prompt(
            fund_name=fund_info.name,
            fund_type=factors["type"],
            underlying=factors["underlying"],
            volatility=technical_indicators.get("volatility", 0),
            high_20d=technical_indicators.get("high_20d", 0),
            low_20d=technical_indicators.get("low_20d", 0),
        )

        response = await provider.text_chat(
            prompt=prompt,
            session_id=f"fund_risk_{fund_info.code}",
            persist=False,
        )

        return response.completion_text

    def get_influence_factors(self, fund_name: str) -> dict:
        """
        获取基金影响因素

        Args:
            fund_name: 基金名称

        Returns:
            影响因素字典
        """
        return self.factors.get_factors(fund_name)

    # ============================================================
    # 量化分析方法（无需 LLM）
    # ============================================================

    def get_quant_summary(self, history_data: list[dict]) -> str:
        """
        获取量化分析摘要（无需 LLM）

        Args:
            history_data: 历史数据列表

        Returns:
            量化分析文本摘要
        """
        lines = ["📊 **量化分析报告**\n"]

        # 1. 绩效指标
        performance = self.quant.calculate_performance(history_data)
        if performance:
            lines.append("**【绩效分析】**")
            lines.append(f"累计收益: {performance.total_return:+.2f}%")
            lines.append(f"年化收益: {performance.annual_return:+.2f}%")
            lines.append(f"年化波动率: {performance.volatility:.2f}%")
            lines.append(f"最大回撤: {performance.max_drawdown:.2f}%")
            lines.append(f"夏普比率: {performance.sharpe_ratio:.2f}")
            lines.append(f"索提诺比率: {performance.sortino_ratio:.2f}")
            lines.append(f"95% VaR: {performance.var_95:.2f}%")
            lines.append("")

        # 2. 技术指标
        indicators = self.quant.calculate_all_indicators(history_data)
        lines.append("**【技术指标】**")
        if indicators.ma5:
            lines.append(f"MA5: {indicators.ma5:.4f}")
        if indicators.ma20:
            lines.append(f"MA20: {indicators.ma20:.4f}")
        if indicators.rsi_14:
            rsi_status = (
                "超买"
                if indicators.rsi_14 > 70
                else "超卖"
                if indicators.rsi_14 < 30
                else "中性"
            )
            lines.append(f"RSI(14): {indicators.rsi_14:.2f} ({rsi_status})")
        if indicators.macd_hist is not None:
            macd_status = "红柱" if indicators.macd_hist > 0 else "绿柱"
            lines.append(f"MACD: {macd_status}")
        lines.append(f"综合评分: {indicators.trend_score} 分")
        lines.append(f"**技术信号: {indicators.signal}**")
        lines.append("")

        # 3. 回测结果
        backtests = self.quant.run_all_backtests(history_data)
        if backtests:
            lines.append("**【策略回测】**")
            for bt in backtests:
                lines.append(
                    f"• {bt.strategy_name}: 收益 {bt.total_return:+.2f}%, 胜率 {bt.win_rate:.1f}%"
                )
            lines.append("")

        return "\n".join(lines)

    def get_technical_signal(self, history_data: list[dict]) -> tuple[str, int]:
        """
        获取技术信号

        Args:
            history_data: 历史数据列表

        Returns:
            (信号文本, 评分) 元组
        """
        indicators = self.quant.calculate_all_indicators(history_data)
        return indicators.signal, indicators.trend_score

    def get_performance_metrics(self, history_data: list[dict]):
        """
        获取绩效指标

        Args:
            history_data: 历史数据列表

        Returns:
            PerformanceMetrics 对象或 None
        """
        return self.quant.calculate_performance(history_data)

    def get_backtest_results(self, history_data: list[dict]):
        """
        获取回测结果

        Args:
            history_data: 历史数据列表

        Returns:
            BacktestResult 列表
        """
        return self.quant.run_all_backtests(history_data)
