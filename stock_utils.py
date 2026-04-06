import asyncio
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At
from astrbot.api.star import Context, StarTools

from .config_utils import ensure_flat_config
from .fund_analyzer import AIFundAnalyzer
from .fund_analysis_utils import can_handle_stock_extension, handle_stock_extension_command
from .fund_stock import DebateEngine, StockAnalyzer as AkStockAnalyzer
from .memory_state_store import delete_json_state, load_json_state, save_json_state, shared_conn
from .passive_memory_utils import record_passive_habit


def _data_dir():
    return StarTools.get_data_dir("astrbot_stock")


def _watchlist_file():
    _data_dir().mkdir(parents=True, exist_ok=True)
    return _data_dir() / "watchlist.json"


def _watchlist_state_namespace() -> str:
    return "stock_watchlist"


_WATCHLIST_MIGRATION_NAMESPACE = "stock_watchlist_migrated"
_WATCHLIST_STOCKS_TABLE = "stock_watchlist_stocks"
_WATCHLIST_REMINDERS_TABLE = "stock_watchlist_reminders"
_WATCHLIST_ALERTS_TABLE = "stock_watchlist_price_alerts"
_STOCK_MAP_MIGRATION_NAMESPACE = "stock_code_cache_migrated"
_STOCK_MAP_TABLE = "stock_code_cache"


def _stock_map_file():
    _data_dir().mkdir(parents=True, exist_ok=True)
    return _data_dir() / "stock_codes.json"


