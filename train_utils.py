import asyncio
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent


def _format_train_text(api_data: dict) -> str:
    go = api_data.get("go", "")
    to = api_data.get("to", "")
    date = api_data.get("date", "")
    time_str = api_data.get("time", "")
    data_list = api_data.get("data") or []
    lines = [f"🚆 {go} → {to}", f"📅 日期：{date}  |  更新时间：{time_str}", "=" * 40]
    for item in data_list:
        train_no = item.get("TrainNumber", "")
        start = item.get("start", "")
        end = item.get("end", "")
        depart = item.get("DepartTime", "")
        arrive = item.get("ArriveTime", "")
        duration = item.get("TimeDifference", "")
        seats = item.get("SeatList") or []
        seat_str = "  |  ".join(
            f"{s.get('SeatName', '')}-{s.get('SeatPrice', 0)}元 余{s.get('Seatresidue', 0)}" for s in seats
        )
        lines.append(f"【{train_no}】{start}→{end}")
        lines.append(f"   {depart}—{arrive}  全程{duration}")
        lines.append(f"   {seat_str}")
        lines.append("-" * 40)
    if not data_list:
        lines.append("暂无车次信息")
    return "\n".join(lines)


def _get_chinese_font(size: int) -> ImageFont.FreeTypeFont | None:
    plugin_dir = Path(__file__).resolve().parent
    fonts_dir = plugin_dir / "fonts"
    local_fonts: list[str] = []
    if fonts_dir.is_dir():
        for name in ("NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf", "wqy-zenhei.ttc", "msyh.ttc"):
            f = fonts_dir / name
            if f.is_file():
                local_fonts.append(str(f))
    if not local_fonts:
        for name in ("NotoSansCJK-Regular.ttc", "wqy-zenhei.ttc", "msyh.ttc"):
            local_fonts.append(str(fonts_dir / name))
    system_fonts = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
    ]
    for path in local_fonts + system_fonts:
        if not path or not os.path.isfile(path):
            continue
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def _draw_train_image(api_data: dict) -> str | None:
    data_list = api_data.get("data") or []
    go = api_data.get("go", "")
    to = api_data.get("to", "")
    date = api_data.get("date", "")
    if not data_list:
        return None
    font_l = _get_chinese_font(18)
    font_s = _get_chinese_font(14)
    if font_l is None or font_s is None:
        logger.warning("未找到中文字体，图片模式将退回文本")
        return None
    try:
        row_h = 28
        col_w = [80, 70, 70, 60, 60, 60, 220]
        header = ["车次", "出发站", "到达站", "出发", "到达", "历时", "座位与余票"]
        width = sum(col_w)
        height = 40 + len(data_list) * row_h + 20
        img = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        y = 8
        title = f"{go} → {to}  日期:{date}"
        draw.text((10, y), title, fill=(0, 0, 0), font=font_l)
        y += 32
        draw.line([(0, y), (width, y)], fill=(200, 200, 200))
        y += 6
        x = 0
        for i, h in enumerate(header):
            draw.text((x + 4, y), h, fill=(80, 80, 80), font=font_s)
            x += col_w[i]
        y += row_h
        draw.line([(0, y), (width, y)], fill=(200, 200, 200))
        for item in data_list:
            x = 0
            seats = item.get("SeatList") or []
            seat_str = " ".join(
                f"{s.get('SeatName')}{s.get('SeatPrice')}元余{s.get('Seatresidue')}" for s in seats[:5]
            )
            if len(seat_str) > 35:
                seat_str = seat_str[:32] + "..."
            row = [
                item.get("TrainNumber", ""),
                item.get("start", ""),
                item.get("end", ""),
                item.get("DepartTime", ""),
                item.get("ArriveTime", ""),
                item.get("TimeDifference", ""),
                seat_str,
            ]
            for i, cell in enumerate(row):
                draw.text((x + 4, y + 4), str(cell), fill=(0, 0, 0), font=font_s)
                x += col_w[i]
            y += row_h
        fp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        img.save(fp.name)
        return fp.name
    except Exception as e:
        logger.error("生成火车票图片失败: %s", e)
        return None


async def _fetch_trains(api_url: str, departure: str, arrival: str) -> dict | None:
    url = f"{api_url}?departure={quote(departure)}&arrival={quote(arrival)}&type=json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning("火车票 API 状态码: %s", resp.status)
                    return None
                data = await resp.json()
                if data.get("code") != 200:
                    logger.warning("火车票 API 返回 code: %s", data.get("code"))
                    return None
                return data
    except asyncio.TimeoutError:
        logger.error("火车票 API 请求超时")
        return None
    except Exception as e:
        logger.error("火车票 API 请求异常: %s", e)
        return None


async def _do_query(api_url: str, default_format: str, departure: str, arrival: str, event: AstrMessageEvent):
    image_path: str | None = None
    try:
        api_data = await _fetch_trains(api_url, departure, arrival)
        if not api_data:
            yield event.plain_result("❌ 查询失败或无数据，请检查出发地/目的地或稍后重试。")
            return
        fmt = str(default_format).lower()
        if fmt == "image":
            image_path = _draw_train_image(api_data)
            if image_path:
                yield event.image_result(image_path)
            else:
                text = _format_train_text(api_data)
                yield event.plain_result(f"🚆 火车票查询\n\n{text}")
        else:
            text = _format_train_text(api_data)
            yield event.plain_result(f"🚆 火车票查询\n\n{text}")
    finally:
        if image_path:
            try:
                os.unlink(image_path)
            except Exception:
                pass


async def handle_train_command(event: AstrMessageEvent, config: AstrBotConfig):
    """
    火车票查询命令入口（命令模式，未启用自然语言识别）。
    """
    msg_parts = event.get_message_str().strip().split()
    if len(msg_parts) < 3:
        yield event.plain_result(
            "❌ 用法：/火车票 出发地 目的地\n"
            "示例：/火车票 厦门 上海\n\n"
            "（当前整合版仅支持命令模式）"
        )
        return
    departure, arrival = msg_parts[1].strip(), msg_parts[2].strip()
    api_url = (getattr(config, "train_api_url", None) or "https://api.lolimi.cn/API/hc/api").rstrip("/")
    default_format = getattr(config, "train_default_format", "text")
    async for r in _do_query(api_url, default_format, departure, arrival, event):
        yield r


async def handle_train_help(event: AstrMessageEvent):
    text = (
        "🚆 火车票查询\n\n"
        "【命令】\n"
        "• /火车票 出发地 目的地\n"
        "• 示例：/火车票 厦门 上海\n\n"
        "说明：\n"
        "- 当前 `astrbot_all_char` 版本仅实现命令查询，不启用自然语言识别；\n"
        "- 返回格式可通过配置 `train_default_format` 设置为 text/image。"
    )
    yield event.plain_result(text)

