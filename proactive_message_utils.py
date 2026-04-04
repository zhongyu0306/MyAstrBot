from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.star import Context, StarTools

from .memory_utils import get_memory_admin_qq_ids


_TARGETS_FILE_NAME = "proactive_message_targets.json"
_SCHEDULE_STATE_FILE_NAME = "proactive_message_schedule_state.json"
_TARGETS_LOCK = asyncio.Lock()
_SCHEDULE_STATE_LOCK = asyncio.Lock()
_DEFAULT_ADMIN_QQ_IDS: tuple[str, ...] = ()
_DEFAULT_PLATFORM = "aiocqhttp"
_SCHEDULE_STATE_KEEP_DAYS = 7
_configured_admin_qq_ids: tuple[str, ...] = _DEFAULT_ADMIN_QQ_IDS

SCENE_GROUP = "group"
SCENE_PRIVATE = "private"
SCOPE_BOTH = "both"


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


def _normalize_admin_qq_ids(raw_value: Any) -> tuple[str, ...]:
    result = _normalize_digit_list(raw_value)
    return result if result else _DEFAULT_ADMIN_QQ_IDS


def configure_proactive_admin_qq_ids(raw_value: Any) -> tuple[str, ...]:
    global _configured_admin_qq_ids
    _configured_admin_qq_ids = _normalize_admin_qq_ids(raw_value)
    return _configured_admin_qq_ids


def get_proactive_admin_qq_ids() -> tuple[str, ...]:
    return _configured_admin_qq_ids or get_memory_admin_qq_ids()


def get_proactive_admin_display_text() -> str:
    admin_ids = get_proactive_admin_qq_ids()
    return "、".join(admin_ids) if admin_ids else "未配置"


def _safe_sender_id(event: AstrMessageEvent) -> str:
    try:
        sender_id = event.get_sender_id()
        if sender_id is not None:
            return str(sender_id).strip()
    except Exception:
        pass
    return ""


def _safe_sender_name(event: AstrMessageEvent) -> str:
    try:
        sender_name = event.get_sender_name()
        if sender_name:
            return str(sender_name).strip()
    except Exception:
        pass
    return ""


def _safe_group_id(event: AstrMessageEvent) -> str:
    try:
        group_id = event.get_group_id()
        if group_id:
            return str(group_id).strip()
    except Exception:
        pass
    return ""


def _safe_is_private_chat(event: AstrMessageEvent) -> bool:
    try:
        return bool(event.is_private_chat())
    except Exception:
        return False


def _safe_session_id(event: AstrMessageEvent) -> str:
    try:
        session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "")
        return str(session_id or "").strip()
    except Exception:
        return ""


def _is_admin_event(event: AstrMessageEvent) -> bool:
    sender_id = _safe_sender_id(event)
    return bool(sender_id and sender_id in get_proactive_admin_qq_ids())


def is_proactive_admin_event(event: AstrMessageEvent) -> bool:
    return _is_admin_event(event)


def _build_admin_denied_text() -> str:
    if not get_proactive_admin_qq_ids():
        return (
            "主动消息管理员尚未配置，请先在 `proactive_admin_qq_ids` 中填写管理员 QQ；"
            "若留空，则会回退使用 `memory_admin_qq_ids`。"
        )
    return f"主动消息仅允许管理员使用，当前允许的管理员 QQ：{get_proactive_admin_display_text()}"


def _normalize_alias(alias: str) -> str:
    return re.sub(r"\s+", " ", str(alias or "").strip()).lower()


def _normalize_scope_token(token: str | None) -> str:
    raw = str(token or "").strip().lower()
    if raw in {"群", "群聊", "group", "g"}:
        return SCENE_GROUP
    if raw in {"私聊", "私信", "个人", "private", "p"}:
        return SCENE_PRIVATE
    if raw in {"全部", "all", "both"}:
        return SCOPE_BOTH
    return ""


