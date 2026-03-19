from typing import Any

from astrbot.api import AstrBotConfig

_MODULE_KEYS = (
    "train",
    "sy",
    "stock",
    "fund",
    "weather",
    "epic",
    "jrys",
    "ocr",
    "qianfan_search",
    "animetrace",
    "music",
    "email",
)
_SENTINEL = object()


def _get_nested(cfg: Any, group: str, key: str) -> Any:
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
    if isinstance(g, dict) and "items" in g:
        items = g.get("items")
        if isinstance(items, dict) and key in items:
            v = items[key]
            if isinstance(v, dict) and "value" in v:
                return v["value"]
            return v
        if hasattr(items, key):
            return getattr(items, key)
    return _SENTINEL


def ensure_flat_config(config: AstrBotConfig) -> AstrBotConfig:
    class _FlatConfig:
        def __getattr__(self, name: str) -> Any:
            if isinstance(config, dict):
                if name in config:
                    return config[name]
            else:
                direct = getattr(config, name, _SENTINEL)
                if direct is not _SENTINEL:
                    return direct

            for group in _MODULE_KEYS:
                val = _get_nested(config, group, name)
                if val is not _SENTINEL:
                    return val

            if isinstance(config, dict):
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
