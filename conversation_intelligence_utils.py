from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .memory_state_store import shared_conn


_MESSAGE_TABLE = "conversation_messages"
_INSIGHT_TABLE = "conversation_insights"
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9._-]{1,15}")
_NORMALIZE_WHITESPACE = re.compile(r"\s+")
_LEADING_PUNCT = " \t\r\n\"'“”‘’[]【】()（）<>《》"
_STOPWORDS = {
    "",
    "这个",
    "那个",
    "这里",
    "那里",
    "今天",
    "明天",
    "昨天",
    "最近",
    "刚刚",
    "刚才",
    "现在",
    "一下",
    "一个",
    "一种",
    "一些",
    "还是",
    "已经",
    "因为",
    "所以",
    "就是",
    "然后",
    "但是",
    "而且",
    "如果",
    "怎么",
    "什么",
    "这样",
    "那样",
    "我们",
    "你们",
    "他们",
    "自己",
    "感觉",
    "事情",
    "东西",
    "问题",
    "时候",
    "一下子",
    "哈哈",
    "哈哈哈",
    "嗯嗯",
    "好的",
    "可以",
    "真的",
    "有点",
    "一下下",
}
_QUESTION_HINTS = (
    "?",
    "？",
    "吗",
    "呢",
    "怎么",
    "如何",
    "为什么",
    "咋办",
    "要不要",
    "行不行",
    "帮我",
)
_SUPPORT_HINTS = (
    "烦",
    "累",
    "焦虑",
    "难受",
    "崩溃",
    "委屈",
    "难过",
    "紧张",
    "害怕",
    "慌",
    "压力",
    "失眠",
    "心累",
    "不开心",
    "糟糕",
)
_PLANNING_HINTS = (
    "明天",
    "今晚",
    "待会",
    "等下",
    "这周",
    "周末",
    "下周",
    "计划",
    "安排",
    "准备",
    "打算",
    "deadline",
    "ddl",
)
_COMMAND_PREFIXES = (
    "/",
    "!",
    "！",
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
    "订阅邮件",
    "取消邮件订阅",
    "我的邮件订阅",
)
_EMOTION_KEYWORDS = {
    "焦虑": ("焦虑", "紧张", "慌", "压力", "不安"),
    "疲惫": ("累", "好累", "疲惫", "困", "撑不住", "心累"),
    "低落": ("难过", "委屈", "崩溃", "糟糕", "不开心", "伤心"),
    "开心": ("开心", "高兴", "兴奋", "爽", "快乐", "激动"),
}
_GOAL_PATTERNS = (
    re.compile(r"(?:我(?:最近)?(?:正在|在|准备|想|想要|打算|计划))(?P<goal>[^。！？\n]{2,48})"),
    re.compile(r"(?:这周|最近|目前|这阵子)[^。！？\n]{0,8}(?:在|要|准备|忙着|一直在)(?P<goal>[^。！？\n]{2,48})"),
)
_TASK_PATTERNS = (
    re.compile(r"(?P<task>(?:明天|今晚|待会|等下|这周|周末|下周)[^。！？\n]{0,10}(?:要|得|准备|打算)[^。！？\n]{2,48})"),
    re.compile(r"(?P<task>(?:ddl|deadline|截止)[^。！？\n]{0,20})", re.IGNORECASE),
)
_ASSISTANT_COMMIT_PATTERNS = (
    re.compile(r"(?:我可以|我能|我帮你|要不要我|我们可以)(?P<commit>[^。！？\n]{2,48})"),
    re.compile(r"(?:接下来|然后)可以(?P<commit>[^。！？\n]{2,48})"),
)
_INSIGHT_TYPE_LABELS = {
    "user_goal": "用户最近在忙",
    "pending_task": "待跟进事项",
    "emotion_state": "用户当前情绪",
    "assistant_commitment": "你上轮承诺过",
}
_INSIGHT_BASE_WEIGHTS = {
    "user_goal": 0.78,
    "pending_task": 0.82,
    "emotion_state": 0.72,
    "assistant_commitment": 0.8,
}

_STORE: "ConversationIntelligenceStore | None" = None


def _now_ts() -> float:
    return datetime.now().timestamp()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _trim_clause(text: Any, max_len: int = 64) -> str:
    raw = str(text or "").strip()
    raw = raw.strip(_LEADING_PUNCT)
    raw = _NORMALIZE_WHITESPACE.sub(" ", raw)
    if len(raw) > max_len:
        raw = raw[:max_len].rstrip(" ，,.;；：:")
    return raw


