import asyncio
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import File, Record
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


DEFAULT_MUSIC_API_BASE = "https://wyy.xhily.com/"
SESSION_TTL_SECONDS = 300


_music_sessions: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


def _get_music_config(config: AstrBotConfig) -> Dict[str, Any]:
    """
    读取点歌相关配置，兼容扁平/嵌套结构。

    对应 _conf_schema.json 中的 music 模块（兼容原 astrbot_plugin_music）：
    - nodejs_base_url
    - song_limit
    - select_mode
    - send_modes
    - enable_comments / enable_lyrics（目前未使用，占位）
    - proxy / timeout / timeout_recall / clear_cache / playlist_limit（目前未用或预留）
    """
    # NodeJS / 网易云 API 根地址
    base_url = getattr(config, "nodejs_base_url", None) or DEFAULT_MUSIC_API_BASE

    # 音质档位：沿用原默认 exhigh，可在后续按需暴露更多选项
    quality = "exhigh"

    # 搜索结果数量
    try:
        limit = int(getattr(config, "song_limit", 5) or 5)
    except Exception:
        limit = 5
    if limit <= 0:
        limit = 5

    # 发送模式优先级：从字符串列表中抽取 card/record/file/text 这四类关键字
    raw_modes = getattr(config, "send_modes", None)
    send_modes: list[str] = []
    if isinstance(raw_modes, list):
        for m in raw_modes:
            s = str(m or "").strip()
            if not s:
                continue
            # 兼容 "card(卡片模式)" 这类写法
            base = s.split("(", 1)[0].strip().lower()
            if base in {"card", "record", "file", "text"} and base not in send_modes:
                send_modes.append(base)
    if not send_modes:
        send_modes = ["card", "text"]

    proxy = getattr(config, "proxy", None) or ""
    return {
        "base_url": str(base_url).strip() or DEFAULT_MUSIC_API_BASE,
        "quality": str(quality).strip() or "exhigh",
        "limit": limit,
        "send_modes": send_modes,
        "proxy": str(proxy).strip(),
    }


async def _http_get_json(url: str) -> Dict[str, Any] | None:
    """
    简单的 GET+JSON 封装，供网易云 NodeJS API 使用。
    """
    return await _http_request_json(url)


async def _http_request_json(
    url: str,
    *,
    method: str = "GET",
    data: Dict[str, Any] | None = None,
    proxy: str | None = None,
    headers: Dict[str, str] | None = None,
    cookies: Dict[str, str] | None = None,
) -> Dict[str, Any] | List[Any] | None:
    """
    通用 HTTP + JSON 封装，兼容 GET / POST，请求失败时返回 None。
    """
    logger.info("[music] 请求 URL: %s", url)
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as session:
            request = session.post if str(method).upper() == "POST" else session.get
            async with request(url, data=data, proxy=proxy or None, cookies=cookies) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(
                        "[music] 接口返回非 200：status=%s body_prefix=%s",
                        resp.status,
                        (text or "")[:200],
                    )
                    return None
                if not text:
                    return None
                try:
                    return await resp.json()
                except Exception:
                    # 部分兼容实现可能直接返回 JSON 字符串
                    import json

                    try:
                        return json.loads(text)
                    except Exception:
                        logger.warning("[music] 解析 JSON 失败，body_prefix=%s", text[:200])
                        return None
    except asyncio.TimeoutError:
        logger.error("[music] 请求超时")
        return None
    except Exception as e:
        logger.error("[music] 请求异常: %s", e)
        return None


