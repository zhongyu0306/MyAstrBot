from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.star import Context, StarTools

from .config_utils import ensure_flat_config
from .memory_state_store import load_json_state, save_json_state
from .memory_utils import init_user_memory_store
from .passive_memory_utils import init_passive_memory_store, record_passive_habit


_SUBSCRIPTION_FILE_NAME = "lol_match_subscriptions.json"
_SUBSCRIPTION_STATE_NAMESPACE = "lol_match_subscriptions"
_SUBSCRIPTION_LOCK = asyncio.Lock()

_DEFAULT_TENCENT_AREA = 1
_DEFAULT_TENCENT_BASE_URL = "https://www.wegame.com.cn"
_DEFAULT_FETCH_COUNT = 3
_DEFAULT_PUSH_LIMIT = 3
_DEFAULT_TIMEOUT_SECONDS = 12
_DEFAULT_TENCENT_ACCOUNT_TYPE = 2
_DEFAULT_TENCENT_REFERER = "https://www.wegame.com.cn/ioi"
_DEFAULT_TENCENT_CALLER = "wegame.pallas.web.LolBattle"
_DEFAULT_TENCENT_FROM_SRC = "lol_helper"
_TENCENT_BIZ_PATH = "/api/v1/wegame.pallas.game.LolBattle"

_TENCENT_QUEUE_ID_MAP = {
    2: "自定义",
    4: "排位赛",
    6: "匹配模式",
    31: "人机模式",
    33: "大乱斗",
    42: "无限火力",
    65: "极地大乱斗",
    76: "云顶排位",
    78: "云顶匹配",
    170: "斗魂竞技场",
    430: "匹配模式",
    440: "灵活排位",
    450: "大乱斗",
    900: "无限火力",
    1700: "斗魂竞技场",
}

_TENCENT_AREA_NAME_MAP: dict[int, str] = {
    1: "艾欧尼亚",
    2: "比尔吉沃特",
    3: "祖安",
    4: "诺克萨斯",
    5: "班德尔城",
    6: "德玛西亚",
    7: "皮尔特沃夫",
    8: "战争学院",
    9: "弗雷尔卓德",
    10: "巨神峰",
    11: "雷瑟守备",
    12: "无畏先锋",
    13: "裁决之地",
    14: "黑色玫瑰",
    15: "暗影岛",
    16: "恕瑞玛",
    17: "钢铁烈阳",
    18: "水晶之痕",
    19: "均衡教派",
    20: "扭曲丛林",
    21: "教育网专区",
    22: "影流",
    23: "守望之海",
    24: "征服之海",
    25: "卡拉曼达",
    26: "巨龙之巢",
    27: "皮城警备",
    30: "男爵领域",
}

_TENCENT_AREA_ALIAS_MAP: dict[str, int] = {
    "艾欧尼亚": 1,
    "电一": 1,
    "电信一区": 1,
    "比尔吉沃特": 2,
    "网二": 2,
    "网通二区": 2,
    "祖安": 3,
    "诺克萨斯": 4,
    "班德尔城": 5,
    "德玛西亚": 6,
    "网一": 6,
    "网通一区": 6,
    "皮尔特沃夫": 7,
    "战争学院": 8,
    "弗雷尔卓德": 9,
    "巨神峰": 10,
    "雷瑟守备": 11,
    "无畏先锋": 12,
    "裁决之地": 13,
    "黑色玫瑰": 14,
    "黑玫": 14,
    "暗影岛": 15,
    "恕瑞玛": 16,
    "钢铁烈阳": 17,
    "水晶之痕": 18,
    "均衡教派": 19,
    "扭曲丛林": 20,
    "教育网专区": 21,
    "影流": 22,
    "守望之海": 23,
    "征服之海": 24,
    "卡拉曼达": 25,
    "巨龙之巢": 26,
    "皮城警备": 27,
    "男爵领域": 30,
    "男爵": 30,
}

_CHAMPION_NAME_CACHE: dict[str, str] = {}
_CHAMPION_CACHE_LOCK = asyncio.Lock()


def _get_subscription_file_path() -> Path:
    base = Path(StarTools.get_data_dir("astrbot_all_char"))
    base.mkdir(parents=True, exist_ok=True)
    return base / _SUBSCRIPTION_FILE_NAME


def _safe_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if minimum is not None and parsed < minimum:
        return minimum
    if maximum is not None and parsed > maximum:
        return maximum
    return parsed


def _safe_sender_id(event: AstrMessageEvent | None) -> str:
    if event is None:
        return ""
    try:
        value = event.get_sender_id()
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_sender_name(event: AstrMessageEvent | None) -> str:
    if event is None:
        return ""
    try:
        value = event.get_sender_name()
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_group_id(event: AstrMessageEvent | None) -> str:
    if event is None:
        return ""
    try:
        value = event.get_group_id()
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_is_private_chat(event: AstrMessageEvent | None) -> bool:
    if event is None:
        return False
    try:
        return bool(event.is_private_chat())
    except Exception:
        return False


def _safe_session_id(event: AstrMessageEvent | None) -> str:
    if event is None:
        return ""
    for attr in ("unified_msg_origin", "session_id"):
        value = getattr(event, attr, None)
        cleaned = str(value or "").strip()
        if cleaned:
            return cleaned
    return ""


def _get_cfg(config: Any, key: str, default: Any) -> Any:
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _format_area_name(area: int) -> str:
    return _TENCENT_AREA_NAME_MAP.get(int(area), f"大区{int(area)}")


def _decode_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        if "%" in text:
            decoded = unquote(text)
            if decoded:
                return decoded
    except Exception:
        pass
    return text


def _build_tencent_headers(cookie: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "trpc-caller": _DEFAULT_TENCENT_CALLER,
        "cookie": str(cookie or "").strip(),
        "referer": _DEFAULT_TENCENT_REFERER,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
    }


async def _ensure_champion_name_cache(session: aiohttp.ClientSession) -> None:
    if _CHAMPION_NAME_CACHE:
        return
    async with _CHAMPION_CACHE_LOCK:
        if _CHAMPION_NAME_CACHE:
            return
        try:
            async with session.get(
                "https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js"
            ) as resp:
                if resp.status != 200:
                    return
                text = await resp.text()
        except Exception:
            return

        match = re.search(r"(\{.*\})", text, re.S)
        if not match:
            return
        try:
            payload = json.loads(match.group(1))
        except Exception:
            return
        heroes = payload.get("hero")
        if not isinstance(heroes, list):
            return

        for item in heroes:
            if not isinstance(item, dict):
                continue
            hero_id = str(item.get("heroId") or "").strip()
            hero_name = str(item.get("name") or "").strip()
            if hero_id and hero_name:
                _CHAMPION_NAME_CACHE[hero_id] = hero_name


def _champion_name(champion_id: Any) -> str:
    cleaned = str(champion_id or "").strip()
    if not cleaned:
        return "未知"
    return _CHAMPION_NAME_CACHE.get(cleaned) or f"英雄{cleaned}"


