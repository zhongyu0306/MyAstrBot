from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At
from astrbot.api.star import Context, StarTools

from .memory_state_store import delete_json_state, load_json_state, save_json_state, shared_conn
from .passive_memory_utils import record_passive_habit


_REMINDER_FILE_NAME = "simple_reminders.json"
_REMINDER_HISTORY_FILE_NAME = "simple_reminders_history.json"
_REMINDER_STATE_NAMESPACE = "simple_reminders"
_REMINDER_HISTORY_STATE_NAMESPACE = "simple_reminders_history"
_REMINDER_MIGRATION_NAMESPACE = "simple_reminders_migrated"
_REMINDER_HISTORY_MIGRATION_NAMESPACE = "simple_reminders_history_migrated"
_REMINDER_TABLE = "simple_reminders"
_REMINDER_HISTORY_TABLE = "simple_reminder_history"
_REMINDER_LOCK = asyncio.Lock()
_MAX_SEND_RETRY = 5


def _normalize_natural_hour(hour: int, period: str) -> Optional[int]:
    """将自然语言时段转换成 24 小时制。"""
    if hour < 0 or hour > 23:
        return None
    if not period:
        return hour if 0 <= hour <= 23 else None

    if period in ("凌晨",):
        if hour == 12:
            return 0
        return hour if 0 <= hour <= 11 else None

    if period in ("早上", "上午"):
        if hour == 12:
            return 0
        return hour if 0 <= hour <= 11 else None

    if period in ("中午",):
        if hour == 12:
            return 12
        if 1 <= hour <= 11:
            return hour + 12
        return None

    if period in ("下午", "晚上"):
        if hour == 12:
            return 12
        if 1 <= hour <= 11:
            return hour + 12
        return None

    return None


def _parse_natural_time_expression(base: datetime, expr: str) -> Optional[datetime]:
    """
    解析中文自然时间表达：
    - 今天/明天/后天 + 上午/下午/晚上 + X点(半/分)
    - 今晚/明早/明晚 + X点(半/分)
    """
    s = re.sub(r"\s+", "", expr)
    if not s:
        return None

    day_token = ""
    period = ""

    # 先匹配复合词，避免被“今天+晚上”拆错
    merged_day_tokens = {
        "今晚": (0, "晚上"),
        "今早": (0, "早上"),
        "明晚": (1, "晚上"),
        "明早": (1, "早上"),
    }
    for token, (_, token_period) in merged_day_tokens.items():
        if s.startswith(token):
            day_token = token
            period = token_period
            s = s[len(token) :]
            break

    if not day_token:
        m = re.match(r"^(今天|明天|后天)?(早上|上午|中午|下午|晚上|凌晨)?(.*)$", s)
        if not m:
            return None
        day_token = m.group(1) or ""
        period = m.group(2) or ""
        s = m.group(3) or ""

    # X点半 / X点 / X点Y分
    m = re.match(r"^(\d{1,2})点(?:(\d{1,2})分?|半)?$", s)
    if m:
        hour = int(m.group(1))
        minute = 0
        if m.group(2) is not None:
            minute = int(m.group(2))
        elif "半" in s:
            minute = 30
    else:
        # X:YY
        m = re.match(r"^(\d{1,2}):(\d{2})$", s)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2))

    if minute < 0 or minute > 59:
        return None

    norm_hour = _normalize_natural_hour(hour, period)
    if norm_hour is None:
        return None

    day_offset_map = {
        "": 0,
        "今天": 0,
        "明天": 1,
        "后天": 2,
        "今晚": 0,
        "今早": 0,
        "明晚": 1,
        "明早": 1,
    }
    day_offset = day_offset_map.get(day_token)
    if day_offset is None:
        return None

    dt = (base + timedelta(days=day_offset)).replace(
        hour=norm_hour, minute=minute, second=0, microsecond=0
    )
    if day_offset == 0 and dt <= base:
        dt = dt + timedelta(days=1)
    return dt