async def _search_songs(cfg: Dict[str, Any], keyword: str) -> List[Dict[str, Any]]:
    """
    使用 NeteaseCloudMusicApi 的 /search 接口按关键词搜索歌曲。
    文档参考：https://neteasecloudmusicapi.js.org/#/search
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    proxy = str(cfg.get("proxy") or "").strip() or None
    songs = await _search_songs_via_nodejs(cfg, kw, proxy=proxy)
    if songs:
        return songs
    songs = await _search_songs_via_meting(cfg, kw, proxy=proxy)
    if songs:
        logger.info("[music] NodeJS 搜索失败，已回退到 Meting 搜索")
        return songs
    songs = await _search_songs_via_official(kw, limit=int(cfg.get("limit") or 3), proxy=proxy)
    if songs:
        logger.info("[music] 已回退到网易官方 cloudsearch 搜索")
        return songs
    return []


async def _search_songs_via_nodejs(
    cfg: Dict[str, Any],
    keyword: str,
    *,
    proxy: str | None = None,
) -> List[Dict[str, Any]]:
    """
    优先走兼容 NeteaseCloudMusicApi 的 NodeJS 服务。
    这里使用 POST 与原 astrbot_plugin_music 保持一致。
    """
    base = str(cfg.get("base_url") or DEFAULT_MUSIC_API_BASE).rstrip("/") + "/"
    limit = int(cfg.get("limit") or 3)
    url = f"{base}search"
    data = await _http_request_json(
        url,
        method="POST",
        data={"keywords": keyword, "limit": limit, "type": 1, "offset": 0},
        proxy=proxy,
    )
    if not data:
        return []
    return _parse_nodejs_search_result(data)


async def _search_songs_via_meting(
    cfg: Dict[str, Any],
    keyword: str,
    *,
    proxy: str | None = None,
) -> List[Dict[str, Any]]:
    """
    NodeJS API 不可用时，回退到 qijieya 的 Meting 搜索接口。
    该接口当前可稳定返回中文搜索结果和播放链接。
    """
    limit = int(cfg.get("limit") or 3)
    url = f"https://api.qijieya.cn/meting/?server=netease&type=search&id={quote(keyword)}&limit={limit}"
    data = await _http_request_json(url, proxy=proxy)
    if not isinstance(data, list):
        return []
    parsed: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        song_id = _extract_song_id_from_url(item.get("url")) or _extract_song_id_from_url(item.get("lrc"))
        parsed.append(
            {
                "id": song_id,
                "name": str(item.get("name") or ""),
                "artists": str(item.get("artist") or "未知歌手"),
                "album": "",
                "pic_url": str(item.get("pic") or ""),
                "play_url": str(item.get("url") or ""),
            }
        )
    return [song for song in parsed if song.get("name")]


async def _search_songs_via_official(
    keyword: str,
    *,
    limit: int = 3,
    proxy: str | None = None,
) -> List[Dict[str, Any]]:
    """
    最后回退到网易官方 cloudsearch 接口。
    该接口对中文搜索不算稳定，但至少能在部分环境下作为兜底。
    """
    data = await _http_request_json(
        "https://music.163.com/api/cloudsearch/pc",
        method="POST",
        data={"s": keyword, "limit": limit, "type": 1, "offset": 0},
        proxy=proxy,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        },
        cookies={"appver": "2.0.2"},
    )
    if not data:
        return []
    return _parse_nodejs_search_result(data)


def _parse_nodejs_search_result(data: Dict[str, Any] | List[Any]) -> List[Dict[str, Any]]:
    """
    兼容 NodeJS API / 官方 cloudsearch 的搜索结构。
    """
    if not isinstance(data, dict):
        return []
    result = data.get("result") or {}
    songs = result.get("songs") or []
    if not isinstance(songs, list):
        return []
    parsed: List[Dict[str, Any]] = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        song_id = s.get("id")
        name = s.get("name") or ""
        artists = s.get("ar") or s.get("artists") or []
        if isinstance(artists, list):
            artist_names = [str(a.get("name", "")) for a in artists if isinstance(a, dict)]
        else:
            artist_names = []
        album = s.get("al") or s.get("album") or {}
        pic_url = album.get("picUrl") if isinstance(album, dict) else None
        parsed.append(
            {
                "id": song_id,
                "name": str(name),
                "artists": ", ".join(a for a in artist_names if a) or "未知歌手",
                "album": album.get("name") if isinstance(album, dict) else "",
                "pic_url": pic_url or "",
            }
        )
    return parsed


def _extract_song_id_from_url(url: Any) -> int | None:
    """
    从 Meting 返回的 URL 中提取 song id，例如：
    https://api.qijieya.cn/meting/?server=netease&type=url&id=2104034295
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
        song_id = parse_qs(parsed.query).get("id", [None])[0]
        if song_id is None:
            return None
        return int(song_id)
    except Exception:
        return None


