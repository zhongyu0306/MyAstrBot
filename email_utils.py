# 邮件发送（QQ 邮箱 SMTP + 授权码）
import re
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent


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
