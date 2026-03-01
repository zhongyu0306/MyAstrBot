# astrbot_all_char 配置兼容层
# _conf_schema.json 按模块分组（train / sy / stock / weather / epic / jrys）后，
# 若框架以嵌套形式存储配置，本模块提供扁平化视图，保证各 utils 中 getattr(config, "xxx", default) 仍可用。

from typing import Any

from astrbot.api import AstrBotConfig

_MODULE_KEYS = ("train", "sy", "stock", "weather", "epic", "jrys", "ocr", "qianfan_search")
_SENTINEL = object()


def _get_nested(cfg: Any, group: str, key: str) -> Any:
    """从嵌套 config 中读取 group.key，支持对象或 dict。键不存在时返回 _SENTINEL。"""
    if isinstance(cfg, dict):
        g = cfg.get(group, _SENTINEL)
    elif hasattr(cfg, "__dict__") or hasattr(cfg, "__getitem__"):
        try:
            g = getattr(cfg, group, _SENTINEL) if not isinstance(cfg, dict) else cfg.get(group, _SENTINEL)
        except Exception:
            g = _SENTINEL
    else:
        g = _SENTINEL
    if g is _SENTINEL or g is None:
        return _SENTINEL
    if isinstance(g, dict) and key in g:
        return g[key]
    if hasattr(g, key):
        return getattr(g, key)
    # 兼容 schema 的 items 结构：部分框架存为 group -> { "items": { key: value } }
    if isinstance(g, dict) and "items" in g:
        items = g.get("items")
        if isinstance(items, dict) and key in items:
            v = items[key]
            # 部分框架存为 { key: { "value": "实际值" } }
            if isinstance(v, dict) and "value" in v:
                return v["value"]
            return v
        if hasattr(items, key):
            return getattr(items, key)
    return _SENTINEL


def ensure_flat_config(config: AstrBotConfig) -> AstrBotConfig:
    """
    若 config 已是扁平结构（如 config.train_api_url 存在），直接返回原 config；
    若为嵌套结构或 dict，返回扁平化代理，使各 utils 用 getattr(config, "xxx") 即可读到配置。
    """
    # 若顶层已有 train_api_url，认为已是扁平结构
    if hasattr(config, "train_api_url") and getattr(config, "train_api_url", _SENTINEL) is not _SENTINEL:
        return config

    class _FlatConfig:
        def __getattr__(self, name: str) -> Any:
            for group in _MODULE_KEYS:
                val = _get_nested(config, group, name)
                if val is not _SENTINEL:
                    return val
            # 兼容 dict：顶层键或嵌套在 group 下（与 gitee_aiimg 的 config.get 一致）
            if isinstance(config, dict):
                if name in config:
                    return config[name]
                for group in _MODULE_KEYS:
                    g = config.get(group)
                    if isinstance(g, dict) and name in g:
                        return g[name]
                return config.get(name)
            try:
                return getattr(config, name)
            except AttributeError:
                raise

    return _FlatConfig()  # type: ignore[return-value]
