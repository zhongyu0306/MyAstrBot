import asyncio
import os
import tempfile
from urllib.parse import quote

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent


async def _query_weather_text(api_url: str, api_key: str, city: str, days: int | None) -> str | None:
    try:
        url = _build_url(api_url, api_key, city, days, fmt="text")
        logger.info("请求URL: %s", url)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    text_data = await response.text()
                    return text_data or None
                return None
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
                if response.status == 200:
                    content_type = response.headers.get("Content-Type", "")
                    data = await response.read()
                    if not data:
                        return None
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
                return None
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

    api_url = getattr(config, "weather_api_url", "https://api.nycnm.cn/API/weather.php")
    api_key = getattr(config, "weather_api_key", "")
    default_format = getattr(config, "weather_default_format", "image")

    try:
        if str(default_format).lower() == "image":
            image_path = await _query_weather_image(api_url, api_key, city, days)
            if image_path:
                yield event.image_result(image_path)
                try:
                    os.unlink(image_path)
                except Exception:
                    pass
            else:
                text = await _query_weather_text(api_url, api_key, city, days)
                if text:
                    title = f"📍 {city}天气" if not days or days == 1 else f"📍 {city} {days}天天气预报"
                    yield event.plain_result(f"{title}\n\n{text}")
                else:
                    yield event.plain_result("❌ 查询失败或无数据")
        else:
            text = await _query_weather_text(api_url, api_key, city, days)
            if text:
                title = f"📍 {city}天气" if not days or days == 1 else f"📍 {city} {days}天天气预报"
                yield event.plain_result(f"{title}\n\n{text}")
            else:
                yield event.plain_result("❌ 查询失败或无数据")
    except Exception as e:
        logger.error("查询天气时发生错误: %s", e)
        yield event.plain_result(f"❌ 查询失败: {str(e)}")


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

