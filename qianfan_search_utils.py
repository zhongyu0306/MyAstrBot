# 百度千帆智能搜索与网页搜索（astrbot_all_char 集成）
# - /智能搜索：调用 ai_search/chat/completions 后交给当前 LLM 整理输出，本地统计每日最多 100 次
# - /搜索：调用 ai_search/web_search 后交给当前 LLM 整理输出，本地统计每日最多 1000 次

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from astrbot.api import AstrBotConfig, logger

import aiohttp

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools

# 本地每日上限（达到后不再调用接口）
DAILY_LIMIT_SMART = 100
DAILY_LIMIT_WEB = 1000
PLUGIN_DATA_DIR = "astrbot_all_char"
COUNT_FILE_NAME = "qianfan_search_daily.json"
_COUNT_FILE_LOCK = asyncio.Lock()

# 千帆 ai_search 接口（鉴权：X-Appbuilder-Authorization: Bearer <API Key>，仅需 API Key）
CHAT_COMPLETIONS_URL = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"
WEB_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"


def _norm_api_key_val(val: Any) -> str:
    """配置值可能是字符串或 { 'value': '...' }，统一返回 strip 后的字符串。"""
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    return str(val).strip() if val else ""


def _get_qianfan_api_key(config: AstrBotConfig) -> str:
    """从配置读取千帆 API Key（直接作为 Bearer 使用，无需 Secret Key）。兼容多种 config 结构。"""
    val = getattr(config, "qianfan_search_ak", None) or ""
    if val:
        return _norm_api_key_val(val)
    # 兼容 config 为 dict 或嵌套结构（如部分上传插件/配置面板的存储方式）
    if isinstance(config, dict):
        val = config.get("qianfan_search_ak") or config.get("qianfan_api_key")
        if val:
            return _norm_api_key_val(val)
        g = config.get("qianfan_search")
        if isinstance(g, dict):
            val = g.get("qianfan_search_ak") or g.get("qianfan_api_key")
            if val:
                return _norm_api_key_val(val)
            items = g.get("items")
            if isinstance(items, dict):
                val = items.get("qianfan_search_ak") or items.get("qianfan_api_key")
                if val:
                    return _norm_api_key_val(val)
    elif hasattr(config, "__dict__"):
        d = getattr(config, "__dict__", {}) or {}
        val = d.get("qianfan_search_ak") or d.get("qianfan_api_key")
        if val:
            return str(val).strip()
    # 兼容通过 .get 访问的配置对象
    try:
        g = config.get("qianfan_search") if hasattr(config, "get") else None
        if isinstance(g, dict):
            val = g.get("qianfan_search_ak") or g.get("qianfan_api_key")
            if val:
                return str(val).strip()
            items = g.get("items")
            if isinstance(items, dict):
                val = items.get("qianfan_search_ak") or items.get("qianfan_api_key")
                if val:
                    return str(val).strip()
    except Exception:
        pass
    # 部分配置面板用 schema 的 description 作为 key（如「千帆 API Key」）
    try:
        val = getattr(config, "千帆 API Key", None)
        if val:
            return str(val).strip()
        if isinstance(config, dict):
            val = config.get("千帆 API Key")
            if val:
                return str(val).strip()
        if hasattr(config, "get"):
            g = config.get("qianfan_search")
            if isinstance(g, dict):
                val = g.get("千帆 API Key")
                if val:
                    return str(val).strip()
    except Exception:
        pass
    # 兜底：从 qianfan_search 组内任取一个像 API Key 的字符串（兼容未知 key 名）
    try:
        g = None
        if isinstance(config, dict):
            g = config.get("qianfan_search")
        elif hasattr(config, "get"):
            g = config.get("qianfan_search")
        if isinstance(g, dict):
            for v in g.values():
                s = _norm_api_key_val(v) if v else ""
                if len(s) > 20 and ("bce" in s or "ALTAK" in s or "api" in s.lower()):
                    return s
            items = g.get("items")
            if isinstance(items, dict):
                for v in items.values():
                    s = _norm_api_key_val(v) if v else ""
                    if len(s) > 20 and ("bce" in s or "ALTAK" in s or "api" in s.lower()):
                        return s
    except Exception:
        pass
    return ""


