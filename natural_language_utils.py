# 自然语言触发层（astrbot_all_char）
# 在保留全部指令的前提下，对「非 / 开头」的句子做意图匹配，转成等效命令并复用现有 handler。
# 自然语言命中后由本模块统一产出回复，建议在框架侧终止后续 LLM 处理，避免重复回复。

from __future__ import annotations

import re
from typing import Any, AsyncIterator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from .train_cities import is_chinese_city


def _is_command_message(text: str) -> bool:
    """判断是否已为命令形式（以 / 或 ／ 开头），这类消息不进入 NL 分支。"""
    t = (text or "").strip()
    return t.startswith("/") or t.startswith("／")


class _NLWrappedEvent:
    """包装原始 event，使 get_message_str() / message_str 返回「等效命令字符串」，供现有 handler 直接解析。"""

    __slots__ = ("_event", "_fake_message")

    def __init__(self, original: AstrMessageEvent, fake_message: str):
        self._event = original
        self._fake_message = fake_message.strip()

    def get_message_str(self) -> str:
        return self._fake_message

    @property
    def message_str(self) -> str:
        return self._fake_message

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)


# ---------- 各模块自然语言匹配：返回 (fake_command_message) 或 None ----------


def _match_weather(text: str) -> str | None:
    """匹配：北京天气、上海今天天气、明天北京天气 等。"""
    t = text.strip()
    if not t or len(t) > 50:
        return None
    # 不含天气相关词时不按天气处理，避免「从上海到北京的车票」整句被当城市
    if not re.search(r"天气|多少度|几度", t):
        return None
    # 明天/后天/今天 + 可选城市 + 天气
    m = re.match(
        r"^(?:明天|后天|今天)?\s*([^\s]+?)(?:的?天气|天气怎么样?|多少度|几度)?\s*$",
        t,
        re.IGNORECASE,
    )
    if m:
        city = m.group(1).strip()
        if city and len(city) <= 20 and not city.startswith("/"):
            return f"天气 {city}"
    # 纯「某地+天气」：取第一个看起来像地名的片段（简单：非数字、长度 2-10）
    parts = re.split(r"[\s，,]+", t)
    for p in parts:
        p = p.strip()
        if 2 <= len(p) <= 10 and not re.match(r"^\d", p) and "天气" not in p:
            return f"天气 {p}"
    return None


def _match_train(text: str) -> str | None:
    """匹配：从厦门到上海的车票、帮我查下从武汉到上海的车票 等；出发地、目的地均需命中中国城市。"""
    t = text.strip()
    if not t or len(t) > 60:
        return None
    # 可选前缀「帮我查/查下/查一下」+ 从 A 到 B；第二段用贪婪 [^\s到的]+ 匹配完整目的地
    m = re.search(
        r"(?:帮我?查(?:一?下)?\s*)?(?:从)?\s*([^\s到\-]+?)\s*[到\-至]\s*([^\s到的]+)(?:的?(?:车票|火车票|票))?",
        t,
    )
    if m:
        go, to = m.group(1).strip(), m.group(2).strip()
        if go and to and len(go) <= 10 and len(to) <= 10 and is_chinese_city(go) and is_chinese_city(to):
            return f"火车票 {go} {to}"
    # 兜底：含「到/至」且含「车票/火车/票」则按「到」或「至」拆成两段
    if ("车票" in t or "火车" in t or "票" in t) and re.search(r"[到至]", t):
        parts = re.split(r"[到至]", t, maxsplit=1)
        if len(parts) == 2:
            go = re.sub(r"^(?:帮我?查(?:一?下)?\s*)?(?:从\s*)?", "", parts[0]).strip()
            to = re.sub(r"的?(?:车票|火车票|票).*$", "", parts[1]).strip()
            if go and to and len(go) <= 10 and len(to) <= 10 and is_chinese_city(go) and is_chinese_city(to):
                return f"火车票 {go} {to}"
    return None


