# 邮件发送（QQ 邮箱 SMTP + 授权码）
from __future__ import annotations

import re
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import AsyncIterator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

# 匹配常见邮箱地址（含 qq.com、163.com 等）
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


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


async def handle_send_email_command(event: AstrMessageEvent, config: AstrBotConfig):
    """
    处理 /发邮件 命令：/发邮件 <收件人> <主题> <正文>
    收件人、主题、正文之间用空格分隔；若正文含空格，可用引号包裹或放在最后整体作为正文。
    简化解析：第一段为收件人，第二段为主题，剩余为正文。
    """
    raw = (event.get_message_str() or "").strip()
    # 去掉命令头
    for prefix in ("/发邮件", "发邮件", "/发送邮件", "发送邮件"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    if not raw:
        yield event.plain_result(
            "用法：/发邮件 <收件人邮箱> <主题> <正文>\n"
            "示例：/发邮件 someone@qq.com 测试 这是一封测试邮件\n"
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

    # 简单解析：按空格分三段，第一段收件人，第二段主题，剩余为正文
    parts = re.split(r"\s+", raw, maxsplit=2)
    if len(parts) < 3:
        yield event.plain_result(
            "请按格式输入：/发邮件 <收件人邮箱> <主题> <正文>，共三段。"
        )
        return
    to_addr, subject, body = parts[0], parts[1], parts[2]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to_addr):
        yield event.plain_result("收件人邮箱格式不正确，请检查。")
        return

    ok, msg = send_email_sync(
        sender=sender,
        auth_code=auth_code,
        to_addrs=[to_addr],
        subject=subject,
        body=body,
        smtp_host=_get_email_config(config, "email_smtp_host", "smtp.qq.com"),
        smtp_port=int(_get_email_config(config, "email_smtp_port", "465") or "465"),
    )
    yield event.plain_result(msg)


async def _generate_and_send_email(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
    to_addr: str,
    user_prompt: str,
) -> AsyncIterator:
    """根据用户描述用 LLM 生成主题与正文并发送邮件。"""
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
    prompt = (
        "用户希望发一封邮件到 "
        + to_addr
        + "。用户对内容的描述："
        + user_prompt
        + "\n\n请根据上述描述生成邮件的主题和正文。严格按以下格式回复，不要其他说明：\n"
        "第一行：邮件主题（一句话，简短）\n"
        "从第二行起：邮件正文（可多行，纯文本）。"
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
    subject = (lines[0] or "（无主题）").strip()[:200]
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else (lines[0] or "")
    ok, msg = send_email_sync(
        sender=sender,
        auth_code=auth_code,
        to_addrs=[to_addr],
        subject=subject,
        body=body,
        smtp_host=_get_email_config(config, "email_smtp_host", "smtp.qq.com"),
        smtp_port=int(_get_email_config(config, "email_smtp_port", "465") or "465"),
    )
    yield event.plain_result(msg)


# 匹配「发邮件到 / 发到邮箱 / 发送到邮箱」+ 邮箱 + 至少一个内容字符（可从整条消息任意位置匹配）
SEND_TO_EMAIL_PATTERN = re.compile(
    r"(发邮件到|发到邮箱|发送到邮箱)\s+([^\s@]+@[^\s@]+\.[^\s@]+)\s+(.+)",
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
    for prefix in ("/发邮件到", "发邮件到", "发到邮箱", "发送到邮箱"):
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
