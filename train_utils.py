import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent


OFFICIAL_12306_BASE_URL = "https://kyfw.12306.cn/otn"
LEGACY_TRAIN_API_HOST = "api.lolimi.cn"
DEFAULT_QUERY_TIMEOUT = aiohttp.ClientTimeout(total=20)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
DATE_PATTERN = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
DATE_CN_PATTERN = re.compile(r"^(?:(\d{4})年)?(\d{1,2})月(\d{1,2})(?:日|号)?$")
DATE_MD_PATTERN = re.compile(r"^(\d{1,2})[./-](\d{1,2})$")
RELATIVE_DATE_OFFSETS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
SEAT_INDEXES = (
    ("商务座", 32),
    ("特等座", 25),
    ("一等座", 31),
    ("二等座", 30),
    ("高级软卧", 21),
    ("软卧", 23),
    ("动卧", 33),
    ("硬卧", 28),
    ("硬座", 29),
    ("无座", 26),
)

_station_code_cache: dict[str, str] | None = None
_station_cache_lock = asyncio.Lock()


def _format_seat_text(seat: dict, compact: bool = False) -> str:
    name = str(seat.get("SeatName", "")).strip()
    price = str(seat.get("SeatPrice", "")).strip()
    residue = str(seat.get("Seatresidue", "")).strip()
    if not name:
        return ""
    if residue and price:
        return f"{name}{price}元余{residue}" if compact else f"{name}-{price}元 余{residue}"
    if residue:
        return f"{name}余{residue}" if compact else f"{name} 余{residue}"
    return name


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
            seat_text for seat in seats if (seat_text := _format_seat_text(seat))
        )
        lines.append(f"【{train_no}】{start}→{end}")
        lines.append(f"   {depart}—{arrive}  全程{duration}")
        lines.append(f"   {seat_str or '暂无席位信息'}")
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


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text or " ", font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return [""]

    if _text_size(draw, text, font)[0] <= max_width:
        return [text]

    lines: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if current and _text_size(draw, candidate, font)[0] > max_width:
            lines.append(current)
            current = ch
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


def _seat_palette(seat_text: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    if "有" in seat_text or "充足" in seat_text:
        return (227, 252, 239), (39, 117, 63)
    if "无" in seat_text:
        return (246, 247, 249), (116, 126, 139)
    if any(token in seat_text for token in ("1", "2", "3", "4", "5")):
        return (255, 243, 220), (150, 92, 24)
    return (236, 244, 255), (42, 91, 171)


def _draw_rounded_rectangle(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], fill, outline=None, radius: int = 18):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline)