def _parse_time_expression(base: datetime, expr: str) -> Optional[datetime]:
    """
    解析简单时间表达式：
    - "3分钟后"
    - "2小时后"
    - "2026-02-28-08:00"
    - "08:30"（当天，若已过则顺延到明天）
    """
    expr = expr.strip()

    # N 分钟后
    m = re.match(r"^(\d+)\s*分钟后$", expr)
    if m:
        minutes = int(m.group(1))
        if minutes <= 0:
            return None
        return base + timedelta(minutes=minutes)

    # N 小时后
    m = re.match(r"^(\d+)\s*小时后$", expr)
    if m:
        hours = int(m.group(1))
        if hours <= 0:
            return None
        return base + timedelta(hours=hours)

    # 绝对时间：YYYY-MM-DD-HH:MM
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2}):(\d{2})$", expr)
    if m:
        year, month, day, hour, minute = map(int, m.groups())
        try:
            dt = datetime(year, month, day, hour, minute)
        except ValueError:
            return None
        if dt <= base:
            return None
        return dt

    # 当天时间：HH:MM（若已过则顺延到明天）
    m = re.match(r"^(\d{1,2}):(\d{2})$", expr)
    if m:
        hour, minute = map(int, m.groups())
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base:
            dt = dt + timedelta(days=1)
        return dt

    # 自然语言时间：如“明天下午3点”“后天早上8点半”“今晚9点”
    natural_dt = _parse_natural_time_expression(base, expr)
    if natural_dt is not None:
        return natural_dt

    return None


def _split_time_and_text(body: str) -> tuple[str, str]:
    """
    从命令体中拆分 时间表达 与 提醒内容。
    支持有空格和无空格的自然语言整句。
    """
    if " " in body or "\t" in body or "\n" in body:
        first, rest = body.split(maxsplit=1)
        return first.strip(), rest.strip()

    # 按“后”切分：3分钟后提醒我喝水
    idx = body.find("后")
    if idx != -1:
        return body[: idx + 1].strip(), body[idx + 1 :].strip()

    patterns = [
        # 绝对时间 / HH:MM
        r"^(\d{4}-\d{2}-\d{2}-\d{2}:\d{2})(.+)$",
        r"^(\d{1,2}:\d{2})(.+)$",
        # 自然时间：明天下午3点半提醒我开会
        r"^((?:今天|明天|后天|今晚|今早|明早|明晚)?(?:早上|上午|中午|下午|晚上|凌晨)?\d{1,2}点(?:\d{1,2}分?|半)?)(.+)$",
    ]
    for p in patterns:
        m = re.match(p, body)
        if not m:
            continue
        time_str = m.group(1).strip()
        text = m.group(2).strip()
        text = re.sub(r"^(提醒我|提醒|叫我|记得)\s*", "", text)
        return time_str, text

    return "", ""


def _get_data_base_dir() -> Path:
    base = StarTools.get_data_dir("astrbot_all_char")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _get_reminder_file_path() -> Path:
    """获取待执行提醒的持久化文件路径。"""
    return _get_data_base_dir() / _REMINDER_FILE_NAME


def _get_reminder_history_file_path() -> Path:
    """获取提醒历史归档文件路径。"""
    return _get_data_base_dir() / _REMINDER_HISTORY_FILE_NAME


def _atomic_write_json(path: Path, payload: list[dict]) -> None:
    """原子化写入，避免文件中途写坏导致提醒丢失。"""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("读取 json 文件失败 %s: %s", path, e)
        return []


def _ensure_reminder_tables() -> None:
    with shared_conn() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_REMINDER_TABLE} (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                creator_id TEXT NOT NULL DEFAULT '',
                creator_name TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_REMINDER_TABLE}_pending "
            f"ON {_REMINDER_TABLE}(status, run_at, session_id)"
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_REMINDER_HISTORY_TABLE} (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                creator_id TEXT NOT NULL DEFAULT '',
                creator_name TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                run_at TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                archived_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_REMINDER_HISTORY_TABLE}_session "
            f"ON {_REMINDER_HISTORY_TABLE}(session_id, archived_at DESC)"
        )
        conn.commit()