def _normalize_area_from_token(token: Any, default_area: int) -> tuple[int, str]:
    cleaned = str(token or "").strip()
    if not cleaned:
        return int(default_area), ""
    if cleaned.isdigit():
        area = int(cleaned)
        return (area, "") if area > 0 else (0, f"无法识别大区：{cleaned}")
    digit_match = re.search(r"\d{1,3}", cleaned)
    if digit_match:
        area = int(digit_match.group(0))
        if area > 0:
            return area, ""
    direct = _TENCENT_AREA_ALIAS_MAP.get(cleaned)
    if direct:
        return direct, ""
    normalized = re.sub(r"[\s_（）()]+", "", cleaned).lower()
    for alias, area in _TENCENT_AREA_ALIAS_MAP.items():
        candidate = re.sub(r"[\s_（）()]+", "", alias).lower()
        if normalized == candidate:
            return area, ""
    return 0, f"无法识别大区：{cleaned}"


def _normalize_subscription_records(value: Any) -> dict[str, list[dict[str, Any]]]:
    raw = value if isinstance(value, dict) else {"subscriptions": []}
    subscriptions = raw.get("subscriptions")
    if not isinstance(subscriptions, list):
        return {"subscriptions": []}

    normalized: list[dict[str, Any]] = []
    for item in subscriptions:
        if not isinstance(item, dict):
            continue
        openid = str(item.get("openid") or "").strip()
        group_id = str(item.get("group_id") or "").strip()
        session_id = str(item.get("session_id") or "").strip()
        area = _safe_int(item.get("area"), 0, 0, 999)
        nickname = _decode_text(item.get("nickname") or item.get("display_name") or "").strip()
        if not openid or not group_id or not session_id or area <= 0:
            continue
        normalized.append(
            {
                "id": str(item.get("id") or f"{group_id}|tencent|{area}|{openid}").strip(),
                "mode": "tencent",
                "session_id": session_id,
                "group_id": group_id,
                "openid": openid,
                "area": area,
                "nickname": nickname or "未命名召唤师",
                "display_name": _decode_text(item.get("display_name") or nickname or "未命名召唤师").strip()
                or "未命名召唤师",
                "account_type": _safe_int(
                    item.get("account_type"),
                    _DEFAULT_TENCENT_ACCOUNT_TYPE,
                    1,
                    5,
                ),
                "created_by": str(item.get("created_by") or "").strip(),
                "created_by_name": str(item.get("created_by_name") or "").strip(),
                "created_at": str(item.get("created_at") or "").strip(),
                "updated_at": str(item.get("updated_at") or "").strip(),
                "last_match_id": str(item.get("last_match_id") or "").strip(),
                "last_push_at": str(item.get("last_push_at") or "").strip(),
                "last_check_at": str(item.get("last_check_at") or "").strip(),
            }
        )
    return {"subscriptions": normalized}


def _load_subscriptions() -> list[dict[str, Any]]:
    try:
        data = load_json_state(
            _SUBSCRIPTION_STATE_NAMESPACE,
            default={"subscriptions": []},
            normalizer=_normalize_subscription_records,
            legacy_path=_get_subscription_file_path(),
        )
    except Exception as exc:
        logger.error("读取 LoL 订阅失败: %s", exc)
        return []
    raw = data.get("subscriptions")
    return list(raw) if isinstance(raw, list) else []


def _save_subscriptions(subscriptions: list[dict[str, Any]]) -> None:
    save_json_state(_SUBSCRIPTION_STATE_NAMESPACE, {"subscriptions": subscriptions})


def list_lol_subscriptions_by_creator(qq_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    cleaned_id = str(qq_id or "").strip()
    if not cleaned_id:
        return []
    result = [item for item in _load_subscriptions() if str(item.get("created_by") or "").strip() == cleaned_id]
    result.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("group_id") or "")), reverse=True)
    return result[: max(1, int(limit))]


def get_lol_profile_for_user(qq_id: str) -> dict[str, Any]:
    cleaned_id = str(qq_id or "").strip()
    profile = {
        "qq_id": cleaned_id,
        "nickname": "",
        "area": 0,
        "area_name": "",
        "source": "",
    }
    if not cleaned_id:
        return profile

    habits = init_passive_memory_store().list_habits(cleaned_id, limit=100)
    for item in habits:
        if str(item.get("module_name") or "").strip() != "lol":
            continue
        habit_key = str(item.get("habit_key") or "").strip()
        habit_value = str(item.get("habit_value") or "").strip()
        if not habit_value:
            continue
        if habit_key == "summoner_name" and not profile["nickname"]:
            profile["nickname"] = habit_value
            profile["source"] = "memory"
        elif habit_key in {"area_name", "area"} and not profile["area_name"]:
            profile["area_name"] = habit_value
            area, err = _normalize_area_from_token(habit_value, _DEFAULT_TENCENT_AREA)
            if not err and area > 0:
                profile["area"] = area

    if profile["area"] <= 0 and profile["area_name"]:
        area, err = _normalize_area_from_token(profile["area_name"], _DEFAULT_TENCENT_AREA)
        if not err and area > 0:
            profile["area"] = area
    if profile["area"] > 0 and not profile["area_name"]:
        profile["area_name"] = _format_area_name(profile["area"])
    return profile


def _normalize_lookup_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _entry_match_score_for_alias(entry: dict[str, Any], alias_text: str) -> int:
    normalized_alias = _normalize_lookup_text(alias_text)
    if not normalized_alias:
        return 0

    best_score = 0
    candidates: list[str] = []
    candidates.extend(str(item or "").strip() for item in (entry.get("memory_aliases") or []))
    candidates.extend(str((item or {}).get("alias") or "").strip() for item in (entry.get("scoped_aliases") or []))
    candidates.extend(str(item or "").strip() for item in (entry.get("seen_names") or []))
    candidates.append(str(entry.get("platform_name") or "").strip())
    candidates.append(str(entry.get("memory_name") or "").strip())

    for candidate in candidates:
        normalized_candidate = _normalize_lookup_text(candidate)
        if not normalized_candidate:
            continue
        if normalized_candidate == normalized_alias:
            best_score = max(best_score, 300 + min(len(normalized_candidate), 24))
        elif normalized_alias in normalized_candidate:
            best_score = max(best_score, 180 + min(len(normalized_candidate), 18))
    return best_score