def _draw_train_images(api_data: dict) -> list[str]:
    data_list = api_data.get("data") or []
    go = api_data.get("go", "")
    to = api_data.get("to", "")
    date = api_data.get("date", "")
    time_str = api_data.get("time", "")
    if not data_list:
        return []

    title_font = _get_chinese_font(30)
    subtitle_font = _get_chinese_font(16)
    section_font = _get_chinese_font(15)
    badge_font = _get_chinese_font(18)
    body_font = _get_chinese_font(17)
    small_font = _get_chinese_font(14)
    if not all((title_font, subtitle_font, section_font, badge_font, body_font, small_font)):
        logger.warning("未找到中文字体，图片模式将退回文本")
        return []

    try:
        width = 1480
        margin = 34
        header_height = 166
        footer_height = 52
        column_gap = 24
        columns = 2
        card_gap = 18
        card_width = (width - margin * 2 - column_gap) // columns
        temp_img = Image.new("RGB", (width, 400), (245, 240, 232))
        temp_draw = ImageDraw.Draw(temp_img)
        file_paths: list[str] = []

        def build_chip_rows(
            seat_texts: list[str], max_width: int
        ) -> tuple[list[list[tuple[str, tuple[int, int, int], tuple[int, int, int], int, int]]], int]:
            rows: list[list[tuple[str, tuple[int, int, int], tuple[int, int, int], int, int]]] = []
            current_row: list[tuple[str, tuple[int, int, int], tuple[int, int, int], int, int]] = []
            current_width = 0
            row_gap = 10
            chip_gap = 10

            for seat_text in seat_texts[:8]:
                fill_color, text_color = _seat_palette(seat_text)
                text_width, text_height = _text_size(temp_draw, seat_text, small_font)
                chip_width = text_width + 28
                chip_height = text_height + 14
                extra_width = chip_width if not current_row else chip_gap + chip_width

                if current_row and current_width + extra_width > max_width:
                    rows.append(current_row)
                    current_row = []
                    current_width = 0
                    extra_width = chip_width

                current_row.append((seat_text, fill_color, text_color, chip_width, chip_height))
                current_width += extra_width

            if current_row:
                rows.append(current_row)

            if not rows:
                fill_color, text_color = _seat_palette("暂无席位信息")
                text_width, text_height = _text_size(temp_draw, "暂无席位信息", small_font)
                rows = [[("暂无席位信息", fill_color, text_color, text_width + 28, text_height + 14)]]

            rows = rows[:3]
            total_height = sum(max(chip[4] for chip in row) for row in rows) + row_gap * max(0, len(rows) - 1)
            return rows, total_height

        layouts: list[tuple[dict, int, list[list[tuple[str, tuple[int, int, int], tuple[int, int, int], int, int]]]]] = []
        content_inner_width = card_width - 52
        for item in data_list:
            seats = item.get("SeatList") or []
            seat_texts = [_format_seat_text(seat) for seat in seats if _format_seat_text(seat)]
            chip_rows, chip_height = build_chip_rows(seat_texts, content_inner_width)
            card_height = 156 + chip_height
            layouts.append((item, card_height, chip_rows))

        row_heights: list[int] = []
        for index in range(0, len(layouts), columns):
            row_items = layouts[index : index + columns]
            row_heights.append(max(item[1] for item in row_items))

        body_height = sum(row_heights) + card_gap * max(0, len(row_heights) - 1)
        height = header_height + body_height + footer_height + margin
        img = Image.new("RGB", (width, height), (245, 240, 232))
        draw = ImageDraw.Draw(img)

        draw.rectangle((0, 0, width, height), fill=(245, 240, 232))
        draw.rectangle((0, 0, width, 130), fill=(28, 63, 121))
        draw.rectangle((0, 98, width, 176), fill=(45, 87, 158))
        draw.ellipse((width - 300, -100, width + 120, 210), fill=(66, 112, 191))
        draw.ellipse((-120, 54, 220, 230), fill=(231, 220, 193))

        draw.text((margin, 28), f"{go} → {to}", fill=(255, 255, 255), font=title_font)
        draw.text(
            (margin, 86),
            f"出行日期：{date}    更新时间：{time_str or '实时查询'}",
            fill=(230, 238, 255),
            font=subtitle_font,
        )

        count_badge = f"共 {len(data_list)} 趟车次"
        badge_w, badge_h = _text_size(draw, count_badge, subtitle_font)
        badge_box = (width - margin - badge_w - 40, 30, width - margin, 30 + badge_h + 20)
        _draw_rounded_rectangle(draw, badge_box, fill=(239, 244, 255), radius=16)
        draw.text((badge_box[0] + 18, badge_box[1] + 9), count_badge, fill=(33, 76, 147), font=subtitle_font)

        y = header_height
        layout_index = 0
        for row_height in row_heights:
            for col in range(columns):
                if layout_index >= len(layouts):
                    break
                item, card_height, chip_rows = layouts[layout_index]
                left = margin + col * (card_width + column_gap)
                right = left + card_width
                top = y
                bottom = y + card_height

                _draw_rounded_rectangle(
                    draw,
                    (left + 4, top + 6, right + 4, bottom + 6),
                    fill=(229, 223, 211),
                    radius=24,
                )
                _draw_rounded_rectangle(
                    draw,
                    (left, top, right, bottom),
                    fill=(255, 252, 247),
                    outline=(226, 221, 213),
                    radius=24,
                )
                draw.rounded_rectangle((left, top, left + 10, bottom), radius=12, fill=(201, 147, 68))

                badge_box = (left + 20, top + 18, left + 138, top + 60)
                _draw_rounded_rectangle(draw, badge_box, fill=(224, 235, 255), radius=14)
                draw.text((badge_box[0] + 16, badge_box[1] + 9), item.get("TrainNumber", ""), fill=(41, 88, 174), font=badge_font)

                draw.text(
                    (left + 160, top + 22),
                    f"{item.get('start', '')} → {item.get('end', '')}",
                    fill=(35, 39, 46),
                    font=body_font,
                )

                metric_y = top + 78
                metric_block_width = 158
                metrics = [
                    ("出发", item.get("DepartTime", "")),
                    ("到达", item.get("ArriveTime", "")),
                    ("历时", item.get("TimeDifference", "")),
                ]
                for idx, (label, value) in enumerate(metrics):
                    metric_x = left + 22 + idx * metric_block_width
                    draw.text((metric_x, metric_y), label, fill=(131, 119, 104), font=small_font)
                    draw.text((metric_x, metric_y + 24), str(value), fill=(45, 49, 56), font=body_font)

                draw.text((left + 22, top + 126), "席位与余票", fill=(121, 108, 95), font=section_font)
                chip_y = top + 154
                chip_gap = 10
                row_gap = 10
                for row in chip_rows:
                    chip_x = left + 22
                    row_height_actual = 0
                    for seat_text, fill_color, text_color, chip_width, chip_height in row:
                        box = (chip_x, chip_y, chip_x + chip_width, chip_y + chip_height)
                        _draw_rounded_rectangle(draw, box, fill=fill_color, radius=12)
                        draw.text((box[0] + 13, box[1] + 6), seat_text, fill=text_color, font=small_font)
                        chip_x += chip_width + chip_gap
                        row_height_actual = max(row_height_actual, chip_height)
                    chip_y += row_height_actual + row_gap

                layout_index += 1

            y += row_height + card_gap

        footer_text = "官方 12306 实时查询结果"
        draw.text((margin, height - footer_height), footer_text, fill=(113, 104, 95), font=section_font)

        fp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        img.save(fp.name)
        file_paths.append(fp.name)

        return file_paths
    except Exception as e:
        logger.error("生成火车票图片失败: %s", e)
        return []