def _normalize_text(text: Any) -> str:
    return _NORMALIZE_WHITESPACE.sub("", str(text or "").strip()).lower()


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
        name = event.get_sender_name()
        if name:
            return str(name).strip()
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
        group = config.get("conversation")
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


def _looks_like_command_message(text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return True
    if content.isdigit() and len(content) <= 3:
        return True
    return any(content.startswith(prefix) for prefix in _COMMAND_PREFIXES)


def _tokenize(text: str, *, min_len: int = 2) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_PATTERN.findall(str(text or "")):
        cleaned = token.strip(".,!?;:，。！？；：").lower()
        if not cleaned or len(cleaned) < min_len or cleaned in seen or cleaned in _STOPWORDS:
            continue
        tokens.append(cleaned)
        seen.add(cleaned)
    return tokens


def _format_relative_time(ts: float) -> str:
    if ts <= 0:
        return ""
    diff = max(0, _now_ts() - ts)
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{int(diff // 60)}分钟前"
    if diff < 86400:
        return f"{int(diff // 3600)}小时前"
    return f"{int(diff // 86400)}天前"


class ConversationIntelligenceStore:
    def __init__(self) -> None:
        self._init_db()

    def _init_db(self) -> None:
        with shared_conn() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_MESSAGE_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    scene_type TEXT NOT NULL,
                    role TEXT NOT NULL,
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    is_command INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_INSIGHT_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    insight_type TEXT NOT NULL,
                    subject_key TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '',
                    weight REAL NOT NULL DEFAULT 0.7,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_ts REAL NOT NULL,
                    UNIQUE(session_id, insight_type, normalized_content)
                )
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_MESSAGE_TABLE}_session_ts "
                f"ON {_MESSAGE_TABLE}(session_id, created_ts DESC, id DESC)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{_INSIGHT_TABLE}_session_ts "
                f"ON {_INSIGHT_TABLE}(session_id, updated_ts DESC, id DESC)"
            )
            conn.commit()

    def observe_incoming_event(self, event: AstrMessageEvent, config: Any = None) -> None:
        if not _cfg_bool(config, "conversation_enabled", True):
            return
        content = _trim_clause(_safe_get_message_text(event), max_len=240)
        if not content or _looks_like_command_message(content):
            return
        session_id = _safe_get_session_id(event)
        if not session_id:
            return
        self._save_message(
            session_id=session_id,
            scene_type="private" if _safe_is_private_chat(event) else "group",
            role="user",
            sender_id=_safe_get_sender_id(event),
            sender_name=_safe_get_sender_name(event),
            content=content,
            config=config,
        )
        self._extract_user_insights(session_id, content)

    def observe_outgoing_reply(self, event: AstrMessageEvent, reply_text: str, config: Any = None) -> None:
        if not _cfg_bool(config, "conversation_enabled", True):
            return
        content = _trim_clause(reply_text, max_len=320)
        if not content:
            return
        session_id = _safe_get_session_id(event)
        if not session_id:
            return
        self._save_message(
            session_id=session_id,
            scene_type="private" if _safe_is_private_chat(event) else "group",
            role="assistant",
            sender_id="assistant",
            sender_name="assistant",
            content=content,
            config=config,
        )
        self._extract_assistant_insights(session_id, content)

    def build_prompt_for_event(self, event: AstrMessageEvent, message_text: str, config: Any = None) -> str:
        if not _cfg_bool(config, "conversation_enabled", True):
            return ""
        session_id = _safe_get_session_id(event)
        if not session_id:
            return ""
        budget = _cfg_int(config, "conversation_prompt_max_chars", 900, 200, 3000)
        message_window = _cfg_int(config, "conversation_message_window", 8, 4, 20)
        max_insights = _cfg_int(config, "conversation_max_injected_insights", 4, 1, 8)
        recent_messages = self._list_recent_messages(session_id, limit=message_window)
        if not recent_messages:
            return ""

        topics = self._infer_topics(recent_messages)
        participants = self._infer_participants(recent_messages)
        relevant_insights = self._select_relevant_insights(
            session_id,
            message_text,
            limit=max_insights,
            config=config,
        )
        strategy_hints = self._build_strategy_hints(
            recent_messages,
            message_text,
            is_group=not _safe_is_private_chat(event),
            relevant_insights=relevant_insights,
        )

        lines: list[str] = ["【会话态势】"]
        if topics:
            lines.append(f"- 最近核心话题：{'、'.join(topics)}")
        if participants:
            lines.append(f"- 最近主要参与者：{'，'.join(participants)}")

        lines.append("【最近对话纪要】")
        for row in recent_messages:
            speaker = "你" if str(row["role"]) == "assistant" else str(row["sender_name"] or "用户")
            relative = _format_relative_time(float(row["created_ts"] or 0))
            prefix = f"- {relative} {speaker}：".strip()
            text = _trim_clause(row["content"], max_len=72)
            if text:
                lines.append(f"{prefix}{text}")

        if relevant_insights:
            lines.append("【会话短期记忆】")
            for item in relevant_insights:
                label = _INSIGHT_TYPE_LABELS.get(item["insight_type"], "会话记忆")
                lines.append(f"- {label}：{item['content']}")

        if strategy_hints:
            lines.append("【回复建议】")
            for hint in strategy_hints:
                lines.append(f"- {hint}")

        lines.append("【使用原则】默认把这轮消息视为延续当前会话，不要把已经聊过的话题当成第一次出现。")
        prompt = "\n".join(lines).strip()
        if len(prompt) <= budget:
            return prompt
        return prompt[:budget].rstrip() + "\n...(会话上下文已按预算截断)"

    def format_session_snapshot(self, event: AstrMessageEvent, config: Any = None) -> str:
        session_id = _safe_get_session_id(event)
        if not session_id:
            return "当前无法识别会话 ID。"
        recent_messages = self._list_recent_messages(
            session_id,
            limit=_cfg_int(config, "conversation_message_window", 8, 4, 20),
        )
        if not recent_messages:
            return "当前会话还没有可用的聊天增强上下文。"
        topics = self._infer_topics(recent_messages)
        participants = self._infer_participants(recent_messages)
        insights = self._select_relevant_insights(
            session_id,
            _safe_get_message_text(event),
            limit=_cfg_int(config, "conversation_max_injected_insights", 4, 1, 8),
            config=config,
        )
        lines = ["🧩 会话上下文"]
        lines.append(f"会话ID：{session_id}")
        lines.append(f"场景：{'私聊' if _safe_is_private_chat(event) else '群聊'}")
        if topics:
            lines.append(f"最近话题：{'、'.join(topics)}")
        if participants:
            lines.append(f"参与者：{'，'.join(participants)}")
        if insights:
            lines.append("短期记忆：")
            for item in insights:
                label = _INSIGHT_TYPE_LABELS.get(item["insight_type"], "会话记忆")
                lines.append(f"- {label}：{item['content']}")
        lines.append("最近消息：")
        for row in recent_messages:
            speaker = "你" if str(row["role"]) == "assistant" else str(row["sender_name"] or "用户")
            relative = _format_relative_time(float(row["created_ts"] or 0))
            lines.append(f"- {relative} {speaker}：{_trim_clause(row['content'], max_len=72)}")
        return "\n".join(lines)

    def _save_message(
        self,
        *,
        session_id: str,
        scene_type: str,
        role: str,
        sender_id: str,
        sender_name: str,
        content: str,
        config: Any = None,
    ) -> None:
        cleaned_content = _trim_clause(content, max_len=320)
        if not cleaned_content:
            return
        now_ts = _now_ts()
        normalized = _normalize_text(cleaned_content)
        with shared_conn() as conn:
            latest = conn.execute(
                f"""
                SELECT role, normalized_content, created_ts
                FROM {_MESSAGE_TABLE}
                WHERE session_id = ?
                ORDER BY created_ts DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if latest is not None:
                if (
                    str(latest["role"] or "") == role
                    and str(latest["normalized_content"] or "") == normalized
                    and abs(float(latest["created_ts"] or 0) - now_ts) <= 3
                ):
                    return

            conn.execute(
                f"""
                INSERT INTO {_MESSAGE_TABLE}(
                    session_id, scene_type, role, sender_id, sender_name,
                    content, normalized_content, created_at, created_ts, is_command
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    scene_type,
                    role,
                    str(sender_id or "").strip(),
                    _trim_clause(sender_name, max_len=32) or "用户",
                    cleaned_content,
                    normalized,
                    _now_str(),
                    now_ts,
                    0,
                ),
            )
            self._trim_messages(conn, session_id, config=config)
            conn.commit()

    def _trim_messages(self, conn: Any, session_id: str, config: Any = None) -> None:
        keep = _cfg_int(config, "conversation_message_retention_per_session", 80, 20, 300)
        conn.execute(
            f"""
            DELETE FROM {_MESSAGE_TABLE}
            WHERE id IN (
                SELECT id FROM {_MESSAGE_TABLE}
                WHERE session_id = ?
                ORDER BY created_ts DESC, id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (session_id, keep),
        )

    def _save_insight(
        self,
        session_id: str,
        insight_type: str,
        content: str,
        *,
        subject_key: str = "",
        keywords: list[str] | None = None,
    ) -> None:
        cleaned_content = _trim_clause(content, max_len=72)
        normalized = _normalize_text(cleaned_content)
        if not cleaned_content or not normalized:
            return
        payload_keywords = "|".join(_tokenize(" ".join(keywords or []), min_len=2)[:8])
        now_text = _now_str()
        now_ts = _now_ts()
        base_weight = _INSIGHT_BASE_WEIGHTS.get(insight_type, 0.72)
        with shared_conn() as conn:
            row = conn.execute(
                f"""
                SELECT id, evidence_count, weight, keywords
                FROM {_INSIGHT_TABLE}
                WHERE session_id = ? AND insight_type = ? AND normalized_content = ?
                """,
                (session_id, insight_type, normalized),
            ).fetchone()
            if row is None:
                conn.execute(
                    f"""
                    INSERT INTO {_INSIGHT_TABLE}(
                        session_id, insight_type, subject_key, content, normalized_content,
                        keywords, weight, evidence_count, created_at, updated_at, updated_ts
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        insight_type,
                        str(subject_key or "").strip(),
                        cleaned_content,
                        normalized,
                        payload_keywords,
                        base_weight,
                        1,
                        now_text,
                        now_text,
                        now_ts,
                    ),
                )
            else:
                merged_keywords = {
                    item.strip()
                    for item in str(row["keywords"] or "").split("|")
                    if item.strip()
                }
                merged_keywords.update(item.strip() for item in payload_keywords.split("|") if item.strip())
                conn.execute(
                    f"""
                    UPDATE {_INSIGHT_TABLE}
                    SET content = ?, subject_key = ?, keywords = ?, weight = ?, evidence_count = ?, updated_at = ?, updated_ts = ?
                    WHERE id = ?
                    """,
                    (
                        cleaned_content,
                        str(subject_key or "").strip(),
                        "|".join(sorted(merged_keywords)[:10]),
                        min(0.98, float(row["weight"] or base_weight) + 0.06),
                        int(row["evidence_count"] or 1) + 1,
                        now_text,
                        now_ts,
                        int(row["id"]),
                    ),
                )
            conn.commit()

    def _extract_user_insights(self, session_id: str, text: str) -> None:
        cleaned = _trim_clause(text, max_len=120)
        if not cleaned:
            return
        for pattern in _GOAL_PATTERNS:
            match = pattern.search(cleaned)
            if match:
                goal = _trim_clause(match.groupdict().get("goal", ""), max_len=56)
                if goal and len(goal) >= 4:
                    self._save_insight(session_id, "user_goal", goal, keywords=_tokenize(goal))

        for pattern in _TASK_PATTERNS:
            match = pattern.search(cleaned)
            if match:
                task = _trim_clause(match.groupdict().get("task", ""), max_len=56)
                if task and len(task) >= 4:
                    self._save_insight(session_id, "pending_task", task, keywords=_tokenize(task))

        for emotion, keywords in _EMOTION_KEYWORDS.items():
            if any(keyword in cleaned for keyword in keywords):
                self._save_insight(
                    session_id,
                    "emotion_state",
                    f"{emotion}：{cleaned}",
                    subject_key=emotion,
                    keywords=[emotion, *keywords],
                )
                break

    def _extract_assistant_insights(self, session_id: str, text: str) -> None:
        cleaned = _trim_clause(text, max_len=140)
        if not cleaned:
            return
        for pattern in _ASSISTANT_COMMIT_PATTERNS:
            match = pattern.search(cleaned)
            if not match:
                continue
            commitment = _trim_clause(match.groupdict().get("commit", ""), max_len=56)
            if commitment and len(commitment) >= 4:
                self._save_insight(
                    session_id,
                    "assistant_commitment",
                    commitment,
                    keywords=_tokenize(commitment),
                )

    def _list_recent_messages(self, session_id: str, *, limit: int) -> list[dict[str, Any]]:
        with shared_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT role, sender_name, content, created_ts
                FROM {_MESSAGE_TABLE}
                WHERE session_id = ?
                ORDER BY created_ts DESC, id DESC
                LIMIT ?
                """,
                (session_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _infer_topics(self, messages: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
        counter: Counter[str] = Counter()
        total = len(messages)
        for index, row in enumerate(messages):
            weight = 1.0 + (index / max(1, total))
            for token in _tokenize(str(row.get("content") or ""), min_len=2):
                counter[token] += weight
        return [token for token, _score in counter.most_common(limit)]

    def _infer_participants(self, messages: list[dict[str, Any]], *, limit: int = 4) -> list[str]:
        counter: Counter[str] = Counter()
        for row in messages:
            if str(row.get("role") or "") == "assistant":
                counter["你"] += 1
                continue
            name = _trim_clause(row.get("sender_name") or "用户", max_len=18) or "用户"
            counter[name] += 1
        return [f"{name}({count})" for name, count in counter.most_common(limit)]

    def _select_relevant_insights(
        self,
        session_id: str,
        message_text: str,
        *,
        limit: int,
        config: Any = None,
    ) -> list[dict[str, Any]]:
        retention_days = _cfg_int(config, "conversation_insight_retention_days", 14, 1, 60)
        cutoff_ts = (datetime.now() - timedelta(days=retention_days)).timestamp()
        with shared_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT insight_type, content, keywords, weight, evidence_count, updated_ts
                FROM {_INSIGHT_TABLE}
                WHERE session_id = ? AND updated_ts >= ?
                ORDER BY updated_ts DESC, id DESC
                LIMIT 40
                """,
                (session_id, cutoff_ts),
            ).fetchall()
        query_tokens = set(_tokenize(message_text or "", min_len=2))
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            keywords = {value.strip() for value in str(item.get("keywords") or "").split("|") if value.strip()}
            overlap = len(query_tokens & keywords)
            recency_hours = max(0.0, (_now_ts() - float(item.get("updated_ts") or 0)) / 3600.0)
            recency_bonus = max(0.0, 1.4 - min(recency_hours / 24.0, 1.4))
            type_bonus = 0.4 if item["insight_type"] in {"pending_task", "assistant_commitment"} else 0.2
            score = float(item.get("weight") or 0.7) * 10 + overlap * 2.5 + recency_bonus + type_bonus
            score += min(int(item.get("evidence_count") or 1), 4) * 0.35
            if query_tokens and overlap <= 0 and item["insight_type"] == "emotion_state":
                score -= 0.6
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        picked: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _score, item in scored:
            dedupe_key = f"{item['insight_type']}::{item['content']}"
            if dedupe_key in seen:
                continue
            picked.append(item)
            seen.add(dedupe_key)
            if len(picked) >= limit:
                break
        return picked

    def _build_strategy_hints(
        self,
        messages: list[dict[str, Any]],
        message_text: str,
        *,
        is_group: bool,
        relevant_insights: list[dict[str, Any]],
    ) -> list[str]:
        hints: list[str] = []
        current = str(message_text or "").strip()
        participant_names = {
            str(row.get("sender_name") or "").strip()
            for row in messages
            if str(row.get("role") or "") == "user" and str(row.get("sender_name") or "").strip()
        }
        if is_group and len(participant_names) >= 2:
            hints.append("这是多人群聊，回复时尽量明确你在接谁的话，避免把多人内容混成一个人。")
        if any(hint in current for hint in _QUESTION_HINTS):
            hints.append("对方大概率在提问或求助，先正面回答核心问题，再补充建议。")
        if any(hint in current for hint in _SUPPORT_HINTS):
            hints.append("先接住情绪，再给方案，不要一上来就讲大道理。")
        if any(hint.lower() in current.lower() for hint in _PLANNING_HINTS):
            hints.append("当前话题带有计划或时间线，优先承接进度、安排和下一步动作。")
        if any(item["insight_type"] == "assistant_commitment" for item in relevant_insights):
            hints.append("如果你上轮已经答应帮忙，先沿着之前承诺继续，不要重置话题。")
        if not hints:
            hints.append("这是延续型会话，优先承接最近上下文，而不是把它当成全新的陌生对话。")
        return hints[:4]


def init_conversation_intelligence_store() -> ConversationIntelligenceStore:
    global _STORE
    if _STORE is None:
        _STORE = ConversationIntelligenceStore()
        logger.info("[astrbot_all_char] 会话增强存储已初始化")
    return _STORE


def observe_conversation_incoming_event(event: AstrMessageEvent, config: Any = None) -> None:
    init_conversation_intelligence_store().observe_incoming_event(event, config)


def observe_conversation_outgoing_reply(event: AstrMessageEvent, reply_text: str, config: Any = None) -> None:
    init_conversation_intelligence_store().observe_outgoing_reply(event, reply_text, config)


def build_conversation_context_prompt(
    event: AstrMessageEvent,
    message_text: str,
    config: Any = None,
) -> str:
    return init_conversation_intelligence_store().build_prompt_for_event(event, message_text, config)


async def handle_conversation_context_command(event: AstrMessageEvent, config: Any = None):
    text = init_conversation_intelligence_store().format_session_snapshot(event, config)
    yield event.plain_result(text)
