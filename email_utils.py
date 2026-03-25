# 邮件发送（QQ 邮箱 SMTP + 授权码）
from __future__ import annotations

import hashlib
import inspect
import json
import re
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import AsyncIterator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools

from .passive_memory_utils import record_passive_habit

# 匹配常见邮箱地址（含 qq.com、163.com 等）
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_EMAIL_RECENT_SEND_CACHE: dict[str, float] = {}
_EMAIL_DEDUP_WINDOW_SECONDS = 20.0
_EMAIL_HISTORY_SUMMARY_FILE = "email_history_summaries.json"


def _get_email_summary_file_path() -> Path:
    base = StarTools.get_data_dir("astrbot_all_char")
    base.mkdir(parents=True, exist_ok=True)
    return base / _EMAIL_HISTORY_SUMMARY_FILE


def _load_email_summary_map() -> dict:
    path = _get_email_summary_file_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("[email] 读取历史总结缓存失败: %s", e)
        return {}


def _save_email_summary_map(summary_map: dict) -> None:
    path = _get_email_summary_file_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(summary_map, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.warning("[email] 保存历史总结缓存失败: %s", e)


def _build_fallback_email_content(user_prompt: str) -> tuple[str, str]:
    """
    保守兜底：直接基于用户原话构造主题和正文，避免 LLM 编造事实。
    """
    raw = (user_prompt or "").strip()
    if not raw:
        return "邮件主题", ""

    first_line = raw.splitlines()[0].strip()
    subject_seed = first_line if first_line else raw
    if len(subject_seed) > 28:
        subject_seed = subject_seed[:28].rstrip("，。！？；：,.!?;:")
    subject = f"邮件主题：{subject_seed}" if subject_seed else "邮件主题"
    return subject, raw


def _is_recent_duplicate_send(event: AstrMessageEvent, to_addr: str, user_prompt: str) -> bool:
    sender_id = ""
    session_id = ""
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:
        sender_id = ""
    session_id = str(getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "") or "")

    key_raw = f"{sender_id}|{session_id}|{to_addr.strip().lower()}|{(user_prompt or '').strip()}"
    key = hashlib.sha1(key_raw.encode("utf-8")).hexdigest()
    now = time.monotonic()
    last_ts = _EMAIL_RECENT_SEND_CACHE.get(key, 0.0)
    _EMAIL_RECENT_SEND_CACHE[key] = now

    # 清理过期项，避免缓存无限增长
    expired_before = now - (_EMAIL_DEDUP_WINDOW_SECONDS * 3)
    stale_keys = [k for k, ts in _EMAIL_RECENT_SEND_CACHE.items() if ts < expired_before]
    for k in stale_keys:
        _EMAIL_RECENT_SEND_CACHE.pop(k, None)

    return (now - last_ts) < _EMAIL_DEDUP_WINDOW_SECONDS