async def _get_song_url(cfg: Dict[str, Any], song_id: Any) -> str | None:
    """
    使用 NeteaseCloudMusicApi 的 /song/url/v1 获取歌曲播放链接。
    优先使用配置的 music_quality，若该档不可用则由服务端自动降级。
    """
    if song_id is None:
        return None

    # 1) 优先尝试 NodeJS NeteaseCloudMusicApi 的 /song/url/v1
    base = str(cfg.get("base_url") or DEFAULT_MUSIC_API_BASE).rstrip("/") + "/"
    level = str(cfg.get("quality") or "exhigh")
    proxy = str(cfg.get("proxy") or "").strip() or None
    url = f"{base}song/url/v1?id={song_id}&level={quote(level)}"
    data = await _http_request_json(url, proxy=proxy)

    if not data:
        # 兼容部分旧版 NodeJS API，仅支持 /song/url
        fallback_url = f"{base}song/url?id={song_id}"
        data = await _http_request_json(fallback_url, proxy=proxy)

    play_url: str | None = None
    if data:
        items = data.get("data") or []
        if isinstance(items, list) and items:
            first = items[0] or {}
            url_field = first.get("url")
            if url_field:
                play_url = str(url_field)

    # 2) 若 NodeJS 未返回可用直链，回退到网易云官方外链形式
    #    大多数公开歌曲均可通过该 URL 访问，少量 VIP 曲目可能仍不可播。
    if not play_url:
        play_url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"

    return play_url


def _format_song_list(songs: List[Dict[str, Any]], keyword: str) -> str:
    """
    将候选歌曲列表格式化为文本。
    """
    if not songs:
        return f"未找到与「{keyword}」相关的歌曲。"
    lines: List[str] = []
    lines.append(f"🎵 为你找到以下与「{keyword}」相关的歌曲：")
    for idx, s in enumerate(songs, start=1):
        name = s.get("name") or ""
        artists = s.get("artists") or ""
        album = s.get("album") or ""
        line = f"{idx}. {name}"
        if artists:
            line += f" - {artists}"
        if album:
            line += f"（{album}）"
        lines.append(line)
    return "\n".join(lines)


async def _build_play_text(cfg: Dict[str, Any], song: Dict[str, Any]) -> str:
    """
    根据单首歌曲构造播放文本，包含基础信息与音频 URL。
    """
    name = song.get("name") or ""
    artists = song.get("artists") or ""
    album = song.get("album") or ""
    song_id = song.get("id")
    play_url = song.get("play_url") or await _get_song_url(cfg, song_id)

    header = "🎶 正在为你播放："
    title = name
    if artists:
        title += f" - {artists}"
    if album:
        title += f"（{album}）"

    lines = [header + title]
    if play_url:
        lines.append("")
        lines.append(f"播放链接：{play_url}")
    else:
        lines.append("")
        lines.append("未能获取稳定的音频直链，请尝试更换平台或稍后重试。")
    return "\n".join(lines)