def _resolve_lol_profile_from_person_alias(alias_text: str) -> dict[str, Any] | None:
    cleaned_alias = str(alias_text or "").strip()
    if not cleaned_alias:
        return None

    store = init_user_memory_store()
    candidates = store.search_memories(cleaned_alias, limit=8, include_observed_only=False)
    if not candidates:
        return None

    scored: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for entry in candidates:
        qq_id = str(entry.get("qq_id") or "").strip()
        if not qq_id:
            continue
        profile = get_lol_profile_for_user(qq_id)
        if not str(profile.get("nickname") or "").strip():
            continue
        score = _entry_match_score_for_alias(entry, cleaned_alias)
        if score <= 0:
            continue
        scored.append((score, entry, profile))

    if not scored:
        return None

    scored.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("updated_at") or ""),
            str(item[1].get("qq_id") or ""),
        )
    )
    _, entry, profile = scored[0]
    resolved = dict(profile)
    resolved["person_qq_id"] = str(entry.get("qq_id") or "").strip()
    resolved["person_name"] = (
        str(entry.get("memory_name") or "").strip()
        or str(entry.get("platform_name") or "").strip()
        or str(entry.get("qq_id") or "").strip()
    )
    return resolved


def _should_try_memory_alias_fallback(error_text: str) -> bool:
    lowered = str(error_text or "").strip().lower()
    return any(
        token in lowered
        for token in (
            "8000004",
            "empty",
            "未搜索到该昵称的玩家",
            "目标大区未找到",
        )
    )


def _remember_lol_profile(event: AstrMessageEvent | None, nickname: str, area: int) -> None:
    if event is None:
        return
    cleaned_nickname = str(nickname or "").strip()
    if cleaned_nickname:
        record_passive_habit(
            event,
            "lol",
            "summoner_name",
            cleaned_nickname,
            source_text=f"LOL 常查昵称：{cleaned_nickname}",
        )
    area_name = _format_area_name(int(area))
    if area_name:
        record_passive_habit(
            event,
            "lol",
            "area_name",
            area_name,
            source_text=f"LOL 常用大区：{area_name}",
        )


def _parse_bool_like(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "win", "yes"}