def _ensure_watchlist_tables() -> None:
    with shared_conn() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_WATCHLIST_STOCKS_TABLE} (
                session_id TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, code)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_WATCHLIST_REMINDERS_TABLE} (
                session_id TEXT NOT NULL,
                time TEXT NOT NULL,
                repeat_type TEXT NOT NULL,
                creator_id TEXT NOT NULL DEFAULT '',
                creator_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, time)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_WATCHLIST_ALERTS_TABLE} (
                session_id TEXT NOT NULL,
                code TEXT NOT NULL,
                condition TEXT NOT NULL,
                target_price REAL NOT NULL,
                creator_id TEXT NOT NULL DEFAULT '',
                creator_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, code, condition)
            )
            """
        )
        conn.commit()


def _is_watchlist_migrated() -> bool:
    return bool(
        load_json_state(
            _WATCHLIST_MIGRATION_NAMESPACE,
            default=False,
            normalizer=lambda value: bool(value),
        )
    )


def _mark_watchlist_migrated() -> None:
    save_json_state(_WATCHLIST_MIGRATION_NAMESPACE, True)


def _normalize_watchlist_payload(data: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_session_id, raw_record in data.items():
        session_id = str(raw_session_id or "").strip()
        if not session_id or not isinstance(raw_record, dict):
            continue
        stocks = [_normalize_code(str(code or "")) for code in (raw_record.get("stocks") or [])]
        reminders = []
        for item in raw_record.get("reminders") or []:
            if not isinstance(item, dict):
                continue
            time_str = str(item.get("time") or "").strip()
            if not time_str:
                continue
            repeat = "once" if str(item.get("repeat") or "").strip() == "once" else "daily"
            reminders.append(
                {
                    "time": time_str,
                    "repeat": repeat,
                    "creator_id": str(item.get("creator_id") or "").strip(),
                    "creator_name": str(item.get("creator_name") or "").strip(),
                }
            )
        alerts = []
        for item in raw_record.get("price_alerts") or []:
            if not isinstance(item, dict):
                continue
            code = _normalize_code(str(item.get("code") or ""))
            condition = str(item.get("condition") or "").strip()
            if code == "" or condition not in {"below", "above"}:
                continue
            try:
                target_price = float(item.get("target_price") or 0)
            except Exception:
                continue
            alerts.append(
                {
                    "code": code,
                    "target_price": target_price,
                    "condition": condition,
                    "creator_id": str(item.get("creator_id") or "").strip(),
                    "creator_name": str(item.get("creator_name") or "").strip(),
                }
            )
        normalized[session_id] = {
            "stocks": [code for code in stocks if code],
            "reminders": reminders,
            "price_alerts": alerts,
        }
    return normalized


def _ensure_watchlist_migrated() -> None:
    _ensure_watchlist_tables()
    if _is_watchlist_migrated():
        return
    legacy = load_json_state(
        _watchlist_state_namespace(),
        default={},
        normalizer=lambda value: value if isinstance(value, dict) else {},
        legacy_path=_watchlist_file(),
    )
    normalized = _normalize_watchlist_payload(legacy)
    if normalized:
        _save_watchlist_to_tables(normalized)
    _mark_watchlist_migrated()
    delete_json_state(_watchlist_state_namespace())


def _ensure_stock_map_table() -> None:
    with shared_conn() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_STOCK_MAP_TABLE} (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_STOCK_MAP_TABLE}_name "
            f"ON {_STOCK_MAP_TABLE}(name)"
        )
        conn.commit()


def _normalize_stock_map_item(item: dict[str, Any]) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    code = _normalize_code(str(item.get("code") or ""))
    name = str(item.get("name") or "").strip()
    if not code or not name:
        return None
    return code, name


def _is_stock_map_migrated() -> bool:
    return bool(
        load_json_state(
            _STOCK_MAP_MIGRATION_NAMESPACE,
            default=False,
            normalizer=lambda value: bool(value),
        )
    )


def _mark_stock_map_migrated() -> None:
    save_json_state(_STOCK_MAP_MIGRATION_NAMESPACE, True)


def _ensure_stock_map_migrated() -> None:
    _ensure_stock_map_table()
    if _is_stock_map_migrated():
        return
    path = _stock_map_file()
    if not path.exists():
        _mark_stock_map_migrated()
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "stocks" in data:
            raw_items = data["stocks"]
        elif isinstance(data, list):
            raw_items = data
        else:
            raw_items = []
        normalized = [item for item in (_normalize_stock_map_item(x) for x in raw_items) if item is not None]
        if normalized:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with shared_conn() as conn:
                conn.executemany(
                    f"INSERT OR REPLACE INTO {_STOCK_MAP_TABLE}(code, name, updated_at) VALUES (?, ?, ?)",
                    [(code, name, now) for code, name in normalized],
                )
                conn.commit()
    except Exception as e:
        logger.error("迁移股票代码表失败: %s", e)
    _mark_stock_map_migrated()


def _load_stock_map() -> list[dict]:
    _ensure_stock_map_migrated()
    try:
        with shared_conn() as conn:
            rows = conn.execute(
                f"SELECT code, name FROM {_STOCK_MAP_TABLE} ORDER BY updated_at DESC, code ASC"
            ).fetchall()
        return [{"code": str(row["code"]), "name": str(row["name"])} for row in rows]
    except Exception as e:
        logger.error("加载股票代码表失败: %s", e)
    return []


def _save_stock_map(stocks: list[dict]) -> None:
    try:
        _ensure_stock_map_table()
        normalized = [item for item in (_normalize_stock_map_item(x) for x in stocks) if item is not None]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with shared_conn() as conn:
            conn.execute(f"DELETE FROM {_STOCK_MAP_TABLE}")
            if normalized:
                conn.executemany(
                    f"INSERT INTO {_STOCK_MAP_TABLE}(code, name, updated_at) VALUES (?, ?, ?)",
                    [(code, name, now) for code, name in normalized],
                )
            conn.commit()
        _mark_stock_map_migrated()
    except Exception as e:
        logger.error("保存股票代码表失败: %s", e)


def _normalize_code(code: str) -> str:
    """将 600519 / 000001 转为 6 位代码（存储用）"""
    code = re.sub(r"\s+", "", code).strip()
    if not code:
        return ""
    code_upper = code.upper()
    if code_upper.startswith(("SH", "SZ")):
        num = re.sub(r"\D", "", code)[:6].zfill(6)
    else:
        num = re.sub(r"\D", "", code)[:6].zfill(6)
    if len(num) < 5:
        return ""
    if num[0] == "6":
        return num
    if num[0] in "03":
        return num
    return ""


def _load_watchlist() -> dict:
    try:
        _ensure_watchlist_migrated()
        with shared_conn() as conn:
            stocks_rows = conn.execute(
                f"SELECT session_id, code FROM {_WATCHLIST_STOCKS_TABLE} ORDER BY session_id, created_at, code"
            ).fetchall()
            reminder_rows = conn.execute(
                f"""
                SELECT session_id, time, repeat_type, creator_id, creator_name
                FROM {_WATCHLIST_REMINDERS_TABLE}
                ORDER BY session_id, time
                """
            ).fetchall()
            alert_rows = conn.execute(
                f"""
                SELECT session_id, code, target_price, condition, creator_id, creator_name
                FROM {_WATCHLIST_ALERTS_TABLE}
                ORDER BY session_id, code, condition
                """
            ).fetchall()
        data: dict[str, dict[str, Any]] = {}
        for row in stocks_rows:
            session_id = str(row["session_id"])
            rec = data.setdefault(session_id, {"stocks": [], "reminders": [], "price_alerts": []})
            rec["stocks"].append(str(row["code"]))
        for row in reminder_rows:
            session_id = str(row["session_id"])
            rec = data.setdefault(session_id, {"stocks": [], "reminders": [], "price_alerts": []})
            rec["reminders"].append(
                {
                    "time": str(row["time"]),
                    "repeat": str(row["repeat_type"]),
                    "creator_id": str(row["creator_id"] or ""),
                    "creator_name": str(row["creator_name"] or ""),
                }
            )
        for row in alert_rows:
            session_id = str(row["session_id"])
            rec = data.setdefault(session_id, {"stocks": [], "reminders": [], "price_alerts": []})
            rec["price_alerts"].append(
                {
                    "code": str(row["code"]),
                    "target_price": float(row["target_price"]),
                    "condition": str(row["condition"]),
                    "creator_id": str(row["creator_id"] or ""),
                    "creator_name": str(row["creator_name"] or ""),
                }
            )
        return data
    except Exception as e:
        logger.error("加载自选数据失败: %s", e)
        return {}


def _save_watchlist_to_tables(data: dict[str, dict[str, Any]]) -> None:
    normalized = _normalize_watchlist_payload(data)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stock_rows: list[tuple[str, str, str]] = []
    reminder_rows: list[tuple[str, str, str, str, str, str]] = []
    alert_rows: list[tuple[str, str, str, float, str, str, str]] = []
    for session_id, rec in normalized.items():
        for code in rec.get("stocks") or []:
            stock_rows.append((session_id, code, now))
        for item in rec.get("reminders") or []:
            reminder_rows.append(
                (
                    session_id,
                    str(item.get("time") or "").strip(),
                    str(item.get("repeat") or "daily").strip(),
                    str(item.get("creator_id") or "").strip(),
                    str(item.get("creator_name") or "").strip(),
                    now,
                )
            )
        for item in rec.get("price_alerts") or []:
            alert_rows.append(
                (
                    session_id,
                    str(item.get("code") or "").strip(),
                    str(item.get("condition") or "").strip(),
                    float(item.get("target_price") or 0),
                    str(item.get("creator_id") or "").strip(),
                    str(item.get("creator_name") or "").strip(),
                    now,
                )
            )
    with shared_conn() as conn:
        conn.execute(f"DELETE FROM {_WATCHLIST_STOCKS_TABLE}")
        conn.execute(f"DELETE FROM {_WATCHLIST_REMINDERS_TABLE}")
        conn.execute(f"DELETE FROM {_WATCHLIST_ALERTS_TABLE}")
        if stock_rows:
            conn.executemany(
                f"INSERT INTO {_WATCHLIST_STOCKS_TABLE}(session_id, code, created_at) VALUES (?, ?, ?)",
                stock_rows,
            )
        if reminder_rows:
            conn.executemany(
                f"""
                INSERT INTO {_WATCHLIST_REMINDERS_TABLE}(
                    session_id, time, repeat_type, creator_id, creator_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                reminder_rows,
            )
        if alert_rows:
            conn.executemany(
                f"""
                INSERT INTO {_WATCHLIST_ALERTS_TABLE}(
                    session_id, code, condition, target_price, creator_id, creator_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                alert_rows,
            )
        conn.commit()


def _save_watchlist(data: dict) -> None:
    try:
        _ensure_watchlist_tables()
        _save_watchlist_to_tables(data if isinstance(data, dict) else {})
        _mark_watchlist_migrated()
    except Exception as e:
        logger.error("保存自选数据失败: %s", e)


_ak_stock_analyzer: AkStockAnalyzer | None = None


def _get_ak_stock_analyzer() -> AkStockAnalyzer:
    global _ak_stock_analyzer
    if _ak_stock_analyzer is None:
        _ak_stock_analyzer = AkStockAnalyzer()
    return _ak_stock_analyzer


def _search_code_by_name_sync(keyword: str, max_results: int = 5) -> list[dict]:
    """通过本地缓存按名称模糊搜索股票代码（同步，给查询/添加等用）"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    local_stocks = _load_stock_map()
    if local_stocks:
        hits: list[dict] = []
        for s in local_stocks:
            name = str(s.get("name", "")).strip()
            code = str(s.get("code", "")).strip()
            if keyword in name and code:
                hits.append({"code": code, "name": name or code})
                if len(hits) >= max_results:
                    break
        if hits:
            return hits

    # 不再从 AkShare 拉取全市场列表，若本地无缓存则直接返回空结果
    return []


async def _search_code_by_name(keyword: str, max_results: int = 5) -> list[dict]:
    kw = (keyword or "").strip()
    if not kw:
        return []
    try:
        analyzer = _get_ak_stock_analyzer()
        results = await analyzer.search_stock(kw, max_results=max_results)
        matches = [
            {"code": str(item.get("code", "")).strip(), "name": str(item.get("name", "")).strip()}
            for item in results
            if item.get("code")
        ]
        if matches:
            return matches
    except ImportError:
        logger.warning("股票名称搜索依赖 akshare/pandas，当前未安装，退回本地缓存搜索。")
    except Exception as e:
        logger.warning("通过 AKShare 搜索股票名称失败，退回本地缓存: %s", e)
    return await asyncio.to_thread(_search_code_by_name_sync, keyword, max_results)


async def _fetch_quotes(codes: list[str]) -> list[dict]:
    """统一入口：优先通过 AKShare 获取股票实时行情。"""
    if not codes:
        return []
    codes = list(dict.fromkeys(codes))
    try:
        analyzer = _get_ak_stock_analyzer()
        infos = await asyncio.gather(*(analyzer.get_stock_realtime(code) for code in codes))
        quotes: list[dict] = []
        for info in infos:
            if info is None:
                continue
            quotes.append(
                {
                    "code": info.code,
                    "name": info.name or info.code,
                    "current": info.latest_price,
                    "open": info.open_price,
                    "prev_close": info.prev_close,
                    "change": info.change_amount,
                    "change_pct": info.change_rate,
                    "high": info.high_price,
                    "low": info.low_price,
                }
            )
        return quotes
    except ImportError:
        logger.warning("股票行情依赖 akshare/pandas，当前未安装。")
        return []
    except Exception as e:
        logger.warning("通过 AKShare 获取股票行情失败: %s", e)
        return []


def _format_quotes(quotes: list[dict], title: str = "行情") -> str:
    if not quotes:
        return "暂无行情数据。"
    lines = [f"📈 {title}\n"]
    for q in quotes:
        sign = "▲" if q["change"] >= 0 else "▼"
        pct = q["change_pct"]
        change = q.get("change", 0.0)
        open_price = q.get("open")
        prev = q.get("prev_close")
        high = q.get("high")
        low = q.get("low")
        lines.append(f"{q['name']}({q['code']})")
        lines.append(f"  当前：{q['current']:.2f} 元（{sign}{abs(pct):.2f}% {change:+.2f}）")
        if open_price is not None and prev is not None:
            lines.append(f"  今开 / 昨收：{open_price:.2f} / {prev:.2f}")
        elif prev is not None:
            lines.append(f"  昨收：{prev:.2f}")
        if high is not None and low is not None:
            lines.append(f"  最高 / 最低：{high:.2f} / {low:.2f}")
        lines.append("")
    return "\n".join(lines).rstrip()


class StockModule:
    """封装原股票插件逻辑（仅命令与定时，不含自然语言识别）。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = ensure_flat_config(config)
        self._scheduler = None
        self._last_reminder_tick: str | None = None
        self._ai_analyzer: AIFundAnalyzer | None = None
        self._debate_engine: DebateEngine | None = None
        self._start_scheduler()
        logger.info("股票模块初始化完成（股票行情通过 AKShare 获取）")

    def refresh_runtime(self, context: Context, config: AstrBotConfig) -> None:
        """刷新运行期上下文与配置，避免单例持有旧配置。"""
        self.context = context
        self.config = ensure_flat_config(config)
        if self._ai_analyzer is not None:
            self._ai_analyzer.context = context
        if self._debate_engine is not None:
            self._debate_engine.context = context

    @staticmethod
    def _safe_int(value: object, default: int) -> int:
        try:
            return int(str(value))
        except Exception:
            return default

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

    async def _resolve_stock_target(
        self,
        query: str | None,
    ) -> tuple[str | None, str | None]:
        """将股票代码或名称解析为唯一股票代码。"""
        raw = (query or "").strip()
        if not raw:
            return None, "请提供股票代码或名称，例如 /股票 智能分析 600519"

        code = _normalize_code(raw)
        if code:
            return code, None

        matches = await _search_code_by_name(raw, max_results=5)
        if not matches:
            return None, f"未找到名称包含「{raw}」的股票，请改用代码试试。"
        if len(matches) > 1:
            lines = ["找到多只匹配的股票，请改用代码分析："]
            for item in matches:
                lines.append(f"  • {item['name']}（{item['code']}）")
            return None, "\n".join(lines)
        return matches[0]["code"], None

    @staticmethod
    def _format_stock_flow_text(flow_data: list[dict]) -> str:
        """格式化股票资金流向，供 AI 深度分析使用。"""
        if not flow_data:
            return "暂无个股资金流向数据"

        def fmt_amount(value: float) -> str:
            abs_value = abs(value)
            if abs_value >= 1e8:
                return f"{value / 1e8:+.2f}亿"
            if abs_value >= 1e4:
                return f"{value / 1e4:+.2f}万"
            return f"{value:+.0f}"

        lines = ["【近10日个股资金流向】"]
        for item in flow_data[-10:]:
            lines.append(
                f"- {item.get('date', '--')}: 主力{fmt_amount(float(item.get('main_net_inflow', 0) or 0))}，"
                f"超大单{fmt_amount(float(item.get('super_large_inflow', 0) or 0))}，"
                f"大单{fmt_amount(float(item.get('large_inflow', 0) or 0))}，"
                f"中单{fmt_amount(float(item.get('medium_inflow', 0) or 0))}，"
                f"小单{fmt_amount(float(item.get('small_inflow', 0) or 0))}"
            )
        return "\n".join(lines)

    def _start_scheduler(self):
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            tz = getattr(self.config, "stock_reminder_timezone", None) or "Asia/Shanghai"
            self._scheduler = AsyncIOScheduler(timezone=tz)
            self._scheduler.add_job(
                self._run_reminders,
                CronTrigger(minute="*", timezone=tz),
                id="stock_reminder_minute",
            )
            self._scheduler.start()
            logger.info("股票定时提醒已启动，时区=%s", tz)
        except ImportError:
            logger.warning("未安装 apscheduler，定时提醒不可用")
        except Exception as e:
            logger.error("股票定时任务启动失败: %s", e)

    async def _run_reminders(self):
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        # 防止在同一分钟内被调度多次，导致重复推送
        if self._last_reminder_tick == minute_key:
            return
        self._last_reminder_tick = minute_key

        data = _load_watchlist()
        to_remove: list[tuple[str, str]] = []
        for session_id, rec in data.items():
            stocks = rec.get("stocks") or []
            reminders = rec.get("reminders") or []
            if not stocks or not reminders:
                continue
            mention_creators: dict[str, str | None] = {}
            for r in reminders:
                if r.get("time") != current_time:
                    continue
                if r.get("repeat") == "once":
                    to_remove.append((session_id, r.get("time")))
                creator_id = r.get("creator_id")
                creator_name = r.get("creator_name")
                logger.info(
                    "股票定时提醒触发：session_id=%s time=%s repeat=%s creator_id=%s creator_name=%s stocks=%s",
                    session_id,
                    current_time,
                    r.get("repeat"),
                    creator_id,
                    creator_name,
                    ",".join(stocks),
                )
                if creator_id:
                    if creator_id not in mention_creators:
                        mention_creators[creator_id] = creator_name
                quotes = await _fetch_quotes(stocks)
                if not quotes:
                    continue
                text = _format_quotes(quotes, "自选股定时提醒")
                try:
                    chain = MessageChain()
                    for cid, cname in mention_creators.items():
                        try:
                            if cname:
                                chain.chain.append(At(qq=cid, name=cname))
                            else:
                                chain.chain.append(At(qq=cid))
                        except Exception:
                            continue
                    chain.message(text)
                    await self.context.send_message(session_id, chain)
                except Exception as e:
                    logger.error("定时推送到 %s 失败: %s", session_id[:50], e)
        for session_id, time_str in to_remove:
            rec = data.get(session_id)
            if not rec:
                continue
            rec["reminders"] = [x for x in (rec.get("reminders") or []) if x.get("time") != time_str]
            data[session_id] = rec
        if to_remove:
            _save_watchlist(data)

        price_alert_removed = False
        for session_id, rec in list(data.items()):
            alerts = rec.get("price_alerts") or []
            if not alerts:
                continue
            codes = list(dict.fromkeys([a.get("code") for a in alerts if a.get("code")]))
            if not codes:
                continue
            quotes = await _fetch_quotes(codes)
            if not quotes:
                continue
            quote_map = {q["code"]: q for q in quotes}
            to_remove_alerts = []
            for a in alerts:
                code = a.get("code")
                target = a.get("target_price")
                cond = a.get("condition", "below")
                creator_id = a.get("creator_id")
                creator_name = a.get("creator_name")
                if code is None or target is None:
                    continue
                q = quote_map.get(code)
                if not q:
                    continue
                curr = q["current"]
                triggered = False
                if cond == "below" and curr <= target:
                    triggered = True
                elif cond == "above" and curr >= target:
                    triggered = True
                if not triggered:
                    continue
                to_remove_alerts.append(a)
                base_text = (
                    f"🔔 价格提醒\n{q['name']}({code}) 当前 {curr:.2f} 元，"
                    f"{'已跌破' if cond == 'below' else '已涨破'}您设置的 {target} 元提醒线。"
                )

                # 默认使用固定文案，作为 LLM 不可用时的兜底
                final_text = base_text

                # 尝试交给当前会话的 LLM，用该会话激活人格的口吻重新表述价格提醒
                try:
                    provider_id = await self.context.get_current_chat_provider_id(
                        umo=session_id
                    )
                    if provider_id:
                        prompt = (
                            "你是当前会话里的聊天角色，请用你平时的人格和说话风格，"
                            "把下面这条股票价格提醒说给对方听。\n"
                            "要求：\n"
                            "1. 只回复一到两句话，简短自然，像朋友在群里/私聊里提醒。\n"
                            "2. 必须保留核心信息：股票名称、代码、当前价格、触发方向（涨破/跌破）和目标价格。\n"
                            "3. 不要改变数字含义，不要添加新的投资建议或风险提示。\n"
                            "4. 不要解释自己是系统或机器人，只当普通人说话。\n"
                            f"原始提醒文案：{base_text}"
                        )
                        llm_resp = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=prompt,
                        )
                        out = (getattr(llm_resp, "completion_text", None) or "").strip()
                        if out:
                            final_text = out
                except Exception as e:
                    logger.exception("价格提醒生成自然语言文本失败: %s", e)

                try:
                    chain = MessageChain()
                    if creator_id:
                        try:
                            if creator_name:
                                chain.chain.append(At(qq=creator_id, name=creator_name))
                            else:
                                chain.chain.append(At(qq=creator_id))
                        except Exception:
                            pass
                    chain.message(final_text)
                    await self.context.send_message(session_id, chain)
                except Exception as e:
                    logger.error("价格提醒推送到 %s 失败: %s", session_id[:50], e)
            for a in to_remove_alerts:
                rec["price_alerts"].remove(a)
                price_alert_removed = True
        if price_alert_removed:
            _save_watchlist(data)

    def _session_data(self, session_id: str) -> dict:
        data = _load_watchlist()
        if session_id not in data:
            data[session_id] = {"stocks": [], "reminders": [], "price_alerts": []}
        rec = data[session_id]
        if "price_alerts" not in rec:
            rec["price_alerts"] = []
        return rec

    async def handle_command(self, event: AstrMessageEvent):
        msg = event.get_message_str().strip()
        parts = msg.split()
        if len(parts) < 2:
            async for r in self._send_help_v2(event):
                yield r
            return
        cmd, args = parts[1].strip().lower(), parts[2:] if len(parts) > 2 else []
        if cmd == "量化分析":
            async for r in self._handle_stock_quant_analysis(event, args):
                yield r
            return
        if cmd == "智能分析":
            async for r in self._handle_stock_ai_analysis(event, args):
                yield r
            return
        if cmd == "股票智能分析":
            async for r in self._handle_stock_multi_agent_analysis(event, args):
                yield r
            return
        if can_handle_stock_extension(cmd):
            async for r in handle_stock_extension_command(event, self.context, cmd, args):
                yield r
            return
        session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "")
        if not session_id:
            yield event.plain_result("❌ 无法获取当前会话。")
            return

        creator_id: str | None = None
        creator_name: str | None = None
        try:
            if hasattr(event, "get_sender_id"):
                creator_id = event.get_sender_id()
            if hasattr(event, "get_sender_name"):
                creator_name = event.get_sender_name()
        except Exception:
            creator_id = creator_id or None
            creator_name = creator_name or None

        if cmd in ("添加", "add"):
            if not args:
                yield event.plain_result("用法：/股票 添加 代码，如 /股票 添加 600519")
                return
            code = _normalize_code(args[0])
            if not code:
                yield event.plain_result("❌ 无效股票代码。")
                return
            rec = self._session_data(session_id)
            if code in rec["stocks"]:
                yield event.plain_result(f"✅ {code} 已在自选中。")
                return
            rec["stocks"].append(code)
            data = _load_watchlist()
            data[session_id] = rec
            _save_watchlist(data)
            record_passive_habit(event, "stock", "stock_code", code, source_text=msg)
            yield event.plain_result(f"✅ 已添加自选：{code}")

        elif cmd in ("删除", "移除", "remove", "del"):
            if not args:
                yield event.plain_result("用法：/股票 删除 代码")
                return
            code = _normalize_code(args[0])
            rec = self._session_data(session_id)
            if code in rec["stocks"]:
                rec["stocks"].remove(code)
                data = _load_watchlist()
                data[session_id] = rec
                _save_watchlist(data)
                yield event.plain_result(f"✅ 已移除：{code}")
            else:
                yield event.plain_result(f"❌ 自选无 {code}")

        elif cmd in ("列表", "list", "ls"):
            rec = self._session_data(session_id)
            if not rec["stocks"]:
                yield event.plain_result("当前暂无自选，使用 /股票 添加 代码 添加。")
                return
            quotes = await _fetch_quotes(rec["stocks"])
            yield event.plain_result(_format_quotes(quotes, "自选股列表"))

        elif cmd in ("查询", "查", "query", "q"):
            codes: list[str] = []
            name_keywords: list[str] = []
            if args:
                for a in args:
                    code = _normalize_code(a)
                    if code:
                        codes.append(code)
                    else:
                        name_keywords.append(a.strip())
                if name_keywords:
                    if len(name_keywords) > 1:
                        yield event.plain_result("一次仅支持按一个名称关键字查询，请改用：/股票 查询 股票名 或 代码。")
                        return
                    kw = name_keywords[0]
                    matches = await _search_code_by_name(kw, max_results=5)
                    if not matches:
                        yield event.plain_result(f"未找到名称包含「{kw}」的股票，请改用代码试试。")
                        return
                    if len(matches) > 1:
                        lines = ["找到多只匹配的股票，请改用代码查询："]
                        for m in matches:
                            lines.append(f"  • {m['name']}（{m['code']}）")
                        yield event.plain_result("\n".join(lines))
                        return
                    codes.append(matches[0]["code"])
            else:
                rec = self._session_data(session_id)
                codes = (rec.get("stocks") or [])[:20]
            if not codes:
                yield event.plain_result("请指定代码或先添加自选，如：/股票 查询 600519 或 /股票 查询 贵州茅台")
                return
            quotes = await _fetch_quotes(codes)
            for code in codes[:5]:
                record_passive_habit(event, "stock", "stock_code", code, source_text=msg)
            yield event.plain_result(_format_quotes(quotes, "行情"))

        elif cmd in ("提醒", "定时", "remind"):
            if not args:
                yield event.plain_result("用法：/股票 提醒 09:30 或 /股票 提醒 09:30 一次")
                return
            time_str = args[0].strip().replace("：", ":")
            if not re.match(r"^\d{1,2}:\d{2}$", time_str):
                yield event.plain_result("时间格式请用 09:30")
                return
            repeat = "once" if len(args) > 1 and args[1] in ("一次", "once") else "daily"
            rec = self._session_data(session_id)
            reminders = rec.get("reminders") or []
            if any(r.get("time") == time_str for r in reminders):
                yield event.plain_result(f"✅ 已有 {time_str} 的提醒。")
                return
            reminders.append(
                {
                    "time": time_str,
                    "repeat": repeat,
                    "creator_id": creator_id,
                    "creator_name": creator_name,
                }
            )
            rec["reminders"] = reminders
            data = _load_watchlist()
            data[session_id] = rec
            _save_watchlist(data)
            logger.info(
                "股票定时提醒已设置：session_id=%s time=%s repeat=%s creator_id=%s creator_name=%s",
                session_id,
                time_str,
                repeat,
                creator_id,
                creator_name,
            )
            record_passive_habit(event, "stock", "reminder_time", time_str, source_text=msg)
            yield event.plain_result(f"✅ 已设置 {time_str} 定时提醒（{'每天' if repeat == 'daily' else '仅一次'}）")

        elif cmd in ("提醒列表", "remindlist"):
            rec = self._session_data(session_id)
            reminders = rec.get("reminders") or []
            if not reminders:
                yield event.plain_result("当前未设置定时提醒。")
                return
            lines = ["⏰ 定时提醒："]
            for r in reminders:
                lines.append(f"  • {r.get('time', '')}（{'每天' if r.get('repeat') == 'daily' else '一次'}）")
            yield event.plain_result("\n".join(lines))

        elif cmd in ("取消提醒", "cancelremind"):
            if not args:
                yield event.plain_result("用法：/股票 取消提醒 09:30")
                return
            time_str = args[0].strip()
            rec = self._session_data(session_id)
            reminders = rec.get("reminders") or []
            new_reminders = [r for r in reminders if r.get("time") != time_str]
            if len(new_reminders) == len(reminders):
                yield event.plain_result(f"未找到 {time_str} 的提醒。")
                return
            rec["reminders"] = new_reminders
            data = _load_watchlist()
            data[session_id] = rec
            _save_watchlist(data)
            yield event.plain_result(f"✅ 已取消 {time_str} 的提醒。")

        elif cmd in ("跌到", "提醒跌", "跌价提醒"):
            if len(args) < 2:
                yield event.plain_result("用法：/股票 跌到 代码 价格 — 如 /股票 跌到 600519 1800")
                return
            code = _normalize_code(args[0])
            if not code:
                yield event.plain_result("❌ 无效股票代码。")
                return
            try:
                price = float(args[1].replace(",", ""))
            except ValueError:
                yield event.plain_result("❌ 价格请填数字，如 1800 或 18.5")
                return
            if price <= 0:
                yield event.plain_result("❌ 价格须大于 0。")
                return
            rec = self._session_data(session_id)
            alerts = rec.get("price_alerts") or []
            if any(a.get("code") == code and a.get("condition") == "below" for a in alerts):
                yield event.plain_result(f"✅ 已有 {code} 的跌价提醒，请先取消再设。")
                return
            alerts.append(
                {
                    "code": code,
                    "target_price": price,
                    "condition": "below",
                    "creator_id": creator_id,
                    "creator_name": creator_name,
                }
            )
            rec["price_alerts"] = alerts
            data = _load_watchlist()
            data[session_id] = rec
            _save_watchlist(data)
            logger.info(
                "股票价格提醒已设置（跌到）：session_id=%s code=%s target=%.4f creator_id=%s creator_name=%s",
                session_id,
                code,
                price,
                creator_id,
                creator_name,
            )
            record_passive_habit(event, "stock", "stock_code", code, source_text=msg)
            yield event.plain_result(f"✅ 已设置：{code} 跌到 {price} 元时提醒。")

        elif cmd in ("涨到", "提醒涨", "涨价提醒"):
            if len(args) < 2:
                yield event.plain_result("用法：/股票 涨到 代码 价格 — 如 /股票 涨到 600519 2000")
                return
            code = _normalize_code(args[0])
            if not code:
                yield event.plain_result("❌ 无效股票代码。")
                return
            try:
                price = float(args[1].replace(",", ""))
            except ValueError:
                yield event.plain_result("❌ 价格请填数字。")
                return
            if price <= 0:
                yield event.plain_result("❌ 价格须大于 0。")
                return
            rec = self._session_data(session_id)
            alerts = rec.get("price_alerts") or []
            if any(a.get("code") == code and a.get("condition") == "above" for a in alerts):
                yield event.plain_result(f"✅ 已有 {code} 的涨价提醒，请先取消再设。")
                return
            alerts.append(
                {
                    "code": code,
                    "target_price": price,
                    "condition": "above",
                    "creator_id": creator_id,
                    "creator_name": creator_name,
                }
            )
            rec["price_alerts"] = alerts
            data = _load_watchlist()
            data[session_id] = rec
            _save_watchlist(data)
            logger.info(
                "股票价格提醒已设置（涨到）：session_id=%s code=%s target=%.4f creator_id=%s creator_name=%s",
                session_id,
                code,
                price,
                creator_id,
                creator_name,
            )
            record_passive_habit(event, "stock", "stock_code", code, source_text=msg)
            yield event.plain_result(f"✅ 已设置：{code} 涨到 {price} 元时提醒。")

        elif cmd in ("价格提醒列表", "跌价列表", "涨价列表"):
            rec = self._session_data(session_id)
            alerts = rec.get("price_alerts") or []
            if not alerts:
                yield event.plain_result("当前未设置价格提醒。")
                return
            lines = ["🔔 价格提醒列表："]
            for a in alerts:
                cond = "跌到" if a.get("condition") == "below" else "涨到"
                lines.append(f"  • {a.get('code', '')} {cond} {a.get('target_price', '')} 元")
            yield event.plain_result("\n".join(lines))

        elif cmd in ("取消价格提醒", "取消跌价", "取消涨价"):
            if not args:
                yield event.plain_result("用法：/股票 取消价格提醒 代码")
                return
            code = _normalize_code(args[0])
            rec = self._session_data(session_id)
            alerts = rec.get("price_alerts") or []
            new_alerts = [a for a in alerts if a.get("code") != code]
            if len(new_alerts) == len(alerts):
                yield event.plain_result(f"未找到 {code} 的价格提醒。")
                return
            rec["price_alerts"] = new_alerts
            data = _load_watchlist()
            data[session_id] = rec
            _save_watchlist(data)
            yield event.plain_result(f"✅ 已取消 {code} 的价格提醒。")

        elif cmd in ("帮助", "help", "?"):
            async for r in self._send_help_v2(event):
                yield r
        else:
            async for r in self._send_help_v2(event):
                yield r

    async def _handle_stock_quant_analysis(self, event: AstrMessageEvent, args: list[str]):
        stock_code, error = await self._resolve_stock_target(args[0] if args else None)
        if error:
            yield event.plain_result(error)
            return

        logger.info("开始执行股票量化分析: %s", stock_code)
        yield event.plain_result(f"正在分析 {stock_code} 的量化指标，通常需要几秒到几十秒，请稍等。")

        analyzer = _get_ak_stock_analyzer()
        info, history = await asyncio.gather(
            analyzer.get_stock_realtime(stock_code),
            analyzer.get_stock_history(stock_code, days=90),
        )
        if not info:
            yield event.plain_result(f"无法获取股票 {stock_code} 的实时信息。")
            return
        if not history:
            yield event.plain_result(
                f"{info.name}({info.code}) 的历史行情暂时获取失败，可能是数据源网络异常，请稍后重试。"
            )
            return
        if len(history) < 20:
            yield event.plain_result(f"{info.name}({info.code}) 历史数据不足，至少需要 20 个交易日数据才能做量化分析。")
            return

        report = self.ai_analyzer.get_quant_summary(history)
        header = (
            f"📈 {info.name}({info.code}) 股票量化分析\n"
            f"当前价格: {info.latest_price:.2f} ({info.change_rate:+.2f}%)\n"
            f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        record_passive_habit(event, "stock", "stock_code", info.code, source_text=event.get_message_str().strip())
        yield event.plain_result(f"{header}\n{report}\n\n提示：量化指标基于 A 股历史数据，不代表未来表现。")

    async def _handle_stock_ai_analysis(self, event: AstrMessageEvent, args: list[str]):
        runtime_config = ensure_flat_config(getattr(self.context, "config", None) or self.config)

        provider = self.context.get_using_provider()
        stock_provider_id = str(getattr(runtime_config, "stock_ai_provider_id", "") or "").strip()
        stock_provider_configs_raw = getattr(runtime_config, "stock_ai_providers", None)
        fund_provider_id = str(getattr(runtime_config, "fund_ai_provider_id", "") or "").strip()
        fund_provider_configs_raw = getattr(runtime_config, "fund_ai_providers", None)
        stock_provider_configs = self.ai_analyzer.normalize_provider_configs(stock_provider_configs_raw)
        fund_provider_configs = self.ai_analyzer.normalize_provider_configs(fund_provider_configs_raw)
        provider_id = stock_provider_id or fund_provider_id
        provider_configs = stock_provider_configs or fund_provider_configs
        if stock_provider_configs_raw and not stock_provider_configs:
            logger.warning(
                "股票智能分析服务商配置解析为空: stock_raw_type=%s",
                type(stock_provider_configs_raw).__name__,
            )
            yield event.plain_result(
                "股票智能分析服务商配置未被正确识别，请重新保存一次“股票智能分析服务商”的 API 地址、API Key 和模型名。"
            )
            return
        if not stock_provider_configs and not stock_provider_id and fund_provider_configs_raw and not fund_provider_configs:
            logger.warning(
                "基金智能分析服务商配置解析为空（股票兜底）: fund_raw_type=%s",
                type(fund_provider_configs_raw).__name__,
            )
            yield event.plain_result(
                "基金智能分析服务商配置未被正确识别，请重新保存一次“基金智能分析服务商”的 API 地址、API Key 和模型名。"
            )
            return
        timeout_seconds = self._safe_int(
            getattr(runtime_config, "stock_ai_timeout_seconds", None)
            or getattr(runtime_config, "fund_ai_timeout_seconds", 90),
            90,
        )
        if not provider and not provider_id and not provider_configs:
            yield event.plain_result("当前没有配置大模型提供商，无法执行股票智能分析。")
            return

        stock_code, error = await self._resolve_stock_target(args[0] if args else None)
        if error:
            yield event.plain_result(error)
            return

        logger.info("开始执行股票智能分析: %s", stock_code)
        logger.info(
            "股票智能分析模型配置来源: %s, provider_id=%s, provider_configs=%s, timeout=%ss",
            "stock"
            if stock_provider_id or stock_provider_configs
            else "fund"
            if fund_provider_id or fund_provider_configs
            else "session-default",
            provider_id or "<session-default>",
            "yes" if provider_configs else "no",
            timeout_seconds,
        )
        logger.info(
            "股票智能分析 provider 配置解析: stock_raw_type=%s, stock_count=%s, fund_raw_type=%s, fund_count=%s",
            type(stock_provider_configs_raw).__name__ if stock_provider_configs_raw is not None else "None",
            len(stock_provider_configs),
            type(fund_provider_configs_raw).__name__ if fund_provider_configs_raw is not None else "None",
            len(fund_provider_configs),
        )
        yield event.plain_result(f"正在分析 {stock_code} 的行情、历史走势和资金流，通常需要 10 到 60 秒，请稍等。")

        analyzer = _get_ak_stock_analyzer()
        logger.info("股票智能分析开始拉取基础数据: %s", stock_code)
        info, history, flow_data = await asyncio.gather(
            analyzer.get_stock_realtime(stock_code),
            analyzer.get_stock_history(stock_code, days=90),
            analyzer.get_stock_fund_flow(stock_code, days=10),
        )
        logger.info(
            "股票智能分析基础数据拉取完成: %s, realtime=%s, history_count=%s, flow_count=%s",
            stock_code,
            "ok" if info else "missing",
            len(history),
            len(flow_data),
        )
        if not info:
            yield event.plain_result(f"无法获取股票 {stock_code} 的实时信息。")
            return
        if not history:
            yield event.plain_result(
                f"{info.name}({info.code}) 的历史行情暂时获取失败，可能是数据源网络异常，请稍后重试。"
            )
            return
        if len(history) < 20:
            yield event.plain_result(f"{info.name}({info.code}) 历史数据不足，无法执行股票智能分析。")
            return

        technical_indicators = self.ai_analyzer.quant.calculate_all_indicators(history)
        logger.info(
            "股票智能分析技术指标已计算: %s, signal=%s, trend_score=%s",
            stock_code,
            technical_indicators.signal,
            technical_indicators.trend_score,
        )
        flow_text = self._format_stock_flow_text(flow_data)
        try:
            logger.info("股票智能分析开始调用 AI 分析器: %s", stock_code)
            report = await self.ai_analyzer.analyze(
                fund_info=info,
                history_data=history,
                technical_indicators={},
                user_id=str(getattr(event, "get_sender_id", lambda: "default")() or "default"),
                fund_flow_text=flow_text,
                provider_id=provider_id,
                timeout_seconds=timeout_seconds,
                provider_configs=provider_configs,
            )
            logger.info("股票智能分析 AI 分析器返回成功: %s, report_length=%s", stock_code, len(report))
        except Exception as exc:
            logger.error("股票智能分析失败: %s", exc)
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                yield event.plain_result("股票智能分析失败：大模型请求超时，请稍后重试，或检查当前模型/API 网络状态。")
            else:
                yield event.plain_result(f"股票智能分析失败：{exc}")
            return

        signal, score = self.ai_analyzer.get_technical_signal(history)
        header = (
            f"🤖 {info.name}({info.code}) 股票智能分析\n"
            f"当前价格: {info.latest_price:.2f} ({info.change_rate:+.2f}%)\n"
            f"技术信号: {signal} ({score})\n"
            f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        record_passive_habit(event, "stock", "stock_code", info.code, source_text=event.get_message_str().strip())
        yield event.plain_result(f"{header}\n{report}\n\n提示：内容由模型基于 A 股实时/历史数据生成，仅供参考。")

    async def _handle_stock_multi_agent_analysis(self, event: AstrMessageEvent, args: list[str]):
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("当前没有配置大模型提供商，无法执行股票多智能体分析。")
            return

        stock_code, error = await self._resolve_stock_target(args[0] if args else None)
        if error:
            yield event.plain_result(error)
            return

        yield event.plain_result(
            f"正在为 {stock_code} 启动 A 股多智能体博弈分析，通常需要 20 到 60 秒，请稍等。"
        )

        analyzer = _get_ak_stock_analyzer()
        info, history, flow_data = await asyncio.gather(
            analyzer.get_stock_realtime(stock_code),
            analyzer.get_stock_history(stock_code, days=90),
            analyzer.get_stock_fund_flow(stock_code, days=10),
        )
        if not info:
            yield event.plain_result(f"无法获取股票 {stock_code} 的实时信息。")
            return
        if len(history) < 20:
            yield event.plain_result(f"{info.name}({info.code}) 历史数据不足，无法执行股票多智能体分析。")
            return

        try:
            news_summary = await self.ai_analyzer.get_news_summary(info.name, info.code)
            factors_text = self.ai_analyzer.factors.format_factors_text(info.name)
            global_situation_text = self.ai_analyzer.factors.format_global_situation_text(info.name)
            result = await self.debate_engine.run_debate(
                fund_info=info,
                history_data=history,
                fund_flow_data=flow_data,
                news_summary=news_summary,
                factors_text=factors_text,
                global_situation_text=global_situation_text,
                quant_analyzer=self.ai_analyzer.quant,
                eastmoney_api=None,
            )
        except Exception as exc:
            logger.error("股票多智能体分析失败: %s", exc)
            yield event.plain_result(f"股票多智能体分析失败：{exc}")
            return

        record_passive_habit(event, "stock", "stock_code", info.code, source_text=event.get_message_str().strip())
        yield event.plain_result(self.debate_engine.format_debate_summary(result))

    async def _send_help(self, event: AstrMessageEvent):
        help_text = (
            "📈 股票插件 — 发送 /股票 或 /股票 帮助 可随时查看本说明\n\n"
            "【自选与行情】\n"
            "• /股票 添加 代码 — 添加自选\n"
            "• /股票 删除 代码 — 移除自选\n"
            "• /股票 列表 — 自选行情\n"
            "• /股票 查询 [代码…] — 查行情\n"
            "• /股票 提醒 09:30 [每天|一次] — 定时提醒\n"
            "• /股票 提醒列表 — 查看定时提醒\n"
            "• /股票 取消提醒 09:30 — 取消定时提醒\n\n"
            "【价格提醒】跌到/涨到某价时通知\n"
            "• /股票 跌到 代码 价格 — 跌到该价时提醒（如 /股票 跌到 600519 1800）\n"
            "• /股票 涨到 代码 价格 — 涨到该价时提醒\n"
            "• /股票 价格提醒列表 — 查看价格提醒\n"
            "• /股票 取消价格提醒 代码 — 取消该股价格提醒"
        )
        yield event.plain_result(help_text)


    async def _send_help_v2(self, event: AstrMessageEvent):
        help_text = (
            "📱 股票功能说明\n\n"
            "【自选与行情】\n"
            "• /股票 添加 代码 - 添加自选\n"
            "• /股票 删除 代码 - 移除自选\n"
            "• /股票 列表 - 查看自选行情\n"
            "• /股票 查询 [代码或名称] - 查询实时行情\n"
            "• /股票 提醒 09:30 [每天|一次] - 定时提醒\n"
            "• /股票 提醒列表 - 查看定时提醒\n"
            "• /股票 取消提醒 09:30 - 取消定时提醒\n\n"
            "【价格提醒】\n"
            "• /股票 跌到 代码 价格 - 跌到目标价提醒\n"
            "• /股票 涨到 代码 价格 - 涨到目标价提醒\n"
            "• /股票 价格提醒列表 - 查看价格提醒\n"
            "• /股票 取消价格提醒 代码 - 取消价格提醒\n\n"
            "【基金与分析】\n"
            "• /股票 搜索股票 关键词 - 搜索 A 股名称（依赖 akshare）\n"
            "• /股票 量化分析 [代码] - A 股量化指标总结\n"
            "• /股票 智能分析 [代码] - 基于 A 股数据的 AI 深度分析\n"
            "• /股票 股票智能分析 [代码] - 基于 A 股数据的多智能体博弈分析\n"
            "• 基金相关请优先使用 /基金\n"
            "• /基金 [代码] - 查询基金/ETF/LOF 行情\n"
            "• /基金 搜索 关键词 - 搜索基金代码\n"
            "• /基金 设置 代码 - 设置默认基金\n"
            "• /基金 分析 [代码] - 技术分析\n"
            "• /基金 历史 [代码] [天数] - 历史行情\n"
            "• /基金 对比 代码1 代码2 - 两只基金对比\n"
            "• /基金 量化 [代码] - 量化指标总结\n"
            "• /基金 智能 [代码] - AI 深度分析\n"
            "• /基金 博弈 [代码] - 多智能体博弈分析\n"
            "• 旧用法 /股票 基金... 仍然兼容"
        )
        yield event.plain_result(help_text)


_stock_module: StockModule | None = None


def init_stock_module(context: Context, config: AstrBotConfig) -> StockModule:
    global _stock_module
    if _stock_module is None:
        _stock_module = StockModule(context, config)
    else:
        _stock_module.refresh_runtime(context, config)
    return _stock_module


async def handle_stock_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_stock_module(context, config)
    async for r in module.handle_command(event):
        yield r