async def _send_song_with_modes(
    event: AstrMessageEvent,
    cfg: Dict[str, Any],
    song: Dict[str, Any],
) -> None:
    """
    参考 astrbot_plugin_music 的发送策略，根据 send_modes 依次尝试：
    - card：QQ 音乐卡片（仅 Aiocqhttp + 网易云歌曲）；
    - record：语音（暂时回退为文本）；
    - file：文件（暂时回退为文本）；
    - text：文本 + 链接。
    """
    name = song.get("name") or ""
    artists = song.get("artists") or ""
    song_id = song.get("id")
    play_url = song.get("play_url") or await _get_song_url(cfg, song_id)

    # 预构造文本消息（兜底）
    header = "🎶 正在为你播放："
    title = name
    if artists:
        title += f" - {artists}"
    album = song.get("album") or ""
    if album:
        title += f"（{album}）"
    text_lines = [header + title]
    if play_url:
        text_lines.append("")
        text_lines.append(f"播放链接：{play_url}")
    else:
        text_lines.append("")
        text_lines.append("未能获取稳定的音频直链，请尝试更换平台或稍后重试。")
    text_msg = "\n".join(text_lines)

    modes: list[str] = cfg.get("send_modes") or ["card", "text"]
    sent = False

    for mode in modes:
        m = (mode or "").lower()
        # 1) QQ 卡片
        if m == "card" and isinstance(event, AiocqhttpMessageEvent) and song_id:
            payloads: dict = {
                "message": [
                    {
                        "type": "music",
                        "data": {
                            "type": "163",
                            "id": song_id,
                        },
                    }
                ]
            }
            try:
                if event.is_private_chat():
                    payloads["user_id"] = event.get_sender_id()
                    await event.bot.api.call_action("send_private_msg", **payloads)
                else:
                    payloads["group_id"] = event.get_group_id()
                    await event.bot.api.call_action("send_group_msg", **payloads)
                sent = True
                break
            except Exception as e:
                logger.error("[music] 发送 QQ 音乐卡片失败: %s", e)
                continue

        # 2) 语音 / 文件：当前版本简化为发送文本 + 链接
        if m in {"record", "file"}:
            try:
                # 若未来需要真正的语音/文件，可在此扩展 Downloader 逻辑。
                if play_url:
                    seg = Record.fromURL(play_url) if m == "record" else File(
                        name=f"{name or 'song'}.mp3",
                        url=play_url,
                    )
                    await event.send(event.chain_result([seg]))
                    sent = True
                    break
            except Exception as e:
                logger.error("[music] 发送 %s 失败: %s", m, e)
                continue

        # 3) 文本兜底
        if m == "text":
            await event.send(event.plain_result(text_msg))
            sent = True
            break

    if not sent:
        # 所有模式都失败或不支持时，最后再尝试一次纯文本
        await event.send(event.plain_result(text_msg))


def _get_session_id(event: AstrMessageEvent) -> str | None:
    """
    统一获取会话标识，用于在「点歌 → 数字回复」之间关联上下文。
    """
    session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", None)
    if not session_id:
        return None
    return str(session_id)


def _get_active_session(session_id: str) -> List[Dict[str, Any]] | None:
    """
    读取仍在有效期内的点歌会话。
    """
    now = time.time()
    info = _music_sessions.get(session_id)
    if not info:
        return None
    expire_ts, songs = info
    if now > expire_ts:
        _music_sessions.pop(session_id, None)
        return None
    return songs


def _save_session(session_id: str, songs: List[Dict[str, Any]]) -> None:
    """
    保存当前会话的候选歌曲列表，供后续数字选择使用。
    """
    expire_ts = time.time() + SESSION_TTL_SECONDS
    _music_sessions[session_id] = (expire_ts, songs)