def _normalize_reminder_record(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    reminder_id = str(item.get("id") or "").strip() or uuid4().hex
    session_id = str(item.get("session_id") or "").strip()
    text = str(item.get("text") or "").strip()
    run_at = str(item.get("run_at") or "").strip()
    if not session_id or not text or not run_at:
        return None
    try:
        attempts = int(item.get("attempts", 0) or 0)
    except Exception:
        attempts = 0
    status = str(item.get("status") or "pending").strip() or "pending"
    created_at = str(item.get("created_at") or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "id": reminder_id,
        "session_id": session_id,
        "creator_id": str(item.get("creator_id") or "").strip(),
        "creator_name": str(item.get("creator_name") or "").strip(),
        "text": text,
        "run_at": run_at,
        "status": status,
        "created_at": created_at,
        "last_attempt_at": str(item.get("last_attempt_at") or "").strip(),
        "attempts": max(0, attempts),
        "last_error": str(item.get("last_error") or "").strip(),
        "finished_at": str(item.get("finished_at") or "").strip(),
    }


def _row_to_reminder(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "creator_id": str(row["creator_id"] or ""),
        "creator_name": str(row["creator_name"] or ""),
        "text": str(row["text"]),
        "run_at": str(row["run_at"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "last_attempt_at": str(row["last_attempt_at"] or ""),
        "attempts": int(row["attempts"] or 0),
        "last_error": str(row["last_error"] or ""),
        "finished_at": str(row["finished_at"] or ""),
    }


def _row_to_reminder_history(row: sqlite3.Row) -> dict[str, Any]:
    record = _row_to_reminder(row)
    record["archived_at"] = str(row["archived_at"] or "")
    return record


def ensure_simple_reminder_storage() -> None:
    _ensure_reminder_data_migrated()


def list_simple_reminders_for_creator(
    creator_id: str,
    *,
    pending_limit: int = 100,
    history_limit: int = 100,
) -> dict[str, list[dict[str, Any]]]:
    ensure_simple_reminder_storage()
    cleaned_creator = str(creator_id or "").strip()
    if not cleaned_creator:
        return {"pending": [], "history": []}
    with shared_conn() as conn:
        pending_rows = conn.execute(
            f"""
            SELECT id, session_id, creator_id, creator_name, text, run_at, status,
                   created_at, last_attempt_at, attempts, last_error, finished_at
            FROM {_REMINDER_TABLE}
            WHERE creator_id = ?
            ORDER BY run_at ASC, created_at ASC, id ASC
            LIMIT ?
            """,
            (cleaned_creator, max(1, int(pending_limit))),
        ).fetchall()
        history_rows = conn.execute(
            f"""
            SELECT id, session_id, creator_id, creator_name, text, run_at, status,
                   created_at, last_attempt_at, attempts, last_error, finished_at, archived_at
            FROM {_REMINDER_HISTORY_TABLE}
            WHERE creator_id = ?
            ORDER BY archived_at DESC, finished_at DESC, id DESC
            LIMIT ?
            """,
            (cleaned_creator, max(1, int(history_limit))),
        ).fetchall()
    return {
        "pending": [_row_to_reminder(row) for row in pending_rows],
        "history": [_row_to_reminder_history(row) for row in history_rows],
    }


def get_simple_reminder_counts_by_creator() -> dict[str, int]:
    ensure_simple_reminder_storage()
    counts: dict[str, int] = {}
    with shared_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT creator_id, COUNT(*) AS total
            FROM {_REMINDER_TABLE}
            WHERE creator_id <> ''
            GROUP BY creator_id
            """
        ).fetchall()
    for row in rows:
        creator_id = str(row["creator_id"] or "").strip()
        if creator_id:
            counts[creator_id] = int(row["total"] or 0)
    return counts


def create_simple_reminder_for_creator(
    creator_id: str,
    *,
    creator_name: str,
    session_id: str,
    text: str,
    run_at: str,
) -> dict[str, Any] | None:
    ensure_simple_reminder_storage()
    cleaned_creator = str(creator_id or "").strip()
    cleaned_session = str(session_id or "").strip()
    cleaned_text = str(text or "").strip()
    cleaned_creator_name = str(creator_name or "").strip()
    cleaned_run_at = str(run_at or "").strip()
    if not cleaned_creator or not cleaned_session or not cleaned_text or not cleaned_run_at:
        return None
    try:
        normalized_run_at = datetime.strptime(cleaned_run_at, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    record = {
        "id": uuid4().hex,
        "session_id": cleaned_session,
        "creator_id": cleaned_creator,
        "creator_name": cleaned_creator_name,
        "text": cleaned_text,
        "run_at": normalized_run_at,
        "status": "pending",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_attempt_at": "",
        "attempts": 0,
        "last_error": "",
        "finished_at": "",
    }
    _insert_reminders([record], history=False)
    return record


def update_simple_reminder_for_creator(
    reminder_id: str,
    creator_id: str,
    *,
    session_id: str,
    text: str,
    run_at: str,
) -> dict[str, Any] | None:
    ensure_simple_reminder_storage()
    cleaned_id = str(reminder_id or "").strip()
    cleaned_creator = str(creator_id or "").strip()
    cleaned_session = str(session_id or "").strip()
    cleaned_text = str(text or "").strip()
    cleaned_run_at = str(run_at or "").strip()
    if not cleaned_id or not cleaned_creator or not cleaned_session or not cleaned_text or not cleaned_run_at:
        return None
    try:
        normalized_run_at = datetime.strptime(cleaned_run_at, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    with shared_conn() as conn:
        existing = conn.execute(
            f"""
            SELECT id, session_id, creator_id, creator_name, text, run_at, status,
                   created_at, last_attempt_at, attempts, last_error, finished_at
            FROM {_REMINDER_TABLE}
            WHERE id = ? AND creator_id = ?
            """,
            (cleaned_id, cleaned_creator),
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            f"""
            UPDATE {_REMINDER_TABLE}
            SET session_id = ?, text = ?, run_at = ?
            WHERE id = ? AND creator_id = ?
            """,
            (cleaned_session, cleaned_text, normalized_run_at, cleaned_id, cleaned_creator),
        )
        row = conn.execute(
            f"""
            SELECT id, session_id, creator_id, creator_name, text, run_at, status,
                   created_at, last_attempt_at, attempts, last_error, finished_at
            FROM {_REMINDER_TABLE}
            WHERE id = ? AND creator_id = ?
            """,
            (cleaned_id, cleaned_creator),
        ).fetchone()
        conn.commit()
    return _row_to_reminder(row) if row is not None else None


def delete_simple_reminder_for_creator(reminder_id: str, creator_id: str) -> bool:
    ensure_simple_reminder_storage()
    cleaned_id = str(reminder_id or "").strip()
    cleaned_creator = str(creator_id or "").strip()
    if not cleaned_id or not cleaned_creator:
        return False
    with shared_conn() as conn:
        pending_cursor = conn.execute(
            f"DELETE FROM {_REMINDER_TABLE} WHERE id = ? AND creator_id = ?",
            (cleaned_id, cleaned_creator),
        )
        history_cursor = conn.execute(
            f"DELETE FROM {_REMINDER_HISTORY_TABLE} WHERE id = ? AND creator_id = ?",
            (cleaned_id, cleaned_creator),
        )
        deleted = pending_cursor.rowcount > 0 or history_cursor.rowcount > 0
        conn.commit()
    return deleted


def _is_migrated(namespace: str) -> bool:
    return bool(
        load_json_state(
            namespace,
            default=False,
            normalizer=lambda value: bool(value),
        )
    )


def _mark_migrated(namespace: str) -> None:
    save_json_state(namespace, True)


def _insert_reminders(records: list[dict[str, Any]], *, history: bool) -> None:
    normalized = [item for item in (_normalize_reminder_record(r) for r in records) if item is not None]
    if not normalized:
        return
    table_name = _REMINDER_HISTORY_TABLE if history else _REMINDER_TABLE
    with shared_conn() as conn:
        if history:
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {table_name}(
                    id, session_id, creator_id, creator_name, text, run_at, status,
                    created_at, last_attempt_at, attempts, last_error, finished_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["id"],
                        item["session_id"],
                        item["creator_id"],
                        item["creator_name"],
                        item["text"],
                        item["run_at"],
                        item["status"],
                        item["created_at"],
                        item["last_attempt_at"],
                        item["attempts"],
                        item["last_error"],
                        item["finished_at"],
                        item["finished_at"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    for item in normalized
                ],
            )
        else:
            conn.executemany(
                f"""
                INSERT OR REPLACE INTO {table_name}(
                    id, session_id, creator_id, creator_name, text, run_at, status,
                    created_at, last_attempt_at, attempts, last_error, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["id"],
                        item["session_id"],
                        item["creator_id"],
                        item["creator_name"],
                        item["text"],
                        item["run_at"],
                        item["status"],
                        item["created_at"],
                        item["last_attempt_at"],
                        item["attempts"],
                        item["last_error"],
                        item["finished_at"],
                    )
                    for item in normalized
                ],
            )
        conn.commit()


def _ensure_reminder_data_migrated() -> None:
    _ensure_reminder_tables()
    if not _is_migrated(_REMINDER_MIGRATION_NAMESPACE):
        legacy_pending = load_json_state(
            _REMINDER_STATE_NAMESPACE,
            default=[],
            normalizer=lambda value: value if isinstance(value, list) else [],
            legacy_path=_get_reminder_file_path(),
        )
        _insert_reminders(list(legacy_pending), history=False)
        _mark_migrated(_REMINDER_MIGRATION_NAMESPACE)
        delete_json_state(_REMINDER_STATE_NAMESPACE)

    if not _is_migrated(_REMINDER_HISTORY_MIGRATION_NAMESPACE):
        legacy_history = load_json_state(
            _REMINDER_HISTORY_STATE_NAMESPACE,
            default=[],
            normalizer=lambda value: value if isinstance(value, list) else [],
            legacy_path=_get_reminder_history_file_path(),
        )
        _insert_reminders(list(legacy_history), history=True)
        _mark_migrated(_REMINDER_HISTORY_MIGRATION_NAMESPACE)
        delete_json_state(_REMINDER_HISTORY_STATE_NAMESPACE)


def _load_all_reminders() -> list[dict]:
    _ensure_reminder_data_migrated()
    with shared_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, session_id, creator_id, creator_name, text, run_at, status,
                   created_at, last_attempt_at, attempts, last_error, finished_at
            FROM {_REMINDER_TABLE}
            ORDER BY run_at ASC, created_at ASC, id ASC
            """
        ).fetchall()
    return [_row_to_reminder(row) for row in rows]


def _save_all_reminders(reminders: list[dict]) -> None:
    try:
        _ensure_reminder_data_migrated()
        normalized = [item for item in (_normalize_reminder_record(r) for r in reminders) if item is not None]
        with shared_conn() as conn:
            conn.execute(f"DELETE FROM {_REMINDER_TABLE}")
            if normalized:
                conn.executemany(
                    f"""
                    INSERT INTO {_REMINDER_TABLE}(
                        id, session_id, creator_id, creator_name, text, run_at, status,
                        created_at, last_attempt_at, attempts, last_error, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item["id"],
                            item["session_id"],
                            item["creator_id"],
                            item["creator_name"],
                            item["text"],
                            item["run_at"],
                            item["status"],
                            item["created_at"],
                            item["last_attempt_at"],
                            item["attempts"],
                            item["last_error"],
                            item["finished_at"],
                        )
                        for item in normalized
                    ],
                )
            conn.commit()
    except Exception as e:
        logger.error("保存提醒数据失败: %s", e)