def _match_reminder(text: str) -> str | None:
    """匹配：3分钟后提醒我喝水、明天8点提醒我开会、半小时后提醒 等。"""
    t = text.strip()
    if not t or len(t) > 120:
        return None
    # N分钟后提醒(我)xxx、N小时后提醒(我)xxx、提醒我 xxx
    m = re.match(
        r"^(\d+\s*[分钟小时天后]+)\s*(?:提醒(?:我)?)?\s*(.+)$",
        t,
    )
    if m:
        time_part = m.group(1).strip().replace(" ", "")
        content = m.group(2).strip()
        if content:
            return f"提醒 {time_part} {content}"
    # 提醒我 xxx 在 yyy 时间
    m2 = re.match(r"^提醒(?:我)?\s*(.+?)\s+(?:在|到|等)?\s*(\d.+)$", t)
    if m2:
        content, time_part = m2.group(1).strip(), m2.group(2).strip()
        if content and time_part:
            return f"提醒 {time_part} {content}"
    return None


def _match_stock(text: str) -> str | None:
    """匹配：贵州茅台股价、查一下600519、我的自选股 等。"""
    t = text.strip()
    if not t or len(t) > 40:
        return None
    # 仅当明确出现「自选/自选股」或「我的股票/我的自选」时视为查列表，避免「把我的…」误命中
    if not re.search(r"添加|删除", t) and (
        re.search(r"自选(股)?", t) or re.search(r"我的(股票|自选)", t)
    ):
        return "股票 列表"
    # 6 位数字代码
    code = re.search(r"\b(6\d{5}|0\d{5}|3\d{5})\b", t)
    if code and re.search(r"股|行情|涨|跌|查", t):
        return f"股票 查询 {code.group(1)}"
    # 常见名称
    if re.search(r"(贵州茅台|比亚迪|宁德时代|茅台)\s*(股价|行情)?", t):
        name = "贵州茅台" if "茅台" in t and "贵州" in t else (t[:4] if len(t) >= 2 else None)
        if name:
            return f"股票 查询 {name}"
    return None


def _match_epic(text: str) -> str | None:
    """匹配：Epic免费游戏、喜加一、e宝、最近有什么免费游戏 等。"""
    t = text.strip().lower()
    if not t or len(t) > 40:
        return None
    if any(k in t for k in ("epic", "喜加一", "e宝", "免费游戏", "白嫖")):
        return "epic"
    return None


def _match_jrys(text: str) -> str | None:
    """匹配：今日运势、运势、jrys 等。"""
    t = text.strip()
    if not t or len(t) > 20:
        return None
    if t.lower() in ("jrys", "今日运势", "运势", "查运势", "今日运势怎么样"):
        return "jrys"
    return None


def _match_smart_search(text: str) -> str | None:
    """匹配：智能搜索 xxx、帮我查一下 xxx（偏问题型）。"""
    t = text.strip()
    if not t or len(t) < 2 or len(t) > 200:
        return None
    if t.lower().startswith("智能搜索"):
        q = t[4:].strip()
        if q:
            return f"智能搜索 {q}"
    if re.match(r"^(帮我查一下?|查一下?|搜索一下?|问问)\s+", t):
        q = re.sub(r"^(帮我查一下?|查一下?|搜索一下?|问问)\s+", "", t, flags=re.I)
        if q:
            return f"智能搜索 {q}"
    return None


def _match_web_search(text: str) -> str | None:
    """匹配：搜一下 xxx、网页搜索 xxx（偏关键词型）。"""
    t = text.strip()
    if not t or len(t) < 2 or len(t) > 100:
        return None
    if t.startswith("搜索") or t.startswith("搜一下") or t.startswith("搜 "):
        q = re.sub(r"^(搜索|搜一下?|搜)\s*", "", t, flags=re.I).strip()
        if q:
            return f"搜索 {q}"
    return None


