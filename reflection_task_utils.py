from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .memory_state_store import shared_conn
from .passive_memory_utils import init_passive_memory_store


_THREAD_TABLE = "task_threads"
_REFLECTION_TABLE = "posthoc_reflections"
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9._-]{1,15}")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_TRIM_PUNCT = " \t\r\n\"'“”‘’[]【】()（）<>《》"
_TASK_PATTERNS = (
    re.compile(r"(?:我(?:最近)?(?:正在|在|准备|打算|计划|想要|想|要|得|需要))(?P<task>[^。！？\n]{2,48})"),
    re.compile(r"(?P<task>(?:明天|今晚|待会|等下|这周|周末|下周|月底前|月底|最近)[^。！？\n]{0,14}(?:要|得|准备|打算|去|做|完成|提交|见|处理)[^。！？\n]{2,48})"),
    re.compile(r"(?P<task>(?:ddl|deadline|截止)[^。！？\n]{0,24})", re.IGNORECASE),
    re.compile(r"(?:我(?:最近)?(?:忙着|一直在))(?P<task>[^。！？\n]{2,48})"),
)
_DUE_PATTERNS = (
    re.compile(r"(明天|今晚|待会|等下|这周|周末|下周|月底前|月底|最近|今天下午|今天晚上|今天早上|下个月)"),
    re.compile(r"(ddl|deadline|截止[^，。！？\n]{0,16})", re.IGNORECASE),
)
_QUESTION_HINTS = ("?", "？", "吗", "怎么", "如何", "为什么", "能不能", "行不行", "要不要", "帮我")
_SUPPORT_HINTS = ("烦", "累", "焦虑", "难受", "委屈", "崩溃", "压力", "紧张", "害怕", "心累", "不开心")
_COMPLETION_HINTS = ("完成了", "搞定了", "做完了", "结束了", "交了", "过了", "通过了", "已经搞定", "已经完成")
_PAUSE_HINTS = ("先放放", "先缓缓", "先搁置", "先暂停", "回头再弄", "之后再说")
_REOPEN_HINTS = ("继续搞", "继续做", "重新开始", "又开始", "接着弄", "继续推进")
_ASSISTANT_COMMIT_PATTERNS = (
    re.compile(r"(?:我可以|我能|我帮你|我来帮你|要不要我|我们可以)(?P<commit>[^。！？\n]{2,52})"),
    re.compile(r"(?:接下来|然后|下一步)(?P<commit>[^。！？\n]{2,52})"),
    re.compile(r"(?:建议你|你可以先)(?P<commit>[^。！？\n]{2,52})"),
)
_FOLLOW_UP_HINT_PATTERNS = (
    re.compile(r"(?:先|接下来|下一步|第一步)(?P<step>[^。！？\n]{2,52})"),
    re.compile(r"(?:建议|可以先)(?P<step>[^。！？\n]{2,52})"),
)
_STOPWORDS = {
    "",
    "这个",
    "那个",
    "这里",
    "那里",
    "今天",
    "明天",
    "最近",
    "一下",
    "一个",
    "我们",
    "你们",
    "他们",
    "事情",
    "东西",
    "问题",
    "就是",
    "然后",
    "因为",
    "所以",
    "现在",
    "已经",
    "感觉",
    "一下子",
}
_COMMAND_PREFIXES = (
    "/",
    "!",
    "！",
    "任务线程",
    "线程",
    "记账支出",
    "记账收入",
    "查账统计",
    "日统计",
    "月统计",
    "查账详情",
    "按类统计",
    "删除账单",
    "发邮件",
    "发邮件到",
    "发到邮箱",
    "发送到邮箱",
    "发送邮件",
)
_STATUS_OPEN = "open"
_STATUS_PAUSED = "paused"
_STATUS_COMPLETED = "completed"
_STATUS_STALE = "stale"
_STATUS_LABELS = {
    _STATUS_OPEN: "进行中",
    _STATUS_PAUSED: "已搁置",
    _STATUS_COMPLETED: "已完成",
    _STATUS_STALE: "待确认",
}
_STATUS_ORDER = {
    _STATUS_OPEN: 0,
    _STATUS_PAUSED: 1,
    _STATUS_STALE: 2,
    _STATUS_COMPLETED: 3,
}
_REFLECTION_NEED_HINTS = {
    "情绪支持": _SUPPORT_HINTS,
}
_STORE: "ReflectionTaskStore | None" = None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_ts() -> float:
    return datetime.now().timestamp()


def _trim_text(text: Any, *, max_len: int = 80) -> str:
    raw = str(text or "").strip()
    raw = raw.strip(_TRIM_PUNCT)
    raw = _WHITESPACE_PATTERN.sub(" ", raw)
    if len(raw) > max_len:
        raw = raw[:max_len].rstrip(" ，,.;；：:")
    return raw


def _normalize_text(text: Any) -> str:
    return _WHITESPACE_PATTERN.sub("", str(text or "").strip()).lower()


def _safe_get_message_text(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_message_str() or "").strip()
    except Exception:
        return ""


def _safe_get_sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or "").strip()
    except Exception:
        return ""


def _safe_get_sender_name(event: AstrMessageEvent) -> str:
    try:
        value = event.get_sender_name()
        if value:
            return str(value).strip()
    except Exception:
        pass
    return "用户"


def _safe_get_session_id(event: AstrMessageEvent) -> str:
    try:
        value = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", None)
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_is_private_chat(event: AstrMessageEvent) -> bool:
    try:
        return bool(event.is_private_chat())
    except Exception:
        return False


def _cfg_value(config: Any, key: str, default: Any) -> Any:
    try:
        value = getattr(config, key)
        if value is not None:
            return value
    except Exception:
        pass
    if isinstance(config, dict):
        if key in config and config[key] is not None:
            return config[key]
        group = config.get("cognition")
        if isinstance(group, dict):
            if key in group and group[key] is not None:
                return group[key]
            items = group.get("items")
            if isinstance(items, dict):
                raw = items.get(key)
                if isinstance(raw, dict) and "value" in raw:
                    raw = raw.get("value")
                if raw is not None:
                    return raw
    return default


