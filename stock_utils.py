import asyncio
import json
import re
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At
from astrbot.api.star import Context, StarTools


def _data_dir():
    try:
        return StarTools.get_data_dir("astrbot_stock")
    except Exception:
        return Path("data", "plugins_data", "astrbot_stock")


def _watchlist_file():
    _data_dir().mkdir(parents=True, exist_ok=True)
    return _data_dir() / "watchlist.json"


def _stock_map_file():
    _data_dir().mkdir(parents=True, exist_ok=True)
    return _data_dir() / "stock_codes.json"


def _load_stock_map() -> list[dict]:
    path = _stock_map_file()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "stocks" in data:
            return data["stocks"]
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.error("加载股票代码表失败: %s", e)
    return []


def _save_stock_map(stocks: list[dict]) -> None:
    try:
        with open(_stock_map_file(), "w", encoding="utf-8") as f:
            json.dump({"stocks": stocks}, f, ensure_ascii=False, indent=2)
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


def _to_sina_code(six_digit: str) -> str:
    """6 位代码转新浪格式：sh600519 / sz000001"""
    if not six_digit or len(six_digit) < 5:
        return ""
    n = six_digit[:6].zfill(6)
    if n[0] == "6":
        return "sh" + n
    if n[0] in "03":
        return "sz" + n
    return ""


def _load_watchlist() -> dict:
    path = _watchlist_file()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("加载自选数据失败: %s", e)
        return {}


def _save_watchlist(data: dict) -> None:
    try:
        with open(_watchlist_file(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("保存自选数据失败: %s", e)


SINA_HQ = "https://hq.sinajs.cn/list="
SINA_REFERER = "https://finance.sina.com.cn/"


async def _fetch_sina_quotes(sina_codes: list[str]) -> list[dict]:
    """异步请求新浪行情，使用 aiohttp，返回 list[dict]。"""
    if not sina_codes:
        return []
    url = SINA_HQ + ",".join(sina_codes)
    result: list[dict] = []
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Referer": SINA_REFERER}) as resp:
                if resp.status != 200:
                    logger.warning("新浪行情请求失败，HTTP %s", resp.status)
                    return []
                raw = await resp.text(encoding="gbk", errors="replace")
    except asyncio.TimeoutError:
        logger.warning("新浪行情请求超时")
        return []
    except Exception as e:
        logger.warning("新浪行情请求异常: %s", e)
        return []

    for line in raw.split("\n"):
        line = line.strip()
        if "var hq_str_" not in line or "=" not in line:
            continue
        try:
            i = line.index("=")
            code_key = line[line.index("var hq_str_") + 10 : i].strip()
            rest = line[i + 1 :].strip().strip('";')
            if rest.startswith('"'):
                rest = rest[1:]
            if rest.endswith('";'):
                rest = rest[:-2]
            parts = rest.split(",")
            if len(parts) < 4:
                continue
            name = parts[0].strip()
            try:
                open_price = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
                prev = float(parts[2])
                curr = float(parts[3])
            except (ValueError, IndexError):
                continue
            change = curr - prev if prev else 0
            pct = (change / prev * 100) if prev else 0
            code_6 = re.sub(r"\D", "", code_key)[:6].zfill(6)
            result.append(
                {
                    "code": code_6,
                    "name": name or code_6,
                    "current": curr,
                    "open": open_price or prev,
                    "prev_close": prev,
                    "change": change,
                    "change_pct": pct,
                    "high": float(parts[4]) if len(parts) > 4 else curr,
                    "low": float(parts[5]) if len(parts) > 5 else curr,
                }
            )
        except Exception as e:
            logger.debug("新浪解析行失败: %s", e)
            continue
    return result


def _fetch_akshare_spot_sync():
    """已废弃：保留空壳以兼容旧代码，不再实际访问 AkShare。"""
    raise ImportError("AkShare 功能已禁用")


def _fetch_akshare_quotes_sync(codes: list[str]) -> list[dict]:
    """已废弃：不再通过 AkShare 获取行情。"""
    raise ImportError("AkShare 功能已禁用")


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
    return await asyncio.to_thread(_search_code_by_name_sync, keyword, max_results)


async def _fetch_quotes(codes: list[str]) -> list[dict]:
    """统一入口：仅使用新浪行情源获取报价。"""
    if not codes:
        return []
    codes = list(dict.fromkeys(codes))
    sina_codes = [_to_sina_code(c) for c in codes if _to_sina_code(c)]
    if not sina_codes:
        return []
    return await _fetch_sina_quotes(sina_codes)


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
        self.config = config
        self._scheduler = None
        self._last_reminder_tick: str | None = None
        self._start_scheduler()
        logger.info("股票模块初始化完成（使用新浪行情，无 AkShare 依赖）")

    def _start_scheduler(self):
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            tz = getattr(self.config, "stock_reminder_timezone", "Asia/Shanghai")
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
        from datetime import datetime

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
                text = (
                    f"🔔 价格提醒\n{q['name']}({code}) 当前 {curr:.2f} 元，"
                    f"{'已跌破' if cond == 'below' else '已涨破'}您设置的 {target} 元提醒线。"
                )
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
                    chain.message(text)
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
            async for r in self._send_help(event):
                yield r
            return
        cmd, args = parts[1].strip().lower(), parts[2:] if len(parts) > 2 else []
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
            async for r in self._send_help(event):
                yield r
        else:
            async for r in self._send_help(event):
                yield r

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


_stock_module: StockModule | None = None


def init_stock_module(context: Context, config: AstrBotConfig) -> StockModule:
    global _stock_module
    if _stock_module is None:
        _stock_module = StockModule(context, config)
    return _stock_module


async def handle_stock_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    module = init_stock_module(context, config)
    async for r in module.handle_command(event):
        yield r


