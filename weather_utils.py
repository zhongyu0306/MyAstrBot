import asyncio
import json
import os
import re
import tempfile
from datetime import datetime
from urllib.parse import quote

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent

from .passive_memory_utils import record_passive_habit


LEGACY_WEATHER_API_URL = "https://api.nycnm.cn/API/weather.php"
DEFAULT_WEATHER_FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_WEATHER_GEOCODING_API_URL = "https://geocoding-api.open-meteo.com/v1/search"
MAX_FORECAST_DAYS = 7
WEEKDAY_NAMES_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
RELATIVE_WEATHER_DAY_OFFSETS = {
    "今天": 0,
    "今日": 0,
    "明天": 1,
    "明日": 1,
    "后天": 2,
    "大后天": 3,
}
WEEKDAY_CHAR_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
WEEKDAY_EXPR_PATTERN = re.compile(r"^(?:(这|本|下下|下))?(?:周|星期|礼拜)([一二三四五六日天])$")
DAY_RANGE_PATTERN = re.compile(r"^(?:未来)?([1-7])天$")
PURE_CJK_PLACE_PATTERN = re.compile(r"^[\u4e00-\u9fff·]{2,20}$")
WEATHER_CODE_MAP = {
    0: "晴",
    1: "基本晴朗",
    2: "少云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "强雷暴伴大冰雹",
}
GLOBAL_CITY_ALIASES = {
    "东京": ("Tokyo", "JP"),
    "大阪": ("Osaka", "JP"),
    "京都": ("Kyoto", "JP"),
    "首尔": ("Seoul", "KR"),
    "釜山": ("Busan", "KR"),
    "新加坡": ("Singapore", "SG"),
    "曼谷": ("Bangkok", "TH"),
    "吉隆坡": ("Kuala Lumpur", "MY"),
    "河内": ("Hanoi", "VN"),
    "胡志明市": ("Ho Chi Minh City", "VN"),
    "雅加达": ("Jakarta", "ID"),
    "迪拜": ("Dubai", "AE"),
    "伊斯坦布尔": ("Istanbul", "TR"),
    "伦敦": ("London", "GB"),
    "巴黎": ("Paris", "FR"),
    "柏林": ("Berlin", "DE"),
    "罗马": ("Rome", "IT"),
    "马德里": ("Madrid", "ES"),
    "阿姆斯特丹": ("Amsterdam", "NL"),
    "莫斯科": ("Moscow", "RU"),
    "纽约": ("New York", "US"),
    "洛杉矶": ("Los Angeles", "US"),
    "旧金山": ("San Francisco", "US"),
    "华盛顿": ("Washington", "US"),
    "西雅图": ("Seattle", "US"),
    "芝加哥": ("Chicago", "US"),
    "波士顿": ("Boston", "US"),
    "温哥华": ("Vancouver", "CA"),
    "多伦多": ("Toronto", "CA"),
    "悉尼": ("Sydney", "AU"),
    "墨尔本": ("Melbourne", "AU"),
    "奥克兰": ("Auckland", "NZ"),
}
GEOCODING_FEATURE_SCORES = {
    "PPLC": 120,
    "PPLA": 110,
    "PPLA2": 100,
    "PPLA3": 95,
    "PPLA4": 90,
    "PPLG": 88,
    "PPL": 70,
    "AIRP": 20,
}


def _weather_text_from_code(code) -> str:
    try:
        return WEATHER_CODE_MAP.get(int(code), f"天气代码 {code}")
    except Exception:
        return "天气未知"


def _format_number(value, digits: int = 1) -> str | None:
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    text = f"{num:.{digits}f}"
    return text.rstrip("0").rstrip(".")


def _format_temperature(value) -> str | None:
    text = _format_number(value)
    return f"{text}°C" if text is not None else None


def _format_speed(value) -> str | None:
    text = _format_number(value)
    return f"{text} km/h" if text is not None else None