# 智能搜索交给 LLM 时的默认提示词（占位符：{smart_search_result}）
_DEFAULT_SMART_SEARCH_LLM_PROMPT = (
    "请严格以当前设定的人格与口吻整理以下智能搜索结果。"
    "使用正常中文标点，避免大量星号（*）或 Markdown 符号。直接输出整理后的回答，不要编造内容。\n\n"
    "原始结果：\n{smart_search_result}"
)


def _get_smart_search_prompt_template(config: AstrBotConfig) -> str:
    """从配置读取智能搜索 LLM 提示词模板，占位符 {smart_search_result}。留空或未配置则用默认。"""
    val = getattr(config, "qianfan_search_smart_prompt", None)
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(config, dict):
        val = config.get("qianfan_search_smart_prompt")
        if isinstance(val, str) and val.strip():
            return val.strip()
        g = config.get("qianfan_search")
        if isinstance(g, dict):
            val = g.get("qianfan_search_smart_prompt")
            if isinstance(val, str) and val.strip():
                return val.strip()
    try:
        if hasattr(config, "get"):
            g = config.get("qianfan_search")
            if isinstance(g, dict):
                val = g.get("qianfan_search_smart_prompt")
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    return _DEFAULT_SMART_SEARCH_LLM_PROMPT


# 网页搜索交给 LLM 时的默认提示词（占位符：{query}、{search_results}）
_DEFAULT_WEB_SEARCH_LLM_PROMPT = (
    "请严格以当前设定的人格与口吻回复。\n"
    "以下是一次网页搜索的原始结果，请根据用户的问题或关键词，用简洁、有条理的中文总结并回答。\n"
    "使用正常中文标点（如全角逗号、句号、省略号……），不要使用混用或奇怪符号。\n"
    "若结果中有链接，可保留重要链接。不要编造未在结果中出现的内容。\n\n"
    "用户搜索关键词：{query}\n\n"
    "搜索结果：\n{search_results}"
)


def _get_web_search_prompt_template(config: AstrBotConfig) -> str:
    """从配置读取网页搜索 LLM 提示词模板，支持占位符 {query}、{search_results}。留空或未配置则用默认。"""
    val = getattr(config, "qianfan_search_web_prompt", None)
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(config, dict):
        val = config.get("qianfan_search_web_prompt")
        if isinstance(val, str) and val.strip():
            return val.strip()
        g = config.get("qianfan_search")
        if isinstance(g, dict):
            val = g.get("qianfan_search_web_prompt")
            if isinstance(val, str) and val.strip():
                return val.strip()
    try:
        if hasattr(config, "get"):
            g = config.get("qianfan_search")
            if isinstance(g, dict):
                val = g.get("qianfan_search_web_prompt")
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    return _DEFAULT_WEB_SEARCH_LLM_PROMPT


def _get_count_file_path() -> Path:
    try:
        base = StarTools.get_data_dir(PLUGIN_DATA_DIR)
    except Exception:
        # 回退到本地 data 目录，避免因环境不完整导致功能完全不可用
        base = Path("data") / "plugin_data" / PLUGIN_DATA_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / COUNT_FILE_NAME


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def _get_daily_counts() -> tuple[int, int]:
    """返回今日已用次数 (智能搜索, 网页搜索)。"""
    path = _get_count_file_path()
    async with _COUNT_FILE_LOCK:
        if not path.exists():
            return 0, 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0, 0
        today = _today_str()
        day_data = data.get(today) or {}
        return day_data.get("smart_search", 0), day_data.get("web_search", 0)


async def _increment_daily_count(which: str) -> int:
    """which 为 'smart_search' 或 'web_search'。递增并返回今日该类型的新次数。"""
    path = _get_count_file_path()
    async with _COUNT_FILE_LOCK:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            data = {}
        today = _today_str()
        if today not in data:
            data[today] = {"smart_search": 0, "web_search": 0}
        data[today][which] = data[today].get(which, 0) + 1
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data[today][which]


