import json
from pathlib import Path
from typing import Any, AsyncIterator

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent

from .ocr_utils import _extract_image_from_event


ANIMETRACE_DEFAULT_URL = "https://api.animetrace.com/v1/search"


def _get_animetrace_config(config: AstrBotConfig) -> dict[str, Any]:
    """
    从配置中读取 AnimeTrace 相关参数，兼容扁平/嵌套结构。
    """
    api_url = getattr(config, "animetrace_api_url", None) or ANIMETRACE_DEFAULT_URL
    model = getattr(config, "animetrace_model", None) or "animetrace_high_beta"
    is_multi = bool(getattr(config, "animetrace_is_multi", False))
    ai_detect_enabled = bool(getattr(config, "animetrace_ai_detect", True))

    return {
        "api_url": str(api_url).strip() or ANIMETRACE_DEFAULT_URL,
        "model": str(model).strip() or "animetrace_high_beta",
        "is_multi": 1 if is_multi else 0,
        "ai_detect": 1 if ai_detect_enabled else 2,
    }


async def _prepare_file_field(image_src: str) -> tuple[str, bytes] | None:
    """
    将本地路径或 URL 转为 (filename, bytes) 形式，供 multipart file 字段使用。

    - 若为本地文件：直接读取。
    - 若为 HTTP/HTTPS URL：先下载到内存。
    """
    s = (image_src or "").strip()
    if not s:
        return None

    # 本地路径
    if not s.startswith("http://") and not s.startswith("https://"):
        path = Path(s)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
            return path.name or "image.jpg", data
        except Exception as e:
            logger.warning("[animetrace] 读取本地图片失败: %s", e)
            return None

    # 远程 URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(s, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.warning("[animetrace] 下载远程图片失败，HTTP %s", resp.status)
                    return None
                data = await resp.read()
                if not data:
                    return None
                filename = s.rsplit("/", 1)[-1] or "image.jpg"
                return filename, data
    except Exception as e:
        logger.warning("[animetrace] 下载远程图片异常: %s", e)
        return None


async def _call_animetrace(api_url: str, payload: dict[str, Any], file_field: tuple[str, bytes]) -> dict[str, Any] | None:
    """
    调用 AnimeTrace 接口。
    文档参考：https://ai.animedb.cn/zh/api-docs/
    """
    filename, file_bytes = file_field
    post_url = (api_url or ANIMETRACE_DEFAULT_URL).strip() or ANIMETRACE_DEFAULT_URL

    form = aiohttp.FormData()
    # AnimeTrace 支持 file / url / base64，这里统一使用 file 方式
    form.add_field("file", file_bytes, filename=filename, content_type="image/jpeg")
    for k, v in payload.items():
        if v is None:
            continue
        form.add_field(k, str(v))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                post_url,
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning("[animetrace] 接口返回非 200：status=%s, body=%s", resp.status, text[:500])
                    return None
                try:
                    data = await resp.json()
                except Exception:
                    # 某些情况下服务器可能返回非 JSON 文本
                    logger.warning("[animetrace] 解析 JSON 失败，返回原始文本")
                    return {"raw_text": text}
                return data  # type: ignore[return-value]
    except Exception as e:
        logger.warning("[animetrace] 接口请求异常: %s", e)
        return None


def _format_animetrace_result(data: dict[str, Any]) -> str:
    """
    将 AnimeTrace 返回结果转为可读文本。
    为兼容未来变更，这里尽量宽松解析，若结构不符合预期则回退为原始 JSON。
    """
    if not isinstance(data, dict):
        try:
            return json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            return str(data)

    if "raw_text" in data:
        return f"AnimeTrace 原始响应：\n{data.get('raw_text')}"

    # 尝试从常见字段中抽取结果列表
    candidates = None
    for key in ("data", "result", "results"):
        val = data.get(key)
        if isinstance(val, list) and val:
            candidates = val
            break
    if not candidates:
        # 没有列表，直接输出完整 JSON
        try:
            return "AnimeTrace 识别结果（原始 JSON）：\n" + json.dumps(
                data, ensure_ascii=False, indent=2
            )
        except Exception:
            return "AnimeTrace 识别结果：\n" + str(data)

    lines: list[str] = []
    lines.append("AnimeTrace 识别结果：")

    for idx, item in enumerate(candidates[:5], start=1):  # 最多展示前 5 条
        if not isinstance(item, dict):
            lines.append(f"\n[{idx}] {item}")
            continue

        title = (
            item.get("title")
            or item.get("anime_title")
            or item.get("name")
            or item.get("anilist_title")
            or "未知作品"
        )
        similarity = item.get("similarity") or item.get("similarity_percent")
        ep = item.get("episode") or item.get("ep")
        ts_from = item.get("from") or item.get("at") or item.get("time")
        image = item.get("image") or item.get("preview") or item.get("image_url")

        lines.append(f"\n[{idx}] {title}")
        if similarity is not None:
            try:
                sim_f = float(similarity)
                if sim_f <= 1:
                    sim_f *= 100
                lines.append(f"  相似度：{sim_f:.2f}%")
            except Exception:
                lines.append(f"  相似度：{similarity}")
        if ep is not None:
            lines.append(f"  集数：{ep}")
        if ts_from is not None:
            lines.append(f"  时间点：{ts_from}")
        if image:
            lines.append(f"  预览图：{image}")

    if len(candidates) > 5:
        lines.append(f"\n（共返回 {len(candidates)} 条结果，已截取前 5 条展示。）")

    return "\n".join(lines)


async def handle_animetrace_command(event: AstrMessageEvent, config: AstrBotConfig) -> AsyncIterator[Any]:
    """
    处理动漫图片识别指令：从消息中取图并调用 AnimeTrace API。
    """
    image_src = _extract_image_from_event(event)
    if not image_src:
        yield event.plain_result(
            "未检测到图片。\n请发送「/搜番」或「/识别动漫」并在同一条消息中附带一张动漫截图/图片。"
        )
        return

    file_field = await _prepare_file_field(image_src)
    if not file_field:
        yield event.plain_result("图片获取失败（无法读取或下载），请重试或换一张图片。")
        return

    cfg = _get_animetrace_config(config)
    api_url = cfg["api_url"]
    payload = {
        "model": cfg["model"],
        "is_multi": cfg["is_multi"],
        "ai_detect": cfg["ai_detect"],
    }

    data = await _call_animetrace(api_url, payload, file_field)
    if not data:
        yield event.plain_result("AnimeTrace 识别失败，请稍后重试或检查网络。")
        return

    text = _format_animetrace_result(data)
    # 为避免消息过长，简单截断到约 4000 字
    max_len = 4000
    if len(text) > max_len:
        text = text[:max_len] + "\n\n（结果过长，已截断显示。）"
    yield event.plain_result(text)