def _scene_label(scene_type: str, scene_value: str = "") -> str:
    if scene_type == SCENE_GROUP:
        return f"群聊 {scene_value}" if scene_value else "群聊"
    if scene_type == SCENE_PRIVATE:
        return f"私聊 {scene_value}" if scene_value else "私聊"
    return "未知"


def _detect_event_scene(event: AstrMessageEvent) -> tuple[str, str]:
    if _safe_is_private_chat(event):
        return SCENE_PRIVATE, _safe_sender_id(event)
    group_id = _safe_group_id(event)
    if group_id:
        return SCENE_GROUP, group_id
    return "", ""


def _data_dir() -> Path:
    base = Path(StarTools.get_data_dir("astrbot_all_char"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _targets_path() -> Path:
    return _data_dir() / _TARGETS_FILE_NAME


def _schedule_state_path() -> Path:
    return _data_dir() / _SCHEDULE_STATE_FILE_NAME


def _atomic_write_json(path: Path, payload: Any) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _load_targets() -> list[dict[str, Any]]:
    path = _targets_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("读取主动消息目标失败 %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _save_targets(records: list[dict[str, Any]]) -> None:
    try:
        _atomic_write_json(_targets_path(), records)
    except Exception as exc:
        logger.error("保存主动消息目标失败: %s", exc)
        raise


def _validate_alias(alias: str) -> tuple[bool, str]:
    cleaned = str(alias or "").strip()
    if not cleaned:
        return False, "请提供目标别名，例如：`/主动消息 绑定 工作群`"
    if len(cleaned) > 40:
        return False, "目标别名请控制在 40 个字符以内。"
    if re.search(r"\s", cleaned):
        return False, "目标别名暂不支持空格，请改成连续文本，例如：工作群、老王私聊。"
    return True, cleaned


def _match_targets(
    records: list[dict[str, Any]],
    *,
    alias: str,
    scene_type: str = "",
) -> list[dict[str, Any]]:
    normalized = _normalize_alias(alias)
    matched = [item for item in records if str(item.get("alias_normalized") or "") == normalized]
    if scene_type:
        matched = [item for item in matched if str(item.get("scene_type") or "") == scene_type]
    return matched


async def bind_current_target(event: AstrMessageEvent, alias: str) -> tuple[bool, str]:
    ok, cleaned_alias = _validate_alias(alias)
    if not ok:
        return False, cleaned_alias

    session_id = _safe_session_id(event)
    if not session_id:
        return False, "无法获取当前会话 ID，暂时不能绑定主动消息目标。"

    scene_type, scene_value = _detect_event_scene(event)
    if scene_type not in {SCENE_GROUP, SCENE_PRIVATE}:
        return False, "当前场景不是群聊或私聊，暂不支持绑定。"

    sender_id = _safe_sender_id(event)
    sender_name = _safe_sender_name(event)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    normalized_alias = _normalize_alias(cleaned_alias)

    async with _TARGETS_LOCK:
        records = _load_targets()
        updated = False
        for item in records:
            if (
                str(item.get("alias_normalized") or "") == normalized_alias
                and str(item.get("scene_type") or "") == scene_type
            ):
                item["alias"] = cleaned_alias
                item["session_id"] = session_id
                item["scene_value"] = scene_value
                item["updated_at"] = now
                item["creator_id"] = sender_id
                item["creator_name"] = sender_name
                updated = True
                break

        if not updated:
            records.append(
                {
                    "id": uuid4().hex,
                    "alias": cleaned_alias,
                    "alias_normalized": normalized_alias,
                    "session_id": session_id,
                    "scene_type": scene_type,
                    "scene_value": scene_value,
                    "creator_id": sender_id,
                    "creator_name": sender_name,
                    "created_at": now,
                    "updated_at": now,
                    "last_sent_at": "",
                    "send_count": 0,
                }
            )
        _save_targets(records)

    action_text = "已更新" if updated else "已绑定"
    return True, f"{action_text}主动消息目标：{cleaned_alias}（{_scene_label(scene_type, scene_value)}）"


async def list_targets(scene_type: str = "") -> list[dict[str, Any]]:
    async with _TARGETS_LOCK:
        records = _load_targets()
    if scene_type:
        records = [item for item in records if str(item.get("scene_type") or "") == scene_type]
    records.sort(key=lambda item: (str(item.get("scene_type") or ""), str(item.get("alias") or "")))
    return records


async def delete_target(alias: str, scene_type: str = "") -> tuple[bool, str]:
    ok, cleaned_alias = _validate_alias(alias)
    if not ok:
        return False, cleaned_alias

    async with _TARGETS_LOCK:
        records = _load_targets()
        matched = _match_targets(records, alias=cleaned_alias, scene_type=scene_type)
        if not matched:
            scene_text = f"{_scene_label(scene_type)} " if scene_type else ""
            return False, f"未找到别名为「{cleaned_alias}」的{scene_text}主动消息目标。"
        if len(matched) > 1:
            return False, (
                f"别名「{cleaned_alias}」同时存在群聊和私聊目标，请明确写："
                f"`/主动消息 删除 群 {cleaned_alias}` 或 `/主动消息 删除 私聊 {cleaned_alias}`"
            )
        target = matched[0]
        records = [item for item in records if str(item.get("id") or "") != str(target.get("id") or "")]
        _save_targets(records)

    return True, (
        f"已删除主动消息目标：{cleaned_alias}"
        f"（{_scene_label(str(target.get('scene_type') or ''), str(target.get('scene_value') or ''))}）"
    )


async def send_message_to_target(
    context: Context,
    *,
    alias: str,
    text: str,
    scene_type: str = "",
) -> tuple[bool, str]:
    ok, cleaned_alias = _validate_alias(alias)
    if not ok:
        return False, cleaned_alias

    content = str(text or "").strip()
    if not content:
        return False, "请提供要主动发送的消息内容。"

    async with _TARGETS_LOCK:
        records = _load_targets()
        matched = _match_targets(records, alias=cleaned_alias, scene_type=scene_type)
        if not matched:
            scene_text = f"{_scene_label(scene_type)} " if scene_type else ""
            return False, f"未找到别名为「{cleaned_alias}」的{scene_text}主动消息目标。"
        if len(matched) > 1:
            return False, (
                f"别名「{cleaned_alias}」同时存在群聊和私聊目标，请明确指定 scene_type，"
                f"例如：group / private。"
            )
        target = dict(matched[0])

    session_id = str(target.get("session_id") or "").strip()
    if not session_id:
        return False, f"目标「{cleaned_alias}」缺少会话 ID，请重新绑定。"

    chain = MessageChain()
    chain.message(content)
    try:
        await context.send_message(session_id, chain)
    except Exception as exc:
        logger.error("主动消息发送失败 alias=%s session_id=%s err=%s", cleaned_alias, session_id[:80], exc)
        return False, f"主动消息发送失败：{exc}"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with _TARGETS_LOCK:
        records = _load_targets()
        for item in records:
            if str(item.get("id") or "") == str(target.get("id") or ""):
                item["last_sent_at"] = now
                item["send_count"] = int(item.get("send_count", 0) or 0) + 1
                item["updated_at"] = now
                break
        _save_targets(records)

    return True, (
        f"已主动发送到「{cleaned_alias}」"
        f"（{_scene_label(str(target.get('scene_type') or ''), str(target.get('scene_value') or ''))}）"
    )


def _format_targets_text(records: list[dict[str, Any]], scene_type: str = "") -> str:
    if not records:
        label = _scene_label(scene_type) if scene_type else "全部"
        return f"当前没有已绑定的{label}主动消息目标。"

    title = "已绑定的主动消息目标："
    if scene_type:
        title = f"已绑定的{_scene_label(scene_type)}主动消息目标："

    lines = [title]
    for index, item in enumerate(records, start=1):
        item_scene_type = str(item.get("scene_type") or "")
        item_scene_value = str(item.get("scene_value") or "")
        last_sent_at = str(item.get("last_sent_at") or "").strip() or "从未"
        send_count = int(item.get("send_count", 0) or 0)
        lines.append(
            f"{index}. {item.get('alias') or '未命名'}"
            f" | {_scene_label(item_scene_type, item_scene_value)}"
            f" | 已发送 {send_count} 次"
            f" | 最近发送：{last_sent_at}"
        )
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "主动消息用法：\n"
        "1. 先在目标群或目标私聊里绑定当前会话：\n"
        "   `/主动消息 绑定 工作群`\n"
        "   `/主动消息 绑定 老王私聊`\n"
        "2. 查看已绑定目标：\n"
        "   `/主动消息 列表`\n"
        "   `/主动消息 列表 群`\n"
        "   `/主动消息 列表 私聊`\n"
        "3. 按别名主动发送：\n"
        "   `/主动消息 发送 群 工作群 今天 18:00 开会`\n"
        "   `/主动消息 发送 私聊 老王私聊 记得看下日报`\n"
        "4. 删除目标：\n"
        "   `/主动消息 删除 群 工作群`\n"
        "   `/主动消息 删除 私聊 老王私聊`\n"
        "说明：\n"
        "- 命令绑定属于兼容模式；现在更推荐直接使用配置里的 QQ 列表、群号列表和时段规则。\n"
        "- 同一个别名可以分别绑定一条群聊目标和一条私聊目标。\n"
        "- 当前功能仅允许管理员使用。"
    )


async def handle_proactive_message_command(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig | None = None,
):
    del config

    if not _is_admin_event(event):
        yield event.plain_result(_build_admin_denied_text())
        return

    raw = str(event.get_message_str() or "").strip()
    matched = re.match(r"^[\/！!]?主动消息(?:[\s\n]+(.+))?$", raw)
    body = (matched.group(1) or "").strip() if matched else ""
    if not body or body in {"帮助", "help", "h"}:
        yield event.plain_result(_help_text())
        return

    if body.startswith("绑定"):
        alias = body[len("绑定") :].strip()
        ok, msg = await bind_current_target(event, alias)
        yield event.plain_result(msg)
        return

    if body.startswith("列表"):
        scope_text = body[len("列表") :].strip()
        scene_type = _normalize_scope_token(scope_text)
        if scene_type == SCOPE_BOTH:
            scene_type = ""
        records = await list_targets(scene_type=scene_type)
        yield event.plain_result(_format_targets_text(records, scene_type=scene_type))
        return

    if body.startswith("删除"):
        delete_body = body[len("删除") :].strip()
        if not delete_body:
            yield event.plain_result("用法：`/主动消息 删除 群 工作群` 或 `/主动消息 删除 私聊 老王私聊`")
            return
        parts = delete_body.split(maxsplit=1)
        scene_type = _normalize_scope_token(parts[0])
        alias = parts[1].strip() if scene_type in {SCENE_GROUP, SCENE_PRIVATE} and len(parts) > 1 else delete_body
        ok, msg = await delete_target(alias, scene_type=scene_type if scene_type in {SCENE_GROUP, SCENE_PRIVATE} else "")
        yield event.plain_result(msg)
        return

    if body.startswith("发送"):
        send_body = body[len("发送") :].strip()
        if not send_body:
            yield event.plain_result("用法：`/主动消息 发送 群 工作群 今天 18:00 开会`")
            return

        first_parts = send_body.split(maxsplit=1)
        if len(first_parts) < 2:
            yield event.plain_result("用法：`/主动消息 发送 群 工作群 今天 18:00 开会`")
            return

        first_token = first_parts[0].strip()
        remainder = first_parts[1].strip()
        scene_type = _normalize_scope_token(first_token)

        if scene_type in {SCENE_GROUP, SCENE_PRIVATE}:
            rest_parts = remainder.split(maxsplit=1)
            if len(rest_parts) < 2:
                yield event.plain_result("用法：`/主动消息 发送 群 工作群 今天 18:00 开会`")
                return
            alias = rest_parts[0].strip()
            text = rest_parts[1].strip()
        else:
            alias = first_token
            text = remainder
            scene_type = ""

        ok, msg = await send_message_to_target(
            context=context,
            alias=alias,
            text=text,
            scene_type=scene_type,
        )
        yield event.plain_result(msg)
        return

    yield event.plain_result(_help_text())


def _to_int_in_range(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _get_timezone_name(config: AstrBotConfig | None) -> str:
    if config is None:
        return "Asia/Shanghai"
    value = str(getattr(config, "proactive_timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
    return value or "Asia/Shanghai"


def _get_now_in_timezone(config: AstrBotConfig | None) -> datetime:
    tz_name = _get_timezone_name(config)
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        logger.warning("主动消息时区配置无效，将回退到 Asia/Shanghai: %s", tz_name)
        return datetime.now(ZoneInfo("Asia/Shanghai"))


def _get_proactive_platform(config: AstrBotConfig | None) -> str:
    if config is None:
        return _DEFAULT_PLATFORM
    value = str(getattr(config, "proactive_send_platform", _DEFAULT_PLATFORM) or _DEFAULT_PLATFORM).strip().lower()
    return value or _DEFAULT_PLATFORM


def _normalize_time_slots(raw_value: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_value, list):
        return []

    slots: list[dict[str, Any]] = []
    for index, item in enumerate(raw_value, start=1):
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or f"时段规则 {index}").strip() or f"时段规则 {index}"
        rule_id = str(item.get("id") or item.get("_id") or f"slot_{index}").strip() or f"slot_{index}"
        target_scope = str(item.get("target_scope") or SCOPE_BOTH).strip().lower()
        if target_scope not in {SCOPE_BOTH, SCENE_PRIVATE, SCENE_GROUP}:
            target_scope = SCOPE_BOTH

        slot = {
            "id": rule_id,
            "name": name,
            "enabled": bool(item.get("enabled", True)),
            "target_scope": target_scope,
            "start_hour": _to_int_in_range(item.get("start_hour"), 0, 0, 23),
            "end_hour": _to_int_in_range(item.get("end_hour"), 23, 0, 23),
            "minute": _to_int_in_range(item.get("minute"), 0, 0, 59),
            "message_text": str(item.get("message_text") or "").strip(),
            "prompt_template": str(item.get("prompt_template") or "").strip(),
        }
        if not slot["message_text"] and not slot["prompt_template"]:
            continue
        slots.append(slot)
    return slots


def _is_hour_in_slot(hour: int, start_hour: int, end_hour: int) -> bool:
    if start_hour <= end_hour:
        return start_hour <= hour <= end_hour
    return hour >= start_hour or hour <= end_hour


def _build_slot_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M")


def _safe_format(template: str, values: dict[str, Any]) -> str:
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return str(template or "").format_map(_SafeDict(values))


def _build_template_values(
    now: datetime,
    *,
    rule: dict[str, Any],
    target_type: str,
    target_id: str,
) -> dict[str, Any]:
    weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "hour": f"{now.hour:02d}",
        "minute": f"{now.minute:02d}",
        "weekday": weekday_map[now.weekday()],
        "slot_name": str(rule.get("name") or "").strip(),
        "target_type": "群聊" if target_type == SCENE_GROUP else "私聊",
        "target_id": target_id,
    }


def _load_schedule_state() -> dict[str, Any]:
    path = _schedule_state_path()
    if not path.exists():
        return {"entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("读取主动消息定时状态失败 %s: %s", path, exc)
        return {"entries": []}
    if not isinstance(data, dict):
        return {"entries": []}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    data["entries"] = [item for item in entries if isinstance(item, dict)]
    return data


def _save_schedule_state(data: dict[str, Any]) -> None:
    try:
        _atomic_write_json(_schedule_state_path(), data)
    except Exception as exc:
        logger.error("保存主动消息定时状态失败: %s", exc)
        raise


def _prune_schedule_entries(entries: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=_SCHEDULE_STATE_KEEP_DAYS)
    result: list[dict[str, Any]] = []
    for item in entries:
        sent_at = str(item.get("sent_at") or "").strip()
        if not sent_at:
            continue
        try:
            sent_at_dt = datetime.strptime(sent_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if sent_at_dt >= cutoff.replace(tzinfo=None):
            result.append(item)
    return result[-5000:]


async def _was_slot_sent(key: str, now: datetime) -> bool:
    async with _SCHEDULE_STATE_LOCK:
        state = _load_schedule_state()
        entries = _prune_schedule_entries(list(state.get("entries") or []), now)
        if entries != state.get("entries"):
            state["entries"] = entries
            _save_schedule_state(state)
        return any(str(item.get("key") or "") == key for item in entries)


async def _mark_slot_sent(
    *,
    key: str,
    now: datetime,
    rule: dict[str, Any],
    target_type: str,
    target_id: str,
) -> None:
    async with _SCHEDULE_STATE_LOCK:
        state = _load_schedule_state()
        entries = _prune_schedule_entries(list(state.get("entries") or []), now)
        if any(str(item.get("key") or "") == key for item in entries):
            state["entries"] = entries
            _save_schedule_state(state)
            return
        entries.append(
            {
                "key": key,
                "sent_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "rule_id": str(rule.get("id") or ""),
                "rule_name": str(rule.get("name") or ""),
                "target_type": target_type,
                "target_id": target_id,
            }
        )
        state["entries"] = entries
        _save_schedule_state(state)


class _ConfigProactiveMessageCenter:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self._scheduler = None
        self._available = False
        self._start_scheduler()

    @property
    def is_available(self) -> bool:
        return self._available

    def refresh(self, context: Context, config: AstrBotConfig) -> None:
        self.context = context
        self.config = config

    def _start_scheduler(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.interval import IntervalTrigger

            tz_name = _get_timezone_name(self.config)
            scheduler = AsyncIOScheduler(timezone=tz_name)
            scheduler.add_job(
                self._run_tick,
                IntervalTrigger(seconds=30, timezone=tz_name),
                id="proactive_message_tick",
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            self._scheduler = scheduler
            self._available = True
            logger.info("主动消息配置调度已启动，时区=%s", tz_name)
        except ImportError:
            logger.warning("未安装 apscheduler，配置式主动消息不可用。")
            self._scheduler = None
            self._available = False
        except Exception as exc:
            logger.error("主动消息配置调度启动失败: %s", exc)
            self._scheduler = None
            self._available = False

    async def _run_tick(self) -> None:
        if not bool(getattr(self.config, "proactive_enabled", False)):
            return

        private_targets = _normalize_digit_list(getattr(self.config, "proactive_private_qq_ids", None))
        group_targets = _normalize_digit_list(getattr(self.config, "proactive_group_ids", None))
        if not private_targets and not group_targets:
            return

        slots = _normalize_time_slots(getattr(self.config, "proactive_time_slots", None))
        if not slots:
            return

        now = _get_now_in_timezone(self.config)
        for rule in slots:
            if not rule.get("enabled", True):
                continue
            if now.minute != int(rule.get("minute", 0) or 0):
                continue
            if not _is_hour_in_slot(now.hour, int(rule.get("start_hour", 0) or 0), int(rule.get("end_hour", 23) or 23)):
                continue
            await self._dispatch_rule(rule=rule, now=now, private_targets=private_targets, group_targets=group_targets)

    async def _dispatch_rule(
        self,
        *,
        rule: dict[str, Any],
        now: datetime,
        private_targets: tuple[str, ...],
        group_targets: tuple[str, ...],
    ) -> None:
        targets: list[tuple[str, str]] = []
        scope = str(rule.get("target_scope") or SCOPE_BOTH)
        if scope in {SCOPE_BOTH, SCENE_PRIVATE}:
            targets.extend((SCENE_PRIVATE, target_id) for target_id in private_targets)
        if scope in {SCOPE_BOTH, SCENE_GROUP}:
            targets.extend((SCENE_GROUP, target_id) for target_id in group_targets)

        if not targets:
            return

        success_count = 0
        for target_type, target_id in targets:
            key = f"{rule.get('id') or ''}|{target_type}|{target_id}|{_build_slot_bucket(now)}"
            if await _was_slot_sent(key, now):
                continue

            text = await self._build_message_text(
                rule=rule,
                now=now,
                target_type=target_type,
                target_id=target_id,
            )
            if not text:
                continue

            ok, err = await self._send_by_target_id(
                target_type=target_type,
                target_id=target_id,
                text=text,
            )
            if not ok:
                logger.warning(
                    "主动消息配置发送失败 rule=%s target_type=%s target_id=%s err=%s",
                    rule.get("name") or rule.get("id") or "",
                    target_type,
                    target_id,
                    err,
                )
                continue

            await _mark_slot_sent(
                key=key,
                now=now,
                rule=rule,
                target_type=target_type,
                target_id=target_id,
            )
            success_count += 1
            await asyncio.sleep(0.2)

        if success_count > 0:
            logger.info(
                "主动消息配置规则已触发 rule=%s count=%s bucket=%s",
                rule.get("name") or rule.get("id") or "",
                success_count,
                _build_slot_bucket(now),
            )

    async def _build_message_text(
        self,
        *,
        rule: dict[str, Any],
        now: datetime,
        target_type: str,
        target_id: str,
    ) -> str:
        values = _build_template_values(now, rule=rule, target_type=target_type, target_id=target_id)
        message_text = _safe_format(str(rule.get("message_text") or ""), values).strip()
        prompt_template = _safe_format(str(rule.get("prompt_template") or ""), values).strip()

        if not prompt_template:
            return message_text

        llm_umo = str(getattr(self.config, "proactive_llm_umo", "") or "").strip()
        if not llm_umo:
            return message_text

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=llm_umo)
        except Exception as exc:
            logger.warning("主动消息获取 LLM Provider 失败，回退固定文案: %s", exc)
            return message_text
        if not provider_id:
            return message_text

        prompt = (
            "请直接输出最终要发送的一条中文消息，不要解释，不要加引号，不要分点。\n"
            f"当前时间：{values['now']}\n"
            f"发送场景：{values['target_type']}\n"
            f"配置规则：{values['slot_name']}\n"
            f"补充要求：{prompt_template}"
        )
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            out = (getattr(llm_resp, "completion_text", None) or "").strip()
            if out:
                return out
        except Exception as exc:
            logger.warning("主动消息 LLM 生成失败，回退固定文案: %s", exc)
        return message_text

    async def _send_by_target_id(
        self,
        *,
        target_type: str,
        target_id: str,
        text: str,
    ) -> tuple[bool, str]:
        if not target_id or not str(target_id).isdigit():
            return False, "target id is invalid"
        content = str(text or "").strip()
        if not content:
            return False, "message text is empty"

        chain = MessageChain()
        chain.message(content)
        message_type = "GroupMessage" if target_type == SCENE_GROUP else "PrivateMessage"
        platform = _get_proactive_platform(self.config)

        try:
            await StarTools.send_message_by_id(
                type=message_type,
                id=str(target_id),
                message_chain=chain,
                platform=platform,
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)


_PROACTIVE_CENTER: _ConfigProactiveMessageCenter | None = None


def init_proactive_message_center(
    context: Context,
    config: AstrBotConfig,
) -> _ConfigProactiveMessageCenter | None:
    global _PROACTIVE_CENTER

    if _PROACTIVE_CENTER is None:
        center = _ConfigProactiveMessageCenter(context, config)
        if center.is_available:
            _PROACTIVE_CENTER = center
            return center
        return None

    _PROACTIVE_CENTER.refresh(context, config)
    return _PROACTIVE_CENTER if _PROACTIVE_CENTER.is_available else None