async def _call_smart_search(api_key: str, query: str) -> str | None:
    """调用千帆智能搜索 chat/completions，返回模型回复文本。鉴权：X-Appbuilder-Authorization: Bearer <API Key>。"""
    headers = {
        "Content-Type": "application/json",
        "X-Appbuilder-Authorization": f"Bearer {api_key}",
    }
    body = {
        "messages": [{"role": "user", "content": query}],
        "stream": False,
        "model": "ernie-4.5-turbo-32k",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CHAT_COMPLETIONS_URL,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("千帆智能搜索 API 非 200: %s %s", resp.status, text[:300])
                    return None
                data = await resp.json()
                choices = (data or {}).get("choices") or []
                if not choices:
                    return None
                msg = choices[0].get("message") or {}
                content = msg.get("content") or ""
                return content.strip() or None
    except Exception as e:
        logger.exception("千帆智能搜索请求异常: %s", e)
        return None


def _extract_web_search_results_list(data: Any) -> list | None:
    """
    从千帆 web_search 响应中只提取「原始搜索结果列表」，不包含 API 可能自带的 answer/content 等现成总结。
    保证交给 LLM 的是条目的 title/snippet/url，最终回复由用户自己的 LLM 生成。
    """
    if data is None:
        return None
    if isinstance(data, list):
        return data if data else None
    if not isinstance(data, dict):
        return None
    # 顶层常见字段名（千帆 web_search 实际返回 references）
    for key in ("references", "results", "search_results", "items", "data", "list", "hits", "web_results"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    # 嵌套：data / result 等对象内再取列表
    for outer in ("data", "result", "body", "response"):
        inner = data.get(outer)
        if isinstance(inner, list):
            return inner
        if isinstance(inner, dict):
            for k in ("results", "search_results", "items", "list", "hits", "data"):
                v = inner.get(k)
                if isinstance(v, list):
                    return v
    return None


async def _call_web_search(api_key: str, query: str) -> str | None:
    """调用千帆网页搜索 POST /v2/ai_search/web_search，返回原始结果文本（用于交给 LLM 整理）。鉴权：X-Appbuilder-Authorization: Bearer <API Key>。"""
    headers = {
        "Content-Type": "application/json",
        "X-Appbuilder-Authorization": f"Bearer {api_key}",
    }
    # 文档要求：messages + search_source + resource_type_filter
    body = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": 20}],
        "search_recency_filter": "year",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WEB_SEARCH_URL,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("千帆网页搜索 API 非 200: %s %s", resp.status, text[:300])
                    return None
                data = await resp.json()
                # 千帆可能返回：1) 仅原始结果列表  2) 结果列表 + 现成 AI 总结(answer/content)
                # 只把「原始搜索结果列表」交给 LLM，不把 API 自带的总结当材料，保证最终回复来自用户自己的 LLM
                results_list = _extract_web_search_results_list(data)
                if results_list is not None:
                    parts = []
                    for i, item in enumerate(results_list[:20], 1):
                        if isinstance(item, str):
                            parts.append(f"{i}. {item}")
                        elif isinstance(item, dict):
                            title = item.get("title") or item.get("name") or ""
                            snippet = item.get("snippet") or item.get("content") or item.get("body") or ""
                            url = item.get("url") or item.get("link") or ""
                            line = f"{i}. {title}\n   {snippet}".strip()
                            if url:
                                line += f"\n   链接: {url}"
                            parts.append(line)
                        else:
                            parts.append(f"{i}. {json.dumps(item, ensure_ascii=False)[:500]}")
                    return "\n\n".join(parts) if parts else None
                # 无结果列表时：若 API 返回了现成总结(answer/content)，仍交给 LLM 并注明来源，避免直接丢结果
                if isinstance(data, dict):
                    logger.info(
                        "千帆网页搜索响应无可解析的 results 列表，顶层 keys: %s（请据此调整 _extract_web_search_results_list）",
                        list(data.keys()),
                    )
                    for key in ("answer", "content", "summary", "message", "text"):
                        text = data.get(key)
                        if isinstance(text, str) and text.strip():
                            return f"[千帆搜索直接返回的总结，请整理后回复]\n\n{text.strip()}"
                    # 嵌套：data.content / result.answer 等
                    for outer in ("data", "result", "body"):
                        obj = data.get(outer)
                        if isinstance(obj, dict):
                            for k in ("answer", "content", "summary", "message", "text"):
                                t = obj.get(k)
                                if isinstance(t, str) and t.strip():
                                    return f"[千帆搜索直接返回的总结，请整理后回复]\n\n{t.strip()}"
                return None
    except Exception as e:
        logger.exception("千帆网页搜索请求异常: %s", e)
        return None


async def handle_smart_search_command(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
) -> AsyncIterator[Any]:
    """
    /智能搜索 <问题>
    调用千帆 ai_search/chat/completions 获取结果，再交给当前会话的 LLM 整理后输出（人格 + 正常标点，避免大量*）。
    本地统计每日最多 100 次，达上限后拒绝调用。
    """
    api_key = _get_qianfan_api_key(config)
    if not api_key:
        yield event.plain_result(
            "未配置百度千帆 API Key。请在插件配置中填写「千帆 API Key」。"
        )
        return

    raw = event.get_message_str().strip()
    if not raw.startswith("/") and not raw.startswith("／"):
        # 兼容「智能搜索 xxx」
        if raw.lower().startswith("智能搜索"):
            query = raw[len("智能搜索") :].strip()
        else:
            yield event.plain_result("用法：/智能搜索 <你的问题>")
            return
    else:
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法：/智能搜索 <你的问题>")
            return
        cmd = parts[0].lstrip("/／").strip().lower()
        if cmd != "智能搜索":
            yield event.plain_result("用法：/智能搜索 <你的问题>")
            return
        query = parts[1].strip()

    if not query:
        yield event.plain_result("请输入要搜索的问题。")
        return

    smart_count, _ = await _get_daily_counts()
    if smart_count >= DAILY_LIMIT_SMART:
        yield event.plain_result(
            f"今日智能搜索已达本地上限（{DAILY_LIMIT_SMART} 次），明日再试。"
        )
        return

    raw_result = await _call_smart_search(api_key, query)
    if raw_result is None:
        yield event.plain_result("智能搜索请求失败或未返回结果，请稍后重试。")
        return
    await _increment_daily_count("smart_search")

    # 交给当前 LLM 整理（人格 + 正常标点，避免大量 * 等）
    umo = getattr(event, "unified_msg_origin", None) or ""
    try:
        provider_id = await context.get_current_chat_provider_id(umo=umo)
        template = _get_smart_search_prompt_template(config)
        prompt = template.replace("{smart_search_result}", raw_result)
        llm_resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        out = (llm_resp.completion_text or "").strip()
        yield event.plain_result(out or "（未生成有效回复）")
    except Exception as e:
        logger.exception("智能搜索后调用 LLM 失败: %s", e)
        yield event.plain_result(raw_result)


async def handle_web_search_command(
    event: AstrMessageEvent,
    context: Context,
    config: AstrBotConfig,
) -> AsyncIterator[Any]:
    """
    /搜索 <关键词>
    调用千帆 ai_search/web_search 获取网页搜索结果，再交给当前会话的 LLM 整理后输出。
    本地统计每日最多 1000 次，达上限后拒绝调用。
    """
    api_key = _get_qianfan_api_key(config)
    if not api_key:
        yield event.plain_result(
            "未配置百度千帆 API Key。请在插件配置中填写「千帆 API Key」。"
        )
        return

    raw = event.get_message_str().strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        yield event.plain_result("用法：/搜索 <关键词>")
        return
    cmd = parts[0].lstrip("/／").strip().lower()
    if cmd != "搜索":
        yield event.plain_result("用法：/搜索 <关键词>")
        return
    query = parts[1].strip()
    if not query:
        yield event.plain_result("请输入要搜索的关键词。")
        return

    _, web_count = await _get_daily_counts()
    if web_count >= DAILY_LIMIT_WEB:
        yield event.plain_result(
            f"今日网页搜索已达本地上限（{DAILY_LIMIT_WEB} 次），明日再试。"
        )
        return

    search_text = await _call_web_search(api_key, query)
    if not search_text:
        yield event.plain_result("网页搜索未返回结果，请稍后重试或更换关键词。")
        return
    await _increment_daily_count("web_search")

    umo = getattr(event, "unified_msg_origin", None) or ""
    try:
        provider_id = await context.get_current_chat_provider_id(umo=umo)
        template = _get_web_search_prompt_template(config)
        prompt = template.replace("{query}", query).replace("{search_results}", search_text)
        llm_resp = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        yield event.plain_result(llm_resp.completion_text.strip() or "（未生成有效回复）")
    except Exception as e:
        logger.exception("搜索后调用 LLM 失败: %s", e)
        # 降级：直接返回原始搜索结果摘要
        yield event.plain_result(
            "当前 LLM 暂时不可用，以下是原始搜索结果：\n\n" + (search_text[:3000] + "…" if len(search_text) > 3000 else search_text)
        )