def _list_pending_reminders(session_id: str, creator_id: Optional[str]) -> list[dict]:
    """
    读取当前会话下的待执行提醒。
    若传入 creator_id，则仅返回该用户创建的提醒。
    """
    reminders = _load_all_reminders()
    creator = str(creator_id) if creator_id is not None else ""
    result: list[dict] = []
    for r in reminders:
        if str(r.get("status", "pending")) != "pending":
            continue
        if str(r.get("session_id", "")) != session_id:
            continue
        if creator and str(r.get("creator_id", "")) != creator:
            continue
        result.append(r)
    result.sort(key=lambda x: str(x.get("run_at", "")))
    return result


def _append_reminder_history(record: dict) -> None:
    """
    将已触发提醒写入历史归档，满足“长期持久化”诉求。
    """
    try:
        _ensure_reminder_data_migrated()
        _insert_reminders([record], history=True)
    except Exception as e:
        logger.error("保存提醒历史失败: %s", e)


def _build_human_fallback_text(text: str, creator_name: Optional[str]) -> str:
    """LLM 不可用时的人性化提醒兜底文案。"""
    hour = datetime.now().hour
    if hour < 6:
        greet = "夜深了"
    elif hour < 11:
        greet = "早上好"
    elif hour < 14:
        greet = "中午好"
    elif hour < 18:
        greet = "下午好"
    else:
        greet = "晚上好"

    name = (creator_name or "").strip()
    prefix = f"{name}，" if name else ""

    templates = [
        f"{prefix}{greet}，你之前交代的提醒到点啦：{text}",
        f"{prefix}来提醒你一下：{text}",
        f"{prefix}时间到了，别忘了：{text}",
        f"{prefix}小闹钟准时上线，{text}",
    ]
    return random.choice(templates)