def _cfg_bool(config: Any, key: str, default: bool) -> bool:
    return bool(_cfg_value(config, key, default))


def _cfg_int(config: Any, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_cfg_value(config, key, default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _looks_like_command(text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return True
    return any(content.startswith(prefix) for prefix in _COMMAND_PREFIXES)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？!?；;\n]+", str(text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def _tokenize(text: Any, *, min_len: int = 2) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_PATTERN.findall(str(text or "")):
        cleaned = token.strip(".,!?;:，。！？；：").lower()
        if not cleaned or len(cleaned) < min_len or cleaned in seen or cleaned in _STOPWORDS:
            continue
        tokens.append(cleaned)
        seen.add(cleaned)
    return tokens


def _pack_keywords(tokens: list[str]) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        cleaned = _trim_text(token, max_len=16).lower()
        if not cleaned or cleaned in seen or cleaned in _STOPWORDS:
            continue
        unique.append(cleaned)
        seen.add(cleaned)
    return "|".join(unique[:10])


def _unpack_keywords(payload: str) -> list[str]:
    return [item.strip() for item in str(payload or "").split("|") if item.strip()]


def _format_relative_time(ts: float) -> str:
    if ts <= 0:
        return ""
    diff = max(0.0, _now_ts() - ts)
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{int(diff // 60)}分钟前"
    if diff < 86400:
        return f"{int(diff // 3600)}小时前"
    return f"{int(diff // 86400)}天前"


def _extract_due_hint(text: str) -> str:
    cleaned = str(text or "").strip()
    for pattern in _DUE_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return _trim_text(match.group(0), max_len=20)
    return ""


def _derive_need(user_text: str) -> str:
    message = str(user_text or "").strip()
    if not message:
        return "陪伴回应"
    if any(hint in message for hint in _SUPPORT_HINTS):
        return "情绪支持"
    if any(hint in message for hint in _QUESTION_HINTS):
        return "明确回答"
    if _extract_due_hint(message):
        return "任务推进"
    if any(pattern.search(message) for pattern in _TASK_PATTERNS):
        return "任务推进"
    if any(hint in message for hint in _COMPLETION_HINTS):
        return "结果确认"
    return "延续交流"


def _derive_emotion(user_text: str) -> str:
    message = str(user_text or "").strip()
    if not message:
        return "平稳"
    if any(hint in message for hint in _SUPPORT_HINTS):
        return "压力偏高"
    if any(hint in message for hint in ("开心", "高兴", "兴奋", "爽", "激动")):
        return "积极"
    return "平稳"


def _extract_follow_up_hint(reply_text: str) -> str:
    content = str(reply_text or "").strip()
    if not content:
        return ""
    for sentence in _split_sentences(content):
        for pattern in _FOLLOW_UP_HINT_PATTERNS:
            match = pattern.search(sentence)
            if match:
                step = _trim_text(match.group("step"), max_len=52)
                if step:
                    return step
    return ""


def _extract_commitments(reply_text: str) -> list[str]:
    content = str(reply_text or "").strip()
    results: list[str] = []
    seen: set[str] = set()
    for sentence in _split_sentences(content):
        for pattern in _ASSISTANT_COMMIT_PATTERNS:
            match = pattern.search(sentence)
            if not match:
                continue
            commitment = _trim_text(match.group("commit"), max_len=52)
            normalized = _normalize_text(commitment)
            if not commitment or normalized in seen:
                continue
            results.append(commitment)
            seen.add(normalized)
            if len(results) >= 3:
                return results
    return results


def _clean_task_title(text: str) -> str:
    raw = str(text or "").strip()
    replacements = (
        "我最近正在",
        "我最近在",
        "我正在",
        "我在",
        "我准备",
        "我打算",
        "我计划",
        "我想要",
        "我想",
        "我得",
        "我需要",
        "明天要",
        "今晚要",
        "这周要",
        "下周要",
        "最近要",
        "周末要",
    )
    for prefix in replacements:
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    raw = re.sub(r"^(要|得|准备|打算|去|做|完成|提交|见|处理)", "", raw).strip()
    raw = _trim_text(raw, max_len=28)
    return raw or _trim_text(text, max_len=28)


def _build_thread_key(title: str) -> str:
    tokens = _tokenize(title, min_len=2)
    if tokens:
        return "_".join(tokens[:4])
    return _normalize_text(title)[:32]


def _calc_priority(text: str, due_hint: str) -> int:
    content = str(text or "").strip()
    if any(word in due_hint for word in ("明天", "今晚", "待会", "等下")):
        return 2
    if any(word.lower() in content.lower() for word in ("ddl", "deadline", "截止")):
        return 2
    if due_hint:
        return 1
    return 0


def _extract_task_candidates(text: str) -> list[dict[str, Any]]:
    content = str(text or "").strip()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in _TASK_PATTERNS:
        for match in pattern.finditer(content):
            summary = _trim_text(match.group("task"), max_len=64)
            if not summary or len(summary) < 4:
                continue
            title = _clean_task_title(summary)
            thread_key = _build_thread_key(title)
            if not thread_key or thread_key in seen:
                continue
            due_hint = _extract_due_hint(summary)
            candidates.append(
                {
                    "title": title,
                    "summary": summary,
                    "thread_key": thread_key,
                    "due_hint": due_hint,
                    "priority": _calc_priority(summary, due_hint),
                    "keywords": _pack_keywords(_tokenize(summary)),
                }
            )
            seen.add(thread_key)
            if len(candidates) >= 3:
                return candidates
    return candidates


class ReflectionTaskStore:
    def __init__(self) -> None:
        self._init_db()

    def _init_db(self) -> None:
        with shared_conn() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_THREAD_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    scene_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    summary TEXT NOT NULL DEFAULT '',
                    due_hint TEXT NOT NULL DEFAULT '',
                    next_step TEXT NOT NULL DEFAULT '',
                    last_user_note TEXT NOT NULL DEFAULT '',
                    last_bot_commitment TEXT NOT NULL DEFAULT '',
                    keywords TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.72,
                    priority INTEGER NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    archived_to_event INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    last_active_ts REAL NOT NULL,
                    UNIQUE(session_id, qq_id, normalized_key)
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_REFLECTION_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    user_need TEXT NOT NULL DEFAULT '',
                    emotional_tone TEXT NOT NULL DEFAULT '',
                    follow_up_hint TEXT NOT NULL DEFAULT '',
                    assistant_commitment TEXT NOT NULL DEFAULT '',
                    user_message TEXT NOT NULL DEFAULT '',
                    assistant_reply TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.72,
                    created_at TEXT NOT NULL,
                    created_ts REAL NOT NULL
                )
                """
            )
            self._ensure_column(conn, _THREAD_TABLE, "archived_to_event", "INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_THREAD_TABLE}_owner_active "
                f"ON {_THREAD_TABLE}(qq_id, status, last_active_ts DESC, updated_at DESC)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_THREAD_TABLE}_session_active "
                f"ON {_THREAD_TABLE}(session_id, qq_id, status, last_active_ts DESC)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_REFLECTION_TABLE}_owner_time "
                f"ON {_REFLECTION_TABLE}(qq_id, created_ts DESC, id DESC)"
            )
            conn.commit()

    def _ensure_column(self, conn: Any, table_name: str, column_name: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {str(row['name']) for row in rows}
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def observe_incoming_event(self, event: AstrMessageEvent, config: Any = None) -> None:
        if not _cfg_bool(config, "cognition_enabled", True):
            return
        if not _cfg_bool(config, "cognition_task_threads_enabled", True):
            return
        message_text = _safe_get_message_text(event)
        if not message_text or _looks_like_command(message_text):
            return
        qq_id = _safe_get_sender_id(event)
        session_id = _safe_get_session_id(event)
        if not qq_id or not session_id:
            return

        matched = self._find_best_match(session_id, qq_id, message_text)
        if matched and any(hint in message_text for hint in _COMPLETION_HINTS):
            self._update_thread(
                int(matched["id"]),
                status=_STATUS_COMPLETED,
                summary=_trim_text(message_text, max_len=72),
                last_user_note=_trim_text(message_text, max_len=72),
            )
            return
        if matched and any(hint in message_text for hint in _PAUSE_HINTS):
            self._update_thread(
                int(matched["id"]),
                status=_STATUS_PAUSED,
                summary=_trim_text(message_text, max_len=72),
                last_user_note=_trim_text(message_text, max_len=72),
            )
            return

        candidates = _extract_task_candidates(message_text)
        if candidates:
            for candidate in candidates:
                self._upsert_thread(
                    session_id=session_id,
                    qq_id=qq_id,
                    scene_type="private" if _safe_is_private_chat(event) else "group",
                    title=str(candidate["title"]),
                    summary=str(candidate["summary"]),
                    due_hint=str(candidate["due_hint"]),
                    keywords=str(candidate["keywords"]),
                    priority=int(candidate["priority"]),
                    last_user_note=_trim_text(message_text, max_len=72),
                )
            return

        if matched:
            new_status = _STATUS_OPEN if any(hint in message_text for hint in _REOPEN_HINTS) else str(matched["status"] or _STATUS_OPEN)
            self._update_thread(
                int(matched["id"]),
                status=new_status,
                summary=_trim_text(message_text, max_len=72),
                last_user_note=_trim_text(message_text, max_len=72),
            )

    def observe_outgoing_reply(self, event: AstrMessageEvent, reply_text: str, config: Any = None) -> None:
        if not _cfg_bool(config, "cognition_enabled", True):
            return
        qq_id = _safe_get_sender_id(event)
        session_id = _safe_get_session_id(event)
        user_text = _safe_get_message_text(event)
        if not qq_id or not session_id or not user_text or _looks_like_command(user_text):
            return

        cleaned_reply = _trim_text(reply_text, max_len=280)
        if not cleaned_reply:
            return

        if _cfg_bool(config, "cognition_posthoc_reflection_enabled", True):
            self._save_reflection(
                session_id=session_id,
                qq_id=qq_id,
                topic=self._build_topic(user_text),
                user_need=_derive_need(user_text),
                emotional_tone=_derive_emotion(user_text),
                follow_up_hint=_extract_follow_up_hint(cleaned_reply),
                assistant_commitment="；".join(_extract_commitments(cleaned_reply)[:2]),
                user_message=_trim_text(user_text, max_len=120),
                assistant_reply=cleaned_reply,
            )

        matched = self._find_best_match(session_id, qq_id, f"{user_text}\n{cleaned_reply}")
        follow_up_hint = _extract_follow_up_hint(cleaned_reply)
        commitments = _extract_commitments(cleaned_reply)
        if matched:
            self._update_thread(
                int(matched["id"]),
                status=_STATUS_OPEN if str(matched["status"] or "") != _STATUS_COMPLETED else _STATUS_COMPLETED,
                next_step=follow_up_hint or str(matched.get("next_step") or ""),
                last_bot_commitment=commitments[0] if commitments else str(matched.get("last_bot_commitment") or ""),
                summary=_trim_text(user_text, max_len=72),
            )
            return

        if any(pattern.search(user_text) for pattern in _TASK_PATTERNS):
            candidates = _extract_task_candidates(user_text)
            for candidate in candidates[:1]:
                self._upsert_thread(
                    session_id=session_id,
                    qq_id=qq_id,
                    scene_type="private" if _safe_is_private_chat(event) else "group",
                    title=str(candidate["title"]),
                    summary=str(candidate["summary"]),
                    due_hint=str(candidate["due_hint"]),
                    keywords=str(candidate["keywords"]),
                    priority=int(candidate["priority"]),
                    next_step=follow_up_hint,
                    last_user_note=_trim_text(user_text, max_len=72),
                    last_bot_commitment=commitments[0] if commitments else "",
                )

    def build_prompt_for_event(self, event: AstrMessageEvent, message_text: str, config: Any = None) -> str:
        if not _cfg_bool(config, "cognition_enabled", True):
            return ""
        qq_id = _safe_get_sender_id(event)
        session_id = _safe_get_session_id(event)
        if not qq_id:
            return ""

        thread_limit = _cfg_int(config, "cognition_max_prompt_threads", 4, 1, 8)
        reflection_limit = _cfg_int(config, "cognition_max_prompt_reflections", 2, 1, 4)
        max_chars = _cfg_int(config, "cognition_task_prompt_max_chars", 700, 200, 2500)

        threads = self._select_threads_for_prompt(qq_id, session_id, message_text, limit=thread_limit)
        reflections = self._list_recent_reflections(qq_id, session_id, limit=reflection_limit)
        if not threads and not reflections:
            return ""

        lines = ["【任务线程与事后反思】"]
        if threads:
            lines.append("- 当前值得延续的任务线程：")
            for row in threads:
                status_text = _STATUS_LABELS.get(str(row["status"] or _STATUS_OPEN), "进行中")
                due_hint = str(row.get("due_hint") or "").strip()
                next_step = str(row.get("next_step") or "").strip()
                suffix_parts: list[str] = [status_text]
                if due_hint:
                    suffix_parts.append(f"时间线 {due_hint}")
                if next_step:
                    suffix_parts.append(f"下一步 {next_step}")
                lines.append(f"  - #{row['id']} {row['title']}｜{'｜'.join(suffix_parts)}")
        if reflections:
            lines.append("- 最近两轮互动里得到的反思：")
            for row in reflections:
                need = str(row.get("user_need") or "").strip()
                emotion = str(row.get("emotional_tone") or "").strip()
                follow_up = str(row.get("follow_up_hint") or "").strip()
                commitment = str(row.get("assistant_commitment") or "").strip()
                parts: list[str] = []
                if need:
                    parts.append(f"用户更需要：{need}")
                if emotion and emotion != "平稳":
                    parts.append(f"情绪：{emotion}")
                if follow_up:
                    parts.append(f"建议延续：{follow_up}")
                if commitment:
                    parts.append(f"你上轮承诺：{commitment}")
                if parts:
                    lines.append(f"  - {'；'.join(parts)}")
        lines.extend(
            [
                "- 使用规则：",
                "  - 这些内容用于帮助你延续同一件事，不要把旧线程硬塞进无关话题。",
                "  - 如果用户当下明确表示任务完成、取消或换方向，以当前表达为准并及时转向。",
                "  - 当存在未完成线程时，优先承接上轮进度、下一步和你自己已经做过的承诺。",
            ]
        )
        prompt = "\n".join(lines).strip()
        if len(prompt) <= max_chars:
            return prompt
        return prompt[:max_chars].rstrip() + "\n...(任务线程提示已按预算截断)"

    def list_threads(self, event: AstrMessageEvent, *, include_finished: bool = False) -> list[dict[str, Any]]:
        qq_id = _safe_get_sender_id(event)
        session_id = _safe_get_session_id(event)
        if not qq_id:
            return []
        with shared_conn() as conn:
            if include_finished:
                rows = conn.execute(
                    f"""
                    SELECT * FROM {_THREAD_TABLE}
                    WHERE qq_id = ?
                    ORDER BY
                        CASE status
                            WHEN 'open' THEN 0
                            WHEN 'paused' THEN 1
                            WHEN 'stale' THEN 2
                            ELSE 3
                        END,
                        CASE WHEN session_id = ? THEN 0 ELSE 1 END,
                        last_active_ts DESC,
                        id DESC
                    LIMIT 30
                    """,
                    (qq_id, session_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM {_THREAD_TABLE}
                    WHERE qq_id = ? AND status IN ('open', 'paused', 'stale')
                    ORDER BY
                        CASE status
                            WHEN 'open' THEN 0
                            WHEN 'paused' THEN 1
                            ELSE 2
                        END,
                        CASE WHEN session_id = ? THEN 0 ELSE 1 END,
                        last_active_ts DESC,
                        id DESC
                    LIMIT 30
                    """,
                    (qq_id, session_id),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_thread_for_event(self, event: AstrMessageEvent, thread_id: int) -> dict[str, Any] | None:
        qq_id = _safe_get_sender_id(event)
        if not qq_id:
            return None
        with shared_conn() as conn:
            row = conn.execute(
                f"SELECT * FROM {_THREAD_TABLE} WHERE id = ? AND qq_id = ?",
                (int(thread_id), qq_id),
            ).fetchone()
        return dict(row) if row else None

    def set_thread_status(self, event: AstrMessageEvent, thread_id: int, status: str) -> dict[str, Any] | None:
        thread = self.get_thread_for_event(event, thread_id)
        if not thread:
            return None
        self._update_thread(int(thread_id), status=status)
        return self.get_thread_for_event(event, thread_id)

    def delete_thread(self, event: AstrMessageEvent, thread_id: int) -> bool:
        qq_id = _safe_get_sender_id(event)
        if not qq_id:
            return False
        with shared_conn() as conn:
            cursor = conn.execute(
                f"DELETE FROM {_THREAD_TABLE} WHERE id = ? AND qq_id = ?",
                (int(thread_id), qq_id),
            )
            deleted = cursor.rowcount > 0
            if deleted:
                conn.commit()
            return deleted

    def run_sleep_maintenance(self, config: Any = None) -> dict[str, int]:
        if not _cfg_bool(config, "cognition_enabled", True):
            return {"stale_marked": 0, "merged": 0, "archived": 0, "reflections_pruned": 0, "threads_pruned": 0}

        now = datetime.now()
        stale_days = _cfg_int(config, "cognition_thread_stale_days", 7, 1, 90)
        retention_days = _cfg_int(config, "cognition_thread_retention_days", 45, 3, 180)
        stale_cutoff_ts = (now - timedelta(days=stale_days)).timestamp()
        retention_cutoff_ts = (now - timedelta(days=retention_days)).timestamp()
        stats = {"stale_marked": 0, "merged": 0, "archived": 0, "reflections_pruned": 0, "threads_pruned": 0}

        with shared_conn() as conn:
            stale_rows = conn.execute(
                f"""
                SELECT id, confidence
                FROM {_THREAD_TABLE}
                WHERE status IN ('open', 'paused') AND last_active_ts < ?
                """,
                (stale_cutoff_ts,),
            ).fetchall()
            for row in stale_rows:
                new_confidence = max(0.42, float(row["confidence"] or 0.7) - 0.08)
                conn.execute(
                    f"""
                    UPDATE {_THREAD_TABLE}
                    SET status = ?, confidence = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_STATUS_STALE, new_confidence, _now_str(), int(row["id"])),
                )
                stats["stale_marked"] += 1

            threads = conn.execute(
                f"""
                SELECT * FROM {_THREAD_TABLE}
                WHERE status IN ('open', 'paused', 'stale')
                ORDER BY qq_id, session_id, last_active_ts DESC, id DESC
                """
            ).fetchall()
            grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for row in threads:
                item = dict(row)
                grouped.setdefault((str(item["qq_id"]), str(item["session_id"])), []).append(item)
            for (_qq_id, _session_id), items in grouped.items():
                primary_items: list[dict[str, Any]] = []
                for item in items:
                    duplicate = self._find_duplicate_in_list(primary_items, item)
                    if duplicate is None:
                        primary_items.append(item)
                        continue
                    self._merge_threads(conn, primary=duplicate, duplicate=item)
                    stats["merged"] += 1

            archivable = conn.execute(
                f"""
                SELECT * FROM {_THREAD_TABLE}
                WHERE status = 'completed' AND archived_to_event = 0 AND evidence_count >= 2 AND last_active_ts < ?
                """,
                ((now - timedelta(days=1)).timestamp(),),
            ).fetchall()
            conn.commit()

        passive_store = init_passive_memory_store()
        for row in archivable:
            item = dict(row)
            summary = _trim_text(f"最近一直在处理「{item['title']}」，当前已经完成。", max_len=48)
            saved = passive_store.save_event(
                str(item["qq_id"]),
                summary,
                event_date_label="最近",
                confidence=min(0.93, float(item.get("confidence") or 0.72) + 0.05),
                source_text="task_thread_sleep",
            )
            if saved:
                with shared_conn() as conn:
                    conn.execute(
                        f"UPDATE {_THREAD_TABLE} SET archived_to_event = 1, updated_at = ? WHERE id = ?",
                        (_now_str(), int(item["id"])),
                    )
                    conn.commit()
                stats["archived"] += 1

        with shared_conn() as conn:
            reflections_cursor = conn.execute(
                f"DELETE FROM {_REFLECTION_TABLE} WHERE created_ts < ?",
                (retention_cutoff_ts,),
            )
            stats["reflections_pruned"] = max(0, int(reflections_cursor.rowcount or 0))
            threads_cursor = conn.execute(
                f"""
                DELETE FROM {_THREAD_TABLE}
                WHERE status IN ('completed', 'stale') AND last_active_ts < ?
                """,
                (retention_cutoff_ts,),
            )
            stats["threads_pruned"] = max(0, int(threads_cursor.rowcount or 0))
            conn.commit()
        return stats

    def _build_topic(self, text: str) -> str:
        counter = Counter(_tokenize(text, min_len=2))
        if not counter:
            return ""
        return "、".join(token for token, _count in counter.most_common(3))

    def _save_reflection(
        self,
        *,
        session_id: str,
        qq_id: str,
        topic: str,
        user_need: str,
        emotional_tone: str,
        follow_up_hint: str,
        assistant_commitment: str,
        user_message: str,
        assistant_reply: str,
    ) -> None:
        now_text = _now_str()
        now_ts = _now_ts()
        with shared_conn() as conn:
            last_row = conn.execute(
                f"""
                SELECT user_message, assistant_reply, created_ts
                FROM {_REFLECTION_TABLE}
                WHERE session_id = ? AND qq_id = ?
                ORDER BY created_ts DESC, id DESC
                LIMIT 1
                """,
                (session_id, qq_id),
            ).fetchone()
            if last_row is not None:
                if (
                    _normalize_text(last_row["user_message"]) == _normalize_text(user_message)
                    and _normalize_text(last_row["assistant_reply"]) == _normalize_text(assistant_reply)
                    and abs(float(last_row["created_ts"] or 0) - now_ts) <= 8
                ):
                    return

            conn.execute(
                f"""
                INSERT INTO {_REFLECTION_TABLE}(
                    session_id, qq_id, topic, user_need, emotional_tone, follow_up_hint,
                    assistant_commitment, user_message, assistant_reply, confidence, created_at, created_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    qq_id,
                    _trim_text(topic, max_len=40),
                    _trim_text(user_need, max_len=20),
                    _trim_text(emotional_tone, max_len=20),
                    _trim_text(follow_up_hint, max_len=52),
                    _trim_text(assistant_commitment, max_len=60),
                    _trim_text(user_message, max_len=120),
                    _trim_text(assistant_reply, max_len=280),
                    0.74,
                    now_text,
                    now_ts,
                ),
            )
            conn.commit()

    def _list_recent_reflections(self, qq_id: str, session_id: str, *, limit: int) -> list[dict[str, Any]]:
        with shared_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM {_REFLECTION_TABLE}
                WHERE qq_id = ?
                ORDER BY CASE WHEN session_id = ? THEN 0 ELSE 1 END, created_ts DESC, id DESC
                LIMIT ?
                """,
                (qq_id, session_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def _list_candidate_threads(self, qq_id: str, session_id: str) -> list[dict[str, Any]]:
        with shared_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM {_THREAD_TABLE}
                WHERE qq_id = ? AND status IN ('open', 'paused', 'stale', 'completed')
                ORDER BY CASE WHEN session_id = ? THEN 0 ELSE 1 END, last_active_ts DESC, id DESC
                LIMIT 30
                """,
                (qq_id, session_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _select_threads_for_prompt(
        self,
        qq_id: str,
        session_id: str,
        message_text: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        candidates = self._list_candidate_threads(qq_id, session_id)
        if not candidates:
            return []
        message_tokens = set(_tokenize(message_text, min_len=2))
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in candidates:
            status = str(item.get("status") or _STATUS_OPEN)
            if status == _STATUS_COMPLETED:
                continue
            thread_tokens = set(_unpack_keywords(str(item.get("keywords") or ""))) or set(_tokenize(item.get("title") or "", min_len=2))
            overlap = len(message_tokens & thread_tokens) if message_tokens else 0
            base = 8.0 if status == _STATUS_OPEN else 6.5 if status == _STATUS_PAUSED else 5.0
            base += 2.0 if str(item.get("session_id") or "") == session_id else 0.3
            base += min(int(item.get("priority") or 0), 2) * 1.1
            base += overlap * 2.2
            base += min(int(item.get("evidence_count") or 1), 5) * 0.25
            scored.append((base, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _score, item in scored[: max(1, int(limit))]]

    def _find_best_match(self, session_id: str, qq_id: str, text: str) -> dict[str, Any] | None:
        message_tokens = set(_tokenize(text, min_len=2))
        if not message_tokens:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for item in self._list_candidate_threads(qq_id, session_id):
            item_tokens = set(_unpack_keywords(str(item.get("keywords") or ""))) or set(_tokenize(item.get("title") or "", min_len=2))
            if not item_tokens:
                continue
            overlap = len(message_tokens & item_tokens)
            if overlap <= 0:
                continue
            score = overlap * 2.5
            if str(item.get("session_id") or "") == session_id:
                score += 1.5
            if str(item.get("status") or "") in {_STATUS_OPEN, _STATUS_PAUSED}:
                score += 1.0
            if best is None or score > best[0]:
                best = (score, item)
        return best[1] if best and best[0] >= 2.5 else None

    def _find_duplicate_in_list(self, existing_items: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any] | None:
        current_key = str(item.get("normalized_key") or "").strip()
        current_tokens = set(_unpack_keywords(str(item.get("keywords") or ""))) or set(_tokenize(item.get("title") or "", min_len=2))
        for existing in existing_items:
            existing_key = str(existing.get("normalized_key") or "").strip()
            existing_tokens = set(_unpack_keywords(str(existing.get("keywords") or ""))) or set(_tokenize(existing.get("title") or "", min_len=2))
            if current_key and existing_key and current_key == existing_key:
                return existing
            overlap = len(current_tokens & existing_tokens)
            if overlap >= 3:
                return existing
            if current_tokens and existing_tokens:
                ratio = overlap / max(len(current_tokens), len(existing_tokens))
                if ratio >= 0.75 and overlap >= 2:
                    return existing
        return None

    def _merge_threads(self, conn: Any, *, primary: dict[str, Any], duplicate: dict[str, Any]) -> None:
        merged_keywords = {
            item.strip()
            for item in str(primary.get("keywords") or "").split("|")
            if item.strip()
        }
        merged_keywords.update(item.strip() for item in str(duplicate.get("keywords") or "").split("|") if item.strip())
        merged_summary = max(
            [str(primary.get("summary") or "").strip(), str(duplicate.get("summary") or "").strip()],
            key=len,
        )
        merged_note = max(
            [str(primary.get("last_user_note") or "").strip(), str(duplicate.get("last_user_note") or "").strip()],
            key=len,
        )
        merged_commitment = max(
            [str(primary.get("last_bot_commitment") or "").strip(), str(duplicate.get("last_bot_commitment") or "").strip()],
            key=len,
        )
        merged_next_step = max(
            [str(primary.get("next_step") or "").strip(), str(duplicate.get("next_step") or "").strip()],
            key=len,
        )
        status = str(primary.get("status") or _STATUS_OPEN)
        duplicate_status = str(duplicate.get("status") or _STATUS_OPEN)
        if _STATUS_ORDER.get(duplicate_status, 9) < _STATUS_ORDER.get(status, 9):
            status = duplicate_status
        conn.execute(
            f"""
            UPDATE {_THREAD_TABLE}
            SET summary = ?, due_hint = ?, next_step = ?, last_user_note = ?, last_bot_commitment = ?,
                keywords = ?, confidence = ?, priority = ?, evidence_count = ?, archived_to_event = ?,
                status = ?, updated_at = ?, last_active_at = ?, last_active_ts = ?
            WHERE id = ?
            """,
            (
                merged_summary,
                str(primary.get("due_hint") or "").strip() or str(duplicate.get("due_hint") or "").strip(),
                merged_next_step,
                merged_note,
                merged_commitment,
                "|".join(sorted(merged_keywords)[:10]),
                min(0.97, max(float(primary.get("confidence") or 0.72), float(duplicate.get("confidence") or 0.72)) + 0.03),
                max(int(primary.get("priority") or 0), int(duplicate.get("priority") or 0)),
                int(primary.get("evidence_count") or 1) + int(duplicate.get("evidence_count") or 1),
                1 if int(primary.get("archived_to_event") or 0) or int(duplicate.get("archived_to_event") or 0) else 0,
                status,
                _now_str(),
                str(primary.get("last_active_at") or "").strip()
                if float(primary.get("last_active_ts") or 0) >= float(duplicate.get("last_active_ts") or 0)
                else str(duplicate.get("last_active_at") or "").strip(),
                max(float(primary.get("last_active_ts") or 0), float(duplicate.get("last_active_ts") or 0)),
                int(primary["id"]),
            ),
        )
        conn.execute(f"DELETE FROM {_THREAD_TABLE} WHERE id = ?", (int(duplicate["id"]),))

    def _upsert_thread(
        self,
        *,
        session_id: str,
        qq_id: str,
        scene_type: str,
        title: str,
        summary: str,
        due_hint: str,
        keywords: str,
        priority: int,
        next_step: str = "",
        last_user_note: str = "",
        last_bot_commitment: str = "",
    ) -> None:
        normalized_key = _build_thread_key(title)
        now_text = _now_str()
        now_ts = _now_ts()
        with shared_conn() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {_THREAD_TABLE}
                WHERE session_id = ? AND qq_id = ? AND normalized_key = ?
                """,
                (session_id, qq_id, normalized_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    f"""
                    INSERT INTO {_THREAD_TABLE}(
                        session_id, qq_id, scene_type, title, normalized_key, status, summary, due_hint,
                        next_step, last_user_note, last_bot_commitment, keywords, confidence, priority,
                        evidence_count, archived_to_event, created_at, updated_at, last_active_at, last_active_ts
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        qq_id,
                        scene_type,
                        _trim_text(title, max_len=28),
                        normalized_key,
                        _STATUS_OPEN,
                        _trim_text(summary, max_len=72),
                        _trim_text(due_hint, max_len=20),
                        _trim_text(next_step, max_len=52),
                        _trim_text(last_user_note, max_len=72),
                        _trim_text(last_bot_commitment, max_len=60),
                        keywords,
                        0.74,
                        int(priority),
                        1,
                        now_text,
                        now_text,
                        now_text,
                        now_ts,
                    ),
                )
                conn.commit()
                return

            existing = dict(row)
            merged_keywords = {
                item.strip()
                for item in str(existing.get("keywords") or "").split("|")
                if item.strip()
            }
            merged_keywords.update(item.strip() for item in str(keywords or "").split("|") if item.strip())
            status = str(existing.get("status") or _STATUS_OPEN)
            if status == _STATUS_COMPLETED:
                status = _STATUS_OPEN
            conn.execute(
                f"""
                UPDATE {_THREAD_TABLE}
                SET title = ?, status = ?, summary = ?, due_hint = ?, next_step = ?, last_user_note = ?,
                    last_bot_commitment = ?, keywords = ?, confidence = ?, priority = ?, evidence_count = ?,
                    archived_to_event = 0, updated_at = ?, last_active_at = ?, last_active_ts = ?
                WHERE id = ?
                """,
                (
                    _trim_text(title, max_len=28),
                    status,
                    _trim_text(summary, max_len=72) or str(existing.get("summary") or ""),
                    _trim_text(due_hint, max_len=20) or str(existing.get("due_hint") or ""),
                    _trim_text(next_step, max_len=52) or str(existing.get("next_step") or ""),
                    _trim_text(last_user_note, max_len=72) or str(existing.get("last_user_note") or ""),
                    _trim_text(last_bot_commitment, max_len=60) or str(existing.get("last_bot_commitment") or ""),
                    "|".join(sorted(merged_keywords)[:10]),
                    min(0.97, float(existing.get("confidence") or 0.72) + 0.04),
                    max(int(existing.get("priority") or 0), int(priority)),
                    int(existing.get("evidence_count") or 1) + 1,
                    now_text,
                    now_text,
                    now_ts,
                    int(existing["id"]),
                ),
            )
            conn.commit()

    def _update_thread(
        self,
        thread_id: int,
        *,
        status: str | None = None,
        summary: str | None = None,
        next_step: str | None = None,
        last_user_note: str | None = None,
        last_bot_commitment: str | None = None,
    ) -> None:
        with shared_conn() as conn:
            row = conn.execute(f"SELECT * FROM {_THREAD_TABLE} WHERE id = ?", (int(thread_id),)).fetchone()
            if row is None:
                return
            item = dict(row)
            new_status = str(status or item.get("status") or _STATUS_OPEN)
            now_text = _now_str()
            now_ts = _now_ts()
            conn.execute(
                f"""
                UPDATE {_THREAD_TABLE}
                SET status = ?, summary = ?, next_step = ?, last_user_note = ?, last_bot_commitment = ?,
                    confidence = ?, archived_to_event = CASE WHEN ? = 'completed' THEN archived_to_event ELSE 0 END,
                    updated_at = ?, last_active_at = ?, last_active_ts = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    _trim_text(summary, max_len=72) or str(item.get("summary") or ""),
                    _trim_text(next_step, max_len=52) or str(item.get("next_step") or ""),
                    _trim_text(last_user_note, max_len=72) or str(item.get("last_user_note") or ""),
                    _trim_text(last_bot_commitment, max_len=60) or str(item.get("last_bot_commitment") or ""),
                    min(0.97, float(item.get("confidence") or 0.72) + (0.02 if new_status == _STATUS_OPEN else 0)),
                    new_status,
                    now_text,
                    now_text,
                    now_ts,
                    int(thread_id),
                ),
            )
            conn.commit()


def init_reflection_task_store() -> ReflectionTaskStore:
    global _STORE
    if _STORE is None:
        _STORE = ReflectionTaskStore()
        logger.info("[astrbot_all_char] 事后反思与任务线程存储已初始化")
    return _STORE


def observe_reflection_task_incoming_event(event: AstrMessageEvent, config: Any = None) -> None:
    init_reflection_task_store().observe_incoming_event(event, config)


def observe_reflection_task_outgoing_reply(event: AstrMessageEvent, reply_text: str, config: Any = None) -> None:
    init_reflection_task_store().observe_outgoing_reply(event, reply_text, config)


def build_reflection_task_prompt(event: AstrMessageEvent, message_text: str, config: Any = None) -> str:
    return init_reflection_task_store().build_prompt_for_event(event, message_text, config)


def run_reflection_task_sleep_maintenance(config: Any = None) -> dict[str, int]:
    return init_reflection_task_store().run_sleep_maintenance(config)


def _help_text() -> str:
    return (
        "用法：`/任务线程 [子命令]`\n"
        "常用：\n"
        "- `/任务线程` 或 `/任务线程 列表`\n"
        "- `/任务线程 全部`\n"
        "- `/任务线程 查看 12`\n"
        "- `/任务线程 完成 12`\n"
        "- `/任务线程 搁置 12`\n"
        "- `/任务线程 继续 12`\n"
        "- `/任务线程 删除 12`\n"
        "- `/任务线程 整理`\n"
        "说明：系统会从普通聊天里自动提炼长期事项，你也可以用这里查看和管理。"
    )


def _format_thread_summary(thread: dict[str, Any], *, current_session_id: str = "") -> str:
    status = _STATUS_LABELS.get(str(thread.get("status") or _STATUS_OPEN), "进行中")
    session_suffix = "（当前会话）" if current_session_id and str(thread.get("session_id") or "") == current_session_id else ""
    due_hint = str(thread.get("due_hint") or "").strip()
    next_step = str(thread.get("next_step") or "").strip()
    confidence = float(thread.get("confidence") or 0.72)
    evidence = int(thread.get("evidence_count") or 1)
    updated = _format_relative_time(float(thread.get("last_active_ts") or 0))
    parts = [f"#{thread['id']} {thread['title']} {session_suffix}".rstrip(), f"状态: {status}"]
    if due_hint:
        parts.append(f"时间线: {due_hint}")
    if next_step:
        parts.append(f"下一步: {next_step}")
    parts.append(f"最近活跃: {updated or '未知'}")
    parts.append(f"置信度: {confidence:.2f} / 证据 {evidence} 次")
    return "｜".join(parts)


async def handle_task_thread_command(event: AstrMessageEvent, config: Any = None):
    raw = _safe_get_message_text(event)
    matched = re.match(r"^[\/！!]?任务线程(?:[\s\n]+(.+))?$", raw)
    if matched:
        body = (matched.group(1) or "").strip()
    else:
        matched = re.match(r"^[\/！!]?线程(?:[\s\n]+(.+))?$", raw)
        body = (matched.group(1) or "").strip() if matched else ""

    store = init_reflection_task_store()
    current_session_id = _safe_get_session_id(event)

    if not body or body in {"帮助", "help", "h"}:
        yield event.plain_result(_help_text())
        return

    if body in {"列表", "我的", "当前"}:
        threads = store.list_threads(event, include_finished=False)
        if not threads:
            yield event.plain_result("当前没有进行中的任务线程。先正常聊天，系统会自动从对话里提炼。")
            return
        lines = ["🧵 当前任务线程："]
        for item in threads[:12]:
            lines.append(f"- {_format_thread_summary(item, current_session_id=current_session_id)}")
        yield event.plain_result("\n".join(lines))
        return

    if body in {"全部", "所有"}:
        threads = store.list_threads(event, include_finished=True)
        if not threads:
            yield event.plain_result("当前还没有任何任务线程记录。")
            return
        lines = ["🧵 全部任务线程："]
        for item in threads[:20]:
            lines.append(f"- {_format_thread_summary(item, current_session_id=current_session_id)}")
        yield event.plain_result("\n".join(lines))
        return

    if body == "整理":
        stats = run_reflection_task_sleep_maintenance(config)
        yield event.plain_result(
            "已执行一次睡眠整理：\n"
            f"- 标记 stale: {stats['stale_marked']}\n"
            f"- 合并相近线程: {stats['merged']}\n"
            f"- 归档完成事项到事件记忆: {stats['archived']}\n"
            f"- 清理旧反思: {stats['reflections_pruned']}\n"
            f"- 清理旧线程: {stats['threads_pruned']}"
        )
        return

    action_match = re.match(r"^(查看|完成|搁置|暂停|继续|激活|删除)\s+(\d+)\s*$", body)
    if not action_match:
        yield event.plain_result(_help_text())
        return

    action = action_match.group(1)
    thread_id = int(action_match.group(2))
    thread = store.get_thread_for_event(event, thread_id)
    if thread is None:
        yield event.plain_result(f"没有找到编号为 {thread_id} 的任务线程。")
        return

    if action == "查看":
        lines = ["🧵 任务线程详情"]
        lines.append(f"编号: #{thread['id']}")
        lines.append(f"标题: {thread['title']}")
        lines.append(f"状态: {_STATUS_LABELS.get(str(thread.get('status') or _STATUS_OPEN), '进行中')}")
        summary = str(thread.get("summary") or "").strip()
        due_hint = str(thread.get("due_hint") or "").strip()
        next_step = str(thread.get("next_step") or "").strip()
        last_user_note = str(thread.get("last_user_note") or "").strip()
        commitment = str(thread.get("last_bot_commitment") or "").strip()
        if summary:
            lines.append(f"最近摘要: {summary}")
        if due_hint:
            lines.append(f"时间线: {due_hint}")
        if next_step:
            lines.append(f"下一步: {next_step}")
        if last_user_note:
            lines.append(f"最近用户补充: {last_user_note}")
        if commitment:
            lines.append(f"你上轮承诺: {commitment}")
        lines.append(f"最近活跃: {_format_relative_time(float(thread.get('last_active_ts') or 0)) or '未知'}")
        lines.append(f"证据次数: {int(thread.get('evidence_count') or 1)}")
        lines.append(f"置信度: {float(thread.get('confidence') or 0.72):.2f}")
        yield event.plain_result("\n".join(lines))
        return

    if action in {"完成"}:
        updated = store.set_thread_status(event, thread_id, _STATUS_COMPLETED)
        yield event.plain_result(f"已将任务线程 #{thread_id} 标记为已完成：{updated['title']}" if updated else "更新失败。")
        return

    if action in {"搁置", "暂停"}:
        updated = store.set_thread_status(event, thread_id, _STATUS_PAUSED)
        yield event.plain_result(f"已将任务线程 #{thread_id} 标记为已搁置：{updated['title']}" if updated else "更新失败。")
        return

    if action in {"继续", "激活"}:
        updated = store.set_thread_status(event, thread_id, _STATUS_OPEN)
        yield event.plain_result(f"已重新激活任务线程 #{thread_id}：{updated['title']}" if updated else "更新失败。")
        return

    if action == "删除":
        deleted = store.delete_thread(event, thread_id)
        yield event.plain_result(f"已删除任务线程 #{thread_id}。" if deleted else "删除失败。")
        return