def _resolve_train_api_base(api_url: str | None) -> str:
    candidate = (api_url or "").strip().rstrip("/")
    if not candidate or LEGACY_TRAIN_API_HOST in candidate:
        if candidate and LEGACY_TRAIN_API_HOST in candidate:
            logger.warning("检测到旧火车票接口地址，自动切换到官方 12306 接口: %s", candidate)
        return OFFICIAL_12306_BASE_URL

    for suffix in (
        "/leftTicket/queryG",
        "/leftTicket/queryA",
        "/leftTicket/init",
        "/resources/js/framework/station_name.js",
    ):
        if candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)]
            break

    if "12306" not in candidate:
        logger.warning("train_api_url 不是 12306 地址，自动切换到官方接口: %s", candidate)
        return OFFICIAL_12306_BASE_URL
    return candidate.rstrip("/")


def _normalize_station_candidates(name: str) -> list[str]:
    cleaned = re.sub(r"\s+", "", (name or "").strip())
    if not cleaned:
        return []
    candidates = [cleaned]
    if cleaned.endswith("站") and len(cleaned) > 1:
        candidates.append(cleaned[:-1])
    return candidates


def _strip_date_expr(date_expr: str) -> str:
    return re.sub(r"\s+", "", (date_expr or "").strip().strip("，,。；;"))