def _match_bookkeeping(text: str) -> str | None:
    """匹配：记账支出 20 咖啡、记一笔收入 100、今天花了多少、月统计 等。"""
    t = text.strip()
    if not t or len(t) > 80:
        return None
    # 记账支出 数字 [描述]
    m = re.match(r"^(?:记一笔?)?\s*支出\s+(\d+(?:\.\d{1,2})?)\s*(.*)$", t)
    if m:
        return f"记账支出 {m.group(1)} {m.group(2).strip()}"
    m = re.match(r"^(?:记一笔?)?\s*收入\s+(\d+(?:\.\d{1,2})?)\s*(.*)$", t)
    if m:
        return f"记账收入 {m.group(1)} {m.group(2).strip()}"
    if re.search(r"今天花了多少|今日支出|日统计", t):
        return "日统计"
    if re.search(r"本月(花了)?多少|月统计", t):
        return "月统计"
    if re.search(r"查账|统计", t) and "按类" not in t:
        return "查账统计"
    return None


# ---------- 配置：是否启用 NL、各模块开关 ----------


def _is_false_value(v: Any) -> bool:
    """配置里视为「关闭」的值：False、字符串 "false"/"False"、0。"""
    if v is False or v is None:
        return True
    if isinstance(v, bool):
        return not v
    if isinstance(v, str) and v.strip().lower() in ("false", "0", "no", "off"):
        return True
    if isinstance(v, (int, float)) and v == 0:
        return True
    return False


def _get_nl_config(config: AstrBotConfig) -> dict[str, bool]:
    """读取自然语言开关。默认全开。兼容配置面板将布尔存成字符串的情况。"""
    defaults = {
        "enabled": True,
        "weather": True,
        "train": True,
        "reminder": True,
        "stock": True,
        "epic": True,
        "jrys": True,
        "smart_search": True,
        "web_search": True,
        "bookkeeping": True,
    }
    out = dict(defaults)
    try:
        # 仅当配置里明确为「关闭」时才关；兼容 bool/字符串 "false"/0
        if hasattr(config, "nl_enabled"):
            v = getattr(config, "nl_enabled", True)
            if _is_false_value(v):
                out["enabled"] = False
        for k in list(out):
            key = f"nl_{k}_enabled" if k != "enabled" else "nl_enabled"
            if hasattr(config, key):
                v = getattr(config, key)
                if isinstance(v, bool):
                    out[k] = v
                elif _is_false_value(v):
                    out[k] = False
    except Exception:
        pass
    return out


# ---------- 统一入口：对非命令消息尝试 NL，命中则 yield 回复 ----------


