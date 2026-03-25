from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools

from .config_utils import ensure_flat_config
from .eastmoney_api import get_api as get_eastmoney_api
from .fund_analyzer import AIFundAnalyzer, QuantAnalyzer
from .fund_stock import DebateEngine, StockAnalyzer
from .passive_memory_utils import record_passive_habit


@dataclass
class FundInfo:
    code: str
    name: str
    latest_price: float
    change_amount: float
    change_rate: float
    open_price: float
    high_price: float
    low_price: float
    prev_close: float
    volume: float
    amount: float
    turnover_rate: float

    @property
    def trend_emoji(self) -> str:
        if self.change_rate >= 3:
            return "🚀"
        if self.change_rate > 0:
            return "📈"
        if self.change_rate <= -3:
            return "💥"
        if self.change_rate < 0:
            return "📉"
        return "➖"


class FundMarketService:
    DEFAULT_FUND_CODE = "161226"

    def __init__(self) -> None:
        self._api = get_eastmoney_api()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def get_fund_realtime(self, fund_code: str | None = None) -> FundInfo | None:
        code = str(fund_code or self.DEFAULT_FUND_CODE).strip()
        if not code:
            return None
        try:
            data = await self._api.get_fund_realtime(code)
        except Exception as exc:
            logger.error("获取基金实时行情失败: %s", exc)
            return None
        if not data:
            return None
        return FundInfo(
            code=str(data.get("code", code)),
            name=str(data.get("name", code)),
            latest_price=self._safe_float(data.get("latest_price")),
            change_amount=self._safe_float(data.get("change_amount")),
            change_rate=self._safe_float(data.get("change_rate")),
            open_price=self._safe_float(data.get("open_price")),
            high_price=self._safe_float(data.get("high_price")),
            low_price=self._safe_float(data.get("low_price")),
            prev_close=self._safe_float(data.get("prev_close")),
            volume=self._safe_float(data.get("volume")),
            amount=self._safe_float(data.get("amount")),
            turnover_rate=self._safe_float(data.get("turnover_rate")),
        )

    async def get_fund_history(
        self,
        fund_code: str | None = None,
        days: int = 30,
        adjust: str = "qfq",
    ) -> list[dict]:
        code = str(fund_code or self.DEFAULT_FUND_CODE).strip()
        if not code:
            return []
        try:
            data = await self._api.get_fund_history(code, days, adjust)
        except Exception as exc:
            logger.error("获取基金历史行情失败: %s", exc)
            return []
        return data or []

    async def search_fund(self, keyword: str) -> list[dict]:
        kw = (keyword or "").strip()
        if not kw:
            return []
        try:
            return await self._api.search_fund(kw) or []
        except Exception as exc:
            logger.error("搜索基金失败: %s", exc)
            return []

    def calculate_technical_indicators(self, history_data: list[dict]) -> dict[str, Any]:
        if not history_data or len(history_data) < 5:
            return {}

        quant = QuantAnalyzer()
        indicators = quant.calculate_all_indicators(history_data)
        performance = quant.calculate_performance(history_data)

        closes = [self._safe_float(item.get("close")) for item in history_data if item.get("close") is not None]
        closes = [value for value in closes if value > 0]
        if not closes:
            return {}

        current_price = closes[-1]

        def calc_return(days: int) -> float | None:
            if len(closes) <= days:
                return None
            base = closes[-(days + 1)]
            if base == 0:
                return None
            return (current_price - base) / base * 100

        return {
            "ma5": indicators.ma5,
            "ma10": indicators.ma10,
            "ma20": indicators.ma20,
            "return_5d": calc_return(5),
            "return_10d": calc_return(10),
            "return_20d": calc_return(20),
            "volatility": performance.volatility if performance else None,
            "high_20d": max(closes[-20:]) if len(closes) >= 20 else max(closes),
            "low_20d": min(closes[-20:]) if len(closes) >= 20 else min(closes),
            "trend": indicators.signal,
            "current_price": current_price,
        }


