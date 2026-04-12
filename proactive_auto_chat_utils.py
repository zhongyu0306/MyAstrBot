from __future__ import annotations

import asyncio
import inspect
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.star import Context, StarTools

from .memory_state_store import load_json_state, save_json_state


SCENE_GROUP = "group"
SCENE_PRIVATE = "private"
_DEFAULT_PLATFORM = "aiocqhttp"
_STATE_FILE_NAME = "proactive_auto_chat_runtime.json"
_STATE_NAMESPACE = "proactive_auto_chat_runtime"
_STATE_LOCK = asyncio.Lock()


def _cfg_value(config: AstrBotConfig | dict | None, key: str, default: Any = None) -> Any:
    if config is None:
        return default

    try:
        value = getattr(config, key)
        if value is not None:
            return value
    except Exception:
        pass

    if isinstance(config, dict):
        if key in config:
            value = config.get(key)
            if value is not None:
                return value
        group = config.get("proactive")
        if isinstance(group, dict):
            if key in group:
                value = group.get(key)
                if value is not None:
                    return value
            items = group.get("items")
            if isinstance(items, dict):
                raw = items.get(key)
                if isinstance(raw, dict) and "value" in raw:
                    raw = raw.get("value")
                if raw is not None:
                    return raw

    try:
        getter = getattr(config, "get", None)
        if callable(getter):
            value = getter(key)
            if value is not None:
                return value
            group = getter("proactive")
            if isinstance(group, dict):
                value = group.get(key)
                if value is not None:
                    return value
                items = group.get("items")
                if isinstance(items, dict):
                    raw = items.get(key)
                    if isinstance(raw, dict) and "value" in raw:
                        raw = raw.get("value")
                    if raw is not None:
                        return raw
    except Exception:
        pass

    return default


def _normalize_digit_list(raw_value: Any) -> tuple[str, ...]:
    if isinstance(raw_value, (list, tuple, set)):
        iterable = list(raw_value)
    else:
        text = str(raw_value or "").strip()
        iterable = re.split(r"[,，\s]+", text) if text else []

    seen: set[str] = set()
    result: list[str] = []
    for item in iterable:
        value = str(item or "").strip()
        if not value or not value.isdigit() or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return tuple(result)