def _format_precipitation(value) -> str | None:
    text = _format_number(value)
    return f"{text} mm" if text is not None else None


def _format_percentage(value) -> str | None:
    text = _format_number(value, digits=0)
    return f"{text}%" if text is not None else None


def _format_clock_text(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    if "T" in text:
        text = text.split("T", 1)[1]
    return text[:5] if len(text) >= 5 else text


def _format_weekday(date_text: str) -> str | None:
    try:
        dt = datetime.strptime(date_text, "%Y-%m-%d")
        return WEEKDAY_NAMES_ZH[dt.weekday()]
    except Exception:
        return None


def _format_wind_direction(value) -> str | None:
    if value is None:
        return None
    try:
        degree = float(value) % 360
    except Exception:
        return None
    directions = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
    index = int((degree + 22.5) // 45) % 8
    return directions[index]


def _pick(seq, index: int):
    if not isinstance(seq, list) or index >= len(seq):
        return None
    return seq[index]


def _format_resolved_place(location: dict) -> str:
    pieces: list[str] = []
    for key in ("name", "admin1", "country"):
        value = str(location.get(key) or "").strip()
        if value and value not in pieces:
            pieces.append(value)
    return " / ".join(pieces)


def _normalize_days(days: int | None) -> int:
    if days is None:
        return 1
    try:
        return max(1, min(int(days), MAX_FORECAST_DAYS))
    except Exception:
        return 1


def _parse_city_and_country_hint(city: str) -> tuple[str, str | None]:
    raw = (city or "").strip().replace("，", ",")
    if "," not in raw:
        alias = GLOBAL_CITY_ALIASES.get(raw)
        if alias:
            return alias
        return raw, None
    name_part, _, country_part = raw.rpartition(",")
    country_code = country_part.strip().upper()
    if name_part.strip() and len(country_code) == 2 and country_code.isalpha():
        alias = GLOBAL_CITY_ALIASES.get(name_part.strip())
        if alias:
            return alias[0], country_code
        return name_part.strip(), country_code
    return raw, None


def _normalize_geocoding_name(text: str | None) -> str:
    normalized = str(text or "").strip().lower().replace(" ", "")
    for suffix in ("市", "省", "区", "县", "州", "特别行政区"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def _build_geocoding_query_candidates(search_name: str, country_code: str | None) -> list[str]:
    candidates: list[str] = []

    def add_candidate(value: str | None):
        cleaned = str(value or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    add_candidate(search_name)
    if PURE_CJK_PLACE_PATTERN.fullmatch(search_name) and not search_name.endswith(("市", "区", "县", "州")):
        add_candidate(f"{search_name}市")
    if country_code == "CN" and PURE_CJK_PLACE_PATTERN.fullmatch(search_name) and not search_name.endswith(("市", "区", "县", "州")):
        add_candidate(f"{search_name}城区")

    return candidates


def _score_geocoding_result(location: dict, requested_name: str, country_code: str | None) -> tuple[int, int]:
    score = 0
    population_score = 0

    if country_code and str(location.get("country_code") or "").upper() == country_code.upper():
        score += 40

    requested_norm = _normalize_geocoding_name(requested_name)
    result_name_norm = _normalize_geocoding_name(location.get("name"))
    admin2_norm = _normalize_geocoding_name(location.get("admin2"))
    admin1_norm = _normalize_geocoding_name(location.get("admin1"))

    if result_name_norm == requested_norm:
        score += 80
    elif requested_norm and result_name_norm.startswith(requested_norm):
        score += 50

    if admin2_norm == requested_norm:
        score += 35
    if admin1_norm == requested_norm:
        score += 10

    feature_code = str(location.get("feature_code") or "").upper()
    score += GEOCODING_FEATURE_SCORES.get(feature_code, 0)

    try:
        population = int(location.get("population") or 0)
    except Exception:
        population = 0
    population_score = min(population // 100000, 60)
    score += population_score

    return score, population_score


async def _pick_best_geocoding_result(
    session: aiohttp.ClientSession,
    geocoding_api_url: str,
    search_name: str,
    country_code: str | None,
) -> dict | None:
    candidate_queries = _build_geocoding_query_candidates(search_name, country_code)
    collected: list[tuple[int, int, dict, str]] = []
    seen_ids: set[str] = set()

    for query_name in candidate_queries:
        geocoding_params = {
            "name": query_name,
            "count": 10,
            "language": "zh",
            "format": "json",
        }
        if country_code:
            geocoding_params["countryCode"] = country_code

        geocoding = await _fetch_json(session, geocoding_api_url, geocoding_params, f"天气地理编码({query_name})")
        if not geocoding:
            continue

        for location in geocoding.get("results") or []:
            dedupe_key = str(location.get("id") or f"{location.get('latitude')},{location.get('longitude')}")
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            score, population_score = _score_geocoding_result(location, search_name, country_code)
            collected.append((score, population_score, location, query_name))

    if not collected:
        logger.info("天气地理编码未找到匹配城市: %s", search_name)
        return None

    collected.sort(
        key=lambda item: (
            item[0],
            item[1],
            str(item[2].get("feature_code") or ""),
            str(item[2].get("name") or ""),
        ),
        reverse=True,
    )
    best_score, _, best_location, matched_query = collected[0]
    logger.info(
        "天气地理编码已选定地点: query=%s matched_query=%s place=%s/%s/%s feature=%s score=%s",
        search_name,
        matched_query,
        best_location.get("name"),
        best_location.get("admin1"),
        best_location.get("country"),
        best_location.get("feature_code"),
        best_score,
    )
    return best_location


def _strip_weather_suffix(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) > 2 and cleaned.endswith("天气"):
        return cleaned[:-2].strip()
    return cleaned


def _parse_weather_when_text(when_text: str | None) -> tuple[int | None, str | None, int | None]:
    cleaned = _strip_weather_suffix((when_text or "").strip())
    if not cleaned:
        return None, None, None

    if cleaned in RELATIVE_WEATHER_DAY_OFFSETS:
        offset = RELATIVE_WEATHER_DAY_OFFSETS[cleaned]
        return offset, cleaned, None

    matched_range = DAY_RANGE_PATTERN.fullmatch(cleaned)
    if matched_range:
        return None, None, max(1, min(int(matched_range.group(1)), MAX_FORECAST_DAYS))

    matched_weekday = WEEKDAY_EXPR_PATTERN.fullmatch(cleaned)
    if matched_weekday:
        prefix, weekday_char = matched_weekday.groups()
        target_weekday = WEEKDAY_CHAR_MAP[weekday_char]
        today_weekday = datetime.now().weekday()
        delta = target_weekday - today_weekday
        if delta < 0:
            delta += 7
        if prefix == "下":
            delta += 7
        elif prefix == "下下":
            delta += 14
        return delta, cleaned.replace("星期天", "星期日").replace("礼拜天", "礼拜日"), None

    return None, None, None


def _extract_city_and_when(city_text: str) -> tuple[str, str | None]:
    cleaned = _strip_weather_suffix(city_text)
    if not cleaned:
        return "", None

    tokens = cleaned.split()
    if len(tokens) >= 2:
        last_token = tokens[-1].strip()
        day_offset, _, range_days = _parse_weather_when_text(last_token)
        if day_offset is not None or range_days is not None:
            return " ".join(tokens[:-1]).strip(), last_token

    for token in sorted(RELATIVE_WEATHER_DAY_OFFSETS.keys(), key=len, reverse=True):
        if cleaned.endswith(token) and len(cleaned) > len(token):
            return cleaned[:-len(token)].strip(), token

    matched_weekday = re.search(r"^(.*?)(?:(?:这|本|下下|下)?(?:周|星期|礼拜)[一二三四五六日天])$", cleaned)
    if matched_weekday:
        city = matched_weekday.group(1).strip()
        if city:
            return city, cleaned[len(city):].strip()

    matched_range = re.search(r"^(.*?)(?:未来)?([1-7])天$", cleaned)
    if matched_range:
        city = matched_range.group(1).strip()
        if city:
            return city, cleaned[len(city):].strip()

    return cleaned, None


def _resolve_weather_query(
    city: str,
    days: int | None = None,
    when_text: str | None = None,
) -> tuple[dict[str, int | str | None] | None, str | None]:
    raw_city = _strip_weather_suffix(city)
    inline_when = None
    if not when_text:
        raw_city, inline_when = _extract_city_and_when(raw_city)

    final_when = _strip_weather_suffix((when_text or inline_when or "").strip()) or None
    if not raw_city:
        return None, "请提供要查询的城市名称，例如 北京。"

    if final_when:
        day_offset, day_label, range_days = _parse_weather_when_text(final_when)
        if range_days is not None:
            days = range_days
        elif day_offset is not None and day_label:
            if day_offset >= MAX_FORECAST_DAYS:
                return None, f"天气预报目前最多支持未来 {MAX_FORECAST_DAYS} 天，暂时不能直接查询“{day_label}”。"
            return (
                {
                    "city": raw_city,
                    "forecast_days": day_offset + 1,
                    "target_day_offset": day_offset,
                    "target_day_label": day_label,
                },
                None,
            )
        else:
            return None, (
                "暂时无法识别这个天气时间表达。\n"
                "可用示例：今天、明天、后天、大后天、周一、下周三、未来5天。"
            )

    normalized_days = 1
    if days is not None:
        try:
            normalized_days = max(1, min(int(days), MAX_FORECAST_DAYS))
        except Exception:
            normalized_days = 1

    return (
        {
            "city": raw_city,
            "forecast_days": normalized_days,
            "target_day_offset": None,
            "target_day_label": None,
        },
        None,
    )


def _is_legacy_default_api(api_url: str) -> bool:
    return "api.nycnm.cn" in (api_url or "").strip().lower()


def _should_use_open_meteo(api_url: str, geocoding_api_url: str | None = None) -> bool:
    if "open-meteo.com" in (geocoding_api_url or "").strip().lower():
        return True
    normalized_api_url = (api_url or "").strip().lower()
    if not normalized_api_url:
        return True
    if _is_legacy_default_api(normalized_api_url):
        return True
    return "open-meteo.com" in normalized_api_url


def get_weather_runtime_config(config: AstrBotConfig) -> dict[str, str]:
    legacy_api_url = _get_weather_config(config, "weather_api_url", "").strip()
    forecast_api_url = _get_weather_config(config, "weather_forecast_api_url", "").strip()
    geocoding_api_url = _get_weather_config(config, "weather_geocoding_api_url", "").strip()
    default_format = (_get_weather_config(config, "weather_default_format", "text") or "text").strip().lower()
    if default_format not in {"text", "image"}:
        default_format = "text"

    custom_legacy_api = bool(
        legacy_api_url
        and not _is_legacy_default_api(legacy_api_url)
        and "open-meteo.com" not in legacy_api_url.lower()
    )

    if custom_legacy_api:
        forecast_api_url = legacy_api_url
        geocoding_api_url = ""
    elif not forecast_api_url:
        if not legacy_api_url or _is_legacy_default_api(legacy_api_url):
            forecast_api_url = DEFAULT_WEATHER_FORECAST_API_URL
        else:
            forecast_api_url = legacy_api_url

    use_open_meteo = _should_use_open_meteo(forecast_api_url, geocoding_api_url)
    if use_open_meteo and not geocoding_api_url:
        geocoding_api_url = DEFAULT_WEATHER_GEOCODING_API_URL

    return {
        "api_url": forecast_api_url,
        "geocoding_api_url": geocoding_api_url,
        "default_format": default_format,
        "provider": "open-meteo" if use_open_meteo else "legacy",
    }


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict,
    label: str,
) -> dict | None:
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
            raw = await response.text()
            if response.status != 200:
                logger.warning("%s 请求失败: status=%s body=%s", label, response.status, (raw or "")[:500])
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("%s 返回的不是合法 JSON: %s", label, (raw or "")[:500])
                return None
    except asyncio.TimeoutError:
        logger.error("%s 请求超时", label)
        return None
    except Exception as exc:
        logger.error("%s 请求异常: %s", label, exc)
        return None


def _build_open_meteo_daily_line(daily: dict, index: int) -> str | None:
    date_text = _pick(daily.get("time"), index)
    if not date_text:
        return None

    header = date_text
    weekday = _format_weekday(date_text)
    if weekday:
        header = f"{header} {weekday}"

    line_parts = [_weather_text_from_code(_pick(daily.get("weather_code"), index))]

    temp_min = _format_temperature(_pick(daily.get("temperature_2m_min"), index))
    temp_max = _format_temperature(_pick(daily.get("temperature_2m_max"), index))
    if temp_min or temp_max:
        line_parts.append(f"{temp_min or '--'} ~ {temp_max or '--'}")

    feel_min = _format_temperature(_pick(daily.get("apparent_temperature_min"), index))
    feel_max = _format_temperature(_pick(daily.get("apparent_temperature_max"), index))
    if feel_min or feel_max:
        line_parts.append(f"体感 {feel_min or '--'} ~ {feel_max or '--'}")

    rain_prob = _format_percentage(_pick(daily.get("precipitation_probability_max"), index))
    if rain_prob:
        line_parts.append(f"降水概率 {rain_prob}")

    rain_sum = _format_precipitation(_pick(daily.get("precipitation_sum"), index))
    if rain_sum:
        line_parts.append(f"降水量 {rain_sum}")

    wind_speed = _format_speed(_pick(daily.get("wind_speed_10m_max"), index))
    if wind_speed:
        wind_text = f"最大风速 {wind_speed}"
        direction = _format_wind_direction(_pick(daily.get("wind_direction_10m_dominant"), index))
        if direction:
            wind_text += f"（{direction}风）"
        line_parts.append(wind_text)

    sunrise = _format_clock_text(_pick(daily.get("sunrise"), index))
    sunset = _format_clock_text(_pick(daily.get("sunset"), index))
    if sunrise or sunset:
        line_parts.append(f"日出 {sunrise or '--'} / 日落 {sunset or '--'}")

    return f"{header}: " + "，".join(line_parts)


def _format_open_meteo_text(
    city: str,
    location: dict,
    forecast: dict,
    forecast_days: int,
    target_day_offset: int | None = None,
    target_day_label: str | None = None,
) -> str:
    lines: list[str] = []
    place = _format_resolved_place(location)
    if place:
        lines.append(f"📍 {place}")
    else:
        lines.append(f"📍 {city}")

    timezone = str(forecast.get("timezone") or location.get("timezone") or "").strip()
    if timezone:
        lines.append(f"时区: {timezone}")

    current = forecast.get("current") or {}
    current_parts: list[str] = []
    if current:
        current_parts.append(_weather_text_from_code(current.get("weather_code")))

        temp = _format_temperature(current.get("temperature_2m"))
        if temp:
            current_parts.append(temp)

        apparent = _format_temperature(current.get("apparent_temperature"))
        if apparent:
            current_parts.append(f"体感 {apparent}")

        humidity = _format_percentage(current.get("relative_humidity_2m"))
        if humidity:
            current_parts.append(f"湿度 {humidity}")

        precipitation = _format_precipitation(current.get("precipitation"))
        if precipitation:
            current_parts.append(f"当前降水 {precipitation}")

        wind_speed = _format_speed(current.get("wind_speed_10m"))
        if wind_speed:
            wind_text = f"风速 {wind_speed}"
            direction = _format_wind_direction(current.get("wind_direction_10m"))
            if direction:
                wind_text += f"（{direction}风）"
            current_parts.append(wind_text)

    if current_parts:
        lines.append("当前: " + "，".join(current_parts))

    daily = forecast.get("daily") or {}
    daily_lines: list[str] = []
    for index in range(forecast_days):
        line = _build_open_meteo_daily_line(daily, index)
        if line:
            daily_lines.append(line)

    if daily_lines:
        if target_day_offset is not None:
            target_line = _build_open_meteo_daily_line(daily, target_day_offset)
            if target_line:
                label = target_day_label or "目标日期"
                lines.append(f"{label}预报: {target_line}")
        elif forecast_days == 1:
            lines.append("今天预报: " + daily_lines[0].split(": ", 1)[1])
        else:
            lines.append("")
            lines.append("未来天气:")
            lines.extend(daily_lines)

    lines.append("")
    lines.append("数据源: Open-Meteo")
    return "\n".join(lines)


async def _query_open_meteo_weather_text(
    forecast_api_url: str,
    geocoding_api_url: str,
    city: str,
    days: int | None,
    target_day_offset: int | None = None,
    target_day_label: str | None = None,
) -> str | None:
    search_name, country_code = _parse_city_and_country_hint(city)
    if not search_name:
        return None

    forecast_days = _normalize_days(days)
    async with aiohttp.ClientSession() as session:
        location = await _pick_best_geocoding_result(session, geocoding_api_url, search_name, country_code)
        if not location:
            return None

        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is None or longitude is None:
            logger.warning("天气地理编码返回缺少坐标: %s", location)
            return None

        forecast_params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "forecast_days": forecast_days,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "precipitation,weather_code,wind_speed_10m,wind_direction_10m"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "apparent_temperature_max,apparent_temperature_min,"
                "precipitation_probability_max,precipitation_sum,"
                "wind_speed_10m_max,wind_direction_10m_dominant,"
                "sunrise,sunset"
            ),
        }

        forecast = await _fetch_json(session, forecast_api_url, forecast_params, "天气预报")
        if not forecast:
            return None

    return _format_open_meteo_text(
        city,
        location,
        forecast,
        forecast_days,
        target_day_offset=target_day_offset,
        target_day_label=target_day_label,
    )


async def _query_legacy_weather_text(api_url: str, city: str, days: int | None) -> str | None:
    try:
        url = _build_url(api_url, city, days, fmt="text")
        logger.info("请求旧版天气 URL: %s", url)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                raw = await response.text()
                if response.status != 200:
                    logger.warning("旧版天气 API 返回非 200: status=%s body_len=%d", response.status, len(raw or ""))
                    return None
                if not (raw or "").strip():
                    logger.warning("旧版天气 API 返回空内容")
                    return None
                raw = raw.strip()
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw)
                        for key in ("data", "result", "text", "content", "msg", "message"):
                            if key in obj and obj[key] and isinstance(obj[key], str):
                                return obj[key].strip() or None
                        if "data" in obj and isinstance(obj["data"], dict):
                            return raw
                    except Exception:
                        pass
                return raw
    except asyncio.TimeoutError:
        logger.error("旧版天气 API 请求超时")
        return None
    except Exception as exc:
        logger.error("获取旧版天气文本数据时发生错误: %s", exc)
        return None


async def _query_weather_text(
    api_url: str,
    city: str,
    days: int | None,
    geocoding_api_url: str | None = None,
    target_day_offset: int | None = None,
    target_day_label: str | None = None,
) -> str | None:
    if _should_use_open_meteo(api_url, geocoding_api_url):
        return await _query_open_meteo_weather_text(
            api_url or DEFAULT_WEATHER_FORECAST_API_URL,
            geocoding_api_url or DEFAULT_WEATHER_GEOCODING_API_URL,
            city,
            days,
            target_day_offset=target_day_offset,
            target_day_label=target_day_label,
        )
    return await _query_legacy_weather_text(api_url, city, days)


async def _query_legacy_weather_image(api_url: str, city: str, days: int | None) -> str | None:
    try:
        url = _build_url(api_url, city, days, fmt="image")
        logger.info("请求旧版天气图片 URL: %s", url)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                data = await response.read()
                if response.status != 200:
                    logger.warning("旧版天气图片 API 返回非 200: status=%s body_len=%d", response.status, len(data))
                    return None
                if not data:
                    logger.warning("旧版天气图片 API 返回空内容")
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
        logger.error("旧版天气图片 API 请求超时")
        return None
    except Exception as exc:
        logger.error("获取旧版天气图片时发生错误: %s", exc)
        return None


async def _query_weather_image(
    api_url: str,
    city: str,
    days: int | None,
    geocoding_api_url: str | None = None,
) -> str | None:
    if _should_use_open_meteo(api_url, geocoding_api_url):
        logger.info("Open-Meteo 默认仅返回文本天气，图片模式将自动回退到文本模式")
        return None
    return await _query_legacy_weather_image(api_url, city, days)


def _build_url(api_url: str, city: str, days: int | None, fmt: str) -> str:
    q = quote(city)
    url = f"{api_url}?query={q}&format={fmt}"
    if days and days >= 2:
        url += f"&action=forecast&days={days}"
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
        group = config.get("weather")
        if isinstance(group, dict):
            val = group.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
            items = group.get("items")
            if isinstance(items, dict):
                item_val = items.get(key)
                if isinstance(item_val, dict) and "value" in item_val:
                    item_val = item_val["value"]
                if item_val is not None and str(item_val).strip():
                    return str(item_val).strip()
    try:
        group = config.get("weather") if hasattr(config, "get") else None
        if isinstance(group, dict):
            val = group.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
            items = group.get("items")
            if isinstance(items, dict):
                item_val = items.get(key)
                if isinstance(item_val, dict) and "value" in item_val:
                    item_val = item_val["value"]
                if item_val is not None and str(item_val).strip():
                    return str(item_val).strip()
    except Exception:
        pass
    return default


def _parse_weather_command(message_text: str) -> tuple[str, int | None, str | None] | None:
    raw_text = (message_text or "").strip()
    if not raw_text:
        return None
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return None

    body = parts[1].strip()
    if not body:
        return None

    days: int | None = None
    tokens = body.split()
    if tokens:
        try:
            parsed_days = int(tokens[-1])
            if 1 <= parsed_days <= MAX_FORECAST_DAYS:
                days = parsed_days
                body = " ".join(tokens[:-1]).strip()
        except Exception:
            pass

    city, when_text = _extract_city_and_when(body)
    if not city:
        return None
    return city, days, when_text


async def handle_weather_command(event: AstrMessageEvent, config: AstrBotConfig):
    """
    天气查询命令入口（命令模式）。
    """
    message_text = event.get_message_str()
    parsed = _parse_weather_command(message_text)
    if not parsed:
        yield event.plain_result(
            "❌ 参数不足\n\n"
            "用法: /天气 城市 [天数/时间]\n"
            "示例: /天气 北京\n"
            "/天气 上海 5\n"
            "/天气 东京 明天\n"
            "/天气 北京 周一\n"
            "/天气 New York 3\n"
            "/天气 Paris, FR 4\n\n"
            "如遇重名城市，建议补充国家代码，例如 `Paris, FR`。"
        )
        return

    city, days, when_text = parsed
    query_spec, error_message = _resolve_weather_query(city, days=days, when_text=when_text)
    if not query_spec:
        yield event.plain_result(f"❌ {error_message or '参数错误'}")
        return

    weather_cfg = get_weather_runtime_config(config)
    api_url = weather_cfg["api_url"]
    geocoding_api_url = weather_cfg["geocoding_api_url"]
    default_format = weather_cfg["default_format"]
    resolved_city = str(query_spec["city"] or "").strip()
    forecast_days = int(query_spec["forecast_days"] or 1)
    target_day_offset = query_spec["target_day_offset"]
    target_day_label = str(query_spec["target_day_label"] or "").strip() or None

    image_path: str | None = None
    try:
        if str(default_format).lower() == "image":
            image_path = await _query_weather_image(api_url, resolved_city, forecast_days, geocoding_api_url)
            if image_path:
                record_passive_habit(event, "weather", "city", resolved_city, source_text=message_text or "")
                yield event.image_result(image_path)
            else:
                text = await _query_weather_text(
                    api_url,
                    resolved_city,
                    forecast_days,
                    geocoding_api_url,
                    target_day_offset=target_day_offset if isinstance(target_day_offset, int) else None,
                    target_day_label=target_day_label,
                )
                if text:
                    title = (
                        f"🌦 {resolved_city} {target_day_label}天气"
                        if target_day_label
                        else (
                            f"🌦 {resolved_city}天气"
                            if forecast_days <= 1
                            else f"🌦 {resolved_city} 未来{forecast_days}天天气预报"
                        )
                    )
                    record_passive_habit(event, "weather", "city", resolved_city, source_text=message_text or "")
                    yield event.plain_result(f"{title}\n\n{text}")
                else:
                    yield event.plain_result(
                        "❌ 暂时没有查到天气数据\n\n"
                        "请尝试更具体的城市名，例如 `/天气 Paris, FR 明天`。\n"
                        "当前默认数据源已切换为 Open-Meteo，支持全球城市。"
                    )
        else:
            text = await _query_weather_text(
                api_url,
                resolved_city,
                forecast_days,
                geocoding_api_url,
                target_day_offset=target_day_offset if isinstance(target_day_offset, int) else None,
                target_day_label=target_day_label,
            )
            if text:
                title = (
                    f"🌦 {resolved_city} {target_day_label}天气"
                    if target_day_label
                    else (
                        f"🌦 {resolved_city}天气"
                        if forecast_days <= 1
                        else f"🌦 {resolved_city} 未来{forecast_days}天天气预报"
                    )
                )
                record_passive_habit(event, "weather", "city", resolved_city, source_text=message_text or "")
                yield event.plain_result(f"{title}\n\n{text}")
            else:
                yield event.plain_result(
                    "❌ 暂时没有查到天气数据\n\n"
                    "请尝试更具体的城市名，例如 `/天气 Paris, FR 明天`。\n"
                    "当前默认数据源已切换为 Open-Meteo，支持全球城市。"
                )
    except Exception as exc:
        logger.error("查询天气时发生错误: %s", exc)
        yield event.plain_result(f"❌ 查询失败: {str(exc)}")
    finally:
        if image_path:
            try:
                os.unlink(image_path)
            except Exception:
                pass


async def handle_weather_help(event: AstrMessageEvent):
    text = (
        "🌤 智能天气查询（全球城市）\n\n"
        "【命令】\n"
        "• /天气 城市 [天数/时间]\n"
        "• /天气 城市, 国家代码 [天数/时间]\n"
        "• 示例: /天气 北京\n"
        "• 示例: /天气 上海 5\n"
        "• 示例: /天气 东京 明天\n"
        "• 示例: /天气 北京 周一\n"
        "• 示例: /天气 New York 3\n"
        "• 示例: /天气 Paris, FR 4\n\n"
        "说明：\n"
        "- 默认使用 Open-Meteo，支持全球城市天气与 1-7 天预报。\n"
        "- 时间表达支持 今天 / 明天 / 后天 / 大后天 / 周一 / 下周三 / 未来5天。\n"
        "- 遇到重名城市时，建议补充国家代码，例如 Paris, FR / London, CA。\n"
        "- `weather_default_format` 默认建议设为 text；若你接的是旧版自定义图片天气 API，再切回 image。"
    )
    yield event.plain_result(text)