def _extract_history_text(item) -> str:
    """
    从不同类型的历史消息结构中提取可读文本。
    """
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("content", "text", "message", "msg", "raw_message"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        role = str(item.get("role") or item.get("sender") or "").strip()
        content = str(item.get("content") or item.get("text") or "").strip()
        merged = f"{role}: {content}".strip(": ").strip()
        return merged

    # 兜底：对象属性
    for key in ("content", "text", "message", "msg", "raw_message"):
        val = getattr(item, key, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        return str(item).strip()
    except Exception:
        return ""


async def _call_maybe_async(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if inspect.isawaitable(ret):
        return await ret
    return ret


def _session_umo(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "") or "")


async def _fetch_recent_chat_history_lines(
    context: Context, event: AstrMessageEvent, limit: int = 12
) -> tuple[list[str], str]:
    """
    从 AstrBot 上下文中尝试读取最近对话记录（多接口兼容）。
    返回: (历史文本行列表, 命中的接口名)
    """
    umo = _session_umo(event)
    if not umo:
        return [], ""

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
            # 某些实现参数可能不兼容，降级重试
            try:
                data = await _call_maybe_async(fn, umo, limit)
            except Exception:
                continue
        except Exception:
            continue

        if not data:
            continue

        # 统一为 list 处理
        items = data if isinstance(data, list) else [data]
        lines: list[str] = []
        for it in items[-limit:]:
            txt = _extract_history_text(it)
            if txt:
                lines.append(txt)
        if lines:
            logger.info("[email] 已获取会话历史: method=%s lines=%s umo=%s", name, len(lines), umo[:80])
            return lines[-limit:], name

    # 兜底：兼容 astrbot_plugin_infinite_dialogue 的取法（conversation_manager + conversation.history）
    conv_mgr = getattr(context, "conversation_manager", None)
    if conv_mgr is not None:
        try:
            curr_cid = await _call_maybe_async(conv_mgr.get_curr_conversation_id, umo)
            conv = await _call_maybe_async(conv_mgr.get_conversation, umo, curr_cid)
            conv_history = getattr(conv, "history", None) if conv is not None else None
            if conv_history:
                parsed = []
                try:
                    parsed = json.loads(conv_history)
                except Exception:
                    parsed = []
                lines: list[str] = []
                if isinstance(parsed, list):
                    for it in parsed[-limit:]:
                        txt = _extract_history_text(it)
                        if txt:
                            lines.append(txt)
                if lines:
                    logger.info(
                        "[email] 已获取会话历史: method=conversation_manager lines=%s umo=%s",
                        len(lines),
                        umo[:80],
                    )
                    return lines[-limit:], "conversation_manager"
        except Exception as e:
            logger.warning("[email] conversation_manager 读取历史失败: umo=%s err=%s", umo[:80], e)

    logger.info("[email] 未获取到会话历史接口或无历史数据: umo=%s", umo[:80])
    return [], ""


async def _fetch_recent_chat_history(context: Context, event: AstrMessageEvent, limit: int = 12) -> str:
    lines, _ = await _fetch_recent_chat_history_lines(context, event, limit=limit)
    return "\n".join(lines)


def _get_email_config(config: AstrBotConfig, key: str, default: str = ""):
    """从配置读取邮件相关项，兼容扁平/嵌套/items 结构。"""
    val = getattr(config, key, None)
    if val is not None and str(val).strip():
        return str(val).strip()
    if isinstance(config, dict):
        val = config.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
        g = config.get("email")
        if isinstance(g, dict):
            val = g.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
            items = g.get("items")
            if isinstance(items, dict):
                v = items.get(key)
                if isinstance(v, dict) and "value" in v:
                    v = v["value"]
                if v is not None and str(v).strip():
                    return str(v).strip()
    try:
        g = config.get("email") if hasattr(config, "get") else None
        if isinstance(g, dict):
            val = g.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
            items = g.get("items")
            if isinstance(items, dict):
                v = items.get(key)
                if isinstance(v, dict) and "value" in v:
                    v = v["value"]
                if v is not None and str(v).strip():
                    return str(v).strip()
    except Exception:
        pass
    return default


def _safe_int(value: str, default: int) -> int:
    try:
        return int(str(value))
    except Exception:
        return default


async def _generate_history_summary(
    context: Context,
    provider_id: str,
    history_lines: list[str],
    max_retries: int = 2,
) -> str:
    """
    根据历史记录生成紧凑摘要，作为长期上下文。
    """
    if not provider_id or not history_lines:
        return ""

    history_text = "\n".join(history_lines)
    prompt = (
        "请作为对话记录整理器，对以下聊天记录做高密度总结，输出用于后续理解上下文的“前情提要”。\n"
        "要求：\n"
        "1. 只保留事实与明确诉求，禁止杜撰。\n"
        "2. 尽量保留关键数字、时间、人物、结论。\n"
        "3. 直接输出正文，不要寒暄。\n"
        "4. 建议长度 120~300 字。\n\n"
        f"聊天记录：\n{history_text}"
    )
    retries = max(1, max_retries)
    for i in range(retries):
        try:
            llm_resp = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            out = (getattr(llm_resp, "completion_text", None) or "").strip()
            if out:
                return out[:2000]
        except Exception as e:
            logger.warning("[email] 生成历史总结失败(第%s/%s次): %s", i + 1, retries, e)
    return ""


async def _build_email_memory_context(
    context: Context,
    event: AstrMessageEvent,
    config: AstrBotConfig,
    current_provider_id: str,
) -> str:
    """
    构造邮件生成用上下文：
    - 当历史条数达到阈值时，自动总结并持久化（前情提要）
    - 始终附带最近若干条原始对话
    """
    threshold = _safe_int(_get_email_config(config, "email_history_summary_threshold", "40"), 40)
    recent_limit = _safe_int(_get_email_config(config, "email_history_recent_limit", "12"), 12)
    summary_retries = _safe_int(_get_email_config(config, "email_history_summary_retries", "2"), 2)
    summary_provider_id = _get_email_config(config, "email_summary_provider_id", "").strip()
    summary_provider = summary_provider_id or current_provider_id

    history_limit = max(threshold + 10, 60)
    lines, method = await _fetch_recent_chat_history_lines(context, event, limit=history_limit)
    if not lines:
        return "（未获取到会话历史，按当前输入处理）"

    umo = _session_umo(event)
    digest = hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()
    summary_map = _load_email_summary_map()
    rec = summary_map.get(umo, {}) if isinstance(summary_map.get(umo), dict) else {}
    saved_summary = str(rec.get("summary") or "").strip()
    last_digest = str(rec.get("last_digest") or "")

    if len(lines) >= threshold and digest != last_digest:
        logger.info(
            "[email] 历史长度达到阈值，触发自动总结: lines=%s threshold=%s method=%s umo=%s",
            len(lines),
            threshold,
            method or "unknown",
            umo[:80],
        )
        summary = await _generate_history_summary(
            context=context,
            provider_id=summary_provider,
            history_lines=lines,
            max_retries=summary_retries,
        )
        if summary:
            saved_summary = summary
            summary_map[umo] = {
                "summary": summary,
                "last_digest": digest,
                "updated_at": int(time.time()),
                "history_count": len(lines),
            }
            _save_email_summary_map(summary_map)
        else:
            logger.warning("[email] 自动总结触发但未生成有效摘要，保留最近对话原文。")

    recent_lines = lines[-max(1, recent_limit) :]
    if saved_summary:
        return f"【前情提要】\n{saved_summary}\n\n【最近对话】\n" + "\n".join(recent_lines)
    return "【最近对话】\n" + "\n".join(recent_lines)


def send_email_sync(
    sender: str,
    auth_code: str,
    to_addrs: list[str],
    subject: str,
    body: str,
    smtp_host: str = "smtp.qq.com",
    smtp_port: int = 465,
) -> tuple[bool, str]:
    """
    使用 QQ 邮箱 SMTP（SSL）同步发送邮件。

    Args:
        sender: 发件人邮箱（QQ 邮箱地址）
        auth_code: QQ 邮箱授权码（在 QQ 邮箱设置 -> 账户 -> POP3/IMAP 中开启并获取）
        to_addrs: 收件人邮箱列表
        subject: 邮件主题
        body: 邮件正文（纯文本）
        smtp_host: SMTP 服务器，默认 smtp.qq.com
        smtp_port: 端口，默认 465（SSL）

    Returns:
        (成功与否, 说明信息)
    """
    if not sender or not auth_code:
        return False, "未配置发件人邮箱或授权码，请在插件配置中填写 QQ 邮箱与授权码。"
    if not to_addrs:
        return False, "请至少填写一个收件人邮箱。"
    to_addrs = [a.strip() for a in to_addrs if a and a.strip()]
    if not to_addrs:
        return False, "收件人邮箱不能为空。"

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr(("", sender))
        msg["To"] = ", ".join(to_addrs)

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
            server.login(sender, auth_code)
            server.sendmail(sender, to_addrs, msg.as_string())
        logger.info("邮件已发送: 发件人=%s, 收件人=%s, 主题=%s", sender, to_addrs, subject)
        return True, "邮件发送成功。"
    except smtplib.SMTPAuthenticationError as e:
        logger.warning("QQ 邮箱登录失败（请检查邮箱与授权码）: %s", e)
        return False, "发送失败：邮箱或授权码错误，请确认已在 QQ 邮箱中开启 SMTP 并使用授权码（非登录密码）。"
    except Exception as e:
        logger.exception("发送邮件时出错: %s", e)
        return False, f"发送失败：{e}"


async def handle_send_email_command(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
):
    """处理 /发邮件 命令：/发邮件 <收件人邮箱> <内容描述>，交给 LLM 生成正式邮件。"""
    raw = (event.get_message_str() or "").strip()
    for prefix in ("/发邮件", "发邮件", "/发送邮件", "发送邮件"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    if not raw:
        yield event.plain_result(
            "用法：/发邮件 <收件人邮箱> <内容描述>\n"
            "示例：/发邮件 someone@qq.com 告诉他今晚来吃饭\n"
            "请先在插件配置中填写 QQ 邮箱地址和授权码（QQ 邮箱设置 -> 账户 -> POP3/IMAP 服务 -> 授权码）。"
        )
        return

    sender = _get_email_config(config, "email_sender", "")
    auth_code = _get_email_config(config, "email_auth_code", "")
    if not sender or not auth_code:
        yield event.plain_result(
            "未配置发件人邮箱或 QQ 邮箱授权码。请在插件配置的「邮件」中填写 email_sender 与 email_auth_code。"
        )
        return

    parts = re.split(r"\s+", raw, maxsplit=1)
    if len(parts) < 2:
        yield event.plain_result(
            "请按格式输入：/发邮件 <收件人邮箱> <内容描述>。"
        )
        return
    to_addr = parts[0]
    user_prompt = parts[1].strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to_addr):
        yield event.plain_result("收件人邮箱格式不正确，请检查。")
        return
    if not user_prompt:
        yield event.plain_result("请补充要发送的内容描述，例如：/发邮件 xxx@qq.com 告诉他今晚来吃饭")
        return

    event.stop_event()
    async for result in _generate_and_send_email(event, context, config, to_addr, user_prompt):
        yield result


async def _generate_and_send_email(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
    to_addr: str,
    user_prompt: str,
) -> AsyncIterator:
    """根据用户描述用 LLM 生成主题与正文并发送邮件。"""
    if _is_recent_duplicate_send(event, to_addr, user_prompt):
        logger.warning("[email] 检测到重复触发，已跳过重复发送: to_addr=%s", to_addr)
        return

    sender = _get_email_config(config, "email_sender", "")
    auth_code = _get_email_config(config, "email_auth_code", "")
    if not sender or not auth_code:
        yield event.plain_result(
            "未配置发件人邮箱或 QQ 邮箱授权码，请在插件配置的「邮件」中填写后再试。"
        )
        return
    umo = getattr(event, "unified_msg_origin", None) or ""
    try:
        provider_id = await context.get_current_chat_provider_id(umo=umo)
    except Exception as e:
        logger.warning("获取当前会话 LLM 失败: %s", e)
        yield event.plain_result("当前无法使用 LLM 生成邮件内容，请用命令：/发邮件 收件人 主题 正文")
        return
    fallback_subject, fallback_body = _build_fallback_email_content(user_prompt)
    history_block = await _build_email_memory_context(
        context=context,
        event=event,
        config=config,
        current_provider_id=provider_id,
    )
    prompt = (
        "你是邮件文案整理器。请严格基于“用户原文”做轻微润色，不得新增任何事实。\n"
        "硬性约束：\n"
        "1. 绝对禁止新增原文没有的数字、金额、时间、地点、人物、事件。\n"
        "2. 如果信息不足，就保留原文意思，不要自行补充背景。\n"
        "3. 先参考“最近对话记录”，再参考“用户原文”，优先保证事实一致。\n"
        "4. 如果最近对话和用户原文冲突，以用户原文为准。\n"
        "5. 输出仅两行，不要解释。\n"
        "第1行：主题：<简短主题>\n"
        "第2行起：正文：<正文内容>\n\n"
        f"最近对话记录：\n{history_block}\n\n"
        f"收件人：{to_addr}\n"
        f"用户原文：{user_prompt}"
    )
    try:
        llm_resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
    except Exception as e:
        logger.exception("LLM 生成邮件内容失败: %s", e)
        yield event.plain_result("生成邮件内容时出错，请用命令：/发邮件 收件人 主题 正文")
        return
    text = (getattr(llm_resp, "completion_text", None) or "").strip()
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    subject = fallback_subject
    body = fallback_body
    if lines:
        llm_subject = lines[0]
        llm_body = "\n".join(lines[1:]).strip() if len(lines) > 1 else lines[0]
        for prefix in ("第一行：", "邮件主题：", "主题："):
            if llm_subject.startswith(prefix):
                llm_subject = llm_subject[len(prefix) :].strip()
                break
        for prefix in ("从第二行起：", "正文："):
            if llm_body.startswith(prefix):
                llm_body = llm_body[len(prefix) :].strip()
                break
        if llm_subject and llm_body:
            subject = llm_subject[:200]
            body = llm_body

    ok, msg = send_email_sync(
        sender=sender,
        auth_code=auth_code,
        to_addrs=[to_addr],
        subject=subject,
        body=body,
        smtp_host=_get_email_config(config, "email_smtp_host", "smtp.qq.com"),
        smtp_port=int(_get_email_config(config, "email_smtp_port", "465") or "465"),
    )
    if ok:
        record_passive_habit(
            event,
            "email",
            "recipient",
            to_addr.lower(),
            source_text=user_prompt,
        )
    yield event.plain_result(msg)


# 句首「任意称呼 + 逗号/空格」可去掉，便于命中命令（不写死具体名字）
STRIP_CALL_PREFIX = re.compile(r"^\s*[^\s，,]+[，,]\s*")

# 匹配「发邮件到/发到邮箱/…」+ 邮箱 + 内容；邮箱前允许无空格（如 发邮件到484238618@qq.com）
SEND_TO_EMAIL_PATTERN = re.compile(
    r"(发邮件到|发到邮箱|发送到邮箱|发送邮件\s*到)\s*([^\s@]+@[^\s@]+\.[^\s@]+)\s+(.+)",
    re.DOTALL,
)


async def handle_send_email_to_command(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
) -> AsyncIterator:
    """
    处理「发邮件到 <邮箱> <内容描述>」：用 LLM 根据描述生成主题与正文并发送。
    命令形式，优先级高于主对话，能稳定触发。
    """
    raw = (event.get_message_str() or "").strip()
    # 去掉句首「任意称呼+逗号/空格」，方便命中命令（不写死名字，谁称呼都能用）
    raw = STRIP_CALL_PREFIX.sub("", raw).strip()
    for prefix in ("/发邮件到", "发邮件到", "发到邮箱", "发送到邮箱", "发送邮件到", "发送邮件 到"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    else:
        return
    if not raw:
        yield event.plain_result(
            "用法：发邮件到 <收件人邮箱> <内容描述>\n"
            "示例：发邮件到 1102025067@qq.com 今天晚饭\n"
            "示例：发到邮箱 1102025067@qq.com 上海天气汇总"
        )
        return
    parts = re.split(r"\s+", raw, maxsplit=1)
    to_addr = parts[0].strip()
    if not EMAIL_PATTERN.fullmatch(to_addr):
        yield event.plain_result("收件人邮箱格式不正确，请写：发邮件到 xxx@qq.com 内容描述")
        return
    user_prompt = (parts[1] or "").strip()
    if not user_prompt:
        yield event.plain_result("请写上要发的内容描述，例如：发邮件到 xxx@qq.com 今天晚饭")
        return
    event.stop_event()
    async for result in _generate_and_send_email(event, context, config, to_addr, user_prompt):
        yield result


async def handle_send_email_to_in_message(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
) -> AsyncIterator:
    """
    在整条消息中查找「发邮件到/发到邮箱/发送到邮箱 邮箱 内容」，不要求句首，有则解析并发信。
    例如「xxx，发邮件到 1102025067@qq.com 今天晚饭」也能触发。
    """
    raw = (event.get_message_str() or "").strip()
    m = SEND_TO_EMAIL_PATTERN.search(raw)
    if not m:
        return
    to_addr = m.group(2).strip()
    user_prompt = (m.group(3) or "").strip()
    if not user_prompt or len(user_prompt) < 2:
        return
    event.stop_event()
    logger.info("[email] 消息内识别发邮件到 to_addr=%s", to_addr)
    async for result in _generate_and_send_email(event, context, config, to_addr, user_prompt):
        yield result


async def handle_email_intent(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
) -> AsyncIterator:
    """
    当消息中同时包含「邮件」和邮箱地址时，用 LLM 根据用户意图生成邮件主题与正文并发送。
    不处理已以 /发邮件、/发送邮件 开头的命令（交给 handle_send_email_command）。
    """
    raw = (event.get_message_str() or "").strip()
    if "邮件" not in raw:
        return
    # 排除显式命令，交给命令处理器
    for prefix in ("/发邮件", "/发送邮件", "发邮件 ", "发送邮件 "):
        if raw.startswith(prefix) or raw.strip().startswith(prefix.strip()):
            return
    # 避免与「发邮件到 xxx@qq.com ...」规则重叠导致重复发送
    if SEND_TO_EMAIL_PATTERN.search(raw):
        return

    match = EMAIL_PATTERN.search(raw)
    if not match:
        return
    to_addr = match.group(0).strip()
    sender = _get_email_config(config, "email_sender", "")
    auth_code = _get_email_config(config, "email_auth_code", "")
    if not sender or not auth_code:
        yield event.plain_result(
            "未配置发件人邮箱或 QQ 邮箱授权码，请在插件配置的「邮件」中填写后再试。"
        )
        event.stop_event()
        return

    # 立即拦截事件，避免主对话/Agent 也处理本条消息并回复「已发送」
    event.stop_event()
    logger.info("[email_intent] 检测到邮件+邮箱，开始处理 to_addr=%s", to_addr)
    async for result in _generate_and_send_email(event, context, config, to_addr, raw):
        yield result