async def handle_music_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    点歌命令入口（/点歌 <关键词>）。

    说明：
    - 参考 `astrbot_plugin_music` 的交互体验，先搜索再让用户选择；
    - 如果用户在命令中直接附带了数字序号，则直接按该序号播放；
    - 搜索结果会暂存在会话级缓存中，后续纯数字回复会被 `handle_music_number_selection` 处理。
    """
    cfg = _get_music_config(config)
    msg = event.get_message_str().strip()

    # 形如「/点歌 青花」或「点歌 青花 2」
    parts = msg.split(maxsplit=1)
    if len(parts) < 2:
        await event.send(
            event.plain_result(
                "用法：/点歌 <歌名或关键词>\n示例：/点歌 青花 或 /点歌 夜曲 周杰伦"
            )
        )
        return

    raw_arg = parts[1].strip()
    if not raw_arg:
        await event.send(event.plain_result("请在命令后输入要点的歌曲名称，例如：/点歌 夜曲 周杰伦"))
        return

    # 尝试解析结尾数字作为直接选择序号
    select_index: int | None = None
    tokens = raw_arg.split()
    if tokens and tokens[-1].isdigit():
        try:
            select_index = int(tokens[-1])
        except Exception:
            select_index = None
        else:
            raw_arg = " ".join(tokens[:-1]).strip() or raw_arg

    keyword = raw_arg
    songs = await _search_songs(cfg, keyword)
    if not songs:
        await event.send(event.plain_result(f"搜索【{keyword}】无结果。"))
        return

    session_id = _get_session_id(event)

    # 若用户在命令中已指定序号且合法，直接播放对应歌曲
    if select_index is not None and 1 <= select_index <= len(songs):
        song = songs[select_index - 1]
        await _send_song_with_modes(event, cfg, song)
        return

    # 否则发送候选列表并等待用户在同一会话中回复数字
    if session_id:
        _save_session(session_id, songs)

    text = _format_song_list(songs, keyword)
    text += "\n\n请直接回复要播放的序号，例如：1 或 2。"
    await event.send(event.plain_result(text))


async def handle_music_number_selection(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """
    在存在点歌会话时拦截纯数字回复，按序号播放歌曲。
    """
    session_id = _get_session_id(event)
    if not session_id:
        return

    songs = _get_active_session(session_id)
    if not songs:
        # 没有待选歌曲会话，忽略，让后续处理器继续
        return

    msg = event.get_message_str().strip()
    if not msg.isdigit():
        return

    try:
        index = int(msg)
    except Exception:
        return

    if index < 1 or index > len(songs):
        await event.send(event.plain_result("序号超出范围，请重新输入有效的歌曲序号。"))
        # 不中断事件，方便其他插件按需继续处理
        return

    cfg = _get_music_config(config)
    song = songs[index - 1]
    await _send_song_with_modes(event, cfg, song)

    # 本次选择完成后清理会话，避免后续数字误触发
    _music_sessions.pop(session_id, None)

    # 当前消息已完成预期行为，可阻止后续处理器再次响应
    try:
        event.stop_event()
    except Exception:
        pass


async def llm_play_music_by_keyword(
    ctx: Context,
    config: AstrBotConfig,
    keyword: str,
    event: AstrMessageEvent | None = None,
) -> str:
    """
    LLM Tool 入口：根据关键词自动选歌并返回播放文本。

    行为：
    - 使用与 /点歌 相同的搜索逻辑，只取最匹配的一首；
    - 返回一段纯文本，包含歌曲信息与可播放的 URL，由前端/上游决定如何渲染。
    """
    cfg = _get_music_config(config)
    kw = (keyword or "").strip()
    if not kw:
        return "请提供要点歌的关键词或歌名，例如「青花」或「夜曲 周杰伦」。"

    songs = await _search_songs(cfg, kw)
    if not songs:
        return f"未找到与「{kw}」相关的歌曲，请尝试更换关键词。"

    # 直接选第一首作为最佳匹配
    song = songs[0]

    # 若有事件上下文，优先按 send_modes 发送（可出 QQ node / 语音 / 文件 / 文本）
    if event is not None:
        await _send_song_with_modes(event, cfg, song)
        # 工具返回简短说明文本，供上游 Agent 使用
        name = song.get("name") or ""
        artists = song.get("artists") or ""
        if artists:
            return f"已为你播放：{name} - {artists}"
        return f"已为你播放：{name}"

    # 无事件时，仅返回文本 + 链接
    text = await _build_play_text(cfg, song)
    return text