def _get_timezone_name(config: AstrBotConfig | dict | None) -> str:
    value = str(_cfg_value(config, "proactive_timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
    return value or "Asia/Shanghai"


def _get_timezone(config: AstrBotConfig | dict | None) -> ZoneInfo:
    tz_name = _get_timezone_name(config)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("主动消息时区配置无效，将回退到 Asia/Shanghai: %s", tz_name)
        return ZoneInfo("Asia/Shanghai")


def _now(config: AstrBotConfig | dict | None) -> datetime:
    return datetime.now(_get_timezone(config))


def _dt_to_str(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _str_to_dt(value: Any, config: AstrBotConfig | dict | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_get_timezone(config))
    except Exception:
        return None


def _safe_format(template: str, values: dict[str, Any]) -> str:
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return str(template or "").format_map(_SafeDict(values))


def _state_path() -> Path:
    base = Path(StarTools.get_data_dir("astrbot_all_char"))
    base.mkdir(parents=True, exist_ok=True)
    return base / _STATE_FILE_NAME


def _load_runtime_state() -> dict[str, dict[str, Any]]:
    try:
        data = load_json_state(
            _STATE_NAMESPACE,
            default={},
            normalizer=lambda value: value if isinstance(value, dict) else {},
            legacy_path=_state_path(),
        )
    except Exception as exc:
        logger.error("读取主动聊天运行状态失败: %s", exc)
        return {}

    result: dict[str, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return result
    for key, value in data.items():
        if isinstance(value, dict):
            result[str(key)] = value
    return result


def _save_runtime_state(data: dict[str, dict[str, Any]]) -> None:
    try:
        save_json_state(_STATE_NAMESPACE, data)
    except Exception as exc:
        logger.error("保存主动聊天运行状态失败: %s", exc)
        raise


def _get_platform(config: AstrBotConfig | dict | None) -> str:
    value = str(_cfg_value(config, "proactive_send_platform", _DEFAULT_PLATFORM) or _DEFAULT_PLATFORM).strip()
    return value or _DEFAULT_PLATFORM


def _build_target_umo(platform: str, scene_type: str, target_id: str) -> str:
    if not platform or not target_id:
        return ""
    message_type = "GroupMessage" if scene_type == SCENE_GROUP else "FriendMessage"
    return f"{platform}:{message_type}:{target_id}"


def _parse_umo(value: str) -> tuple[str, str, str] | None:
    raw = str(value or "").strip()
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return None
    platform, message_type, target_id = (part.strip() for part in parts)
    if not platform or not message_type or not target_id:
        return None
    return platform, message_type, target_id


def _configured_target_ids(config: AstrBotConfig | dict | None) -> dict[str, tuple[str, ...]]:
    return {
        SCENE_PRIVATE: _normalize_digit_list(_cfg_value(config, "proactive_private_qq_ids", None)),
        SCENE_GROUP: _normalize_digit_list(_cfg_value(config, "proactive_group_ids", None)),
    }


def _extract_target_from_umo(
    umo: str,
    *,
    config: AstrBotConfig | dict | None,
) -> dict[str, str] | None:
    parsed = _parse_umo(umo)
    if not parsed:
        return None

    _, message_type, target_id = parsed
    if "Group" in message_type:
        scene_type = SCENE_GROUP
    elif "Friend" in message_type or "Private" in message_type:
        scene_type = SCENE_PRIVATE
    else:
        return None

    if not target_id.isdigit():
        return None

    configured = _configured_target_ids(config)
    if scene_type == SCENE_GROUP and target_id not in configured[SCENE_GROUP]:
        return None
    if scene_type == SCENE_PRIVATE and target_id not in configured[SCENE_PRIVATE]:
        return None

    return {
        "scene_type": scene_type,
        "target_id": target_id,
        "umo": _build_target_umo(_get_platform(config), scene_type, target_id),
    }


def _configured_target_specs(config: AstrBotConfig | dict | None) -> list[dict[str, str]]:
    platform = _get_platform(config)
    configured = _configured_target_ids(config)
    specs: list[dict[str, str]] = []
    for target_id in configured[SCENE_PRIVATE]:
        specs.append(
            {
                "scene_type": SCENE_PRIVATE,
                "target_id": target_id,
                "umo": _build_target_umo(platform, SCENE_PRIVATE, target_id),
            }
        )
    for target_id in configured[SCENE_GROUP]:
        specs.append(
            {
                "scene_type": SCENE_GROUP,
                "target_id": target_id,
                "umo": _build_target_umo(platform, SCENE_GROUP, target_id),
            }
        )
    return specs


def _bool_cfg(config: AstrBotConfig | dict | None, key: str, default: bool) -> bool:
    return bool(_cfg_value(config, key, default))


def _int_cfg(
    config: AstrBotConfig | dict | None,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(_cfg_value(config, key, default))
    except Exception:
        value = default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _get_min_interval_minutes(config: AstrBotConfig | dict | None) -> int:
    return _int_cfg(config, "proactive_min_interval_minutes", 30, 1, 10080)


def _get_max_interval_minutes(config: AstrBotConfig | dict | None) -> int:
    minimum = _get_min_interval_minutes(config)
    return _int_cfg(config, "proactive_max_interval_minutes", max(minimum, 180), minimum, 10080)


def _get_auto_trigger_minutes(config: AstrBotConfig | dict | None) -> int:
    return _int_cfg(config, "proactive_auto_trigger_after_minutes", 30, 1, 10080)


def _get_max_unanswered_times(config: AstrBotConfig | dict | None) -> int:
    return _int_cfg(config, "proactive_max_unanswered_times", 3, 0, 1000)


def _get_history_limit(config: AstrBotConfig | dict | None) -> int:
    return _int_cfg(config, "proactive_history_limit", 12, 0, 50)


def _get_quiet_hours(config: AstrBotConfig | dict | None) -> str:
    return str(_cfg_value(config, "proactive_quiet_hours", "1-7") or "").strip()


def _get_legacy_slot_content(config: AstrBotConfig | dict | None) -> tuple[str, str]:
    slots = _cfg_value(config, "proactive_time_slots", None)
    if not isinstance(slots, list):
        return "", ""
    for item in slots:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled", True)):
            continue
        message_text = str(item.get("message_text") or "").strip()
        prompt_template = str(item.get("prompt_template") or "").strip()
        if message_text or prompt_template:
            return message_text, prompt_template
    return "", ""


def _get_message_template(config: AstrBotConfig | dict | None) -> str:
    current = str(_cfg_value(config, "proactive_message_text", "") or "").strip()
    if current:
        return current
    legacy_text, _ = _get_legacy_slot_content(config)
    if legacy_text:
        return legacy_text
    return "在忙吗？我来冒个泡。"


def _get_prompt_template(config: AstrBotConfig | dict | None) -> str:
    current = str(_cfg_value(config, "proactive_prompt_template", "") or "").strip()
    if current:
        return current
    _, legacy_prompt = _get_legacy_slot_content(config)
    return legacy_prompt


def _build_template_values(
    *,
    now: datetime,
    scene_type: str,
    target_id: str,
    unanswered_count: int,
) -> dict[str, Any]:
    return {
        "current_time": _dt_to_str(now),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
        "target_type": scene_type,
        "target_label": "QQ群" if scene_type == SCENE_GROUP else "QQ私聊",
        "target_id": target_id,
        "unanswered_count": unanswered_count,
    }


def _random_next_time(now: datetime, config: AstrBotConfig | dict | None) -> datetime:
    minimum = _get_min_interval_minutes(config)
    maximum = _get_max_interval_minutes(config)
    delay = minimum if minimum >= maximum else random.randint(minimum, maximum)
    return now + timedelta(minutes=delay)


def _build_initial_record(
    spec: dict[str, str],
    *,
    now: datetime,
    config: AstrBotConfig | dict | None,
) -> dict[str, Any]:
    next_trigger_at = ""
    if _bool_cfg(config, "proactive_auto_trigger_enabled", True):
        next_trigger_at = _dt_to_str(now + timedelta(minutes=_get_auto_trigger_minutes(config)))
    return {
        "scene_type": spec["scene_type"],
        "target_id": spec["target_id"],
        "umo": spec["umo"],
        "last_user_message_at": "",
        "last_bot_message_at": "",
        "last_activity_at": "",
        "last_proactive_at": "",
        "next_trigger_at": next_trigger_at,
        "unanswered_count": 0,
        "updated_at": _dt_to_str(now),
    }


def _schedule_next_random(record: dict[str, Any], *, now: datetime, config: AstrBotConfig | dict | None) -> None:
    record["next_trigger_at"] = _dt_to_str(_random_next_time(now, config))
    record["updated_at"] = _dt_to_str(now)


def _quiet_range(config: AstrBotConfig | dict | None) -> tuple[int, int] | None:
    quiet = _get_quiet_hours(config)
    matched = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", quiet)
    if not matched:
        return None
    start_hour = int(matched.group(1))
    end_hour = int(matched.group(2))
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        return None
    return start_hour, end_hour


def _is_in_quiet_hours(now: datetime, config: AstrBotConfig | dict | None) -> bool:
    quiet = _quiet_range(config)
    if quiet is None:
        return False
    start_hour, end_hour = quiet
    if start_hour == end_hour:
        return False
    hour = now.hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _quiet_hours_end(now: datetime, config: AstrBotConfig | dict | None) -> datetime | None:
    quiet = _quiet_range(config)
    if quiet is None:
        return None
    start_hour, end_hour = quiet
    if start_hour == end_hour:
        return None
    if not _is_in_quiet_hours(now, config):
        return None

    candidate = now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if start_hour < end_hour:
        return candidate
    if now.hour >= start_hour:
        return (candidate + timedelta(days=1)) if candidate <= now else candidate
    return candidate


async def _call_maybe_async(func: Any, *args: Any, **kwargs: Any) -> Any:
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _extract_history_text(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        role = str(item.get("role") or item.get("sender") or "").strip()
        content = str(item.get("content") or item.get("text") or item.get("message") or "").strip()
        merged = f"{role}: {content}".strip(": ").strip()
        return merged

    for key in ("content", "text", "message", "msg", "raw_message"):
        value = getattr(item, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        return str(item).strip()
    except Exception:
        return ""


async def _fetch_recent_chat_history_lines(
    context: Context,
    *,
    umo: str,
    limit: int,
) -> list[str]:
    if not umo or limit <= 0:
        return []

    candidate_calls = [
        ("get_conversation_history", {"umo": umo, "limit": limit}),
        ("get_chat_history", {"umo": umo, "limit": limit}),
        ("get_recent_messages", {"umo": umo, "limit": limit}),
        ("get_session_history", {"session_id": umo, "limit": limit}),
        ("load_conversation_history", {"umo": umo, "limit": limit}),
        ("load_chat_history", {"umo": umo, "limit": limit}),
    ]

    for name, kwargs in candidate_calls:
        fn = getattr(context, name, None)
        if not callable(fn):
            continue
        try:
            data = await _call_maybe_async(fn, **kwargs)
        except TypeError:
            try:
                data = await _call_maybe_async(fn, umo, limit)
            except Exception:
                continue
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]
        lines = [_extract_history_text(item) for item in items[-limit:]]
        lines = [line for line in lines if line]
        if lines:
            return lines[-limit:]

    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is None:
        return []

    try:
        curr_cid = await _call_maybe_async(conv_mgr.get_curr_conversation_id, umo)
        if not curr_cid:
            return []
        conversation = await _call_maybe_async(conv_mgr.get_conversation, umo, curr_cid)
        history_raw = getattr(conversation, "history", None) if conversation is not None else None
        if not history_raw:
            return []
        parsed = json.loads(history_raw)
        if not isinstance(parsed, list):
            return []
        lines = [_extract_history_text(item) for item in parsed[-limit:]]
        return [line for line in lines if line][-limit:]
    except Exception:
        return []


async def _resolve_provider_id(
    context: Context,
    *,
    config: AstrBotConfig | dict | None,
    target_umo: str,
) -> str:
    explicit_umo = str(_cfg_value(config, "proactive_llm_umo", "") or "").strip()
    candidates: list[str] = []
    for value in (explicit_umo, target_umo):
        if value and value not in candidates:
            candidates.append(value)

    for umo in candidates:
        try:
            provider_id = await context.get_current_chat_provider_id(umo=umo)
            if provider_id:
                return provider_id
        except Exception:
            continue

    try:
        provider = context.get_using_provider(target_umo)
        if provider:
            return provider.meta().id
    except Exception:
        pass

    try:
        provider = context.get_using_provider()
        if provider:
            return provider.meta().id
    except Exception:
        pass

    return ""


class _LightweightProactiveChatCenter:
    def __init__(self, context: Context, config: AstrBotConfig | dict) -> None:
        self.context = context
        self.config = config
        self._scheduler = None
        self._loop_task: asyncio.Task | None = None
        self._tick_lock = asyncio.Lock()
        self._inflight_proactive_targets: set[str] = set()
        self._available = False
        self._start_scheduler()

    @property
    def is_available(self) -> bool:
        return self._available

    def refresh(self, context: Context, config: AstrBotConfig | dict) -> None:
        self.context = context
        self.config = config
        if self._scheduler is None and (self._loop_task is None or self._loop_task.done()):
            self._start_fallback_loop()

    def _start_scheduler(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.interval import IntervalTrigger

            tz_name = _get_timezone_name(self.config)
            scheduler = AsyncIOScheduler(timezone=tz_name)
            scheduler.add_job(
                self._safe_run_tick,
                IntervalTrigger(seconds=30, timezone=tz_name),
                id="proactive_auto_chat_tick",
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            self._scheduler = scheduler
            self._available = True
            logger.info("轻量主动聊天调度已启动，时区=%s", tz_name)
        except ImportError:
            self._scheduler = None
            self._start_fallback_loop()
        except Exception as exc:
            self._scheduler = None
            logger.error("轻量主动聊天调度启动失败: %s", exc)
            self._start_fallback_loop()

    def _start_fallback_loop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._available = True
            return
        try:
            self._loop_task = asyncio.create_task(self._fallback_loop())
            self._available = True
            logger.info("轻量主动聊天回退循环已启动")
        except RuntimeError as exc:
            logger.error("轻量主动聊天回退循环启动失败: %s", exc)
            self._loop_task = None
            self._available = False

    async def _fallback_loop(self) -> None:
        while True:
            try:
                await self._safe_run_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("轻量主动聊天回退循环执行失败: %s", exc)
            await asyncio.sleep(15)

    async def _safe_run_tick(self) -> None:
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            await self._run_tick()

    async def _run_tick(self) -> None:
        if not _bool_cfg(self.config, "proactive_enabled", False):
            return

        now = _now(self.config)
        records = await self._ensure_targets(now)
        if not records:
            return

        due_records = []
        for record in records.values():
            next_trigger_at = _str_to_dt(record.get("next_trigger_at"), self.config)
            if next_trigger_at is None:
                continue
            if next_trigger_at <= now:
                due_records.append(record)

        for record in due_records:
            await self._handle_due_record(record)

    async def _ensure_targets(self, now: datetime) -> dict[str, dict[str, Any]]:
        specs = _configured_target_specs(self.config)
        desired = {spec["umo"]: spec for spec in specs if spec.get("umo")}
        async with _STATE_LOCK:
            state = _load_runtime_state()
            changed = False

            for key in list(state):
                if key not in desired:
                    del state[key]
                    changed = True

            for key, spec in desired.items():
                record = state.get(key)
                if not isinstance(record, dict):
                    state[key] = _build_initial_record(spec, now=now, config=self.config)
                    changed = True
                    continue

                if str(record.get("scene_type") or "") != spec["scene_type"]:
                    record["scene_type"] = spec["scene_type"]
                    changed = True
                if str(record.get("target_id") or "") != spec["target_id"]:
                    record["target_id"] = spec["target_id"]
                    changed = True
                if str(record.get("umo") or "") != spec["umo"]:
                    record["umo"] = spec["umo"]
                    changed = True

                if not str(record.get("next_trigger_at") or "").strip():
                    if (
                        not str(record.get("last_activity_at") or "").strip()
                        and _bool_cfg(self.config, "proactive_auto_trigger_enabled", True)
                    ):
                        record["next_trigger_at"] = _dt_to_str(
                            now + timedelta(minutes=_get_auto_trigger_minutes(self.config))
                        )
                        changed = True

                record["updated_at"] = str(record.get("updated_at") or _dt_to_str(now))
                state[key] = record

            if changed:
                _save_runtime_state(state)
            return {key: dict(value) for key, value in state.items()}

    async def observe_incoming_event(self, event: AstrMessageEvent) -> None:
        if not _bool_cfg(self.config, "proactive_enabled", False):
            return
        if self._is_self_event(event):
            return

        target = _extract_target_from_umo(
            str(getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "") or ""),
            config=self.config,
        )
        if target is None:
            return

        now = _now(self.config)
        async with _STATE_LOCK:
            state = _load_runtime_state()
            record = state.get(target["umo"])
            if not isinstance(record, dict):
                record = _build_initial_record(target, now=now, config=self.config)

            record["scene_type"] = target["scene_type"]
            record["target_id"] = target["target_id"]
            record["umo"] = target["umo"]
            record["last_user_message_at"] = _dt_to_str(now)
            record["last_activity_at"] = _dt_to_str(now)
            record["unanswered_count"] = 0
            _schedule_next_random(record, now=now, config=self.config)
            state[target["umo"]] = record
            _save_runtime_state(state)

    async def observe_sent_event(self, event: AstrMessageEvent) -> None:
        if not _bool_cfg(self.config, "proactive_enabled", False):
            return

        target = _extract_target_from_umo(
            str(getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "") or ""),
            config=self.config,
        )
        if target is None:
            return
        if target["umo"] in self._inflight_proactive_targets:
            return

        now = _now(self.config)
        async with _STATE_LOCK:
            state = _load_runtime_state()
            record = state.get(target["umo"])
            if not isinstance(record, dict):
                record = _build_initial_record(target, now=now, config=self.config)

            record["scene_type"] = target["scene_type"]
            record["target_id"] = target["target_id"]
            record["umo"] = target["umo"]
            record["last_bot_message_at"] = _dt_to_str(now)
            record["last_activity_at"] = _dt_to_str(now)
            _schedule_next_random(record, now=now, config=self.config)
            state[target["umo"]] = record
            _save_runtime_state(state)

    def _is_self_event(self, event: AstrMessageEvent) -> bool:
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:
            sender_id = ""
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        return bool(sender_id and self_id and sender_id == self_id)

    async def _handle_due_record(self, record: dict[str, Any]) -> None:
        target_umo = str(record.get("umo") or "").strip()
        scene_type = str(record.get("scene_type") or "").strip()
        target_id = str(record.get("target_id") or "").strip()
        if not target_umo or scene_type not in {SCENE_GROUP, SCENE_PRIVATE} or not target_id:
            return

        now = _now(self.config)
        if _is_in_quiet_hours(now, self.config):
            await self._defer_until_quiet_hours_end(target_umo, now)
            return

        unanswered_count = int(record.get("unanswered_count", 0) or 0)
        max_unanswered = _get_max_unanswered_times(self.config)
        if max_unanswered > 0 and unanswered_count >= max_unanswered:
            await self._clear_next_trigger(target_umo)
            return

        snapshot_activity = str(record.get("last_activity_at") or "")
        snapshot_unanswered = unanswered_count

        text = await self._build_message_text(
            scene_type=scene_type,
            target_id=target_id,
            target_umo=target_umo,
            unanswered_count=unanswered_count,
        )
        if not text:
            return

        current = await self._get_record(target_umo)
        if current is None:
            return
        if str(current.get("last_activity_at") or "") != snapshot_activity:
            return
        if int(current.get("unanswered_count", 0) or 0) != snapshot_unanswered:
            return

        self._inflight_proactive_targets.add(target_umo)
        try:
            ok, err = await self._send_by_target_id(scene_type=scene_type, target_id=target_id, text=text)
            finished_at = _now(self.config)
            async with _STATE_LOCK:
                state = _load_runtime_state()
                latest = state.get(target_umo)
                if not isinstance(latest, dict):
                    return

                if str(latest.get("last_activity_at") or "") != snapshot_activity:
                    return
                if int(latest.get("unanswered_count", 0) or 0) != snapshot_unanswered:
                    return

                if ok:
                    latest["last_bot_message_at"] = _dt_to_str(finished_at)
                    latest["last_activity_at"] = _dt_to_str(finished_at)
                    latest["last_proactive_at"] = _dt_to_str(finished_at)
                    latest["unanswered_count"] = snapshot_unanswered + 1
                    _schedule_next_random(latest, now=finished_at, config=self.config)
                    logger.info("主动聊天已发送：%s %s", scene_type, target_id)
                else:
                    _schedule_next_random(latest, now=finished_at, config=self.config)
                    logger.warning("主动聊天发送失败：%s %s err=%s", scene_type, target_id, err)

                state[target_umo] = latest
                _save_runtime_state(state)
        finally:
            self._inflight_proactive_targets.discard(target_umo)

    async def _get_record(self, target_umo: str) -> dict[str, Any] | None:
        async with _STATE_LOCK:
            state = _load_runtime_state()
            record = state.get(target_umo)
            return dict(record) if isinstance(record, dict) else None

    async def _clear_next_trigger(self, target_umo: str) -> None:
        async with _STATE_LOCK:
            state = _load_runtime_state()
            record = state.get(target_umo)
            if not isinstance(record, dict):
                return
            if not str(record.get("next_trigger_at") or "").strip():
                return
            record["next_trigger_at"] = ""
            record["updated_at"] = _dt_to_str(_now(self.config))
            state[target_umo] = record
            _save_runtime_state(state)

    async def _defer_until_quiet_hours_end(self, target_umo: str, now: datetime) -> None:
        quiet_end = _quiet_hours_end(now, self.config)
        if quiet_end is None:
            quiet_end = now + timedelta(minutes=30)
        async with _STATE_LOCK:
            state = _load_runtime_state()
            record = state.get(target_umo)
            if not isinstance(record, dict):
                return
            record["next_trigger_at"] = _dt_to_str(quiet_end)
            record["updated_at"] = _dt_to_str(now)
            state[target_umo] = record
            _save_runtime_state(state)

    async def _build_message_text(
        self,
        *,
        scene_type: str,
        target_id: str,
        target_umo: str,
        unanswered_count: int,
    ) -> str:
        now = _now(self.config)
        values = _build_template_values(
            now=now,
            scene_type=scene_type,
            target_id=target_id,
            unanswered_count=unanswered_count,
        )
        message_template = _get_message_template(self.config)
        fallback_text = _safe_format(message_template, values).strip()

        provider_id = await _resolve_provider_id(self.context, config=self.config, target_umo=target_umo)
        if not provider_id:
            return fallback_text

        history_limit = _get_history_limit(self.config)
        history_lines = await _fetch_recent_chat_history_lines(
            self.context,
            umo=target_umo,
            limit=history_limit,
        )
        history_text = "\n".join(history_lines) if history_lines else "暂无可用聊天记录。"

        prompt_template = _safe_format(_get_prompt_template(self.config), values).strip()
        if not prompt_template:
            prompt_template = (
                "请结合最近聊天记录，以 bot 的口吻主动发一条自然的中文消息。"
                "不要解释任务来源，不要说是系统触发，不要加引号，控制在 1 到 3 句。"
                "如果前文没有明确话题，就自然地打个招呼或轻轻开启一个话题。"
            )

        prompt = (
            "你现在不是在回复用户刚刚发来的消息，而是在主动发起一次聊天。\n"
            f"当前时间：{values['current_time']}\n"
            f"目标类型：{values['target_label']}\n"
            f"目标ID：{target_id}\n"
            f"连续未回复次数：{unanswered_count}\n"
            "最近聊天记录如下：\n"
            f"{history_text}\n\n"
            f"额外要求：{prompt_template}\n\n"
            "请直接输出最终要发送的消息正文，不要附加解释。"
        )

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            content = str(getattr(llm_resp, "completion_text", "") or "").strip()
            if content:
                return content
        except Exception as exc:
            logger.warning("主动聊天 LLM 生成失败，回退固定文案: %s", exc)

        return fallback_text

    async def _send_by_target_id(
        self,
        *,
        scene_type: str,
        target_id: str,
        text: str,
    ) -> tuple[bool, str]:
        content = str(text or "").strip()
        if not target_id or not target_id.isdigit():
            return False, "target id is invalid"
        if not content:
            return False, "message text is empty"

        chain = MessageChain()
        chain.message(content)
        platform = _get_platform(self.config)
        errors: list[str] = []

        if platform.casefold() == "aiocqhttp":
            try:
                await StarTools.send_message_by_id(
                    type="GroupMessage" if scene_type == SCENE_GROUP else "PrivateMessage",
                    id=str(target_id),
                    message_chain=chain,
                    platform="aiocqhttp",
                )
                return True, ""
            except Exception as exc:
                errors.append(f"send_message_by_id failed: {exc}")

        target_umo = _build_target_umo(platform, scene_type, target_id)
        if target_umo:
            try:
                sent = await self.context.send_message(target_umo, chain)
                if sent:
                    return True, ""
                errors.append(f"context.send_message returned False for {target_umo}")
            except Exception as exc:
                errors.append(f"context.send_message failed: {exc}")

        return False, " | ".join(errors) if errors else "send failed"


_PROACTIVE_CENTER: _LightweightProactiveChatCenter | None = None


def init_proactive_message_center(
    context: Context,
    config: AstrBotConfig | dict,
) -> _LightweightProactiveChatCenter | None:
    global _PROACTIVE_CENTER

    if _PROACTIVE_CENTER is None:
        center = _LightweightProactiveChatCenter(context, config)
        if center.is_available:
            _PROACTIVE_CENTER = center
            return center
        return None

    _PROACTIVE_CENTER.refresh(context, config)
    return _PROACTIVE_CENTER if _PROACTIVE_CENTER.is_available else None


async def observe_proactive_incoming_event(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig | dict,
) -> None:
    center = init_proactive_message_center(context, config)
    if center is None:
        return
    await center.observe_incoming_event(event)


async def observe_proactive_sent_event(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig | dict,
) -> None:
    center = init_proactive_message_center(context, config)
    if center is None:
        return
    await center.observe_sent_event(event)


def format_configured_proactive_targets_text(
    config: AstrBotConfig | dict | None,
    scene_type: str = "",
) -> str:
    configured = _configured_target_ids(config)
    lines: list[str] = []

    if scene_type in {"", SCENE_PRIVATE}:
        private_ids = configured.get(SCENE_PRIVATE, ())
        if private_ids:
            title = "配置中的私聊 QQ："
            if scene_type == SCENE_PRIVATE:
                title = "配置中的主动私聊 QQ："
            lines.append(title)
            for index, target_id in enumerate(private_ids, start=1):
                lines.append(f"{index}. QQ {target_id}")

    if scene_type in {"", SCENE_GROUP}:
        group_ids = configured.get(SCENE_GROUP, ())
        if group_ids:
            title = "配置中的群聊群号："
            if scene_type == SCENE_GROUP:
                title = "配置中的主动群聊群号："
            lines.append(title)
            for index, target_id in enumerate(group_ids, start=1):
                lines.append(f"{index}. 群 {target_id}")

    return "\n".join(lines)