class FundAnalysisModule:
    SETTINGS_FILE = "fund_settings.json"
    SUPPORTED_COMMANDS = {
        "搜索股票",
        "基金",
        "搜索基金",
        "设置基金",
        "基金分析",
        "基金历史",
        "基金对比",
        "量化分析",
        "智能分析",
        "股票智能分析",
        "基金帮助",
    }

    def __init__(self, context: Context):
        self.context = context
        self.config = ensure_flat_config(getattr(context, "config", {}))
        self.market = FundMarketService()
        self.stock_analyzer = StockAnalyzer()
        self._ai_analyzer: AIFundAnalyzer | None = None
        self._debate_engine: DebateEngine | None = None
        self._data_dir = Path(StarTools.get_data_dir("astrbot_stock"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.user_fund_settings = self._load_user_settings()

    def refresh_runtime(self, context: Context) -> None:
        self.context = context
        self.config = ensure_flat_config(getattr(context, "config", {}))
        if self._ai_analyzer is not None:
            self._ai_analyzer.context = context
        if self._debate_engine is not None:
            self._debate_engine.context = context

    @property
    def ai_analyzer(self) -> AIFundAnalyzer:
        if self._ai_analyzer is None:
            self._ai_analyzer = AIFundAnalyzer(self.context)
        return self._ai_analyzer

    @property
    def debate_engine(self) -> DebateEngine:
        if self._debate_engine is None:
            self._debate_engine = DebateEngine(self.context)
        return self._debate_engine

    def can_handle(self, command: str) -> bool:
        return command in self.SUPPORTED_COMMANDS

    def _settings_path(self) -> Path:
        return self._data_dir / self.SETTINGS_FILE

    def _load_user_settings(self) -> dict[str, str]:
        path = self._settings_path()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception as exc:
            logger.warning("加载基金设置失败: %s", exc)
        return {}

    def _save_user_settings(self) -> None:
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(self.user_fund_settings, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("保存基金设置失败: %s", exc)

    @staticmethod
    def _normalize_fund_code(code: str | None) -> str | None:
        if code is None:
            return None
        digits = "".join(ch for ch in str(code).strip() if ch.isdigit())
        if not digits:
            return None
        return digits[:6].zfill(6)

    @staticmethod
    def _safe_sender_id(event: AstrMessageEvent) -> str:
        try:
            if hasattr(event, "get_sender_id"):
                sender_id = event.get_sender_id()
                if sender_id:
                    return str(sender_id)
        except Exception:
            pass
        return "default"

    def _get_user_fund(self, event: AstrMessageEvent) -> str:
        return self.user_fund_settings.get(
            self._safe_sender_id(event),
            self.market.DEFAULT_FUND_CODE,
        )

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(str(value))
        except Exception:
            return default

    @staticmethod
    def _fmt_number(value: float | None, digits: int = 4, suffix: str = "") -> str:
        if value is None:
            return "--"
        return f"{value:.{digits}f}{suffix}"

    @staticmethod
    def _fmt_percent(value: float | None, digits: int = 2) -> str:
        if value is None:
            return "--"
        return f"{value:+.{digits}f}%"

    @staticmethod
    def _fmt_amount(value: float | None) -> str:
        if value is None:
            return "--"
        abs_value = abs(value)
        if abs_value >= 1e8:
            return f"{value / 1e8:.2f}亿"
        if abs_value >= 1e4:
            return f"{value / 1e4:.2f}万"
        return f"{value:.2f}"

    async def handle_command(self, event: AstrMessageEvent, command: str, args: list[str]):
        if command == "搜索股票":
            async for result in self._handle_search_stock(event, args):
                yield result
            return
        if command == "基金":
            async for result in self._handle_fund_quote(event, args):
                yield result
            return
        if command == "搜索基金":
            async for result in self._handle_search_fund(event, args):
                yield result
            return
        if command == "设置基金":
            async for result in self._handle_set_default_fund(event, args):
                yield result
            return
        if command == "基金分析":
            async for result in self._handle_fund_analysis(event, args):
                yield result
            return
        if command == "基金历史":
            async for result in self._handle_fund_history(event, args):
                yield result
            return
        if command == "基金对比":
            async for result in self._handle_fund_compare(event, args):
                yield result
            return
        if command == "量化分析":
            async for result in self._handle_quant_analysis(event, args):
                yield result
            return
        if command == "智能分析":
            async for result in self._handle_ai_analysis(event, args):
                yield result
            return
        if command == "股票智能分析":
            async for result in self._handle_multi_agent_analysis(event, args):
                yield result
            return
        async for result in self._handle_help(event):
            yield result

    def _resolve_fund_code(self, event: AstrMessageEvent, args: list[str]) -> str:
        code = self._normalize_fund_code(args[0]) if args else None
        return code or self._get_user_fund(event)

    def _format_fund_info(self, info: FundInfo) -> str:
        if info.latest_price == 0:
            return (
                f"📊 {info.name}({info.code})\n"
                "暂无可用的实时行情数据，可能是休市、停牌或数据源暂时未更新。"
            )
        return (
            f"📊 {info.name}({info.code}) {info.trend_emoji}\n"
            f"最新价: {self._fmt_number(info.latest_price)}\n"
            f"涨跌额: {info.change_amount:+.4f}\n"
            f"涨跌幅: {self._fmt_percent(info.change_rate)}\n"
            f"今开/昨收: {self._fmt_number(info.open_price)} / {self._fmt_number(info.prev_close)}\n"
            f"最高/最低: {self._fmt_number(info.high_price)} / {self._fmt_number(info.low_price)}\n"
            f"成交量: {self._fmt_amount(info.volume)}\n"
            f"成交额: {self._fmt_amount(info.amount)}\n"
            f"换手率: {self._fmt_percent(info.turnover_rate)}\n"
            f"查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def _format_fund_analysis(self, info: FundInfo, indicators: dict[str, Any]) -> str:
        if not indicators:
            return f"📈 {info.name}({info.code})\n历史数据不足，暂时无法生成技术分析。"
        ma_parts = []
        current = indicators.get("current_price")
        for key in ("ma5", "ma10", "ma20"):
            value = indicators.get(key)
            if value is None:
                continue
            marker = "上方" if current is not None and current > value else "下方"
            ma_parts.append(f"{key.upper()} {value:.4f}({marker})")
        return (
            f"📈 {info.name}({info.code}) 技术分析\n"
            f"趋势信号: {indicators.get('trend', '--')}\n"
            f"均线: {' | '.join(ma_parts) if ma_parts else '--'}\n"
            f"5日收益: {self._fmt_percent(indicators.get('return_5d'))}\n"
            f"10日收益: {self._fmt_percent(indicators.get('return_10d'))}\n"
            f"20日收益: {self._fmt_percent(indicators.get('return_20d'))}\n"
            f"波动率: {self._fmt_number(indicators.get('volatility'), 2, '%')}\n"
            f"20日高点: {self._fmt_number(indicators.get('high_20d'))}\n"
            f"20日低点: {self._fmt_number(indicators.get('low_20d'))}\n"
            "提示: 技术分析仅供参考，不构成投资建议。"
        )

    async def _handle_search_stock(self, event: AstrMessageEvent, args: list[str]):
        keyword = " ".join(args).strip()
        if not keyword:
            yield event.plain_result("用法：/股票 搜索股票 关键词，例如 /股票 搜索股票 茅台")
            return
        try:
            results = await self.stock_analyzer.search_stock(keyword, max_results=10)
        except ImportError:
            yield event.plain_result("搜索股票依赖 `akshare` 与 `pandas`，请先安装后再使用。")
            return
        except Exception as exc:
            logger.error("搜索股票失败: %s", exc)
            yield event.plain_result(f"搜索股票失败：{exc}")
            return
        if not results:
            yield event.plain_result(f"未找到包含「{keyword}」的股票。")
            return
        lines = [f"🔎 股票搜索结果：{keyword}"]
        for item in results[:10]:
            lines.append(
                f"- {item['name']}({item['code']}) 现价 {item['price']:.2f} {item['change_rate']:+.2f}%"
            )
        lines.append("可继续使用 /股票 查询 代码；基金/ETF/LOF 的博弈分析请使用 /基金 博弈 代码。")
        yield event.plain_result("\n".join(lines))

    async def _handle_fund_quote(self, event: AstrMessageEvent, args: list[str]):
        fund_code = self._resolve_fund_code(event, args)
        info = await self.market.get_fund_realtime(fund_code)
        if not info:
            yield event.plain_result(
                f"暂时无法获取 {fund_code} 的行情数据。\n"
                "你可以先试试：/基金 搜索 关键词"
            )
            return
        record_passive_habit(event, "fund", "fund_code", info.code, source_text=event.message_str.strip())
        yield event.plain_result(self._format_fund_info(info))

    async def _handle_search_fund(self, event: AstrMessageEvent, args: list[str]):
        keyword = " ".join(args).strip()
        if not keyword:
            yield event.plain_result("用法：/基金 搜索 关键词，例如 /基金 搜索 白银")
            return
        results = await self.market.search_fund(keyword)
        if not results:
            yield event.plain_result(f"未找到包含「{keyword}」的基金。")
            return
        lines = [f"🔎 基金搜索结果：{keyword}"]
        for item in results[:10]:
            price = item.get("latest_price")
            change = item.get("change_rate")
            price_text = self._fmt_number(float(price), 4) if price not in (None, "") else "--"
            change_text = self._fmt_percent(float(change), 2) if change not in (None, "") else "--"
            lines.append(f"- {item.get('name', '--')}({item.get('code', '--')}) 现价 {price_text} {change_text}")
        lines.append("可继续使用 /基金 代码 或 /基金 设置 代码。")
        yield event.plain_result("\n".join(lines))

    async def _handle_set_default_fund(self, event: AstrMessageEvent, args: list[str]):
        if not args:
            yield event.plain_result(
                f"当前默认基金：{self._get_user_fund(event)}\n"
                "用法：/基金 设置 161226"
            )
            return
        fund_code = self._normalize_fund_code(args[0])
        if not fund_code:
            yield event.plain_result("请输入 6 位基金代码，例如 /基金 设置 161226")
            return
        info = await self.market.get_fund_realtime(fund_code)
        if not info:
            yield event.plain_result(f"无法校验基金代码 {fund_code}，请先用 /基金 搜索 关键词 查询。")
            return
        self.user_fund_settings[self._safe_sender_id(event)] = fund_code
        self._save_user_settings()
        record_passive_habit(event, "fund", "default_fund_code", info.code, source_text=event.message_str.strip())
        yield event.plain_result(f"已设置默认基金为 {info.name}({info.code})。")

    async def _handle_fund_analysis(self, event: AstrMessageEvent, args: list[str]):
        fund_code = self._resolve_fund_code(event, args)
        info, history = await asyncio.gather(
            self.market.get_fund_realtime(fund_code),
            self.market.get_fund_history(fund_code, days=30),
        )
        if not info:
            yield event.plain_result(f"无法获取 {fund_code} 的基金信息。")
            return
        indicators = self.market.calculate_technical_indicators(history)
        record_passive_habit(event, "fund", "fund_code", info.code, source_text=event.message_str.strip())
        yield event.plain_result(self._format_fund_analysis(info, indicators))

    async def _handle_fund_history(self, event: AstrMessageEvent, args: list[str]):
        fund_code = self._resolve_fund_code(event, args)
        days = 10
        if len(args) >= 2:
            try:
                days = max(1, min(int(args[1]), 60))
            except ValueError:
                days = 10
        info, history = await asyncio.gather(
            self.market.get_fund_realtime(fund_code),
            self.market.get_fund_history(fund_code, days=days),
        )
        if not history:
            yield event.plain_result(f"未获取到 {fund_code} 最近 {days} 天的历史行情。")
            return
        fund_name = info.name if info else fund_code
        closes = [float(item.get("close", 0) or 0) for item in history if item.get("close") not in (None, "")]
        total_return = 0.0
        if len(closes) >= 2 and closes[0] != 0:
            total_return = (closes[-1] - closes[0]) / closes[0] * 100
        lines = [
            f"🧾 {fund_name}({fund_code}) 最近 {days} 日历史行情",
            f"区间收益: {total_return:+.2f}%",
        ]
        if closes:
            lines.append(f"区间最高/最低: {max(closes):.4f} / {min(closes):.4f}")
        lines.append("最近记录：")
        for item in reversed(history[-8:]):
            close = float(item.get("close", 0) or 0)
            change = float(item.get("change_rate", 0) or 0)
            lines.append(
                f"- {item.get('date', '--')} 收盘 {close:.4f} 涨跌 {change:+.2f}%"
            )
        if len(history) > 8:
            lines.append(f"以上展示最近 8 条，共 {len(history)} 条。")
        record_passive_habit(event, "fund", "fund_code", fund_code, source_text=event.message_str.strip())
        yield event.plain_result("\n".join(lines))

    async def _handle_fund_compare(self, event: AstrMessageEvent, args: list[str]):
        if len(args) < 2:
            yield event.plain_result("用法：/基金 对比 代码1 代码2，例如 /基金 对比 161226 513100")
            return
        code1 = self._normalize_fund_code(args[0])
        code2 = self._normalize_fund_code(args[1])
        if not code1 or not code2:
            yield event.plain_result("请提供两个有效的 6 位基金代码。")
            return
        info1, info2, hist1, hist2 = await asyncio.gather(
            self.market.get_fund_realtime(code1),
            self.market.get_fund_realtime(code2),
            self.market.get_fund_history(code1, days=60),
            self.market.get_fund_history(code2, days=60),
        )
        if not info1 or not info2 or not hist1 or not hist2:
            yield event.plain_result("基金对比所需数据不完整，请稍后重试。")
            return
        quant = QuantAnalyzer()
        perf1 = quant.calculate_performance(hist1)
        perf2 = quant.calculate_performance(hist2)
        signal1, score1 = self.ai_analyzer.get_technical_signal(hist1)
        signal2, score2 = self.ai_analyzer.get_technical_signal(hist2)
        winner1 = 0
        winner2 = 0
        if perf1 and perf2:
            if perf1.total_return > perf2.total_return:
                winner1 += 1
            elif perf2.total_return > perf1.total_return:
                winner2 += 1
            if perf1.sharpe_ratio > perf2.sharpe_ratio:
                winner1 += 1
            elif perf2.sharpe_ratio > perf1.sharpe_ratio:
                winner2 += 1
            if perf1.max_drawdown < perf2.max_drawdown:
                winner1 += 1
            elif perf2.max_drawdown < perf1.max_drawdown:
                winner2 += 1
        if score1 > score2:
            winner1 += 1
        elif score2 > score1:
            winner2 += 1
        lines = [
            f"⚖️ 基金对比：{info1.name}({info1.code}) vs {info2.name}({info2.code})",
            "",
            f"{info1.name}: 现价 {info1.latest_price:.4f} 今日 {info1.change_rate:+.2f}% 信号 {signal1}({score1})",
            f"{info2.name}: 现价 {info2.latest_price:.4f} 今日 {info2.change_rate:+.2f}% 信号 {signal2}({score2})",
        ]
        if perf1 and perf2:
            lines.extend(
                [
                    "",
                    f"60日收益: {perf1.total_return:+.2f}% vs {perf2.total_return:+.2f}%",
                    f"夏普比率: {perf1.sharpe_ratio:+.2f} vs {perf2.sharpe_ratio:+.2f}",
                    f"最大回撤: {perf1.max_drawdown:.2f}% vs {perf2.max_drawdown:.2f}%",
                ]
            )
        if winner1 > winner2:
            lines.append(f"\n阶段性结论：{info1.name} 略占优势。")
        elif winner2 > winner1:
            lines.append(f"\n阶段性结论：{info2.name} 略占优势。")
        else:
            lines.append("\n阶段性结论：两者暂时接近，建议结合持仓目标再判断。")
        record_passive_habit(event, "fund", "fund_code", code1, source_text=event.message_str.strip())
        record_passive_habit(event, "fund", "fund_code", code2, source_text=event.message_str.strip())
        yield event.plain_result("\n".join(lines))

    async def _handle_quant_analysis(self, event: AstrMessageEvent, args: list[str]):
        fund_code = self._resolve_fund_code(event, args)
        info, history = await asyncio.gather(
            self.market.get_fund_realtime(fund_code),
            self.market.get_fund_history(fund_code, days=60),
        )
        if not info:
            yield event.plain_result(f"无法获取 {fund_code} 的基金信息。")
            return
        if len(history) < 20:
            yield event.plain_result(f"{info.name}({info.code}) 历史数据不足，至少需要 20 天数据才能做量化分析。")
            return
        report = self.ai_analyzer.get_quant_summary(history)
        header = (
            f"📈 {info.name}({info.code}) 量化分析\n"
            f"当前价格: {info.latest_price:.4f} ({info.change_rate:+.2f}%)\n"
            f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        record_passive_habit(event, "fund", "fund_code", info.code, source_text=event.message_str.strip())
        yield event.plain_result(f"{header}\n{report}\n\n提示：量化指标基于历史数据，不代表未来表现。")

    async def _handle_ai_analysis(self, event: AstrMessageEvent, args: list[str]):
        provider = self.context.get_using_provider()
        provider_id = str(getattr(self.config, "fund_ai_provider_id", "") or "").strip()
        provider_configs_raw = getattr(self.config, "fund_ai_providers", None)
        provider_configs = self.ai_analyzer.normalize_provider_configs(provider_configs_raw)
        timeout_seconds = self._safe_int(
            getattr(self.config, "fund_ai_timeout_seconds", 90),
            90,
        )
        if not provider and not provider_id and not provider_configs:
            yield event.plain_result("当前没有配置大模型提供商，无法执行智能分析。")
            return
        if provider_configs_raw and not provider_configs:
            logger.warning(
                "基金智能分析服务商配置解析为空: raw_type=%s",
                type(provider_configs_raw).__name__,
            )
            yield event.plain_result(
                "基金智能分析服务商配置未被正确识别，请重新保存一次“基金智能分析服务商”的 API 地址、API Key 和模型名。"
            )
            return
        fund_code = self._resolve_fund_code(event, args)
        info, history = await asyncio.gather(
            self.market.get_fund_realtime(fund_code),
            self.market.get_fund_history(fund_code, days=60),
        )
        if not info:
            yield event.plain_result(f"无法获取 {fund_code} 的基金信息。")
            return
        indicators = self.market.calculate_technical_indicators(history)
        fund_flow_text = ""
        try:
            fund_flow = await self.market._api.get_fund_flow(fund_code, days=10)
            fund_flow_text = self.market._api.format_fund_flow_text(fund_flow)
        except Exception as exc:
            logger.debug("获取资金流向失败: %s", exc)
            fund_flow_text = "暂无资金流向数据"
        try:
            report = await self.ai_analyzer.analyze(
                fund_info=info,
                history_data=history,
                technical_indicators=indicators,
                user_id=self._safe_sender_id(event),
                fund_flow_text=fund_flow_text,
                provider_id=provider_id,
                timeout_seconds=timeout_seconds,
                provider_configs=provider_configs,
            )
        except Exception as exc:
            logger.error("智能分析失败: %s", exc)
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                yield event.plain_result("智能分析失败：大模型请求超时，请检查基金智能分析专用 Provider 配置或稍后重试。")
            else:
                yield event.plain_result(f"智能分析失败：{exc}")
            return
        signal, score = self.ai_analyzer.get_technical_signal(history)
        header = (
            f"🤖 {info.name}({info.code}) 智能分析\n"
            f"当前价格: {info.latest_price:.4f} ({info.change_rate:+.2f}%)\n"
            f"技术信号: {signal} ({score})\n"
            f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        record_passive_habit(event, "fund", "fund_code", info.code, source_text=event.message_str.strip())
        yield event.plain_result(f"{header}\n{report}\n\n提示：内容由模型生成，仅供参考。")

    async def _handle_multi_agent_analysis(self, event: AstrMessageEvent, args: list[str]):
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("当前没有配置大模型提供商，无法执行多智能体分析。")
            return
        fund_code = self._resolve_fund_code(event, args)
        yield event.plain_result(
            f"正在为 {fund_code} 启动多智能体博弈分析（当前基于基金/ETF/LOF 数据），通常需要 20 到 60 秒，请稍等。"
        )
        info = await self.market.get_fund_realtime(fund_code)
        if not info:
            yield event.plain_result(f"无法获取 {fund_code} 的基础信息，暂时不能分析。")
            return
        history_task = self.market.get_fund_history(fund_code, days=60)
        flow_task = self.market._api.get_fund_flow(fund_code, days=10)
        history = await history_task
        if len(history) < 10:
            yield event.plain_result(f"{info.name}({info.code}) 历史数据不足，无法执行多智能体分析。")
            return
        fund_flow_data: list[dict] = []
        try:
            fund_flow_data = await flow_task or []
        except Exception as exc:
            logger.debug("多智能体分析获取资金流失败: %s", exc)
        try:
            news_summary = await self.ai_analyzer.get_news_summary(info.name, info.code)
            factors_text = self.ai_analyzer.factors.format_factors_text(info.name)
            global_situation_text = self.ai_analyzer.factors.format_global_situation_text(info.name)
            result = await self.debate_engine.run_debate(
                fund_info=info,
                history_data=history,
                fund_flow_data=fund_flow_data,
                news_summary=news_summary,
                factors_text=factors_text,
                global_situation_text=global_situation_text,
                quant_analyzer=self.ai_analyzer.quant,
                eastmoney_api=self.market._api,
            )
        except Exception as exc:
            logger.error("多智能体分析失败: %s", exc)
            yield event.plain_result(f"多智能体分析失败：{exc}")
            return
        record_passive_habit(event, "fund", "fund_code", info.code, source_text=event.message_str.strip())
        yield event.plain_result(self.debate_engine.format_debate_summary(result))

    async def _handle_help(self, event: AstrMessageEvent):
        help_text = (
            "📚 基金命令说明\n\n"
            "• /基金 [代码] - 查询基金/ETF/LOF 行情\n"
            "• /基金 搜索 关键词 - 搜索基金代码\n"
            "• /基金 设置 代码 - 设置默认基金\n"
            "• /基金 分析 [代码] - 技术分析\n"
            "• /基金 历史 [代码] [天数] - 历史行情\n"
            "• /基金 对比 代码1 代码2 - 两只基金对比\n"
            "• /基金 量化 [代码] - 量化指标总结\n"
            "• /基金 智能 [代码] - AI 深度分析\n"
            "• /基金 博弈 [代码] - 多智能体博弈分析\n\n"
            "说明：A 股相关分析请使用 /股票 量化分析、/股票 智能分析、/股票 股票智能分析。\n"
            "兼容用法：原来的 /股票 基金、/股票 基金分析 等仍可继续使用。"
        )
        yield event.plain_result(help_text)


_fund_analysis_module: FundAnalysisModule | None = None


def init_fund_analysis_module(context: Context) -> FundAnalysisModule:
    global _fund_analysis_module
    if _fund_analysis_module is None:
        _fund_analysis_module = FundAnalysisModule(context)
    else:
        _fund_analysis_module.refresh_runtime(context)
    return _fund_analysis_module


def can_handle_stock_extension(command: str) -> bool:
    return command in FundAnalysisModule.SUPPORTED_COMMANDS


async def handle_stock_extension_command(
    event: AstrMessageEvent,
    context: Context,
    command: str,
    args: list[str],
):
    module = init_fund_analysis_module(context)
    async for result in module.handle_command(event, command, args):
        yield result


_FUND_SUBCOMMAND_ALIASES = {
    "搜索": "搜索基金",
    "搜索基金": "搜索基金",
    "设置": "设置基金",
    "设置基金": "设置基金",
    "分析": "基金分析",
    "基金分析": "基金分析",
    "历史": "基金历史",
    "基金历史": "基金历史",
    "对比": "基金对比",
    "基金对比": "基金对比",
    "量化": "量化分析",
    "量化分析": "量化分析",
    "智能": "智能分析",
    "智能分析": "智能分析",
    "博弈": "股票智能分析",
    "多智能体": "股票智能分析",
    "股票智能分析": "股票智能分析",
    "帮助": "基金帮助",
    "help": "基金帮助",
    "?": "基金帮助",
}


async def handle_fund_command(event: AstrMessageEvent, context: Context):
    msg = event.get_message_str().strip()
    parts = msg.split()
    module = init_fund_analysis_module(context)

    if len(parts) < 2:
        async for result in module.handle_command(event, "基金帮助", []):
            yield result
        return

    first_arg = parts[1].strip()
    normalized_code = module._normalize_fund_code(first_arg)
    if normalized_code and first_arg.replace("/", "") == normalized_code:
        async for result in module.handle_command(event, "基金", [normalized_code, *parts[2:]]):
            yield result
        return

    mapped = _FUND_SUBCOMMAND_ALIASES.get(first_arg.lower(), _FUND_SUBCOMMAND_ALIASES.get(first_arg))
    if mapped is None:
        async for result in module.handle_command(event, "基金", parts[1:]):
            yield result
        return

    async for result in module.handle_command(event, mapped, parts[2:]):
        yield result
