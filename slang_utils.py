from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools


SCENE_GROUP = "group"
SCENE_PRIVATE = "private"
SCENE_SESSION = "session"
_DB_NAME = "user_memory.sqlite3"
_GENERIC_TERMS = {
    "",
    "这个",
    "那个",
    "这里",
    "那里",
    "今天",
    "明天",
    "后天",
    "昨天",
    "刚刚",
    "刚才",
    "现在",
    "你",
    "我",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
}
_GENERIC_MEANINGS = {
    "",
    "这个",
    "那个",
    "这样",
    "那样",
    "一种说法",
    "一种意思",
}
_SLANG_HINTS = (
    "黑话",
    "梗",
    "暗号",
    "缩写",
    "代称",
    "叫法",
    "说法",
    "翻译",
    "意思",
    "我们群里",
    "在我们群里",
    "这边",
    "这里",
    "圈里",
    "以后",
    "下次",
)
_SLANG_QUERY_HINTS = (
    "黑话",
    "啥意思",
    "什么意思",
    "怎么理解",
    "翻译",
    "梗",
    "术语",
    "缩写",
)
_TERM_PATTERN = r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9._\-]{0,15}"
_DEFINITION_PATTERNS = (
    re.compile(
        rf"(?:在我们群里|我们群里|在这边|这里|以后|下次|平时)?(?:说|提到|看到)?\s*[\"“'「]?(?P<term>{_TERM_PATTERN})[\"”'」]?\s*(?:就是|意思是|等于|=|指(?:的)?是|是)\s*(?P<meaning>[^。！？\n]{{2,48}})"
    ),
    re.compile(
        rf"[\"“'「](?P<term>{_TERM_PATTERN})[\"”'」]\s*(?:就是|意思是|等于|=|指(?:的)?是|是)\s*[\"“'「]?(?P<meaning>[^。！？\n]{{2,48}})"
    ),
    re.compile(
        rf"(?P<term>{_TERM_PATTERN})\s*[:：=]\s*(?P<meaning>[^。！？\n]{{2,48}})"
    ),
)
_LLM_DEFINITION_HINTS = (
    "就是",
    "意思是",
    "指的是",
    "我们群里",
    "在我们群里",
    "这里",
    "以后我说",
    "以后说",
    "以后提到",
    "平时说",
    "管这个叫",
    "把这个叫做",
    "代指",
)
_LLM_EXTRACT_PROMPT = (
    "你是“群聊黑话学习器”。\n"
    "你的任务：判断一条消息是否在明确定义当前群聊/当前私聊里的黑话、梗、缩写、代称或特殊说法。\n\n"
    "只在“定义关系非常明确”时提取，宁可漏掉，也不要猜。\n"
    "可提取示例：\n"
    "1. yyds 就是 永远的神\n"
    "2. 以后我说开大，就是发红包\n"
    "3. 在我们群里，“下班”指的是上线打游戏\n"
    "4. 这里说‘补课’其实是加班\n\n"
    "不可提取示例：\n"
    "1. yyds 是什么意思\n"
    "2. 他今天又开大了\n"
    "3. 普通情绪表达、吐槽、玩笑，但没有明确解释\n"
    "4. 只是问句、感叹句、转述句，没有定义词义\n\n"
    "输出要求：\n"
    "1. 只输出 JSON 数组，不要解释。\n"
    "2. 数组元素格式：{\"term\":\"词条\",\"meaning\":\"解释\",\"confidence\":0.0-1.0}\n"
    "3. 没有可学习内容时输出 []\n"
    "4. 最多返回 2 条\n\n"
    "待分析消息：\n"
    "{message_text}"
)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_get_sender_id(event: AstrMessageEvent) -> str:
    try:
        value = event.get_sender_id()
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_get_group_id(event: AstrMessageEvent) -> str:
    try:
        value = event.get_group_id()
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_is_private_chat(event: AstrMessageEvent) -> bool:
    try:
        return bool(event.is_private_chat())
    except Exception:
        return False


