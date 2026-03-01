# astrbot_all_char OCR 模块：图片文字识别，多服务商按顺序尝试

import base64
import re
from pathlib import Path
from typing import Any, AsyncIterator

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent

OCR_PROMPT = "请识别并完整、准确地提取图片中的所有文字（OCR）。只输出图片中的文字内容，不要解释、不要加前后缀。若图中无文字则回复「图中未识别到文字」。"


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.endswith("/v1"):
        url = url + "/v1" if not re.search(r"/v\d+$", url) else url
    return url


def _normalize_api_keys(api_keys: Any) -> list:
    """统一成 list，兼容配置里填成字符串。"""
    if api_keys is None:
        return []
    if isinstance(api_keys, list):
        return [str(k).strip() for k in api_keys if str(k).strip()]
    if isinstance(api_keys, str) and api_keys.strip():
        return [api_keys.strip()]
    return []


def _provider_to_dict(p: Any) -> dict | None:
    """将单条服务商配置转为可用的 dict（兼容对象或 dict 存储，与 gitee_aiimg 的 provider 项一致）。"""
    if p is None:
        return None
    if isinstance(p, dict):
        base_url = (p.get("base_url") or "").strip()
        api_keys = _normalize_api_keys(p.get("api_keys"))
        model = (p.get("model") or "").strip()
        if not base_url and not api_keys:
            return None
        return {"base_url": base_url, "api_keys": api_keys, "model": model}
    base_url = (getattr(p, "base_url", None) or "").strip()
    api_keys = _normalize_api_keys(getattr(p, "api_keys", None))
    model = (getattr(p, "model", None) or "").strip()
    if not base_url and not api_keys:
        return None
    return {"base_url": base_url, "api_keys": api_keys, "model": model}


def _get_providers(config: AstrBotConfig) -> list[dict]:
    """从配置中读取 OCR 服务商列表（与 gitee_aiimg 的 config.get('providers') 类似，兼容 dict/嵌套）。"""
    raw = getattr(config, "ocr_providers", None)
    # 框架可能把 config 当 dict 传，或嵌套在 config["ocr"]["ocr_providers"]
    if (raw is None or (isinstance(raw, list) and len(raw) == 0)) and isinstance(config, dict):
        raw = (config.get("ocr") or {}).get("ocr_providers")
    if raw is None:
        return []
    # template_list 可能是 list，或 dict（如 {"0": {...}}）
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = list(raw.values()) if raw else []
    else:
        try:
            items = list(raw)
        except Exception:
            items = []
    out = []
    for p in items:
        d = _provider_to_dict(p)
        if d and (d.get("base_url") or d.get("api_keys")):
            out.append(d)
    return out


def _get_first_api_key(provider: dict) -> str:
    keys = provider.get("api_keys") or getattr(provider, "api_keys", None) or []
    if isinstance(keys, list) and keys and keys[0]:
        return str(keys[0]).strip()
    if isinstance(keys, str) and keys.strip():
        return keys.strip()
    return ""


async def _fetch_image_as_base64(url_or_path: str) -> str | None:
    """将图片 URL 或本地路径转为 data URL（base64）。"""
    s = (url_or_path or "").strip()
    if not s:
        return None
    # 本地文件
    if not s.startswith("http://") and not s.startswith("https://"):
        path = Path(s)
        if path.is_file():
            try:
                raw = path.read_bytes()
                b64 = base64.standard_b64encode(raw).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
            except Exception as e:
                logger.warning("OCR 读取本地图片失败: %s", e)
        return None
    # 远程 URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(s, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
                if not raw:
                    return None
                b64 = base64.standard_b64encode(raw).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        logger.warning("OCR 下载图片失败: %s", e)
    return None


def _extract_image_from_event(event: AstrMessageEvent) -> str | None:
    """从消息事件中提取第一张图片的 URL 或本地路径。"""
    get_messages = getattr(event, "get_messages", None)
    if not get_messages:
        return None
    try:
        messages = get_messages()
    except Exception:
        return None
    if not messages:
        return None
    for comp in messages:
        if comp is None:
            continue
        cls_name = type(comp).__name__
        if "Image" not in cls_name and "image" not in cls_name.lower():
            continue
        # 常见属性名
        for attr in ("url", "path", "data", "file", "src"):
            if hasattr(comp, attr):
                val = getattr(comp, attr)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        if hasattr(comp, "data") and isinstance(getattr(comp, "data"), dict):
            d = getattr(comp, "data")
            for k in ("url", "path", "file", "src"):
                if k in d and d[k]:
                    return str(d[k]).strip()
    return None


async def _call_openai_vision(
    base_url: str,
    api_key: str,
    model: str,
    image_data_url: str,
) -> str | None:
    """调用 OpenAI 兼容的视觉/多模态 chat 接口。"""
    url = _normalize_base_url(base_url)
    if not url:
        return None
    if not url.endswith("/v1"):
        url = url + "/v1" if "/v1" not in url else url
    post_url = url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    body = {
        "model": (model or "gpt-4o-mini").strip(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ],
        "max_tokens": 4096,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                post_url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("OCR API 非 200: %s %s", resp.status, text[:200])
                    return None
                data = await resp.json()
                choice = (data or {}).get("choices") or []
                if not choice:
                    return None
                msg = choice[0].get("message") or {}
                content = msg.get("content") or ""
                return content.strip() or None
    except Exception as e:
        logger.warning("OCR API 请求异常: %s", e)
    return None


async def handle_ocr_command(event: AstrMessageEvent, config: AstrBotConfig) -> AsyncIterator[Any]:
    """
    处理 /识别图片：从消息中取第一张图，按配置的服务商顺序请求视觉 API 做 OCR，返回识别文字。
    """
    if getattr(config, "ocr_enabled", True) is False:
        yield event.plain_result("OCR 功能未开启。")
        return

    image_src = _extract_image_from_event(event)
    if not image_src:
        yield event.plain_result(
            "未检测到图片。请发送「/识别图片」并附带一张图片（同一条消息里带图）。"
        )
        return

    if image_src.startswith("data:"):
        image_data_url = image_src
    else:
        image_data_url = await _fetch_image_as_base64(image_src)
    if not image_data_url:
        yield event.plain_result("图片获取失败（无法读取或下载），请重试或换一张图。")
        return

    providers = _get_providers(config)
    if not providers:
        yield event.plain_result("未配置 OCR 服务商，请在插件配置的「OCR 服务商」中添加至少一项（API 地址、API Key、模型名称）。")
        return

    last_error: str | None = None
    for prov in providers:
        base_url = (prov.get("base_url") or getattr(prov, "base_url", None) or "").strip()
        model = (prov.get("model") or getattr(prov, "model", None) or "gpt-4o-mini").strip()
        api_key = _get_first_api_key(prov)
        if not base_url or not api_key:
            last_error = "服务商未填完整（需 API 地址、API Key、模型名称）"
            continue
        text = await _call_openai_vision(base_url, api_key, model, image_data_url)
        if text:
            yield event.plain_result(text)
            return
        last_error = "当前服务商未返回有效结果"

    yield event.plain_result(f"识别失败。{last_error or '请检查配置与网络。'}")