def _normalize_tencent_battle(battle: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(battle, dict):
        return None
    game_id = str(battle.get("game_id") or "").strip()
    if not game_id:
        return None
    queue_id = _safe_int(battle.get("game_queue_id"), 0, 0, 99999)
    kills = _safe_int(battle.get("kills"), 0, 0, 999)
    deaths = _safe_int(battle.get("deaths"), 0, 0, 999)
    assists = _safe_int(battle.get("assists"), 0, 0, 999)
    duration_seconds = _safe_int(battle.get("game_time_played"), 0, 0, 24 * 3600)
    start_ts = _safe_int(battle.get("game_start_time"), 0, 0, 10**15)
    champion_id = str(battle.get("champion_id") or "").strip()
    queue_name = _TENCENT_QUEUE_ID_MAP.get(queue_id, "模式")
    champion_name = _champion_name(champion_id)
    if deaths <= 0:
        kda_text = "Perfect"
    else:
        kda_text = f"{(kills + assists) / max(deaths, 1):.2f}"
    if start_ts >= 10**12:
        start_ts //= 1000
    if start_ts > 0:
        try:
            time_text = datetime.fromtimestamp(start_ts).strftime("%m-%d %H:%M")
        except Exception:
            time_text = "--:--"
    else:
        time_text = "--:--"
    return {
        "game_id": game_id,
        "win": _parse_bool_like(battle.get("win")),
        "queue_id": queue_id,
        "queue_name": queue_name,
        "champion_id": champion_id,
        "champion_name": champion_name,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kda_text": kda_text,
        "duration_seconds": duration_seconds,
        "duration_text": _format_tencent_duration(duration_seconds),
        "start_ts": start_ts,
        "time_text": time_text,
    }


def _format_tencent_duration(seconds_like: Any) -> str:
    raw = _safe_int(seconds_like, 0, 0, 24 * 3600)
    minutes, seconds = divmod(raw, 60)
    return f"{minutes}m{seconds:02d}s"


def _format_tencent_battle_line(battle: dict[str, Any]) -> str:
    item = _normalize_tencent_battle(battle)
    if not item:
        return "对局数据异常。"
    result_text = "胜利" if item["win"] else "失败"
    return (
        f"[{item['time_text']}] "
        f"[{item['queue_name']}] "
        f"{result_text} | "
        f"{item['champion_name']} "
        f"{item['kills']}/{item['deaths']}/{item['assists']} "
        f"(KDA {item['kda_text']}) | "
        f"时长 {item['duration_text']}"
    )


async def _wegame_post_json(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    cookie: str,
    method: str,
    payload: dict[str, Any],
) -> tuple[bool, Any]:
    url = f"{str(base_url or _DEFAULT_TENCENT_BASE_URL).rstrip('/')}{_TENCENT_BIZ_PATH}/{method}"
    headers = _build_tencent_headers(cookie)
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                text = (await resp.text()).strip()
                return False, f"WeGame HTTP {resp.status}: {text[:200]}"
            try:
                data = await resp.json(content_type=None)
            except Exception:
                text = (await resp.text()).strip()
                return False, f"WeGame 响应非 JSON: {text[:200]}"
    except asyncio.TimeoutError:
        return False, "WeGame 请求超时。"
    except Exception as exc:
        return False, f"WeGame 请求失败: {exc}"

    if not isinstance(data, dict):
        return False, "WeGame 响应结构异常。"

    result = data.get("result")
    if isinstance(result, dict):
        error_code = _safe_int(result.get("error_code"), 0)
        if error_code != 0:
            if error_code == 8000102:
                return False, "WeGame Cookie 无效或过期（error_code=8000102），请更新 cookie。"
            error_message = str(result.get("error_message") or "").strip()
            suffix = f" {error_message}" if error_message else ""
            return False, f"WeGame 接口错误：{error_code}{suffix}"

    inner = data.get("data")
    if inner is None:
        inner = data
    return True, inner


async def _search_tencent_players(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    cookie: str,
    nickname: str,
) -> tuple[bool, list[dict[str, Any]] | str]:
    ok, payload = await _wegame_post_json(
        session,
        base_url=base_url,
        cookie=cookie,
        method="SearchPlayer",
        payload={
            "nickname": nickname,
            "from_src": _DEFAULT_TENCENT_FROM_SRC,
        },
    )
    if not ok:
        return False, str(payload)

    data = payload if isinstance(payload, dict) else {}
    players = data.get("players")
    if not isinstance(players, list):
        return True, []

    normalized: list[dict[str, Any]] = []
    for item in players:
        if not isinstance(item, dict):
            continue
        area = _safe_int(item.get("area"), 0, 0, 999)
        openid = str(item.get("openid") or "").strip()
        if area <= 0 or not openid:
            continue
        nick = _decode_text(item.get("name") or item.get("nickname") or nickname).strip() or nickname
        normalized.append(
            {
                "area": area,
                "openid": openid,
                "nickname": nick,
                "level": _safe_int(item.get("level"), 0, 0, 999),
            }
        )
    return True, normalized


def _pick_tencent_player(players: list[dict[str, Any]], target_area: int) -> tuple[dict[str, Any] | None, str]:
    if not players:
        return None, "未搜索到该昵称的玩家。"

    in_area = [item for item in players if _safe_int(item.get("area"), 0) == int(target_area)]
    if not in_area:
        area_list = ", ".join(sorted({_format_area_name(_safe_int(item.get("area"), 0)) for item in players}))
        return None, f"该昵称在目标大区未找到，已发现大区：{area_list}"

    in_area.sort(key=lambda item: _safe_int(item.get("level"), 0), reverse=True)
    picked = in_area[0]
    if len(in_area) > 1:
        return (
            picked,
            f"同大区找到 {len(in_area)} 个同名玩家，已默认选择等级最高的一个。如需更精确，可换更独特的昵称。",
        )
    return picked, ""


async def _fetch_tencent_battle_list(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    cookie: str,
    area: int,
    openid: str,
    account_type: int,
    count: int,
    offset: int = 0,
) -> tuple[bool, list[dict[str, Any]] | str]:
    ok, payload = await _wegame_post_json(
        session,
        base_url=base_url,
        cookie=cookie,
        method="GetBattleList",
        payload={
            "account_type": int(account_type),
            "area": int(area),
            "id": str(openid),
            "from_src": _DEFAULT_TENCENT_FROM_SRC,
            "count": int(count),
            "offset": int(offset),
        },
    )
    if not ok:
        return False, str(payload)

    data = payload if isinstance(payload, dict) else {}
    battles = data.get("battles")
    if not isinstance(battles, list):
        return True, []
    return True, [item for item in battles if isinstance(item, dict)]


async def _query_tencent_recent_lines(
    *,
    cookie: str,
    base_url: str,
    area: int,
    nickname: str,
    account_type: int,
    match_count: int,
    timeout_seconds: int,
) -> tuple[bool, dict[str, Any] | str]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _ensure_champion_name_cache(session)

        search_ok, search_data = await _search_tencent_players(
            session,
            base_url=base_url,
            cookie=cookie,
            nickname=nickname,
        )
        if not search_ok:
            return False, str(search_data)
        players = search_data if isinstance(search_data, list) else []
        picked, pick_message = _pick_tencent_player(players, area)
        if not picked:
            return False, pick_message or f"未在 {_format_area_name(area)} 找到昵称“{nickname}”。"

        openid = str(picked.get("openid") or "").strip()
        picked_nickname = str(picked.get("nickname") or nickname).strip() or nickname
        list_ok, list_data = await _fetch_tencent_battle_list(
            session,
            base_url=base_url,
            cookie=cookie,
            area=area,
            openid=openid,
            account_type=account_type,
            count=match_count,
            offset=0,
        )
        if not list_ok:
            return False, str(list_data)

    battles = list_data if isinstance(list_data, list) else []
    normalized_battles = [item for item in (_normalize_tencent_battle(battle) for battle in battles) if item]
    return True, {
        "area": area,
        "area_name": _format_area_name(area),
        "nickname": picked_nickname,
        "openid": openid,
        "pick_message": pick_message,
        "battles": normalized_battles,
        "lines": [_format_tencent_battle_line(battle) for battle in battles if _normalize_tencent_battle(battle)],
    }


def _build_help_text(default_area: int) -> str:
    area_name = _format_area_name(default_area)
    return (
        "LoL 战绩功能（仅国服腾讯服）命令：\n"
        "1. 查询战绩：\n"
        f"   /lol 战绩 {area_name} 某某昵称\n"
        f"   /lol 战绩 {area_name} 某某昵称 5\n"
        "   /lol 战绩 某某昵称（不写大区时优先使用你记住的大区，其次用配置默认大区）\n"
        "   /lol 分析 某某昵称（查询并让 LLM 分析最近战绩）\n"
        "2. 记住默认账号：\n"
        "   /lol 绑定 艾欧尼亚 某某昵称\n"
        "3. 订阅群战绩（仅 QQ 群可用，每小时自动检查）：\n"
        "   /lol 订阅 艾欧尼亚 某某昵称\n"
        "   /lol 订阅（如果你已经绑定过默认账号）\n"
        "4. 查看当前群订阅：\n"
        "   /lol 订阅列表\n"
        "5. 退订：\n"
        "   /lol 退订 1\n"
        "   /lol 退订 全部\n"
        "说明：需要在配置中填写 `lol_tencent_cookie`。"
    )


def _parse_query_text(
    text: str,
    *,
    default_area: int,
    default_count: int,
) -> dict[str, Any]:
    body = str(text or "").strip()
    if not body:
        return {
            "area": default_area,
            "count": default_count,
            "nickname": "",
            "explicit_area": False,
            "explicit_nickname": False,
            "error": "",
        }

    parts = body.split()
    if parts and parts[0].lower() in {"查询", "查", "query", "q", "战绩", "分析"}:
        parts = parts[1:]

    count = int(default_count)
    if parts and re.fullmatch(r"\d{1,2}", parts[-1]):
        count = _safe_int(parts.pop(), default_count, 1, 10)

    area = int(default_area)
    explicit_area = False
    if parts:
        parsed_area, area_err = _normalize_area_from_token(parts[0], default_area)
        if not area_err:
            area = parsed_area
            explicit_area = True
            parts = parts[1:]

    nickname = " ".join(parts).strip()
    return {
        "area": area,
        "count": count,
        "nickname": nickname,
        "explicit_area": explicit_area,
        "explicit_nickname": bool(nickname),
        "error": "",
    }


def _resolve_query_profile(
    event: AstrMessageEvent | None,
    *,
    raw_text: str,
    default_area: int,
    default_count: int,
    area: int | None = None,
    nickname: str | None = None,
    match_count: int | None = None,
) -> dict[str, Any]:
    remembered = get_lol_profile_for_user(_safe_sender_id(event))
    parsed = _parse_query_text(raw_text, default_area=default_area, default_count=default_count)

    final_nickname = str(nickname or parsed["nickname"] or remembered.get("nickname") or "").strip()
    final_count = _safe_int(match_count or parsed["count"], default_count, 1, 10)

    explicit_area = area is not None or parsed["explicit_area"]
    if area is not None:
        final_area = _safe_int(area, default_area, 1, 999)
    elif parsed["explicit_area"]:
        final_area = _safe_int(parsed["area"], default_area, 1, 999)
    elif remembered.get("area"):
        final_area = _safe_int(remembered.get("area"), default_area, 1, 999)
    else:
        final_area = int(default_area)

    if not final_nickname:
        return {
            "ok": False,
            "error": "请提供昵称，或先用 `/lol 绑定 大区 昵称` 记住默认账号。",
        }

    return {
        "ok": True,
        "area": final_area,
        "area_name": _format_area_name(final_area),
        "nickname": final_nickname,
        "count": final_count,
        "used_memory_profile": bool(remembered.get("nickname")) and not str(nickname or parsed["nickname"] or "").strip(),
        "remembered_profile": remembered,
        "explicit_area": explicit_area,
    }


def _compose_query_text(result: dict[str, Any], *, analysis_text: str = "") -> str:
    lines = result.get("lines") or []
    title = f"LoL 最近 {len(lines)} 场：{result['nickname']}（{result['area_name']}）"
    if result.get("pick_message"):
        title = f"{title}\n提示：{result['pick_message']}"
    if not lines:
        text = f"{title}\n暂无最近战绩。"
    else:
        text = f"{title}\n" + "\n".join(lines)
    if analysis_text:
        text += f"\n\n战绩分析：\n{analysis_text}"
    return text


def _summarize_battles_for_analysis(battles: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(battles)
    wins = sum(1 for item in battles if item.get("win"))
    losses = max(0, total - wins)
    avg_kills = sum(int(item.get("kills") or 0) for item in battles) / max(total, 1)
    avg_deaths = sum(int(item.get("deaths") or 0) for item in battles) / max(total, 1)
    avg_assists = sum(int(item.get("assists") or 0) for item in battles) / max(total, 1)
    avg_duration_minutes = (
        sum(int(item.get("duration_seconds") or 0) for item in battles) / max(total, 1) / 60.0
    )
    champion_counter = Counter(str(item.get("champion_name") or "").strip() for item in battles if item.get("champion_name"))
    queue_counter = Counter(str(item.get("queue_name") or "").strip() for item in battles if item.get("queue_name"))
    hottest_champion = champion_counter.most_common(3)
    hottest_queue = queue_counter.most_common(3)
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / max(total, 1)) * 100.0,
        "avg_kills": avg_kills,
        "avg_deaths": avg_deaths,
        "avg_assists": avg_assists,
        "avg_duration_minutes": avg_duration_minutes,
        "champions": hottest_champion,
        "queues": hottest_queue,
    }


