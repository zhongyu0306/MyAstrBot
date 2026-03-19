"""
A股股票分析器
提供A股实时行情查询、搜索等功能
支持多数据源和网络重试机制
"""

import asyncio
import math
import re
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from .models import StockInfo

# 默认超时时间（秒）
DEFAULT_TIMEOUT = 60
# A股实时行情缓存有效期（秒）
STOCK_CACHE_TTL = 600  # 10分钟
# 网络请求最大重试次数
MAX_RETRIES = 3
# 重试间隔（秒）
RETRY_DELAY = 2


class StockAnalyzer:
    """A股股票分析器"""

    def __init__(self):
        self._ak = None
        self._pd = None
        self._initialized = False
        # 缓存 A 股实时行情数据
        self._stock_cache = None
        self._stock_cache_time = None
        # 当前使用的数据源
        self._current_source = "sina"  # 可选: eastmoney, sina

    async def _ensure_init(self):
        """确保akshare已初始化"""
        if not self._initialized:
            try:
                import akshare as ak
                import pandas as pd

                self._ak = ak
                self._pd = pd
                self._initialized = True
                logger.info("StockAnalyzer: AKShare 库初始化成功")
            except ImportError as e:
                logger.error(f"StockAnalyzer: AKShare 库导入失败: {e}")
                raise ImportError("请先安装 akshare 库: pip install akshare")

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        """安全地将值转换为float，处理NaN和None"""
        if value is None:
            return default
        try:
            if isinstance(value, float) and math.isnan(value):
                return default
            result = float(value)
            if math.isnan(result):
                return default
            return result
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _normalize_stock_code(value: Any) -> str:
        """将 sh600519 / SZ000001 / 600519 统一转换为 6 位代码。"""
        text = str(value or "").strip()
        if not text:
            return ""

        matched = re.search(r"(\d{6})", text)
        if matched:
            return matched.group(1)

        digits = re.sub(r"\D", "", text)
        return digits[:6] if digits else ""

    def _build_code_variants(self, stock_code: str) -> set[str]:
        """构建常见代码变体，兼容不同数据源的代码格式。"""
        raw_code = str(stock_code).strip()
        normalized = self._normalize_stock_code(raw_code)
        variants = {
            raw_code,
            raw_code.lower(),
            raw_code.upper(),
        }

        if normalized:
            market = self._market_from_code(normalized)
            variants.update(
                {
                    normalized,
                    f"{market}{normalized}",
                    f"{market.upper()}{normalized}",
                }
            )

        return {item for item in variants if item}

    def _extract_stock_code(self, row, fallback: str) -> str:
        """从数据行提取股票代码，并优先返回标准 6 位格式。"""
        for key in ("代码", "symbol"):
            if key in row.index:
                normalized = self._normalize_stock_code(row[key])
                if normalized:
                    return normalized
                raw_value = str(row[key]).strip()
                if raw_value:
                    return raw_value
        return self._normalize_stock_code(fallback) or str(fallback).strip()

    def _locate_stock_rows(self, df, stock_code: str):
        """在不同数据源字段格式下定位股票行。"""
        variants = self._build_code_variants(stock_code)
        normalized_target = self._normalize_stock_code(stock_code)

        for column in ("代码", "symbol"):
            if column not in df.columns:
                continue

            series = df[column].astype(str).str.strip()
            matched = df[series.isin(variants)]
            if not matched.empty:
                return matched

            if normalized_target:
                normalized_series = series.map(self._normalize_stock_code)
                matched = df[normalized_series == normalized_target]
                if not matched.empty:
                    return matched

        return df.iloc[0:0]

    def _history_records_from_dataframe(self, df, request_days: int) -> list[dict]:
        """将不同来源的历史行情 DataFrame 统一转换为分析所需字段。"""
        if df is None or getattr(df, "empty", True):
            return []

        # 新浪日线通常是英文列名；东方财富日线通常是中文列名。
        date_key = "日期" if "日期" in df.columns else ("date" if "date" in df.columns else None)

        working_df = df.copy()
        if date_key is None:
            working_df = working_df.reset_index()
            date_key = "date" if "date" in working_df.columns else working_df.columns[0]

        rows = list(working_df.iterrows())
        if not rows:
            return []

        records: list[dict] = []
        prev_close_value: float | None = None

        for _, row in rows:
            trade_date = row.get(date_key)
            if hasattr(trade_date, "strftime"):
                trade_date = trade_date.strftime("%Y-%m-%d")
            else:
                trade_date = str(trade_date)

            open_price = self._safe_float(row.get("开盘", row.get("open")))
            close_price = self._safe_float(row.get("收盘", row.get("close")))
            high_price = self._safe_float(row.get("最高", row.get("high")))
            low_price = self._safe_float(row.get("最低", row.get("low")))
            volume = self._safe_float(row.get("成交量", row.get("volume")))
            amount = self._safe_float(row.get("成交额", row.get("amount")))
            turnover_rate = self._safe_float(row.get("换手率", row.get("turnover")))

            change_amount = self._safe_float(row.get("涨跌额"))
            change_rate = self._safe_float(row.get("涨跌幅"))
            amplitude = self._safe_float(row.get("振幅"))

            if prev_close_value not in (None, 0):
                if not change_amount:
                    change_amount = close_price - prev_close_value
                if not change_rate:
                    change_rate = change_amount / prev_close_value * 100
                if not amplitude:
                    amplitude = (high_price - low_price) / prev_close_value * 100

            records.append(
                {
                    "date": trade_date,
                    "open": open_price,
                    "close": close_price,
                    "high": high_price,
                    "low": low_price,
                    "volume": volume,
                    "amount": amount,
                    "amplitude": amplitude,
                    "change_rate": change_rate,
                    "change_amount": change_amount,
                    "turnover_rate": turnover_rate,
                }
            )
            prev_close_value = close_price or prev_close_value

        return records[-request_days:]

    async def _fetch_stock_data_eastmoney(self):
        """从东方财富获取A股实时行情数据"""
        logger.info("尝试从东方财富获取A股实时行情数据...")
        df = await asyncio.wait_for(
            asyncio.to_thread(self._ak.stock_zh_a_spot_em),
            timeout=DEFAULT_TIMEOUT,
        )
        return df

    async def _fetch_stock_data_sina(self):
        """从新浪获取A股实时行情数据（备用数据源）"""
        logger.info("尝试从新浪获取A股实时行情数据...")
        df = await asyncio.wait_for(
            asyncio.to_thread(self._ak.stock_zh_a_spot),
            timeout=DEFAULT_TIMEOUT,
        )
        return df

    async def _get_stock_data_with_retry(self):
        """获取A股实时行情数据，带重试和备用数据源"""
        last_error = None

        # 首先尝试新浪数据源
        for attempt in range(MAX_RETRIES):
            try:
                df = await self._fetch_stock_data_sina()
                self._current_source = "sina"
                logger.info(f"新浪数据获取成功 (尝试 {attempt + 1}/{MAX_RETRIES})")
                return df
            except asyncio.TimeoutError:
                last_error = TimeoutError("新浪数据获取超时")
                logger.warning(f"新浪数据获取超时 (尝试 {attempt + 1}/{MAX_RETRIES})")
            except Exception as e:
                last_error = e
                logger.warning(
                    f"新浪数据获取失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}"
                )

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)

        # 新浪失败，尝试东方财富兜底
        logger.info("新浪数据源不可用，切换到东方财富数据源...")
        for attempt in range(MAX_RETRIES):
            try:
                df = await self._fetch_stock_data_eastmoney()
                self._current_source = "eastmoney"
                logger.info(f"东方财富数据获取成功 (尝试 {attempt + 1}/{MAX_RETRIES})")
                return df
            except asyncio.TimeoutError:
                last_error = TimeoutError("东方财富数据获取超时")
                logger.warning(
                    f"东方财富数据获取超时 (尝试 {attempt + 1}/{MAX_RETRIES})"
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    f"东方财富数据获取失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}"
                )

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)

        # 所有数据源都失败
        raise last_error or Exception("所有数据源均不可用")

    async def _get_stock_data(self):
        """获取A股实时行情数据（带缓存，10分钟有效期）"""
        await self._ensure_init()
        now = datetime.now()

        # 检查缓存是否有效
        if (
            self._stock_cache is not None
            and self._stock_cache_time is not None
            and (now - self._stock_cache_time).total_seconds() < STOCK_CACHE_TTL
        ):
            cache_age = int((now - self._stock_cache_time).total_seconds())
            logger.debug(f"使用缓存的A股行情数据 (缓存时间: {cache_age}秒)")
            return self._stock_cache

        # 缓存过期或不存在，重新获取
        try:
            df = await self._get_stock_data_with_retry()
            # 更新缓存
            self._stock_cache = df
            self._stock_cache_time = now
            logger.info(
                f"A股实时行情数据获取成功，共 {len(df)} 只股票 (数据源: {self._current_source})"
            )
            return df
        except Exception as e:
            logger.error(f"获取A股行情数据失败: {e}")
            # 如果有旧缓存，返回旧缓存
            if self._stock_cache is not None:
                logger.warning("使用过期的缓存数据")
                return self._stock_cache
            raise

    def _parse_stock_row_eastmoney(self, row, stock_code: str) -> StockInfo:
        """解析东方财富数据格式"""
        return StockInfo(
            code=self._extract_stock_code(row, stock_code),
            name=str(row["名称"]) if "名称" in row.index else "",
            latest_price=self._safe_float(
                row["最新价"] if "最新价" in row.index else 0
            ),
            change_amount=self._safe_float(
                row["涨跌额"] if "涨跌额" in row.index else 0
            ),
            change_rate=self._safe_float(row["涨跌幅"] if "涨跌幅" in row.index else 0),
            open_price=self._safe_float(row["今开"] if "今开" in row.index else 0),
            high_price=self._safe_float(row["最高"] if "最高" in row.index else 0),
            low_price=self._safe_float(row["最低"] if "最低" in row.index else 0),
            prev_close=self._safe_float(row["昨收"] if "昨收" in row.index else 0),
            volume=self._safe_float(row["成交量"] if "成交量" in row.index else 0),
            amount=self._safe_float(row["成交额"] if "成交额" in row.index else 0),
            amplitude=self._safe_float(row["振幅"] if "振幅" in row.index else 0),
            turnover_rate=self._safe_float(
                row["换手率"] if "换手率" in row.index else 0
            ),
            pe_ratio=self._safe_float(
                row["市盈率-动态"] if "市盈率-动态" in row.index else 0
            ),
            pb_ratio=self._safe_float(row["市净率"] if "市净率" in row.index else 0),
            total_market_cap=self._safe_float(
                row["总市值"] if "总市值" in row.index else 0
            ),
            circulating_market_cap=self._safe_float(
                row["流通市值"] if "流通市值" in row.index else 0
            ),
        )

    def _parse_stock_row_sina(self, row, stock_code: str) -> StockInfo:
        """解析新浪数据格式"""
        # 新浪数据字段名称略有不同
        return StockInfo(
            code=self._extract_stock_code(row, stock_code),
            name=str(row.get("名称", row.get("name", ""))),
            latest_price=self._safe_float(row.get("最新价", row.get("trade", 0))),
            change_amount=self._safe_float(
                row.get("涨跌额", row.get("pricechange", 0))
            ),
            change_rate=self._safe_float(
                row.get("涨跌幅", row.get("changepercent", 0))
            ),
            open_price=self._safe_float(row.get("今开", row.get("open", 0))),
            high_price=self._safe_float(row.get("最高", row.get("high", 0))),
            low_price=self._safe_float(row.get("最低", row.get("low", 0))),
            prev_close=self._safe_float(row.get("昨收", row.get("settlement", 0))),
            volume=self._safe_float(row.get("成交量", row.get("volume", 0))),
            amount=self._safe_float(row.get("成交额", row.get("amount", 0))),
            amplitude=self._safe_float(row.get("振幅", 0)),
            turnover_rate=self._safe_float(
                row.get("换手率", row.get("turnoverratio", 0))
            ),
            pe_ratio=self._safe_float(row.get("市盈率-动态", row.get("per", 0))),
            pb_ratio=self._safe_float(row.get("市净率", row.get("pb", 0))),
            total_market_cap=self._safe_float(row.get("总市值", row.get("mktcap", 0))),
            circulating_market_cap=self._safe_float(
                row.get("流通市值", row.get("nmc", 0))
            ),
        )

    async def get_stock_realtime(self, stock_code: str) -> StockInfo | None:
        """
        获取A股实时行情

        Args:
            stock_code: 股票代码（如 000001、600519）

        Returns:
            StockInfo 对象或 None
        """
        # 确保股票代码是字符串格式
        stock_code = str(stock_code).strip()
        logger.debug(f"查询股票代码: '{stock_code}'")

        try:
            # 获取A股实时行情（使用缓存）
            df = await self._get_stock_data()

            # 查找指定股票
            stock_data = self._locate_stock_rows(df, stock_code)

            if stock_data.empty:
                logger.warning(f"未找到股票代码: {stock_code}")
                return None

            row = stock_data.iloc[0]

            # 根据数据源使用不同的解析方法
            if self._current_source == "sina":
                return self._parse_stock_row_sina(row, stock_code)
            else:
                return self._parse_stock_row_eastmoney(row, stock_code)

        except Exception as e:
            logger.error(f"获取A股实时行情失败: {e}")
            return None

    @staticmethod
    def _market_from_code(stock_code: str) -> str:
        """根据股票代码推断市场标识。"""
        code = str(stock_code).strip()
        if not code:
            return "sz"
        if code[0] in {"6", "9"}:
            return "sh"
        if code[0] in {"4", "8"}:
            return "bj"
        return "sz"

    async def get_stock_history(
        self,
        stock_code: str,
        days: int = 60,
        adjust: str = "qfq",
    ) -> list[dict]:
        """
        获取 A 股历史行情。

        Args:
            stock_code: 股票代码
            days: 返回最近多少个交易日
            adjust: 复权方式，兼容 {"", "qfq", "hfq"}

        Returns:
            历史行情列表，字段格式与量化分析器兼容
        """
        await self._ensure_init()

        code = str(stock_code).strip()
        if not code:
            return []

        request_days = max(int(days), 1)
        end_date = datetime.now().strftime("%Y%m%d")
        # 预留周末和节假日，避免只拿到不足量的交易日
        start_date = (datetime.now() - timedelta(days=request_days * 3 + 30)).strftime(
            "%Y%m%d"
        )
        last_error = None
        sina_symbol = f"{self._market_from_code(code)}{self._normalize_stock_code(code) or code}"

        for attempt in range(MAX_RETRIES):
            try:
                logger.info(
                    "尝试从新浪获取A股历史行情数据: %s (尝试 %s/%s)",
                    sina_symbol,
                    attempt + 1,
                    MAX_RETRIES,
                )
                df = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._ak.stock_zh_a_daily,
                        symbol=sina_symbol,
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                    ),
                    timeout=DEFAULT_TIMEOUT,
                )
                records = self._history_records_from_dataframe(df, request_days)
                if records:
                    logger.info("新浪历史行情获取成功: %s，共 %s 条", sina_symbol, len(records))
                    return records
            except Exception as e:
                last_error = e
                logger.warning(
                    "新浪历史行情获取失败 (尝试 %s/%s): %s - %s",
                    attempt + 1,
                    MAX_RETRIES,
                    sina_symbol,
                    e,
                )

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)

        logger.info("新浪历史行情不可用，切换到东方财富历史行情源: %s", code)
        for attempt in range(MAX_RETRIES):
            try:
                df = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._ak.stock_zh_a_hist,
                        symbol=code,
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                    ),
                    timeout=DEFAULT_TIMEOUT,
                )
                records = self._history_records_from_dataframe(df, request_days)
                if records:
                    logger.info("东方财富历史行情获取成功: %s，共 %s 条", code, len(records))
                    return records
            except Exception as e:
                last_error = e
                logger.warning(
                    "东方财富历史行情获取失败 (尝试 %s/%s): %s - %s",
                    attempt + 1,
                    MAX_RETRIES,
                    code,
                    e,
                )

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)

        logger.error(f"获取A股历史行情失败: {code} - {last_error}")
        return []

    async def get_stock_fund_flow(
        self,
        stock_code: str,
        days: int = 10,
    ) -> list[dict]:
        """
        获取 A 股个股资金流向。

        Returns:
            字段对齐为 DebateEngine / DataCollector 预期格式
        """
        await self._ensure_init()

        code = str(stock_code).strip()
        if not code:
            return []

        market = self._market_from_code(code)
        try:
            df = await asyncio.wait_for(
                asyncio.to_thread(
                    self._ak.stock_individual_fund_flow,
                    stock=code,
                    market=market,
                ),
                timeout=DEFAULT_TIMEOUT,
            )
        except Exception as e:
            logger.warning(f"获取A股个股资金流失败: {code} - {e}")
            return []

        if df is None or getattr(df, "empty", True):
            return []

        flow_rows: list[dict] = []
        for _, row in df.tail(max(int(days), 1)).iterrows():
            trade_date = row.get("日期")
            if hasattr(trade_date, "strftime"):
                trade_date = trade_date.strftime("%Y-%m-%d")
            else:
                trade_date = str(trade_date)

            flow_rows.append(
                {
                    "date": trade_date,
                    "close_price": self._safe_float(row.get("收盘价")),
                    "change_rate": self._safe_float(row.get("涨跌幅")),
                    "main_net_inflow": self._safe_float(row.get("主力净流入-净额")),
                    "main_net_inflow_ratio": self._safe_float(
                        row.get("主力净流入-净占比")
                    ),
                    "super_large_inflow": self._safe_float(
                        row.get("超大单净流入-净额")
                    ),
                    "super_large_inflow_ratio": self._safe_float(
                        row.get("超大单净流入-净占比")
                    ),
                    "large_inflow": self._safe_float(row.get("大单净流入-净额")),
                    "large_inflow_ratio": self._safe_float(
                        row.get("大单净流入-净占比")
                    ),
                    "medium_inflow": self._safe_float(row.get("中单净流入-净额")),
                    "medium_inflow_ratio": self._safe_float(
                        row.get("中单净流入-净占比")
                    ),
                    "small_inflow": self._safe_float(row.get("小单净流入-净额")),
                    "small_inflow_ratio": self._safe_float(
                        row.get("小单净流入-净占比")
                    ),
                }
            )

        return flow_rows

    async def search_stock(self, keyword: str, max_results: int = 10) -> list[dict]:
        """
        搜索A股股票

        Args:
            keyword: 搜索关键词（股票名称或代码）
            max_results: 最大返回数量

        Returns:
            匹配的股票列表
        """
        try:
            df = await self._get_stock_data()

            keyword = str(keyword).strip()
            if not keyword:
                return []

            normalized_keyword = self._normalize_stock_code(keyword)

            code_mask = None
            for column in ("代码", "symbol"):
                if column not in df.columns:
                    continue

                series = df[column].astype(str).str.strip()
                current_mask = series.str.contains(keyword, case=False, na=False)
                if normalized_keyword:
                    normalized_series = series.map(self._normalize_stock_code)
                    current_mask = current_mask | normalized_series.str.contains(
                        normalized_keyword, na=False
                    )

                code_mask = (
                    current_mask if code_mask is None else (code_mask | current_mask)
                )

            if code_mask is None:
                code_mask = False

            # 搜索匹配的股票（代码或名称包含关键词）
            mask = code_mask | df["名称"].astype(str).str.contains(
                keyword, case=False, na=False
            )
            results = df[mask].head(max_results)

            return [
                {
                    "code": self._extract_stock_code(row, ""),
                    "name": str(row.get("名称", row.get("name", ""))),
                    "price": self._safe_float(row.get("最新价", row.get("trade", 0))),
                    "change_rate": self._safe_float(
                        row.get("涨跌幅", row.get("changepercent", 0))
                    ),
                }
                for _, row in results.iterrows()
            ]

        except Exception as e:
            logger.error(f"搜索股票失败: {e}")
            return []

    def get_cache_info(self) -> dict:
        """获取缓存信息"""
        if self._stock_cache is None:
            return {"cached": False}

        cache_age = 0
        if self._stock_cache_time:
            cache_age = int((datetime.now() - self._stock_cache_time).total_seconds())

        return {
            "cached": True,
            "cache_age_seconds": cache_age,
            "cache_ttl_seconds": STOCK_CACHE_TTL,
            "stock_count": len(self._stock_cache)
            if self._stock_cache is not None
            else 0,
            "data_source": self._current_source,
        }

    def clear_cache(self):
        """清除缓存"""
        self._stock_cache = None
        self._stock_cache_time = None
        logger.info("股票数据缓存已清除")