def _normalize_month_day(month: int, day: int, now: datetime) -> datetime:
    # 从当前年往后找最近一个合法且不早于今天的日期，兼容 2月29日 这类跨闰年场景
    for year in range(now.year, now.year + 8):
        try:
            parsed = datetime(year, month, day)
        except ValueError:
            continue
        if parsed.date() >= now.date():
            return parsed
    raise ValueError("日期格式不正确，请使用有效日期。")


def _looks_like_date_expr(date_expr: str | None) -> bool:
    cleaned = _strip_date_expr(date_expr or "")
    if not cleaned:
        return False
    if cleaned in RELATIVE_DATE_OFFSETS:
        return True
    return bool(
        DATE_PATTERN.fullmatch(cleaned)
        or DATE_CN_PATTERN.fullmatch(cleaned)
        or DATE_MD_PATTERN.fullmatch(cleaned)
    )


def _normalize_date_expr(date_expr: str | None) -> str:
    if not date_expr:
        return datetime.now().strftime("%Y-%m-%d")

    cleaned = _strip_date_expr(date_expr)
    now = datetime.now()
    if cleaned in RELATIVE_DATE_OFFSETS:
        return (now + timedelta(days=RELATIVE_DATE_OFFSETS[cleaned])).strftime("%Y-%m-%d")

    if DATE_PATTERN.fullmatch(cleaned):
        try:
            parsed = datetime.strptime(cleaned, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("日期格式不正确，请使用有效日期。") from exc
        return parsed.strftime("%Y-%m-%d")

    matched_cn = DATE_CN_PATTERN.fullmatch(cleaned)
    if matched_cn:
        year_text, month_text, day_text = matched_cn.groups()
        try:
            month, day = int(month_text), int(day_text)
            if year_text:
                parsed = datetime(int(year_text), month, day)
            else:
                parsed = _normalize_month_day(month, day, now)
        except ValueError as exc:
            raise ValueError("日期格式不正确，请使用有效日期。") from exc
        return parsed.strftime("%Y-%m-%d")

    matched_md = DATE_MD_PATTERN.fullmatch(cleaned)
    if matched_md:
        try:
            month, day = int(matched_md.group(1)), int(matched_md.group(2))
            parsed = _normalize_month_day(month, day, now)
        except ValueError as exc:
            raise ValueError("日期格式不正确，请使用有效日期。") from exc
        return parsed.strftime("%Y-%m-%d")

    raise ValueError("日期仅支持 `YYYY-MM-DD`、`YYYY年M月D日`、`M月D日/号`、`今天`、`明天`、`后天`、`大后天`。")


def _normalize_date_token_for_guessing(date_expr: str | None) -> str:
    return _strip_date_expr(date_expr or "")


def _parse_date_first_command(msg_parts: list[str]) -> tuple[str, str, str] | None:
    if len(msg_parts) < 4:
        return None
    date_token = _normalize_date_token_for_guessing(msg_parts[1])
    if not _looks_like_date_expr(date_token):
        return None
    try:
        travel_date = _normalize_date_expr(date_token)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    departure, arrival = msg_parts[2].strip(), msg_parts[3].strip()
    return departure, arrival, travel_date


async def _load_station_codes(session: aiohttp.ClientSession, base_url: str) -> dict[str, str]:
    global _station_code_cache

    if _station_code_cache is not None:
        return _station_code_cache

    async with _station_cache_lock:
        if _station_code_cache is not None:
            return _station_code_cache

        station_url = f"{base_url}/resources/js/framework/station_name.js"
        async with session.get(station_url, timeout=DEFAULT_QUERY_TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError(f"12306 站点表请求失败，状态码 {resp.status}")
            raw = await resp.text()

        station_map: dict[str, str] = {}
        for item in raw.split("@"):
            if not item:
                continue
            fields = item.split("|")
            if len(fields) < 3:
                continue
            name = fields[1].strip()
            code = fields[2].strip()
            if name and code and name not in station_map:
                station_map[name] = code

        if not station_map:
            raise RuntimeError("12306 站点表解析失败")

        _station_code_cache = station_map
        return _station_code_cache


def _pick_station_code(station_codes: dict[str, str], name: str, kind: str) -> str:
    for candidate in _normalize_station_candidates(name):
        code = station_codes.get(candidate)
        if code:
            return code
    raise ValueError(f"未识别{kind}“{name}”，请使用 12306 站点名，例如：上海、上海虹桥、北京南。")


def _normalize_seat_list(fields: list[str]) -> list[dict]:
    seats: list[dict] = []
    sold_out: list[dict] = []
    for seat_name, index in SEAT_INDEXES:
        if len(fields) <= index:
            continue
        residue = fields[index].strip()
        if not residue or residue == "--":
            continue
        seat_info = {"SeatName": seat_name, "SeatPrice": "", "Seatresidue": residue}
        if residue == "无":
            sold_out.append(seat_info)
        else:
            seats.append(seat_info)

    if seats:
        return seats
    if sold_out:
        return sold_out[:3]
    return [{"SeatName": "暂无可售席位", "SeatPrice": "", "Seatresidue": ""}]


def _normalize_duration(duration: str) -> str:
    value = duration.strip()
    if not value or ":" not in value:
        return value
    hours, minutes = value.split(":", 1)
    hours_i = int(hours)
    minutes_i = int(minutes)
    if hours_i and minutes_i:
        return f"{hours_i}小时{minutes_i}分"
    if hours_i:
        return f"{hours_i}小时"
    return f"{minutes_i}分"


def _normalize_12306_payload(
    payload: dict,
    departure: str,
    arrival: str,
    travel_date: str,
    from_code: str,
    to_code: str,
) -> dict:
    data = payload.get("data") or {}
    station_map = data.get("map") or {}
    result_list = data.get("result") or []
    normalized_rows = []

    for row in result_list:
        fields = row.split("|")
        if len(fields) < 34:
            continue
        normalized_rows.append(
            {
                "TrainNumber": fields[3],
                "start": station_map.get(fields[6], departure),
                "end": station_map.get(fields[7], arrival),
                "DepartTime": fields[8],
                "ArriveTime": fields[9],
                "TimeDifference": _normalize_duration(fields[10]),
                "SeatList": _normalize_seat_list(fields),
            }
        )

    return {
        "go": station_map.get(from_code, departure),
        "to": station_map.get(to_code, arrival),
        "date": travel_date,
        "time": datetime.now().strftime("%H:%M:%S"),
        "data": normalized_rows,
    }


async def _fetch_trains(api_url: str, departure: str, arrival: str, travel_date: str | None = None) -> dict | None:
    base_url = _resolve_train_api_base(api_url)
    query_date = _normalize_date_expr(travel_date)
    referer = f"{base_url}/leftTicket/init?linktypeid=dc"

    try:
        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            async with session.get(referer, timeout=DEFAULT_QUERY_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning("12306 初始化页面状态码: %s", resp.status)
                    return None

            station_codes = await _load_station_codes(session, base_url)
            from_code = _pick_station_code(station_codes, departure, "出发地")
            to_code = _pick_station_code(station_codes, arrival, "目的地")

            query_url = f"{base_url}/leftTicket/queryG"
            params = {
                "leftTicketDTO.train_date": query_date,
                "leftTicketDTO.from_station": from_code,
                "leftTicketDTO.to_station": to_code,
                "purpose_codes": "ADULT",
            }
            headers = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"}
            async with session.get(query_url, params=params, headers=headers, timeout=DEFAULT_QUERY_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning("12306 余票查询状态码: %s", resp.status)
                    return None

                raw_text = (await resp.text()).lstrip("\ufeff")
                if raw_text.lstrip().startswith("<"):
                    logger.warning("12306 余票查询返回了非 JSON 内容")
                    return None

                data = json.loads(raw_text)
                if not data.get("status"):
                    logger.warning("12306 余票查询返回异常: %s", data.get("messages"))
                    return None
                return _normalize_12306_payload(data, departure, arrival, query_date, from_code, to_code)
    except asyncio.TimeoutError:
        logger.error("12306 火车票查询超时")
        return None
    except ValueError:
        raise
    except Exception as e:
        logger.error("12306 火车票查询异常: %s", e)
        return None


def _parse_command_args(message: str) -> tuple[str, str, str]:
    msg_parts = message.strip().split()
    if len(msg_parts) < 3:
        raise ValueError(
            "用法：/火车票 出发地 目的地 [日期]\n"
            "示例：/火车票 厦门 上海\n"
            "示例：/火车票 厦门 上海 明天\n"
            "示例：/火车票 明天 厦门 上海"
        )

    parsed_date_first = _parse_date_first_command(msg_parts)
    if parsed_date_first is not None:
        return parsed_date_first

    departure, arrival = msg_parts[1].strip(), msg_parts[2].strip()
    travel_date = _normalize_date_expr(msg_parts[3]) if len(msg_parts) >= 4 else _normalize_date_expr(None)
    return departure, arrival, travel_date


async def _do_query(
    api_url: str,
    default_format: str,
    departure: str,
    arrival: str,
    travel_date: str,
    event: AstrMessageEvent,
):
    image_paths: list[str] = []
    try:
        try:
            api_data = await _fetch_trains(api_url, departure, arrival, travel_date)
        except ValueError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        if api_data is None:
            yield event.plain_result("❌ 查询失败或无数据，请检查出发地/目的地或稍后重试。")
            return
        fmt = str(default_format).lower()
        if fmt == "image":
            image_paths = _draw_train_images(api_data)
            if image_paths:
                for image_path in image_paths:
                    yield event.image_result(image_path)
            else:
                text = _format_train_text(api_data)
                yield event.plain_result(f"🚆 火车票查询\n\n{text}")
        else:
            text = _format_train_text(api_data)
            yield event.plain_result(f"🚆 火车票查询\n\n{text}")
    finally:
        for image_path in image_paths:
            try:
                os.unlink(image_path)
            except Exception:
                pass


async def handle_train_command(event: AstrMessageEvent, config: AstrBotConfig):
    """
    火车票查询命令入口（命令模式，未启用自然语言识别）。
    """
    try:
        departure, arrival, travel_date = _parse_command_args(event.get_message_str())
    except ValueError as exc:
        yield event.plain_result(f"❌ {exc}\n\n（当前整合版仅支持命令模式）")
        return

    api_url = (getattr(config, "train_api_url", None) or OFFICIAL_12306_BASE_URL).rstrip("/")
    default_format = getattr(config, "train_default_format", "text")
    async for r in _do_query(api_url, default_format, departure, arrival, travel_date, event):
        yield r


async def handle_train_help(event: AstrMessageEvent):
    text = (
        "🚆 火车票查询\n\n"
        "【命令】\n"
        "• /火车票 出发地 目的地 [日期]\n"
        "• /火车票 [日期] 出发地 目的地\n"
        "• 示例：/火车票 厦门 上海\n"
        "• 示例：/火车票 厦门 上海 明天\n"
        "• 示例：/火车票 2026-04-02 厦门 上海\n\n"
        "说明：\n"
        "- 日期支持 `YYYY-MM-DD`、`YYYY年M月D日`、`M月D日/号`、`今天`、`明天`、`后天`、`大后天`，省略时默认查今天；\n"
        "- 当前 `astrbot_all_char` 版本仅实现命令查询，不启用自然语言识别；\n"
        "- 返回格式可通过配置 `train_default_format` 设置为 text/image。"
    )
    yield event.plain_result(text)