def _safe_get_session_id(event: AstrMessageEvent) -> str:
    try:
        value = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", None)
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_get_message_text(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_message_str() or "").strip()
    except Exception:
        return ""


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _trim_term(value: Any) -> str:
    text = str(value or "").strip()
    text = text.strip(" \t\n\r\"'“”‘’「」『』[]【】()（）<>《》")
    text = re.sub(r"\s+", "", text)
    return text[:16]


def _trim_meaning(value: Any) -> str:
    text = str(value or "").strip()
    text = re.split(r"[。！？\n]", text, maxsplit=1)[0].strip()
    text = text.strip(" \t\n\r\"'“”‘’「」『』[]【】()（）<>《》")
    text = re.sub(r"(就是|的意思|意思)$", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:48]


def _looks_like_term(term: str) -> bool:
    normalized = _normalize_text(term)
    if not term or normalized in _GENERIC_TERMS:
        return False
    if len(term) < 2 or len(term) > 16:
        return False
    if term.isdigit():
        return False
    if term.startswith(("http://", "https://", "/")):
        return False
    if any(ch in term for ch in "，。！？；： \t\n\r"):
        return False
    return True


def _looks_like_meaning(term: str, meaning: str) -> bool:
    normalized_meaning = _normalize_text(meaning)
    if not meaning or normalized_meaning in _GENERIC_MEANINGS:
        return False
    if len(meaning) < 2 or len(meaning) > 48:
        return False
    if normalized_meaning == _normalize_text(term):
        return False
    if meaning.endswith(("吗", "么", "?")):
        return False
    if meaning.startswith(("http://", "https://", "/")):
        return False
    return True


def _score_candidate(full_text: str, term: str, meaning: str, raw_match: str) -> int:
    score = 0
    if any(hint in full_text for hint in _SLANG_HINTS):
        score += 2
    if any(mark in raw_match for mark in ('"', "“", "「", "”", "」")):
        score += 1
    if any(token in full_text for token in ("就是", "意思是", "指的是", "翻译")):
        score += 1
    if any(token in raw_match for token in ("=", ":", "：")):
        score += 1
    if len(term) <= 6:
        score += 1
    if len(meaning) <= 24:
        score += 1
    if " " in meaning:
        score -= 1
    return score


def _parse_definition_candidates(text: str) -> list[dict[str, Any]]:
    message = str(text or "").strip()
    if not message or len(message) < 5:
        return []

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pattern in _DEFINITION_PATTERNS:
        for match in pattern.finditer(message):
            term = _trim_term(match.group("term"))
            meaning = _trim_meaning(match.group("meaning"))
            if not _looks_like_term(term) or not _looks_like_meaning(term, meaning):
                continue
            score = _score_candidate(message, term, meaning, match.group(0))
            if score < 3:
                continue
            key = (_normalize_text(term), _normalize_text(meaning))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "term": term,
                    "meaning": meaning,
                    "confidence": min(0.68 + score * 0.05, 0.9),
                }
            )
            if len(results) >= 4:
                return results
    return results


def _detect_scene(event: AstrMessageEvent) -> tuple[str, str, str]:
    group_id = _safe_get_group_id(event)
    sender_id = _safe_get_sender_id(event)
    session_id = _safe_get_session_id(event)
    if _safe_is_private_chat(event):
        scene_value = sender_id or session_id
        return (SCENE_PRIVATE, scene_value, "当前私聊") if scene_value else ("", "", "")
    if group_id:
        return SCENE_GROUP, group_id, f"当前群 {group_id}"
    if session_id:
        return SCENE_SESSION, session_id, "当前会话"
    return "", "", ""


def _is_enabled(config: Any) -> bool:
    return bool(getattr(config, "slang_enabled", True)) if config is not None else True


def _is_auto_learn_enabled(config: Any) -> bool:
    return bool(getattr(config, "slang_auto_learn_enabled", True)) if config is not None else True