class _SimpleReminderCenter:
    """基于 APScheduler 的持久化简易提醒中心。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.config = config
        self._scheduler = None
        self._available = False
        self._start_scheduler()

    @property
    def is_available(self) -> bool:
        return self._available

    def _start_scheduler(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.interval import IntervalTrigger

            tz = getattr(self.config, "simple_reminder_timezone", "Asia/Shanghai")
            scheduler = AsyncIOScheduler(timezone=tz)
            scheduler.add_job(
                self._run_due_reminders,
                IntervalTrigger(seconds=20, timezone=tz),
                id="simple_reminder_tick",
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            self._scheduler = scheduler
            self._available = True
            logger.info("简易提醒持久化调度已启动，时区=%s", tz)
        except ImportError:
            logger.warning("未安装 apscheduler，提醒持久化定时不可用。")
            self._scheduler = None
            self._available = False
        except Exception as e:
            logger.error("简易提醒调度启动失败: %s", e)
            self._scheduler = None
            self._available = False

    async def add_reminder(
        self,
        session_id: str,
        creator_id: Optional[str],
        creator_name: Optional[str],
        text: str,
        run_at: datetime,
    ) -> None:
        """新增一条提醒并持久化。"""
        if not self._available:
            raise RuntimeError("简易提醒调度器未就绪，无法添加提醒。")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": uuid4().hex,
            "session_id": session_id,
            "creator_id": str(creator_id) if creator_id is not None else "",
            "creator_name": creator_name or "",
            "text": text,
            "run_at": run_at.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending",
            "created_at": now,
            "last_attempt_at": "",
            "attempts": 0,
            "last_error": "",
        }

        async with _REMINDER_LOCK:
            _ensure_reminder_data_migrated()
            _insert_reminders([record], history=False)

    async def _run_due_reminders(self) -> None:
        """定时扫描待提醒列表，触发到期提醒。"""
        now = datetime.now()
        async with _REMINDER_LOCK:
            reminders = _load_all_reminders()
            remaining: list[dict] = []

            for r in reminders:
                run_at_str = str(r.get("run_at", "")).strip()
                if not run_at_str:
                    continue

                try:
                    run_at = datetime.strptime(run_at_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue

                if run_at > now:
                    remaining.append(r)
                    continue

                if str(r.get("status", "pending")) != "pending":
                    continue

                attempts = int(r.get("attempts", 0) or 0)
                if attempts >= _MAX_SEND_RETRY:
                    r["status"] = "failed"
                    r["last_error"] = f"连续发送失败，已达最大重试次数({_MAX_SEND_RETRY})"
                    r["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _append_reminder_history(r)
                    continue

                r["attempts"] = attempts + 1
                r["last_attempt_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                ok, err = await self._send_one(
                    session_id=str(r.get("session_id") or ""),
                    creator_id=str(r.get("creator_id") or ""),
                    creator_name=str(r.get("creator_name") or ""),
                    text=str(r.get("text") or ""),
                )

                if ok:
                    r["status"] = "sent"
                    r["last_error"] = ""
                    r["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _append_reminder_history(r)
                else:
                    r["last_error"] = err or "unknown error"
                    remaining.append(r)

            _save_all_reminders(remaining)

    async def _send_one(
        self,
        session_id: str,
        creator_id: Optional[str],
        creator_name: Optional[str],
        text: str,
    ) -> tuple[bool, str]:
        if not session_id:
            return False, "session_id is empty"

        chain = MessageChain()
        if creator_id and str(creator_id).isdigit():
            try:
                chain.chain.append(At(qq=int(creator_id), name=creator_name or None))
            except Exception:
                pass

        final_text = _build_human_fallback_text(text=text, creator_name=creator_name)

        # 尝试交给当前会话 LLM 做更贴近人设的润色
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=session_id)
            if provider_id:
                prompt = (
                    "你是当前会话里的聊天角色，请用平时的人设语气做一条自然提醒。\n"
                    "要求：\n"
                    "1) 只输出一条自然口语提醒，不要解释。\n"
                    "2) 必须保留核心提醒事项，不改变原意。\n"
                    "3) 不要说自己是系统、机器人或助手。\n"
                    f"提醒事项：{text}"
                )
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                )
                out = (getattr(llm_resp, "completion_text", None) or "").strip()
                if out:
                    final_text = out
        except Exception as e:
            logger.exception("定时提醒生成自然语言文本失败: %s", e)

        chain.message(final_text)
        try:
            await self.context.send_message(session_id, chain)
            return True, ""
        except Exception as e:
            logger.error("发送定时提醒到 %s 失败: %s", session_id[:50], e)
            return False, str(e)


_REMINDER_CENTER: _SimpleReminderCenter | None = None


def init_simple_reminder_center(
    context: Context, config: AstrBotConfig
) -> Optional[_SimpleReminderCenter]:
    """初始化（或获取已有）简易提醒中心。"""
    global _REMINDER_CENTER
    if _REMINDER_CENTER is not None:
        return _REMINDER_CENTER if _REMINDER_CENTER.is_available else None

    center = _SimpleReminderCenter(context, config)
    if center.is_available:
        _REMINDER_CENTER = center
        return center
    return None


async def handle_simple_reminder(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    简易提醒命令入口：/提醒 <时间> <内容>

    支持示例：
    - /提醒 3分钟后 喝水
    - /提醒 3分钟后提醒我喝水（会自动按“后”分割）
    - /提醒 2026-02-28-08:00 开会
    - /提醒 08:30 上班打卡
    - /提醒 明天下午3点 开会
    """
    raw = event.get_message_str().strip()
    # 去掉前缀：/提醒 或 提醒
    m = re.match(r"^[\/！!]?提醒[\s\n]+(.+)$", raw)
    if not m:
        yield event.plain_result("用法：/提醒 <时间> <内容>\n例如：/提醒 3分钟后 喝水")
        return

    body = m.group(1).strip()
    if body in ("列表", "提醒列表", "list", "ls"):
        session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "")
        if not session_id:
            yield event.plain_result("无法获取当前会话，暂时不能查看提醒列表。")
            return

        creator_id = event.get_sender_id()
        reminders = _list_pending_reminders(session_id=session_id, creator_id=creator_id)
        if not reminders:
            yield event.plain_result("你当前没有待执行的提醒。")
            return

        lines = ["你的提醒列表："]
        for i, r in enumerate(reminders, 1):
            run_at = str(r.get("run_at", "未知时间"))
            text = str(r.get("text", "")).strip() or "（无内容）"
            lines.append(f"{i}. {run_at} - {text}")
        lines.append("")
        lines.append("可继续使用：/提醒 <时间> <内容>")
        yield event.plain_result("\n".join(lines))
        return

    time_str, text = _split_time_and_text(body)

    if not time_str or not text:
        yield event.plain_result(
            "用法：/提醒 <时间> <内容>\n"
            "示例：\n"
            "  /提醒 3分钟后 喝水\n"
            "  /提醒 3分钟后提醒我喝水\n"
            "  /提醒 08:30 上班打卡\n"
            "  /提醒 明天下午3点 开会"
        )
        return

    now = datetime.now()
    target = _parse_time_expression(now, time_str)
    if target is None:
        yield event.plain_result(
            "暂时只支持以下时间格式：\n"
            "- N分钟后（如：3分钟后）\n"
            "- N小时后（如：2小时后）\n"
            "- 绝对时间：2026-02-28-08:00\n"
            "- 当天时间：08:30（若已过则顺延到明天）\n"
            "- 自然语言：明天下午3点 / 后天早上8点半 / 今晚9点"
        )
        return

    session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "")
    if not session_id:
        yield event.plain_result("无法获取当前会话，定时提醒不可用。")
        return

    creator_id = event.get_sender_id()
    creator_name = event.get_sender_name()

    center = init_simple_reminder_center(context, config)
    if center is None:
        yield event.plain_result(
            "当前环境未安装 apscheduler，提醒的持久化定时不可用。\n"
            "请在运行环境中安装 apscheduler 后重试。"
        )
        return

    await center.add_reminder(session_id, creator_id, creator_name, text, target)

    target_str = target.strftime("%Y-%m-%d %H:%M:%S")
    record_passive_habit(
        event,
        "reminder",
        "reminder_time",
        target.strftime("%H:%M"),
        source_text=raw,
    )
    yield event.plain_result(
        f"好嘞，已经帮你记下了。\n"
        f"将在 {target_str} 提醒你：{text}\n"
        f"这条提醒已写入本地持久化存储，重启后也会继续生效。"
    )


async def handle_sy_rmd_group(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    简化后的 /rmd 与 /rmdg：当前仅保留简易提醒入口。
    """
    yield event.plain_result(
        "当前定时任务功能已简化，仅支持：\n"
        "  /提醒 <时间> <内容>\n"
        "示例：/提醒 3分钟后 喝水\n"
        "旧 /rmd /rmdg 高级用法暂未实现。"
    )
