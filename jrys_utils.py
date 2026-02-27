from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context


_JRYS_PLUGIN = None


def _build_jrys_config(config: AstrBotConfig) -> dict:
    """
    从 astrbot_all_char 配置中提取 jrys_* 字段，构造今日运势插件期望的配置字典。
    """
    return {
        "jrys_keyword_enabled": getattr(config, "jrys_keyword_enabled", True),
        "holiday_rates_enabled": getattr(config, "jrys_holiday_rates_enabled", True),
        "fixed_daily_fortune": getattr(config, "jrys_fixed_daily_fortune", True),
        "holidays": getattr(
            config,
            "jrys_holidays",
            ["01-01", "02-14", "05-01", "10-01", "12-25"],
        ),
        "avatar_cache_expiration": getattr(config, "jrys_avatar_cache_expiration", 86400),
        "pre_cache_background_images": getattr(config, "jrys_pre_cache_background_images", False),
        "cleanup_background_downloads": getattr(config, "jrys_cleanup_background_downloads", True),
    }


def _get_jrys_plugin(context: Context, all_char_config: AstrBotConfig):
    """
    动态从原 `astrbot_plugin_jrys-main` 加载 JrysPlugin，并用映射后的配置初始化一个单例。

    仅作为逻辑库使用，命令入口由 `astrbot_all_char` 统一接管。
    """
    global _JRYS_PLUGIN
    if _JRYS_PLUGIN is not None:
        return _JRYS_PLUGIN

    plugin_main = Path(__file__).resolve().parent.parent / "astrbot_plugin_jrys-main" / "main.py"
    if not plugin_main.is_file():
        logger.error("未找到原今日运势插件目录：%s", plugin_main)
        raise RuntimeError("今日运势核心代码缺失，请保留 astrbot_plugin_jrys-main 目录或后续再完全迁移代码。")

    spec = importlib.util.spec_from_file_location("all_char_jrys", str(plugin_main))
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 jrys 模块 spec。")
    module = importlib.util.module_from_spec(spec)
    sys.modules["all_char_jrys"] = module
    spec.loader.exec_module(module)  # type: ignore[arg-type]

    from all_char_jrys import JrysPlugin  # type: ignore[import]

    jrys_conf = _build_jrys_config(all_char_config)
    plugin = JrysPlugin(context, jrys_conf)  # type: ignore[arg-type]

    # 重定向资源目录到 astrbot_all_char/jrys_assets，避免依赖原插件目录打包
    assets_root = Path(__file__).resolve().parent / "jrys_assets"
    (assets_root / "avatars").mkdir(parents=True, exist_ok=True)
    (assets_root / "backgroundFolder").mkdir(parents=True, exist_ok=True)
    (assets_root / "font").mkdir(parents=True, exist_ok=True)

    try:
        # ResourceManager 路径重定向
        res = plugin.resources
        res.data_dir = str(assets_root)
        res.avatar_dir = str(assets_root / "avatars")
        res.background_dir = str(assets_root / "backgroundFolder")
        res.font_dir = str(assets_root / "font")
    except Exception as e:
        logger.warning("重定向今日运势资源目录失败（ResourceManager）：%s", e)

    try:
        # FortunePainter 路径重定向
        painter = plugin.painter
        painter.data_dir = str(assets_root)
        painter.avatar_dir = str(assets_root / "avatars")
        painter.background_dir = str(assets_root / "backgroundFolder")
        painter.font_dir = str(assets_root / "font")
        painter.font_path = str((assets_root / "font" / painter.font_name))
    except Exception as e:
        logger.warning("重定向今日运势资源目录失败（FortunePainter）：%s", e)

    _JRYS_PLUGIN = plugin
    logger.info("已在 astrbot_all_char 中初始化今日运势插件，并重定向资源到 jrys_assets")
    return _JRYS_PLUGIN


async def handle_jrys_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """处理 /jrys /今日运势 /运势 指令。"""
    try:
        plugin = _get_jrys_plugin(context, config)
    except Exception as e:
        logger.error("初始化今日运势插件失败: %s", e)
        yield event.plain_result("🔮 今日运势核心加载失败，请检查 astrbot_plugin_jrys-main 目录是否存在。")
        return

    async for r in plugin.jrys_command_handler(event):
        yield r


async def handle_jrys_last_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """处理 /jrys_last 指令。"""
    try:
        plugin = _get_jrys_plugin(context, config)
    except Exception as e:
        logger.error("初始化今日运势插件失败: %s", e)
        yield event.plain_result("🔮 今日运势核心加载失败，请检查 astrbot_plugin_jrys-main 目录是否存在。")
        return

    async for r in plugin.jrys_last_command_handler(event):
        yield r