def _list_limit(config: Any, attr_name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(getattr(config, attr_name, default)) if config is not None else default
    except Exception:
        value = default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _is_llm_auto_learn_enabled(config: Any) -> bool:
    return bool(getattr(config, "slang_llm_auto_learn_enabled", True)) if config is not None else True


def _should_try_llm_auto_learn(message_text: str, rule_candidates: list[dict[str, Any]]) -> bool:
    message = str(message_text or "").strip()
    if rule_candidates:
        return False
    if not message or len(message) < 6 or len(message) > 120:
        return False
    if message.startswith("/"):
        return False
    if any(hint in message for hint in _SLANG_QUERY_HINTS) and not any(hint in message for hint in _LLM_DEFINITION_HINTS):
        return False
    if any(token in message for token in ("=", ":", "：")):
        return True
    if any(hint in message for hint in _LLM_DEFINITION_HINTS):
        return True
    if re.search(r"[\"“「][^\"”」]{2,12}[\"”」]", message):
        return True
    return False


def _extract_json_array(text: str) -> list[Any]:
    raw = str(text or "").strip()
    if not raw:
        return []
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, flags=re.S | re.I)
    if fenced:
        raw = fenced.group(1).strip()
    else:
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end >= start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _normalize_llm_items(items: list[Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        term = _trim_term(item.get("term"))
        meaning = _trim_meaning(item.get("meaning"))
        if not _looks_like_term(term) or not _looks_like_meaning(term, meaning):
            continue
        try:
            confidence = float(item.get("confidence") or 0.72)
        except Exception:
            confidence = 0.72
        confidence = max(0.55, min(confidence, 0.9))
        key = (_normalize_text(term), _normalize_text(meaning))
        if key in seen:
            continue
        seen.add(key)
        results.append({"term": term, "meaning": meaning, "confidence": confidence})
        if len(results) >= 2:
            break
    return results


async def _llm_extract_definition_candidates(
    context: Context,
    event: AstrMessageEvent,
    message_text: str,
) -> list[dict[str, Any]]:
    umo = getattr(event, "unified_msg_origin", None) or ""
    try:
        provider_id = await context.get_current_chat_provider_id(umo=umo)
    except Exception as exc:
        logger.debug("[slang] 获取当前会话 provider 失败: %s", exc)
        return []
    if not provider_id:
        return []
    prompt = _LLM_EXTRACT_PROMPT.replace("{message_text}", message_text[:400])
    try:
        llm_resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
    except Exception as exc:
        logger.debug("[slang] LLM 自动学习抽取失败: %s", exc)
        return []
    raw = (getattr(llm_resp, "completion_text", None) or "").strip()
    return _normalize_llm_items(_extract_json_array(raw))


class SlangMemoryStore:
    def __init__(self) -> None:
        self._data_dir = Path(StarTools.get_data_dir("astrbot_all_char"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / _DB_NAME
        self._init_db()

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS slang_terms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scene_type TEXT NOT NULL,
                    scene_value TEXT NOT NULL,
                    term TEXT NOT NULL,
                    normalized_term TEXT NOT NULL,
                    meaning TEXT NOT NULL,
                    normalized_meaning TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.72,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    source_type TEXT NOT NULL DEFAULT 'auto',
                    source_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE (scene_type, scene_value, normalized_term)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_slang_terms_scene "
                "ON slang_terms(scene_type, scene_value, confidence DESC, evidence_count DESC, updated_at DESC)"
            )
            conn.commit()

    def save_term(
        self,
        scene_type: str,
        scene_value: str,
        term: str,
        meaning: str,
        *,
        source_type: str = "auto",
        source_text: str = "",
        force: bool = False,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        cleaned_scene_type = str(scene_type or "").strip()
        cleaned_scene_value = str(scene_value or "").strip()
        cleaned_term = _trim_term(term)
        cleaned_meaning = _trim_meaning(meaning)
        if not cleaned_scene_type or not cleaned_scene_value:
            return {"ok": False, "message": "当前场景不支持记录黑话。"}
        if not _looks_like_term(cleaned_term):
            return {"ok": False, "message": "黑话词条格式不太对，请控制在 2-16 个字或字母数字内。"}
        if not _looks_like_meaning(cleaned_term, cleaned_meaning):
            return {"ok": False, "message": "黑话解释格式不太对，请尽量写成简洁明确的一句话。"}

        normalized_term = _normalize_text(cleaned_term)
        normalized_meaning = _normalize_text(cleaned_meaning)
        source_kind = "manual" if str(source_type or "").strip().lower() == "manual" else "auto"
        now = _now_str()
        target_confidence = confidence
        if target_confidence is None:
            target_confidence = 0.96 if source_kind == "manual" else 0.74

        with self._get_conn() as conn:
            existing = conn.execute(
                """
                SELECT * FROM slang_terms
                WHERE scene_type = ? AND scene_value = ? AND normalized_term = ?
                """,
                (cleaned_scene_type, cleaned_scene_value, normalized_term),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO slang_terms (
                        scene_type, scene_value, term, normalized_term, meaning, normalized_meaning,
                        confidence, evidence_count, source_type, source_text, created_at, updated_at,
                        last_confirmed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        cleaned_scene_type,
                        cleaned_scene_value,
                        cleaned_term,
                        normalized_term,
                        cleaned_meaning,
                        normalized_meaning,
                        float(target_confidence),
                        source_kind,
                        source_text,
                        now,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return {
                    "ok": True,
                    "action": "created",
                    "term": cleaned_term,
                    "meaning": cleaned_meaning,
                }

            current = dict(existing)
            if str(current.get("normalized_meaning") or "") == normalized_meaning:
                next_confidence = max(float(current.get("confidence") or 0), float(target_confidence))
                if source_kind == "auto":
                    next_confidence = min(next_confidence + 0.03, 0.92)
                conn.execute(
                    """
                    UPDATE slang_terms
                    SET term = ?, meaning = ?, confidence = ?, evidence_count = evidence_count + 1,
                        source_type = ?, source_text = CASE WHEN ? <> '' THEN ? ELSE source_text END,
                        updated_at = ?, last_confirmed_at = ?
                    WHERE id = ?
                    """,
                    (
                        cleaned_term,
                        cleaned_meaning,
                        next_confidence,
                        source_kind if source_kind == "manual" else current.get("source_type") or "auto",
                        source_text,
                        source_text,
                        now,
                        now,
                        int(current["id"]),
                    ),
                )
                conn.commit()
                return {
                    "ok": True,
                    "action": "confirmed",
                    "term": cleaned_term,
                    "meaning": cleaned_meaning,
                    "evidence_count": int(current.get("evidence_count") or 1) + 1,
                }

            current_source_type = str(current.get("source_type") or "auto")
            current_confidence = float(current.get("confidence") or 0)
            current_evidence = int(current.get("evidence_count") or 1)

            should_overwrite = force or source_kind == "manual"
            if not should_overwrite and current_source_type == "manual":
                return {
                    "ok": False,
                    "action": "kept_manual",
                    "term": cleaned_term,
                    "meaning": str(current.get("meaning") or ""),
                }
            if not should_overwrite and current_confidence >= 0.8 and current_evidence >= 2:
                return {
                    "ok": False,
                    "action": "conflict",
                    "term": cleaned_term,
                    "meaning": str(current.get("meaning") or ""),
                }

            overwrite_confidence = 0.97 if source_kind == "manual" else max(0.68, float(target_confidence) - 0.04)
            overwrite_evidence = current_evidence + 1 if source_kind == "manual" else 1
            previous_meaning = str(current.get("meaning") or "")
            conn.execute(
                """
                UPDATE slang_terms
                SET term = ?, meaning = ?, normalized_meaning = ?, confidence = ?, evidence_count = ?,
                    source_type = ?, source_text = CASE WHEN ? <> '' THEN ? ELSE source_text END,
                    updated_at = ?, last_confirmed_at = ?
                WHERE id = ?
                """,
                (
                    cleaned_term,
                    cleaned_meaning,
                    normalized_meaning,
                    overwrite_confidence,
                    overwrite_evidence,
                    source_kind,
                    source_text,
                    source_text,
                    now,
                    now,
                    int(current["id"]),
                ),
            )
            conn.commit()
            return {
                "ok": True,
                "action": "overwritten",
                "term": cleaned_term,
                "meaning": cleaned_meaning,
                "previous_meaning": previous_meaning,
            }

    def list_terms(
        self,
        scene_type: str,
        scene_value: str,
        *,
        limit: int = 100,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        cleaned_scene_type = str(scene_type or "").strip()
        cleaned_scene_value = str(scene_value or "").strip()
        if not cleaned_scene_type or not cleaned_scene_value:
            return []
        query = (
            "SELECT * FROM slang_terms WHERE scene_type = ? AND scene_value = ? "
            "AND confidence >= ? ORDER BY confidence DESC, evidence_count DESC, updated_at DESC LIMIT ?"
        )
        with self._get_conn() as conn:
            rows = conn.execute(query, (cleaned_scene_type, cleaned_scene_value, float(min_confidence), int(limit))).fetchall()
        return [dict(row) for row in rows]

    def get_term(self, scene_type: str, scene_value: str, term: str) -> dict[str, Any] | None:
        cleaned_scene_type = str(scene_type or "").strip()
        cleaned_scene_value = str(scene_value or "").strip()
        normalized_term = _normalize_text(term)
        if not cleaned_scene_type or not cleaned_scene_value or not normalized_term:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM slang_terms
                WHERE scene_type = ? AND scene_value = ? AND normalized_term = ?
                """,
                (cleaned_scene_type, cleaned_scene_value, normalized_term),
            ).fetchone()
        return dict(row) if row else None

    def delete_term(self, scene_type: str, scene_value: str, term: str) -> bool:
        cleaned_scene_type = str(scene_type or "").strip()
        cleaned_scene_value = str(scene_value or "").strip()
        normalized_term = _normalize_text(term)
        if not cleaned_scene_type or not cleaned_scene_value or not normalized_term:
            return False
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                DELETE FROM slang_terms
                WHERE scene_type = ? AND scene_value = ? AND normalized_term = ?
                """,
                (cleaned_scene_type, cleaned_scene_value, normalized_term),
            )
            conn.commit()
        return bool(cursor.rowcount)

    def observe_message(self, event: AstrMessageEvent, config: Any = None) -> None:
        if not _is_enabled(config) or not _is_auto_learn_enabled(config):
            return
        message_text = _safe_get_message_text(event)
        if not message_text or message_text.startswith("/"):
            return
        scene_type, scene_value, _scene_label = _detect_scene(event)
        if not scene_type or not scene_value:
            return

        candidates = _parse_definition_candidates(message_text)
        for item in candidates:
            result = self.save_term(
                scene_type,
                scene_value,
                item["term"],
                item["meaning"],
                source_type="auto",
                source_text=message_text,
                confidence=float(item.get("confidence") or 0.74),
            )
            if result.get("ok"):
                logger.info(
                    "[slang] learned scene=%s:%s term=%s action=%s",
                    scene_type,
                    scene_value,
                    item["term"],
                    result.get("action") or "updated",
                )

    def search_relevant_terms(
        self,
        event: AstrMessageEvent,
        message_text: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        scene_type, scene_value, _scene_label = _detect_scene(event)
        if not scene_type or not scene_value:
            return []
        message = str(message_text or "").strip()
        hint_mode = any(hint in message for hint in _SLANG_QUERY_HINTS)
        normalized_message = _normalize_text(message)
        candidates = self.list_terms(scene_type, scene_value, limit=80, min_confidence=0.58)
        if not candidates:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        direct_matches = 0
        for item in candidates:
            normalized_term = str(item.get("normalized_term") or "")
            if not normalized_term:
                continue
            score = 0.0
            if normalized_term in normalized_message:
                score += 30
                direct_matches += 1
            if hint_mode:
                score += 8
            score += float(item.get("confidence") or 0) * 8
            score += min(int(item.get("evidence_count") or 1), 5)
            if not hint_mode and score < 30:
                continue
            if hint_mode and score < 10:
                continue
            enriched = dict(item)
            enriched["match_score"] = round(score, 2)
            scored.append((score, enriched))

        if not scored and hint_mode:
            return candidates[:limit]

        if hint_mode and direct_matches == 0:
            scored.extend(
                (
                    float(item.get("confidence") or 0) * 6 + min(int(item.get("evidence_count") or 1), 5),
                    dict(item),
                )
                for item in candidates[:limit]
            )

        scored.sort(key=lambda pair: (-pair[0], pair[1].get("updated_at", "")))
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _score, item in scored:
            normalized_term = str(item.get("normalized_term") or "")
            if not normalized_term or normalized_term in seen:
                continue
            deduped.append(item)
            seen.add(normalized_term)
            if len(deduped) >= limit:
                break
        return deduped


_store: SlangMemoryStore | None = None


def init_slang_store() -> SlangMemoryStore:
    global _store
    if _store is None:
        _store = SlangMemoryStore()
    return _store


async def observe_slang_message(context: Context, event: AstrMessageEvent, config: Any = None) -> None:
    store = init_slang_store()
    store.observe_message(event, config)
    if not _is_enabled(config) or not _is_auto_learn_enabled(config) or not _is_llm_auto_learn_enabled(config):
        return
    message_text = _safe_get_message_text(event)
    if not message_text or message_text.startswith("/"):
        return
    scene_type, scene_value, _scene_label = _detect_scene(event)
    if not scene_type or not scene_value:
        return
    rule_candidates = _parse_definition_candidates(message_text)
    if not _should_try_llm_auto_learn(message_text, rule_candidates):
        return
    llm_candidates = await _llm_extract_definition_candidates(context, event, message_text)
    for item in llm_candidates:
        result = store.save_term(
            scene_type,
            scene_value,
            item["term"],
            item["meaning"],
            source_type="auto",
            source_text=message_text,
            confidence=float(item.get("confidence") or 0.72),
        )
        if result.get("ok"):
            logger.info(
                "[slang] llm learned scene=%s:%s term=%s action=%s",
                scene_type,
                scene_value,
                item["term"],
                result.get("action") or "updated",
            )


def build_slang_prompt_for_event(event: AstrMessageEvent, message_text: str, config: Any = None) -> str:
    if not _is_enabled(config):
        return ""
    max_items = _list_limit(config, "slang_max_injected_terms", default=5, minimum=1, maximum=12)
    entries = init_slang_store().search_relevant_terms(event, message_text, limit=max_items)
    if not entries:
        return ""
    lines = ["【当前会话黑话词典】"]
    for item in entries:
        term = str(item.get("term") or "").strip()
        meaning = str(item.get("meaning") or "").strip()
        if term and meaning:
            lines.append(f"- {term} = {meaning}")
    lines.extend(
        [
            "使用规则:",
            "1. 这些解释只对当前群聊/当前私聊有效，不要默认当成通用词义。",
            "2. 只有当前消息真的涉及这些词时才参考，不要硬塞进回复。",
            "3. 如果用户当下重新解释或纠正，以当前说法为准。",
        ]
    )
    return "\n".join(lines)


def explain_slang_for_event(event: AstrMessageEvent, term: str) -> str:
    scene_type, scene_value, scene_label = _detect_scene(event)
    if not scene_type or not scene_value:
        return "当前场景不支持查询黑话。"
    entry = init_slang_store().get_term(scene_type, scene_value, term)
    if not entry:
        return f"{scene_label}里还没有记住「{term}」这条黑话。"
    confidence = round(float(entry.get("confidence") or 0), 2)
    evidence = int(entry.get("evidence_count") or 1)
    return (
        f"{scene_label}里，「{entry.get('term')}」通常表示：{entry.get('meaning')}\n"
        f"可信度：{confidence} | 证据次数：{evidence} | 来源：{entry.get('source_type') or 'auto'}"
    )


def _help_text() -> str:
    return (
        "黑话词典用法：\n"
        "1. 自动学习：\n"
        "   - bot 会从明确解释类消息里学习，例如：`yyds 就是 永远的神`\n"
        "   - 会先走规则学习；像“以后我说 XX 就是 YY”这类更自然的句子，也会尝试让当前会话模型保守抽取。\n"
        "2. 手动录入：\n"
        "   - `/黑话 学习 yyds = 永远的神`\n"
        "   - `/黑话 添加 绝绝子 = 非常厉害`\n"
        "3. 查询与查看：\n"
        "   - `/黑话 解释 yyds`\n"
        "   - `/黑话 列表`\n"
        "4. 删除：\n"
        "   - `/黑话 删除 yyds`\n"
        "说明：\n"
        "- 黑话默认按当前群聊 / 当前私聊隔离存储，不会全局串味。\n"
        "- 当前版本更偏保守，宁可少学一点，也尽量避免把普通句子误学成黑话。"
    )


def _parse_manual_definition(text: str) -> tuple[str, str] | None:
    content = str(text or "").strip()
    if not content:
        return None
    match = re.match(
        rf"^\s*[\"“'「]?(?P<term>{_TERM_PATTERN})[\"”'」]?\s*(?:=|:|：|就是|意思是|指的是|指|是)\s*(?P<meaning>.+?)\s*$",
        content,
    )
    if match:
        term = _trim_term(match.group("term"))
        meaning = _trim_meaning(match.group("meaning"))
        if _looks_like_term(term) and _looks_like_meaning(term, meaning):
            return term, meaning
    candidates = _parse_definition_candidates(content)
    if candidates:
        return candidates[0]["term"], candidates[0]["meaning"]
    return None


async def handle_slang_command(event: AstrMessageEvent, config: Any = None):
    del config
    raw = _safe_get_message_text(event)
    matched = re.match(r"^[\/！!]?黑话(?:[\s\n]+(.+))?$", raw)
    body = (matched.group(1) or "").strip() if matched else ""
    if not body or body in {"帮助", "help", "h"}:
        yield event.plain_result(_help_text())
        return

    scene_type, scene_value, scene_label = _detect_scene(event)
    if not scene_type or not scene_value:
        yield event.plain_result("当前场景不支持黑话词典，至少要能识别出当前群聊、私聊或会话。")
        return

    store = init_slang_store()

    if body.startswith("列表"):
        limit = 12
        parts = body.split(maxsplit=1)
        if len(parts) > 1:
            try:
                limit = max(1, min(int(parts[1].strip()), 30))
            except Exception:
                limit = 12
        items = store.list_terms(scene_type, scene_value, limit=limit, min_confidence=0.0)
        if not items:
            yield event.plain_result(f"{scene_label}里还没有学到黑话。")
            return
        lines = [f"{scene_label}黑话词典："]
        for item in items:
            confidence = round(float(item.get("confidence") or 0), 2)
            evidence = int(item.get("evidence_count") or 1)
            lines.append(f"- {item.get('term')} = {item.get('meaning')} | 可信度 {confidence} | {evidence} 次")
        yield event.plain_result("\n".join(lines))
        return

    if body.startswith("解释") or body.startswith("查询"):
        parts = body.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：`/黑话 解释 yyds`")
            return
        yield event.plain_result(explain_slang_for_event(event, parts[1].strip()))
        return

    if body.startswith("学习") or body.startswith("添加") or body.startswith("记住"):
        prefix = "学习" if body.startswith("学习") else "添加" if body.startswith("添加") else "记住"
        payload = body[len(prefix) :].strip()
        parsed = _parse_manual_definition(payload)
        if not parsed:
            yield event.plain_result("用法：`/黑话 学习 yyds = 永远的神`")
            return
        term, meaning = parsed
        result = store.save_term(
            scene_type,
            scene_value,
            term,
            meaning,
            source_type="manual",
            source_text=raw,
            force=True,
        )
        if not result.get("ok"):
            yield event.plain_result(str(result.get("message") or "黑话学习失败，请稍后再试。"))
            return
        previous = str(result.get("previous_meaning") or "").strip()
        if previous:
            yield event.plain_result(
                f"已更新 {scene_label} 的黑话：{term} = {meaning}\n旧解释：{previous}"
            )
            return
        yield event.plain_result(f"已记住 {scene_label} 的黑话：{term} = {meaning}")
        return

    if body.startswith("删除") or body.startswith("移除"):
        prefix = "删除" if body.startswith("删除") else "移除"
        term = body[len(prefix) :].strip()
        if not term:
            yield event.plain_result("用法：`/黑话 删除 yyds`")
            return
        deleted = store.delete_term(scene_type, scene_value, term)
        yield event.plain_result(
            f"已删除 {scene_label} 的黑话：{term}" if deleted else f"{scene_label}里没有找到黑话：{term}"
        )
        return

    parsed = _parse_manual_definition(body)
    if parsed:
        term, meaning = parsed
        result = store.save_term(
            scene_type,
            scene_value,
            term,
            meaning,
            source_type="manual",
            source_text=raw,
            force=True,
        )
        if result.get("ok"):
            yield event.plain_result(f"已记住 {scene_label} 的黑话：{term} = {meaning}")
            return

    yield event.plain_result(_help_text())