async def try_natural_language(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
    *,
    handle_weather_command,
    handle_train_command,
    handle_simple_reminder,
    handle_stock_command,
    handle_epic_command,
    handle_jrys_command,
    handle_smart_search_command,
    handle_web_search_command,
    handle_bookkeeping_expense,
    handle_bookkeeping_income,
    handle_bookkeeping_summary,
    handle_bookkeeping_daily,
    handle_bookkeeping_monthly,
) -> AsyncIterator[Any]:
    """
    对当前消息做自然语言匹配；若命中则调用对应 handler 并 yield 其产出，否则不 yield。
    调用方应在「非 / 开头」且未命中其他命令时调用本函数；命中后建议终止后续 LLM 流程。
    """
    raw = (event.get_message_str() or "").strip()
    if _is_command_message(raw):
        return
    # 纯 URL 不进入自然语言匹配，避免无意义匹配与误触
    if raw.startswith("http://") or raw.startswith("https://"):
        return
    cfg = _get_nl_config(config)
    nl_enabled = cfg.get("enabled", True)
    if not nl_enabled:
        logger.info("[NL] 自然语言已关闭 (nl_enabled=%s)，跳过匹配", nl_enabled)
        return

    logger.info("[NL] 开始匹配 raw=%r len=%d (nl_enabled=%s)", raw, len(raw), nl_enabled)
    fake: str | None = None
    handler_name: str | None = None
    handler_gen = None
    wrapped_event: AstrMessageEvent | None = None

    train_fake = _match_train(raw) if cfg.get("train") else None
    logger.info("[NL] train_fake=%r", train_fake)
    if cfg.get("weather") and (fake := _match_weather(raw)):
        handler_name = "weather"
        logger.info("NL 命中天气: raw=%s -> fake=%s", raw[:50], fake)
        wrapped_event = _NLWrappedEvent(event, fake)
        handler_gen = handle_weather_command(wrapped_event, config)
    elif train_fake:
        handler_name = "train"
        fake = train_fake
        logger.info("NL 命中火车票: raw=%s -> fake=%s", raw[:50], fake)
        wrapped_event = _NLWrappedEvent(event, fake)
        handler_gen = handle_train_command(wrapped_event, config)
    elif cfg.get("reminder") and (fake := _match_reminder(raw)):
        handler_name = "reminder"
        logger.info("NL 命中提醒: raw=%s -> fake=%s", raw[:50], fake)
        wrapped_event = _NLWrappedEvent(event, fake)
        handler_gen = handle_simple_reminder(wrapped_event, context, config)
    elif cfg.get("stock") and (fake := _match_stock(raw)):
        handler_name = "stock"
        logger.info("NL 命中股票: raw=%s -> fake=%s", raw[:50], fake)
        wrapped_event = _NLWrappedEvent(event, fake)
        handler_gen = handle_stock_command(wrapped_event, context, config)
    elif cfg.get("epic") and (fake := _match_epic(raw)):
        handler_name = "epic"
        logger.info("NL 命中Epic: raw=%s -> fake=%s", raw[:50], fake)
        wrapped_event = _NLWrappedEvent(event, fake)
        handler_gen = handle_epic_command(wrapped_event, config)
    elif cfg.get("jrys") and (fake := _match_jrys(raw)):
        handler_name = "jrys"
        logger.info("NL 命中运势: raw=%s -> fake=%s", raw[:50], fake)
        wrapped_event = _NLWrappedEvent(event, fake)
        handler_gen = handle_jrys_command(wrapped_event, context, config)
    # 智能搜索为收费接口，暂不开放自然语言触发，仅支持 /智能搜索 指令
    # elif cfg.get("smart_search") and (fake := _match_smart_search(raw)):
    #     handler_name = "smart_search"
    #     wrapped_event = _NLWrappedEvent(event, fake)
    #     handler_gen = handle_smart_search_command(wrapped_event, context, config)
    elif cfg.get("web_search") and (fake := _match_web_search(raw)):
        handler_name = "web_search"
        logger.info("NL 命中网页搜索: raw=%s -> fake=%s", raw[:50], fake)
        wrapped_event = _NLWrappedEvent(event, fake)
        handler_gen = handle_web_search_command(wrapped_event, context, config)
    elif cfg.get("bookkeeping") and (fake := _match_bookkeeping(raw)):
        handler_name = "bookkeeping"
        logger.info("NL 命中记账: raw=%s -> fake=%s", raw[:50], fake)
        # 记账类 handler 解析的是「记账支出 20 描述」形式，直接传等效命令串
        wrapped_event = _NLWrappedEvent(event, fake)
        if "记账支出" in fake:
            handler_gen = handle_bookkeeping_expense(wrapped_event, context, config)
        elif "记账收入" in fake:
            handler_gen = handle_bookkeeping_income(wrapped_event, context, config)
        elif "查账统计" in fake:
            handler_gen = handle_bookkeeping_summary(wrapped_event, context, config)
        elif fake == "日统计":
            handler_gen = handle_bookkeeping_daily(wrapped_event, context, config)
        elif fake == "月统计":
            handler_gen = handle_bookkeeping_monthly(wrapped_event, context, config)
        else:
            handler_gen = None

    if handler_gen is None or wrapped_event is None:
        return
    logger.info("自然语言将执行: intent=%s fake=%s", handler_name, fake)
    async for result in handler_gen:
        yield result
