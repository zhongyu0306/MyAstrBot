import asyncio
import json
import os
import tempfile
from urllib.parse import quote

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent

from .passive_memory_utils import record_passive_habit


async def _query_weather_text(api_url: str, api_key: str, city: str, days: int | None) -> str | None:
    try:
        url = _build_url(api_url, api_key, city, days, fmt="text")
        logger.info("请求URL: %s", url)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                raw = await response.text()
                if response.status != 200:
                    logger.warning("天气 API 返回非 200: status=%s body_len=%d", response.status, len(raw or ""))
                    return None
                if not (raw or "").strip():
                    logger.warning("天气 API 返回空内容")
                    return None
                # 若返回的是 JSON（如 {"data":"..."} / {"result":"..."}），尝试取出文本再返回
                raw = raw.strip()
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw)
                        for key in ("data", "result", "text", "content", "msg", "message"):
                            if key in obj and obj[key] and isinstance(obj[key], str):
                                return obj[key].strip() or None
                        if "data" in obj and isinstance(obj["data"], dict):
                            return raw  # 保持原样由上层展示
                    except Exception:
                        pass
                return raw
    except asyncio.TimeoutError:
        logger.error("天气 API 请求超时")
        return None
    except Exception as e:
        logger.error("获取天气文本数据时发生错误: %s", e)
        return None


async def _query_weather_image(api_url: str, api_key: str, city: str, days: int | None) -> str | None:
    try:
        url = _build_url(api_url, api_key, city, days, fmt="image")
        logger.info("请求URL(图片): %s", url)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                data = await response.read()
                if response.status != 200:
                    logger.warning("天气图片 API 返回非 200: status=%s body_len=%d", response.status, len(data))
                    return None
                if not data:
                    logger.warning("天气图片 API 返回空内容")
                    return None
                content_type = response.headers.get("Content-Type", "")
                suffix = (
                    ".png"
                    if "png" in content_type.lower()
                    else ".jpg"
                    if ("jpeg" in content_type.lower() or "jpg" in content_type.lower())
                    else ".img"
                )
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                temp_file.write(data)
                temp_file.close()
                return temp_file.name
    except asyncio.TimeoutError:
        logger.error("天气 API 请求超时(图片)")
        return None
    except Exception as e:
        logger.error("获取天气图片时发生错误: %s", e)
        return None


def _build_url(api_url: str, api_key: str, city: str, days: int | None, fmt: str) -> str:
    q = quote(city)
    url = f"{api_url}?query={q}&format={fmt}"
    if days and days >= 2:
        url += f"&action=forecast&days={days}"
    if api_key:
        url += f"&apikey={api_key}"
    return url


def _get_weather_config(config: AstrBotConfig, key: str, default: str = ""):
    """从网页配置中读取天气相关项，兼容多种 config 存储方式（扁平/嵌套/items/value）。"""
    val = getattr(config, key, None)
    if val is not None and str(val).strip():
        return str(val).strip()
    if isinstance(config, dict):
        val = config.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
        g = config.get("weather")
        if isinstance(g, dict):
            val = g.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
            items = g.get("items")
            if isinstance(items, dict):
                v = items.get(key)
                if isinstance(v, dict) and "value" in v:
                    v = v["value"]
                if v is not None and str(v).strip():
                    return str(v).strip()
    try:
        g = config.get("weather") if hasattr(config, "get") else None
        if isinstance(g, dict):
            val = g.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
            items = g.get("items")
            if isinstance(items, dict):
                v = items.get(key)
                if isinstance(v, dict) and "value" in v:
                    v = v["value"]
                if v is not None and str(v).strip():
                    return str(v).strip()
    except Exception:
        pass
    return default


async def handle_weather_command(event: AstrMessageEvent, config: AstrBotConfig):
    """
    天气查询命令入口（命令模式，未启用自然语言识别）。
    """
    message_text = event.get_message_str()
    parts = message_text.strip().split()
    if len(parts) < 2:
        yield event.plain_result(
            "❌ 参数不足\n\n"
            "用法: /天气 城市 [天数]\n"
            "示例: /天气 北京 或 /天气 北京 5\n\n"
            "（当前整合版仅支持命令模式）"
        )
        return

    city = parts[1]
    days: int | None = None
    if len(parts) >= 3:
        try:
            d = int(parts[2])
            if d >= 2:
                days = d
        except Exception:
            days = None

    # 从网页配置读取，兼容扁平/嵌套/items 等多种存储方式
    _default_url = "https://api.nycnm.cn/API/weather.php"
    api_url = _get_weather_config(config, "weather_api_url", _default_url) or _default_url
    api_key = _get_weather_config(config, "weather_api_key", "")
    default_format = _get_weather_config(config, "weather_default_format", "image") or "image"
    if _default_url in api_url and not api_key:
        logger.warning("使用 api.nycnm.cn 未读到 API 密钥，请确认在插件配置「天气」中已填写并保存「API 密钥（apikey）」")

    image_path: str | None = None
    try:
        if str(default_format).lower() == "image":
            image_path = await _query_weather_image(api_url, api_key, city, days)
            if image_path:
                record_passive_habit(event, "weather", "city", city, source_text=message_text or "")
                yield event.image_result(image_path)
            else:
                text = await _query_weather_text(api_url, api_key, city, days)
                if text:
                    title = f"📍 {city}天气" if not days or days == 1 else f"📍 {city} {days}天天气预报"
                    record_passive_habit(event, "weather", "city", city, source_text=message_text or "")
                    yield event.plain_result(f"{title}\n\n{text}")
                else:
                    yield event.plain_result(
                        "❌ 查询失败或无数据\n\n"
                        "使用 api.nycnm.cn 时请在插件配置「天气」中填写「API 密钥（apikey）」后再试。"
                    )
        else:
            text = await _query_weather_text(api_url, api_key, city, days)
            if text:
                title = f"📍 {city}天气" if not days or days == 1 else f"📍 {city} {days}天天气预报"
                record_passive_habit(event, "weather", "city", city, source_text=message_text or "")
                yield event.plain_result(f"{title}\n\n{text}")
            else:
                yield event.plain_result(
                    "❌ 查询失败或无数据\n\n"
                    "使用 api.nycnm.cn 时请在插件配置「天气」中填写「API 密钥（apikey）」后再试。"
                )
    except Exception as e:
        logger.error("查询天气时发生错误: %s", e)
        yield event.plain_result(f"❌ 查询失败: {str(e)}")
    finally:
        if image_path:
            try:
                os.unlink(image_path)
            except Exception:
                pass


async def handle_weather_help(event: AstrMessageEvent):
    text = (
        "🧭 智能天气查询（命令模式）\n\n"
        "【命令】\n"
        "• /天气 城市 [天数]\n"
        "• 示例: /天气 北京 或 /天气 北京 5\n\n"
        "说明：\n"
        "- 当前 `astrbot_all_char` 版本仅以命令为主，不强制自然语言识别；\n"
        "- 配置项 `weather_default_format` 可选择 text/image 两种返回形式。"
    )
    yield event.plain_result(text)