def _fallback_analysis(result: dict[str, Any]) -> str:
    battles = result.get("battles") or []
    if not battles:
        return "最近没有可分析的对局数据。"
    summary = _summarize_battles_for_analysis(battles)
    lines = [
        f"最近 {summary['total']} 场里赢了 {summary['wins']} 场，胜率约 {summary['win_rate']:.0f}%。",
        (
            f"场均 {summary['avg_kills']:.1f}/{summary['avg_deaths']:.1f}/{summary['avg_assists']:.1f}，"
            f"平均时长 {summary['avg_duration_minutes']:.1f} 分钟。"
        ),
    ]
    if summary["champions"]:
        champion_text = "、".join(f"{name} x{count}" for name, count in summary["champions"])
        lines.append(f"最近主要在玩：{champion_text}。")
    if summary["queues"]:
        queue_text = "、".join(f"{name} x{count}" for name, count in summary["queues"])
        lines.append(f"对局模式分布：{queue_text}。")
    if summary["avg_deaths"] >= 7:
        lines.append("从数据看阵亡偏多，建议优先关注中期视野和撤退时机。")
    elif summary["avg_kills"] + summary["avg_assists"] >= 15:
        lines.append("最近参团贡献比较积极，可以继续围绕优势英雄稳定节奏。")
    else:
        lines.append("样本不算多，建议结合更多对局再看趋势。")
    return "\n".join(lines[:4])


async def _generate_lol_analysis(
    context: Context | None,
    session_id: str,
    result: dict[str, Any],
) -> str:
    if context is None or not session_id:
        return _fallback_analysis(result)

    battles = result.get("battles") or []
    if not battles:
        return "最近没有可分析的对局数据。"

    summary = _summarize_battles_for_analysis(battles)
    match_lines = [
        (
            f"{item['time_text']} | {item['queue_name']} | {'胜利' if item['win'] else '失败'} | "
            f"{item['champion_name']} | {item['kills']}/{item['deaths']}/{item['assists']} | "
            f"时长 {item['duration_text']}"
        )
        for item in battles[:8]
    ]
    prompt = (
        "你是一个只基于数据说话的英雄联盟战绩分析助手。\n"
        "请根据下面的最近战绩，给出中文简短分析，控制在 4 段以内：\n"
        "1. 最近状态与胜负走势\n"
        "2. 英雄/模式偏好与发挥特征\n"
        "3. 2 到 4 条具体建议\n"
        "要求：\n"
        "- 只能根据提供的数据分析，不要假装知道分路、段位、队友水平或玩家心态。\n"
        "- 样本少时要明确说样本有限。\n"
        "- 不要使用 Markdown 表格。\n"
        f"账号：{result['nickname']}（{result['area_name']}）\n"
        f"最近 {summary['total']} 场，{summary['wins']} 胜 {summary['losses']} 负，胜率 {summary['win_rate']:.1f}%\n"
        f"场均 K/D/A：{summary['avg_kills']:.1f}/{summary['avg_deaths']:.1f}/{summary['avg_assists']:.1f}\n"
        f"平均时长：{summary['avg_duration_minutes']:.1f} 分钟\n"
        f"常玩英雄：{', '.join(f'{name} x{count}' for name, count in summary['champions']) or '无'}\n"
        f"模式分布：{', '.join(f'{name} x{count}' for name, count in summary['queues']) or '无'}\n"
        "逐场数据：\n"
        + "\n".join(match_lines)
    )
    try:
        provider_id = await context.get_current_chat_provider_id(umo=session_id)
        if not provider_id:
            return _fallback_analysis(result)
        llm_resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        out = str(getattr(llm_resp, "completion_text", None) or "").strip()
        return out or _fallback_analysis(result)
    except Exception as exc:
        logger.warning("LoL 战绩分析失败，回退规则分析: %s", exc)
        return _fallback_analysis(result)


async def query_lol_for_event(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
    *,
    area: int | None = None,
    nickname: str | None = None,
    match_count: int | None = None,
    analyze: bool = False,
    raw_text: str = "",
) -> str:
    cfg = ensure_flat_config(config)
    cookie = str(_get_cfg(cfg, "lol_tencent_cookie", "") or "").strip()
    if not cookie:
        return "未配置 `lol_tencent_cookie`，无法查询腾讯服战绩。"

    default_area = _safe_int(_get_cfg(cfg, "lol_tencent_default_area", _DEFAULT_TENCENT_AREA), _DEFAULT_TENCENT_AREA, 1, 999)
    default_count = _safe_int(_get_cfg(cfg, "lol_query_match_count", _DEFAULT_FETCH_COUNT), _DEFAULT_FETCH_COUNT, 1, 10)
    timeout_seconds = _safe_int(_get_cfg(cfg, "lol_api_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS), _DEFAULT_TIMEOUT_SECONDS, 5, 60)
    account_type = _safe_int(
        _get_cfg(cfg, "lol_tencent_account_type", _DEFAULT_TENCENT_ACCOUNT_TYPE),
        _DEFAULT_TENCENT_ACCOUNT_TYPE,
        1,
        5,
    )
    base_url = str(_get_cfg(cfg, "lol_tencent_base_url", _DEFAULT_TENCENT_BASE_URL) or _DEFAULT_TENCENT_BASE_URL).strip()

    query_spec = _resolve_query_profile(
        event,
        raw_text=raw_text,
        default_area=default_area,
        default_count=default_count,
        area=area,
        nickname=nickname,
        match_count=match_count,
    )
    if not query_spec.get("ok"):
        return str(query_spec.get("error") or "查询参数错误。")

    ok, payload = await _query_tencent_recent_lines(
        cookie=cookie,
        base_url=base_url,
        area=int(query_spec["area"]),
        nickname=str(query_spec["nickname"]),
        account_type=account_type,
        match_count=int(query_spec["count"]),
        timeout_seconds=timeout_seconds,
    )
    resolved_note = ""
    if not ok and _should_try_memory_alias_fallback(str(payload)):
        alias_profile = _resolve_lol_profile_from_person_alias(str(query_spec["nickname"]))
        alias_nickname = str((alias_profile or {}).get("nickname") or "").strip()
        if alias_profile and alias_nickname and alias_nickname != str(query_spec["nickname"]).strip():
            retry_area = (
                int(query_spec["area"])
                if bool(query_spec.get("explicit_area"))
                else _safe_int(alias_profile.get("area"), int(query_spec["area"]), 1, 999)
            )
            ok, payload = await _query_tencent_recent_lines(
                cookie=cookie,
                base_url=base_url,
                area=retry_area,
                nickname=alias_nickname,
                account_type=account_type,
                match_count=int(query_spec["count"]),
                timeout_seconds=timeout_seconds,
            )
            if ok:
                resolved_note = (
                    f"已按记忆面板将“{query_spec['nickname']}”解析为 "
                    f"“{alias_nickname}”"
                    f"（{_format_area_name(retry_area)}）。"
                )
                query_spec["area"] = retry_area
                query_spec["nickname"] = alias_nickname
    if not ok:
        return str(payload)

    result = dict(payload) if isinstance(payload, dict) else {}
    _remember_lol_profile(event, result.get("nickname") or query_spec["nickname"], int(result.get("area") or query_spec["area"]))
    analysis_text = ""
    if analyze:
        analysis_text = await _generate_lol_analysis(context, _safe_session_id(event), result)
    text = _compose_query_text(result, analysis_text=analysis_text)
    return f"{resolved_note}\n{text}" if resolved_note else text


def _format_sub_list(subscriptions: list[dict[str, Any]]) -> str:
    if not subscriptions:
        return "当前群没有 LoL 战绩订阅。"
    lines = ["当前群 LoL 战绩订阅："]
    for idx, item in enumerate(subscriptions, start=1):
        area = _safe_int(item.get("area"), 0, 0, 999)
        lines.append(
            f"{idx}. {str(item.get('display_name') or item.get('nickname') or '未知昵称').strip()} | "
            f"腾讯服 {_format_area_name(area)}({area}) | "
            f"最近推送: {str(item.get('last_push_at') or '暂无').strip() or '暂无'}"
        )
    return "\n".join(lines)


async def _send_group_text(
    context: Context,
    *,
    session_id: str,
    group_id: str,
    text: str,
) -> tuple[bool, str]:
    chain = MessageChain()
    chain.message(text)
    try:
        await context.send_message(session_id, chain)
        return True, ""
    except Exception as exc:
        logger.warning("LoL 订阅通过 session_id 发送失败，尝试群号直发: %s", exc)
    if group_id.isdigit():
        try:
            await StarTools.send_message_by_id(
                type="GroupMessage",
                id=group_id,
                message_chain=chain,
                platform="aiocqhttp",
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)
    return False, "发送失败，且群号不是纯数字。"


class _LolSubscriptionCenter:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = ensure_flat_config(config)
        self._scheduler = None
        self._loop_task: asyncio.Task | None = None
        self._tick_lock = asyncio.Lock()
        self._last_hour_bucket = ""
        self._available = False
        self._start_scheduler()

    @property
    def is_available(self) -> bool:
        return self._available

    def refresh(self, context: Context, config: AstrBotConfig) -> None:
        self.context = context
        self.config = ensure_flat_config(config)
        if self._scheduler is None and (self._loop_task is None or self._loop_task.done()):
            self._start_fallback_loop()

    def _start_scheduler(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            tz = str(_get_cfg(self.config, "lol_subscription_timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
            minute = _safe_int(_get_cfg(self.config, "lol_push_hourly_minute", 0), 0, 0, 59)
            scheduler = AsyncIOScheduler(timezone=tz)
            scheduler.add_job(
                self._safe_run_tick,
                CronTrigger(minute=minute, timezone=tz),
                id="lol_subscription_hourly_tick",
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            self._scheduler = scheduler
            self._available = True
            logger.info("LoL 战绩订阅调度已启动：每小时 %02d 分（%s）", minute, tz)
        except ImportError:
            self._start_fallback_loop()
        except Exception as exc:
            logger.error("LoL 战绩订阅调度启动失败: %s", exc)
            self._start_fallback_loop()

    def _start_fallback_loop(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            self._available = True
            return
        try:
            self._loop_task = asyncio.create_task(self._fallback_loop())
            self._available = True
            logger.info("LoL 战绩订阅 fallback loop 已启动。")
        except Exception as exc:
            self._available = False
            logger.error("LoL 战绩订阅 fallback loop 启动失败: %s", exc)

    async def _fallback_loop(self) -> None:
        minute = _safe_int(_get_cfg(self.config, "lol_push_hourly_minute", 0), 0, 0, 59)
        while True:
            try:
                now = datetime.now()
                bucket = now.strftime("%Y-%m-%d %H")
                if now.minute == minute and bucket != self._last_hour_bucket:
                    self._last_hour_bucket = bucket
                    await self._safe_run_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("LoL 战绩订阅 fallback tick 异常: %s", exc)
            await asyncio.sleep(30)

    async def _safe_run_tick(self) -> None:
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            await self._run_tick()

    async def _run_tick(self) -> None:
        cookie = str(_get_cfg(self.config, "lol_tencent_cookie", "") or "").strip()
        if not cookie:
            logger.warning("LoL 战绩订阅：未配置 lol_tencent_cookie，跳过本轮。")
            return

        base_url = str(_get_cfg(self.config, "lol_tencent_base_url", _DEFAULT_TENCENT_BASE_URL) or _DEFAULT_TENCENT_BASE_URL).strip()
        account_type = _safe_int(
            _get_cfg(self.config, "lol_tencent_account_type", _DEFAULT_TENCENT_ACCOUNT_TYPE),
            _DEFAULT_TENCENT_ACCOUNT_TYPE,
            1,
            5,
        )
        timeout_seconds = _safe_int(
            _get_cfg(self.config, "lol_api_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS),
            _DEFAULT_TIMEOUT_SECONDS,
            5,
            60,
        )
        query_count = _safe_int(
            _get_cfg(self.config, "lol_query_match_count", _DEFAULT_FETCH_COUNT),
            _DEFAULT_FETCH_COUNT,
            1,
            10,
        )
        push_limit = _safe_int(
            _get_cfg(self.config, "lol_subscription_push_limit", _DEFAULT_PUSH_LIMIT),
            _DEFAULT_PUSH_LIMIT,
            1,
            5,
        )

        async with _SUBSCRIPTION_LOCK:
            subscriptions = _load_subscriptions()
        if not subscriptions:
            return

        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await _ensure_champion_name_cache(session)
            dirty = False
            for sub in subscriptions:
                try:
                    changed, push_text = await self._check_single_subscription_tencent(
                        session,
                        sub=sub,
                        cookie=cookie,
                        base_url=base_url,
                        account_type=account_type,
                        query_count=max(query_count, push_limit + 1),
                        push_limit=push_limit,
                    )
                    if changed:
                        dirty = True
                    if not push_text:
                        continue

                    ok, err = await _send_group_text(
                        self.context,
                        session_id=str(sub.get("session_id") or ""),
                        group_id=str(sub.get("group_id") or ""),
                        text=push_text,
                    )
                    if ok:
                        sub["last_push_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        dirty = True
                    else:
                        label = str(sub.get("display_name") or sub.get("nickname") or "").strip()
                        logger.warning(
                            "LoL 战绩订阅推送失败 group_id=%s target=%s err=%s",
                            str(sub.get("group_id") or ""),
                            label,
                            err,
                        )
                    await asyncio.sleep(0.2)
                except Exception as exc:
                    logger.error("LoL 战绩订阅检查失败: %s", exc)
            if dirty:
                _save_subscriptions(subscriptions)

    async def _check_single_subscription_tencent(
        self,
        session: aiohttp.ClientSession,
        *,
        sub: dict[str, Any],
        cookie: str,
        base_url: str,
        account_type: int,
        query_count: int,
        push_limit: int,
    ) -> tuple[bool, str]:
        openid = str(sub.get("openid") or "").strip()
        area = _safe_int(sub.get("area"), 0, 0, 999)
        nickname = str(sub.get("nickname") or "未命名召唤师").strip() or "未命名召唤师"
        if not openid or area <= 0:
            return False, ""

        list_ok, list_data = await _fetch_tencent_battle_list(
            session,
            base_url=base_url,
            cookie=cookie,
            area=area,
            openid=openid,
            account_type=account_type,
            count=query_count,
            offset=0,
        )
        sub["last_check_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not list_ok:
            logger.warning("LoL 战绩订阅读取腾讯战绩失败 nickname=%s: %s", nickname, list_data)
            return True, ""

        battles = list_data if isinstance(list_data, list) else []
        if not battles:
            return True, ""

        latest_match_id = str(battles[0].get("game_id") or "").strip()
        if not latest_match_id:
            return True, ""
        last_match_id = str(sub.get("last_match_id") or "").strip()
        if not last_match_id:
            sub["last_match_id"] = latest_match_id
            return True, ""
        if latest_match_id == last_match_id:
            return True, ""

        new_battles: list[dict[str, Any]] = []
        seen_old = False
        for battle in battles:
            game_id = str(battle.get("game_id") or "").strip()
            if not game_id:
                continue
            if game_id == last_match_id:
                seen_old = True
                break
            new_battles.append(battle)

        if not new_battles:
            sub["last_match_id"] = latest_match_id
            return True, ""

        trimmed_new = list(reversed(new_battles[:push_limit]))
        lines = [_format_tencent_battle_line(item) for item in trimmed_new if _normalize_tencent_battle(item)]
        sub["last_match_id"] = latest_match_id
        if not lines:
            return True, ""

        header = (
            f"LoL 战绩订阅更新\n"
            f"账号：{nickname}\n"
            f"大区：{area}（{_format_area_name(area)}）\n"
            f"检测时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if not seen_old and len(new_battles) > len(trimmed_new):
            header += f"\n提示：本次新对局较多，仅推送最近 {len(trimmed_new)} 场。"
        return True, header + "\n" + "\n".join(lines)


_LOL_CENTER: _LolSubscriptionCenter | None = None


def init_lol_subscription_center(
    context: Context,
    config: AstrBotConfig,
) -> _LolSubscriptionCenter | None:
    global _LOL_CENTER
    if _LOL_CENTER is not None:
        _LOL_CENTER.refresh(context, config)
        return _LOL_CENTER if _LOL_CENTER.is_available else None
    center = _LolSubscriptionCenter(context, config)
    if center.is_available:
        _LOL_CENTER = center
        return center
    return None


async def handle_lol_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    cfg = ensure_flat_config(config)
    init_lol_subscription_center(context, cfg)

    cookie = str(_get_cfg(cfg, "lol_tencent_cookie", "") or "").strip()
    default_area = _safe_int(_get_cfg(cfg, "lol_tencent_default_area", _DEFAULT_TENCENT_AREA), _DEFAULT_TENCENT_AREA, 1, 999)
    default_count = _safe_int(_get_cfg(cfg, "lol_query_match_count", _DEFAULT_FETCH_COUNT), _DEFAULT_FETCH_COUNT, 1, 10)
    timeout_seconds = _safe_int(_get_cfg(cfg, "lol_api_timeout_seconds", _DEFAULT_TIMEOUT_SECONDS), _DEFAULT_TIMEOUT_SECONDS, 5, 60)
    account_type = _safe_int(
        _get_cfg(cfg, "lol_tencent_account_type", _DEFAULT_TENCENT_ACCOUNT_TYPE),
        _DEFAULT_TENCENT_ACCOUNT_TYPE,
        1,
        5,
    )
    base_url = str(_get_cfg(cfg, "lol_tencent_base_url", _DEFAULT_TENCENT_BASE_URL) or _DEFAULT_TENCENT_BASE_URL).strip()

    raw = str(event.get_message_str() or "").strip()
    matched = re.match(r"^[\/!！]?(?:lol|LOL|英雄联盟|联盟战绩)(?:\s+(.+))?$", raw)
    body = (matched.group(1) or "").strip() if matched else ""
    if not body:
        yield event.plain_result(_build_help_text(default_area))
        return

    parts = body.split(maxsplit=1)
    action = str(parts[0] if parts else "").strip().lower()
    extra = str(parts[1] if len(parts) > 1 else "").strip()

    if action in {"help", "帮助", "h"}:
        yield event.plain_result(_build_help_text(default_area))
        return

    if action in {"绑定", "bind", "记住"}:
        if not cookie:
            yield event.plain_result("未配置 `lol_tencent_cookie`，无法校验并绑定腾讯服账号。")
            return
        query_spec = _resolve_query_profile(
            event,
            raw_text=extra,
            default_area=default_area,
            default_count=1,
        )
        if not query_spec.get("ok"):
            yield event.plain_result(str(query_spec.get("error") or "绑定参数错误。"))
            return
        ok, payload = await _query_tencent_recent_lines(
            cookie=cookie,
            base_url=base_url,
            area=int(query_spec["area"]),
            nickname=str(query_spec["nickname"]),
            account_type=account_type,
            match_count=1,
            timeout_seconds=timeout_seconds,
        )
        if not ok:
            yield event.plain_result(str(payload))
            return
        result = dict(payload) if isinstance(payload, dict) else {}
        _remember_lol_profile(event, str(result.get("nickname") or query_spec["nickname"]), int(result.get("area") or query_spec["area"]))
        text = (
            f"已记住你的 LoL 默认账号：{result.get('nickname') or query_spec['nickname']} "
            f"（{result.get('area_name') or _format_area_name(int(query_spec['area']))}）\n"
            "后续可以直接说 `/lol 战绩`、`/lol 分析`，或在群里直接 `/lol 订阅`。"
        )
        if result.get("pick_message"):
            text += f"\n提示：{result['pick_message']}"
        yield event.plain_result(text)
        return

    if action in {"订阅", "subscribe", "sub"}:
        if _safe_is_private_chat(event):
            yield event.plain_result("LoL 战绩订阅仅支持群聊使用。请在目标 QQ 群里发送该命令。")
            return
        group_id = _safe_group_id(event)
        session_id = _safe_session_id(event)
        if not group_id or not session_id:
            yield event.plain_result("无法识别当前群会话，订阅失败。")
            return
        if not cookie:
            yield event.plain_result("未配置 `lol_tencent_cookie`，无法查询腾讯服战绩。")
            return

        query_spec = _resolve_query_profile(
            event,
            raw_text=extra,
            default_area=default_area,
            default_count=default_count,
        )
        if not query_spec.get("ok"):
            yield event.plain_result(str(query_spec.get("error") or "订阅参数错误。"))
            return

        ok, payload = await _query_tencent_recent_lines(
            cookie=cookie,
            base_url=base_url,
            area=int(query_spec["area"]),
            nickname=str(query_spec["nickname"]),
            account_type=account_type,
            match_count=int(query_spec["count"]),
            timeout_seconds=timeout_seconds,
        )
        if not ok:
            yield event.plain_result(str(payload))
            return

        result = dict(payload) if isinstance(payload, dict) else {}
        openid = str(result.get("openid") or "").strip()
        display_name = str(result.get("nickname") or query_spec["nickname"]).strip() or str(query_spec["nickname"])
        if not openid:
            yield event.plain_result("未能解析到该账号的 openid，订阅失败。")
            return

        _remember_lol_profile(event, display_name, int(result.get("area") or query_spec["area"]))
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        creator_id = _safe_sender_id(event)
        creator_name = _safe_sender_name(event)
        sub_id = f"{group_id}|tencent|{int(result.get('area') or query_spec['area'])}|{openid}"
        preview = result.get("lines") or []
        preview_text = preview[0] if preview else "暂无最近战绩。"

        async with _SUBSCRIPTION_LOCK:
            subscriptions = _load_subscriptions()
            updated = False
            for item in subscriptions:
                if str(item.get("id") or "") != sub_id:
                    continue
                item.update(
                    {
                        "id": sub_id,
                        "mode": "tencent",
                        "session_id": session_id,
                        "group_id": group_id,
                        "openid": openid,
                        "area": int(result.get("area") or query_spec["area"]),
                        "nickname": display_name,
                        "display_name": display_name,
                        "account_type": account_type,
                        "updated_at": now_text,
                        "created_by": creator_id,
                        "created_by_name": creator_name,
                        "last_match_id": str((result.get("battles") or [{}])[0].get("game_id") or item.get("last_match_id") or "").strip(),
                    }
                )
                updated = True
                break
            if not updated:
                subscriptions.append(
                    {
                        "id": sub_id,
                        "mode": "tencent",
                        "session_id": session_id,
                        "group_id": group_id,
                        "openid": openid,
                        "area": int(result.get("area") or query_spec["area"]),
                        "nickname": display_name,
                        "display_name": display_name,
                        "account_type": account_type,
                        "created_by": creator_id,
                        "created_by_name": creator_name,
                        "created_at": now_text,
                        "updated_at": now_text,
                        "last_match_id": str((result.get("battles") or [{}])[0].get("game_id") or "").strip(),
                        "last_push_at": "",
                        "last_check_at": "",
                    }
                )
            _save_subscriptions(subscriptions)

        action_text = "已更新" if updated else "已新增"
        message = (
            f"{action_text} LoL 群战绩订阅：{display_name}（{result.get('area_name') or _format_area_name(int(query_spec['area']))}）\n"
            "系统将每小时自动检查并在该群推送新战绩。\n"
            f"当前最近一场：\n{preview_text}"
        )
        if result.get("pick_message"):
            message += f"\n提示：{result['pick_message']}"
        yield event.plain_result(message)
        return

    if action in {"订阅列表", "列表", "list", "ls"}:
        group_id = _safe_group_id(event)
        if not group_id:
            yield event.plain_result("LoL 订阅列表只能在群聊中查看。")
            return
        group_subs = [item for item in _load_subscriptions() if str(item.get("group_id") or "").strip() == group_id]
        yield event.plain_result(_format_sub_list(group_subs))
        return

    if action in {"退订", "取消", "取消订阅", "unsubscribe", "unsub"}:
        group_id = _safe_group_id(event)
        if not group_id:
            yield event.plain_result("LoL 退订只能在群聊中使用。")
            return
        group_subs = [item for item in _load_subscriptions() if str(item.get("group_id") or "").strip() == group_id]
        if not group_subs:
            yield event.plain_result("当前群没有 LoL 战绩订阅。")
            return
        target = extra.strip()
        if not target:
            yield event.plain_result("用法：/lol 退订 <序号> 或 /lol 退订 全部")
            return

        async with _SUBSCRIPTION_LOCK:
            all_subs = _load_subscriptions()
            if target in {"全部", "all", "ALL"}:
                kept = [item for item in all_subs if str(item.get("group_id") or "").strip() != group_id]
                _save_subscriptions(kept)
                yield event.plain_result("退订成功。")
                return

            if not re.fullmatch(r"\d+", target):
                yield event.plain_result("用法：/lol 退订 <序号> 或 /lol 退订 全部")
                return

            idx = int(target)
            if idx <= 0 or idx > len(group_subs):
                yield event.plain_result(f"序号超出范围，请输入 1 到 {len(group_subs)}。")
                return

            removed_id = str(group_subs[idx - 1].get("id") or "").strip()
            kept = [item for item in all_subs if str(item.get("id") or "").strip() != removed_id]
            _save_subscriptions(kept)
        yield event.plain_result("退订成功。")
        return

    if action in {"分析", "analyze", "analyse"}:
        result_text = await query_lol_for_event(
            event,
            context,
            cfg,
            analyze=True,
            raw_text=extra,
        )
        yield event.plain_result(result_text)
        return

    result_text = await query_lol_for_event(
        event,
        context,
        cfg,
        analyze=False,
        raw_text=extra if action in {"查询", "查", "query", "q", "战绩"} else body,
    )
    yield event.plain_result(result_text)
