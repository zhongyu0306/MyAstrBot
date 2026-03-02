from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Optional, List, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import asyncio
import errno
import json
import os
import random
import shutil

import aiofiles
import aiofiles.os
import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools
from PIL import Image, ImageDraw, ImageFont


_JRYS_PLUGIN = None


def _build_jrys_config(config: AstrBotConfig) -> dict:
    """
    从 astrbot_all_char 配置中提取 jrys_* 字段，构造今日运势内部配置字典。
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


class ResourceManager:
    """
    资源管理器，负责用户头像和背景图片的获取、缓存和管理
    """

    ONE_DAY_IN_SECONDS = 86400

    def __init__(self, plugin_config) -> None:
        self._http_timeout = aiohttp.ClientTimeout(total=5)
        self._connection_limit = aiohttp.TCPConnector(limit=10)
        self._session = aiohttp.ClientSession(
            timeout=self._http_timeout, connector=self._connection_limit
        )
        self.plugin_config = plugin_config

        self.avatar_cache_expiration = self.plugin_config.get(
            "avatar_cache_expiration", self.ONE_DAY_IN_SECONDS
        )

        self.is_data_loaded = False

        self._storage_initialized = False
        self._plugin_data_dir: Optional[Path] = None
        self._background_cache_dir: Optional[Path] = None
        self._background_tmp_dir: Optional[Path] = None
        self._precache_task: Optional[asyncio.Task] = None

        assets_root = Path(__file__).resolve().parent / "jrys_assets"
        self.data_dir = str(assets_root)
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.background_dir = os.path.join(self.data_dir, "backgroundFolder")
        self.font_dir = os.path.join(self.data_dir, "font")

        os.makedirs(self.avatar_dir, exist_ok=True)
        os.makedirs(self.background_dir, exist_ok=True)
        os.makedirs(self.font_dir, exist_ok=True)

        # 供缓存目录使用的插件名
        self.name = "astrbot_all_char_jrys"

        self._http_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/58.0.3029.110 Safari/537.3"
            )
        }

    async def get_background_image(self) -> Optional[Tuple[str, bool]]:
        """
        随机获取背景图片，返回 (图片路径, 是否需要清理)。
        """
        try:
            self._ensure_storage_dirs()

            background_files = await asyncio.to_thread(
                lambda: [f for f in os.listdir(self.background_dir) if f.endswith(".txt")]
            )
            if not background_files:
                logger.warning("今日运势：没有找到背景图片列表文件")
                return None

            background_file = random.choice(background_files)
            background_file_path = os.path.join(self.background_dir, background_file)

            async with aiofiles.open(background_file_path, "r", encoding="utf-8") as f:
                background_urls = [line.strip() async for line in f if line.strip()]

            if not background_urls:
                logger.warning("今日运势：背景列表文件中没有有效 URL")
                return None

            random.shuffle(background_urls)
            max_attempts = min(5, len(background_urls))

            pre_cache_enabled = bool(
                self.plugin_config.get("pre_cache_background_images", False)
            )
            cleanup_downloads = bool(
                self.plugin_config.get("cleanup_background_downloads", True)
            )

            for image_url in background_urls[:max_attempts]:
                if not (image_url.startswith("http://") or image_url.startswith("https://")):
                    continue

                cache_path = self._background_cache_path_for_url(image_url)

                if cache_path.exists():
                    return str(cache_path), False

                image_path = cache_path
                should_cleanup = False
                if (not pre_cache_enabled) and cleanup_downloads:
                    image_path = self._background_tmp_path_for_url(image_url)
                    should_cleanup = True

                ok = await self._download_to_path(image_url, image_path, label="背景图")
                if ok:
                    logger.info(f"今日运势：下载背景图成功: {image_url}")
                    return str(image_path), should_cleanup

            logger.warning(f"今日运势：背景图下载失败，已尝试 {max_attempts} 个 URL")
            return None

        except Exception as e:
            logger.error(f"今日运势：获取背景图片时出错: {e}")
            return None

    async def get_avatar_img(self, user_id: str) -> Optional[str]:
        """
        获取用户头像，返回头像本地路径。
        """
        try:
            self._ensure_storage_dirs()
            avatar_path = os.path.join(self.avatar_dir, f"{user_id}.jpg")

            if await aiofiles.os.path.exists(avatar_path):

                def _file_stat(path: str):
                    try:
                        st = os.stat(path)
                        return st.st_mtime
                    except FileNotFoundError:
                        return None

                file_mtime = await asyncio.to_thread(_file_stat, avatar_path)
                if file_mtime is not None:
                    file_age = datetime.now().timestamp() - file_mtime
                    if file_age < self.avatar_cache_expiration:
                        return avatar_path

            url = f"http://q.qlogo.cn/g?b=qq&nk={user_id}&s=640"

            ok = await self._download_to_path(url, Path(avatar_path), label="头像")
            if ok:
                return avatar_path
            return None

        except Exception as e:
            logger.error(f"今日运势：获取用户头像失败: {e}")
            return None

    async def initialize(self):
        """插件加载/重载后执行（适合做缓存预热等异步任务）。"""
        self._ensure_storage_dirs()

        if self.plugin_config.get("pre_cache_background_images", False):
            self._start_background_precache()

    def _migrate_legacy_cache_dir(self, legacy_dir: Path, target_dir: Path, label: str) -> None:
        """将旧版本缓存目录迁移到标准插件数据目录。"""
        try:
            if not legacy_dir.exists() or not legacy_dir.is_dir():
                return

            legacy_resolved = legacy_dir.resolve()
            target_resolved = target_dir.resolve()
            if legacy_resolved == target_resolved:
                return

            target_dir.mkdir(parents=True, exist_ok=True)

            moved = 0
            skipped = 0
            replaced = 0
            failed = 0

            for item in legacy_dir.iterdir():
                if not item.is_file():
                    continue

                dest = target_dir / item.name
                try:
                    if dest.exists():
                        try:
                            src_stat = item.stat()
                            dest_stat = dest.stat()
                            if src_stat.st_mtime <= dest_stat.st_mtime:
                                item.unlink(missing_ok=True)
                                skipped += 1
                                continue
                        except Exception:
                            item.unlink(missing_ok=True)
                            skipped += 1
                            continue

                        replaced += 1

                    try:
                        os.replace(item, dest)
                    except OSError as e:
                        if e.errno == errno.EXDEV:
                            shutil.copy2(item, dest)
                            item.unlink(missing_ok=True)
                        else:
                            raise

                    moved += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"今日运势：迁移{label}缓存失败: {item} -> {dest} | {e}")

            try:
                if not any(legacy_dir.iterdir()):
                    legacy_dir.rmdir()
            except Exception:
                pass

            if moved or replaced or skipped or failed:
                logger.info(
                    f"今日运势：{label}缓存迁移完成: "
                    f"from={legacy_dir} to={target_dir} "
                    f"moved={moved} replaced={replaced} skipped={skipped} failed={failed}"
                )
        except Exception as e:
            logger.warning(f"今日运势：{label}缓存迁移异常: {e}")

    def _ensure_storage_dirs(self) -> None:
        """初始化插件大文件缓存目录（优先 data/plugin_data/{plugin_name}）。"""
        if self._storage_initialized:
            return

        try:
            plugin_name = getattr(self, "name", None) or "astrbot_all_char_jrys"
            plugin_data_dir = StarTools.get_data_dir(plugin_name)
            plugin_data_dir.mkdir(parents=True, exist_ok=True)

            self._plugin_data_dir = plugin_data_dir

            cache_dir = plugin_data_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            self._background_cache_dir = cache_dir / "background_images"
            self._background_cache_dir.mkdir(parents=True, exist_ok=True)
            self._background_tmp_dir = cache_dir / "background_images_tmp"
            self._background_tmp_dir.mkdir(parents=True, exist_ok=True)

            target_avatar_dir = cache_dir / "avatars"
            self.avatar_dir = str(target_avatar_dir)
            os.makedirs(self.avatar_dir, exist_ok=True)

            legacy_avatar_dirs = [
                Path(self.data_dir) / "avatars",
                plugin_data_dir / "avatars",
            ]
            for legacy_dir in legacy_avatar_dirs:
                self._migrate_legacy_cache_dir(legacy_dir, target_avatar_dir, label="头像")

            legacy_background_dirs = [
                Path(self.background_dir) / "images",
                Path(self.data_dir) / "background_images",
                plugin_data_dir / "background_images",
            ]
            for legacy_dir in legacy_background_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_cache_dir, label="背景图"
                )

            legacy_background_tmp_dirs = [
                Path(self.background_dir) / "images_tmp",
                Path(self.data_dir) / "background_images_tmp",
                plugin_data_dir / "background_images_tmp",
            ]
            for legacy_dir in legacy_background_tmp_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_tmp_dir, label="背景图临时"
                )

            self._storage_initialized = True
            logger.info(f"今日运势：插件数据目录初始化完成: {plugin_data_dir}")
        except Exception as e:
            logger.warning(
                f"今日运势：初始化插件数据目录失败，将回退到插件目录缓存: {e}"
            )
            self._plugin_data_dir = Path(self.data_dir)

            cache_dir = self._plugin_data_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            self._background_cache_dir = cache_dir / "background_images"
            self._background_cache_dir.mkdir(parents=True, exist_ok=True)

            self._background_tmp_dir = cache_dir / "background_images_tmp"
            self._background_tmp_dir.mkdir(parents=True, exist_ok=True)

            target_avatar_dir = cache_dir / "avatars"
            self.avatar_dir = str(target_avatar_dir)
            os.makedirs(self.avatar_dir, exist_ok=True)

            legacy_avatar_dirs = [
                Path(self.data_dir) / "avatars",
            ]
            for legacy_dir in legacy_avatar_dirs:
                self._migrate_legacy_cache_dir(legacy_dir, target_avatar_dir, label="头像")

            legacy_background_dirs = [
                Path(self.background_dir) / "images",
                Path(self.data_dir) / "background_images",
            ]
            for legacy_dir in legacy_background_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_cache_dir, label="背景图"
                )

            legacy_background_tmp_dirs = [
                Path(self.background_dir) / "images_tmp",
                Path(self.data_dir) / "background_images_tmp",
            ]
            for legacy_dir in legacy_background_tmp_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_tmp_dir, label="背景图临时"
                )

            self._storage_initialized = True

    def _start_background_precache(self) -> None:
        """启动后台预缓存任务（不会阻塞插件加载/重载）。"""
        if self._precache_task and not self._precache_task.done():
            return
        self._precache_task = asyncio.create_task(self._pre_cache_background_images())

    def _background_cache_path_for_url(self, url: str) -> Path:
        self._ensure_storage_dirs()
        assert self._background_cache_dir is not None

        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if not ext or len(ext) > 10:
            ext = ".img"
        digest = sha256(url.encode("utf-8")).hexdigest()
        return self._background_cache_dir / f"{digest}{ext}"

    def _background_tmp_path_for_url(self, url: str) -> Path:
        self._ensure_storage_dirs()
        assert self._background_tmp_dir is not None

        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if not ext or len(ext) > 10:
            ext = ".img"
        return self._background_tmp_dir / f"{uuid4().hex}{ext}"

    async def _download_to_path(
        self, url: str, dest: Path, label: str = "图片", retries: int = 1
    ) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        retries = max(0, int(retries))

        for attempt in range(retries + 1):
            status: Optional[int] = None
            reason = ""
            tmp_path = dest.parent / f"{dest.name}.{uuid4().hex}.tmp"

            try:
                async with self._session.get(url, headers=self._http_headers) as response:
                    status = response.status
                    reason = (response.reason or "").strip()

                    if status < 200 or status >= 300:
                        if 500 <= status <= 599 and attempt < retries:
                            logger.warning(
                                f"{label}下载失败({attempt + 1}/{retries + 1}): "
                                f"HTTP {status} {reason} | {url}"
                            )
                            continue

                        logger.error(f"{label}下载失败: HTTP {status} {reason} | {url}")
                        return False

                    async with aiofiles.open(tmp_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            await f.write(chunk)

                await asyncio.to_thread(os.replace, tmp_path, dest)
                return True
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): "
                        f"{http_info}Timeout | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(f"{label}下载失败: {http_info}Timeout | {url}")
            except aiohttp.ClientPayloadError as e:
                msg = str(e).strip()
                if ":" in msg:
                    msg = msg.split(":", 1)[0].strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): "
                        f"{http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            except aiohttp.ClientError as e:
                msg = str(e).strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): "
                        f"{http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            except Exception as e:
                msg = str(e).strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): "
                        f"{http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

        return False

    async def _collect_all_background_urls(self) -> List[str]:
        background_files = await asyncio.to_thread(
            lambda: [f for f in os.listdir(self.background_dir) if f.endswith(".txt")]
        )

        urls: set[str] = set()
        for background_file in background_files:
            background_file_path = os.path.join(self.background_dir, background_file)
            try:
                async with aiofiles.open(background_file_path, "r", encoding="utf-8") as f:
                    async for line in f:
                        url = line.strip()
                        if not url:
                            continue
                        if url.startswith("http://") or url.startswith("https://"):
                            urls.add(url)
            except Exception as e:
                logger.warning(f"今日运势：读取背景图列表失败: {background_file_path} | {e}")

        return sorted(urls)

    async def _pre_cache_background_images(self) -> None:
        self._ensure_storage_dirs()

        urls = await self._collect_all_background_urls()
        total = len(urls)
        if total == 0:
            logger.warning("今日运势：预缓存背景图时未找到任何图片 URL")
            return

        try:
            concurrency = int(self.plugin_config.get("pre_cache_concurrency", 3))
        except Exception:
            concurrency = 3
        concurrency = max(1, min(concurrency, 10))

        already_cached = 0
        to_download: List[Tuple[str, Path]] = []
        for url in urls:
            dest = self._background_cache_path_for_url(url)
            if dest.exists():
                already_cached += 1
            else:
                to_download.append((url, dest))

        logger.info(
            f"今日运势：预缓存背景图开始: total={total}, "
            f"cached={already_cached}, download={len(to_download)}, "
            f"concurrency={concurrency}"
        )

        sem = asyncio.Semaphore(concurrency)

        async def _dl(url: str, dest: Path) -> bool:
            if dest.exists():
                return True
            async with sem:
                if dest.exists():
                    return True
                return await self._download_to_path(url, dest, label="背景图")

        downloaded = 0
        failed = 0
        try:
            results = await asyncio.gather(
                *(_dl(url, dest) for url, dest in to_download),
                return_exceptions=True,
            )
            for r in results:
                if r is True:
                    downloaded += 1
                else:
                    failed += 1
        except asyncio.CancelledError:
            raise

        logger.info(
            f"今日运势：预缓存背景图完成: total={total}, "
            f"cached={already_cached}, downloaded={downloaded}, failed={failed}"
        )

    async def _load_jrys_data(self) -> dict:
        """
        初始化并读取 jrys.json 运势数据。
        """
        if self.is_data_loaded:
            return self.jrys_data

        jrys_path = os.path.join(self.data_dir, "jrys.json")

        if not os.path.exists(jrys_path):
            async with aiofiles.open(jrys_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps({}))
                logger.info(f"今日运势：创建空的运势数据文件: {jrys_path}")

        try:
            async with aiofiles.open(jrys_path, "r", encoding="utf-8") as f:
                content = await f.read()
                self.jrys_data = await asyncio.to_thread(json.loads, content)
                self.is_data_loaded = True
                logger.info(f"今日运势：读取运势数据文件: {jrys_path}")

            return self.jrys_data
        except FileNotFoundError:
            logger.error(f"今日运势：运势数据文件 {jrys_path} 未找到")
            return {}
        except json.JSONDecodeError:
            logger.error(f"今日运势：运势数据文件 {jrys_path} 不是有效的 JSON 格式")
            return {}

    async def _save_jrys_data(self):
        """保存运势数据到 jrys.json。"""
        jrys_path = os.path.join(self.data_dir, "jrys.json")
        try:
            async with aiofiles.open(jrys_path, "w", encoding="utf-8") as f:
                content = await asyncio.to_thread(
                    json.dumps, self.jrys_data, ensure_ascii=False, indent=4
                )
                await f.write(content)
        except Exception as e:
            logger.error(f"今日运势：保存运势数据失败: {e}")


IMAGE_HEIGHT = 1920
IMAGE_WIDTH = 1080
AVATAR_SIZE = (150, 150)
AVATAR_POSITION = (60, 1350)
# 使用英文文件名以避免打包到 Linux 后中文文件名编码导致找不到字体文件
FONT_NAME = "jrys_font.ttf"
TEXT_BOX_Y = 1270
TEXT_BOX_HEIGHT = 700
DATE_Y = 1300
SUMMARY_Y = 1400
LUCKY_STAR_Y = 1500
SIGN_TEXT_Y = 1600
UNSIGN_TEXT_Y = 1700
WARNING_TEXT_Y = 1850
WARNING_TEXT_Y_OFFSET = 10
UNSIGN_TEXT_Y_OFFSET = 15
TEXT_WRAP_WIDTH = 1000
LEFT_PADDING = 20


class FortunePainter:
    """
    今日运势海报生成器，负责根据用户头像和背景图生成今日运势海报图片
    """

    def __init__(self, plugin_config) -> None:
        self.plugin_config = plugin_config

        self.font_name = self.plugin_config.get("font_name", FONT_NAME)

        self.image_width = self.plugin_config.get("img_width", IMAGE_WIDTH)
        self.image_height = self.plugin_config.get("img_height", IMAGE_HEIGHT)

        avatar_position_list = self.plugin_config.get(
            "avatar_position", list(AVATAR_POSITION)
        )
        self.avatar_position = tuple(avatar_position_list)

        avatar_size_list = self.plugin_config.get("avatar_size", list(AVATAR_SIZE))
        self.avatar_size = tuple(avatar_size_list)

        self.date_y = self.plugin_config.get("date_y_position", DATE_Y)
        self.summary_y = self.plugin_config.get("summary_y_position", SUMMARY_Y)
        self.lucky_star_y = self.plugin_config.get(
            "lucky_star_y_position", LUCKY_STAR_Y
        )
        self.sign_text_y = self.plugin_config.get("sign_text_y_position", SIGN_TEXT_Y)
        self.unsign_text_y = self.plugin_config.get(
            "unsign_text_y_position", UNSIGN_TEXT_Y
        )
        self.warning_text_y = self.plugin_config.get(
            "warning_text_y_position", WARNING_TEXT_Y
        )

        assets_root = Path(__file__).resolve().parent / "jrys_assets"
        self.data_dir = str(assets_root)
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.background_dir = os.path.join(self.data_dir, "backgroundFolder")
        self.font_dir = os.path.join(self.data_dir, "font")
        self.font_path = os.path.join(self.font_dir, self.font_name)

        os.makedirs(self.avatar_dir, exist_ok=True)
        os.makedirs(self.background_dir, exist_ok=True)
        os.makedirs(self.font_dir, exist_ok=True)

        self.jrys_keyword_enabled = self.plugin_config.get("jrys_keyword_enabled", True)
        self.holiday_rates_enabled = self.plugin_config.get(
            "holiday_rates_enabled", True
        )
        self.fixed_daily_fortune = self.plugin_config.get("fixed_daily_fortune", True)

        self.holidays = self.plugin_config.get(
            "holidays", ["01-01", "02-14", "05-01", "10-01", "12-25"]
        )

        self.normal_rates = self.plugin_config.get(
            "normal_rates", {"good": 40, "normal": 40, "bad": 20}
        )

        self.holiday_rates = self.plugin_config.get(
            "holiday_rates", {"good": 85, "normal": 15, "bad": 0}
        )

        self.fonts = {}
        font_sizes = [50, 60, 36, 30]
        try:
            for size in font_sizes:
                self.fonts[size] = ImageFont.truetype(self.font_path, size)
        except Exception:
            logger.error(f"今日运势：无法加载字体文件 {self.font_path}, 使用默认字体回退")
            default_font = ImageFont.load_default()
            for size in font_sizes:
                self.fonts[size] = default_font

    def generate_image_sync(
        self, user_id: str, avatar_path: str, background_path: str, jrys_data: dict
    ) -> Optional[str]:
        if not jrys_data:
            logger.error("今日运势：运势数据为空")
            return None

        date_y = self.date_y
        summary_y = self.summary_y
        lucky_star_y = self.lucky_star_y
        sign_text_y = self.sign_text_y
        unsign_text_y = self.unsign_text_y
        warning_text_y = self.warning_text_y

        try:
            rng = random.Random()
            if self.fixed_daily_fortune:
                today_str = datetime.now().strftime("%Y-%m-%d")
                seed = f"{user_id}-{today_str}"
                rng.seed(seed)

            valid_keys_list = [k for k in jrys_data.keys() if not k.startswith("_")]

            today_md = datetime.now().strftime("%m-%d")
            if self.holiday_rates_enabled and today_md in self.holidays:
                current_rates = self.holiday_rates
                logger.info(f"今日运势：触发节假日爆率配置，日期: {today_md}")
            else:
                current_rates = self.normal_rates

            good_keys = [k for k in valid_keys_list if int(k) > 70]
            normal_keys = [k for k in valid_keys_list if 56 <= int(k) <= 70]
            bad_keys = [k for k in valid_keys_list if int(k) < 56]

            weights = []
            for k in valid_keys_list:
                val = int(k)
                if val > 70:
                    weights.append(
                        current_rates.get("good", 40) / max(len(good_keys), 1)
                    )
                elif val >= 56:
                    weights.append(
                        current_rates.get("normal", 40) / max(len(normal_keys), 1)
                    )
                else:
                    weights.append(
                        current_rates.get("bad", 20) / max(len(bad_keys), 1)
                    )

            if sum(weights) <= 0:
                weights = [1] * len(valid_keys_list)

            key_1 = rng.choices(valid_keys_list, weights=weights, k=1)[0]
            logger.info(f"今日运势：选择运势一级键: {key_1}")

            if key_1 not in jrys_data:
                logger.error(f"今日运势：运势数据中没有找到 {key_1} 的数据")
                return None

            key_2 = rng.choice(list(range(len(jrys_data[key_1]))))
            fortune_data = jrys_data[key_1][key_2]

            now = datetime.now()
            date = f"{now.strftime('%Y/%m/%d')}"

            fortune_summary = fortune_data.get("fortuneSummary", "运势数据未知")
            lucky_star = fortune_data.get("luckyStar", "幸运星未知")
            sign_text = fortune_data.get("signText", "星座运势未知")
            unsign_text = fortune_data.get("unsignText", "非星座运势未知")
            warning_text = "仅供娱乐 | 相信科学 | 请勿迷信"

            unsign_lines = self.wrap_text(
                unsign_text, font=self.fonts[36], max_width=TEXT_WRAP_WIDTH
            )

            if len(unsign_lines) > 3:
                warning_text_y += (len(unsign_lines) - 3) * WARNING_TEXT_Y_OFFSET
                unsign_text_y -= (len(unsign_lines) - 3) * UNSIGN_TEXT_Y_OFFSET

            image = self.crop_center(background_path)
            if image is None:
                logger.error("今日运势：裁剪背景图片失败")
                return None

            image = self.add_transparent_layer(
                image, position=(0, TEXT_BOX_Y), box_width=IMAGE_WIDTH, box_height=TEXT_BOX_HEIGHT
            )

            image = self.draw_text(
                image,
                text=date,
                position="center",
                y=date_y,
                color=(255, 255, 255),
                font=self.fonts[50],
                gradients=True,
            )
            image = self.draw_text(
                image,
                text=fortune_summary,
                position="center",
                y=summary_y,
                color=(255, 255, 255),
                font=self.fonts[60],
            )
            image = self.draw_text(
                image,
                text=lucky_star,
                position="center",
                y=lucky_star_y,
                color=(255, 255, 255),
                font=self.fonts[60],
                gradients=True,
            )
            image = self.draw_text(
                image,
                text=sign_text,
                position="left",
                y=sign_text_y,
                color=(255, 255, 255),
                font=self.fonts[30],
            )
            image = self.draw_text(
                image,
                text=unsign_text,
                position="left",
                y=unsign_text_y,
                color=(255, 255, 255),
                font=self.fonts[30],
            )
            image = self.draw_text(
                image,
                text=warning_text,
                position="center",
                y=warning_text_y,
                color=(255, 255, 255),
                font=self.fonts[30],
            )

            image = self.draw_avatar_img(avatar_path, image)

            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_file:
                image = image.convert("RGB")
                image.save(temp_file, format="JPEG", quality=85, optimize=True)
                return temp_file.name

        except Exception as e:
            logger.error(f"今日运势：生成图片时出错: {e}")
            return None

    def draw_text(
        self,
        img: Image.Image,
        text: str,
        position: str | tuple[int, int],
        font: ImageFont.ImageFont,
        y: Optional[int] = None,
        color: Tuple[int, int, int] = (255, 255, 255),
        max_width: int = 800,
        gradients: bool = False,
    ) -> Image.Image:
        try:
            draw = ImageDraw.Draw(img)

            lines = self.wrap_text(
                text=text,
                font=font,
                draw=draw,
                max_width=TEXT_WRAP_WIDTH,
            )

            img_width, img_height = img.size

            if isinstance(position, str):
                if position == "center":

                    def x_func(line: str) -> int:
                        bbox = draw.textbbox((0, 0), line, font=font)
                        line_width = bbox[2] - bbox[0]
                        return (img_width - line_width) // 2

                    def offset_x_func(line: str) -> int:
                        bbox = draw.textbbox((0, 0), line, font=font)
                        return -bbox[0]

                elif position == "left":

                    def x_func(line: str) -> int:
                        return LEFT_PADDING

                    def offset_x_func(line: str) -> int:
                        return 0

                else:
                    raise ValueError("position 只能为 'left' 或 'center' 或坐标元组")

                text_y = y if y is not None else 0
            else:
                text_x, text_y = position

                def x_func(line: str) -> int:
                    return text_x

                def offset_x_func(line: str) -> int:
                    return 0

            line_spacing = int(font.size * 1.5)
            for line in lines:
                if gradients:
                    base_x = x_func(line)
                    offset_x = offset_x_func(line)
                    for char in line:
                        colors = self.get_light_color()
                        gradient_char = self.create_gradients_image(char, font, colors)
                        img.paste(
                            gradient_char, (base_x + offset_x, text_y), gradient_char
                        )

                        bbox = font.getbbox(char)
                        char_width = bbox[2] - bbox[0]
                        base_x += char_width
                        offset_x += bbox[0]
                else:
                    offset_x = offset_x_func(line)
                    draw.text(
                        (x_func(line) + offset_x, text_y), line, font=font, fill=color
                    )

                text_y += line_spacing

            return img
        except Exception as e:
            logger.error(f"今日运势：绘制文字时出错: {e}")
            return img

    def crop_center(
        self, image_path: str, width: Optional[int] = None, height: Optional[int] = None
    ) -> Optional[Image.Image]:
        width = width if width is not None else self.image_width
        height = height if height is not None else self.image_height
        try:
            img = Image.open(image_path).convert("RGBA")
            img_width, img_height = img.size

            if img_width < width or img_height < height:
                scale_x = width / img_width
                scale_y = height / img_height
                scale = max(scale_x, scale_y)
                new_width = int(img_width * scale)
                new_height = int(img_height * scale)
                img = img.resize((new_width, new_height), Image.LANCZOS)
            else:
                max_scale = 1.8
                if img_width > width * max_scale or img_height > height * max_scale:
                    scale_x = (width * max_scale) / img_width
                    scale_y = (height * max_scale) / img_height
                    scale = min(scale_x, scale_y)
                    new_width = int(img_width * scale)
                    new_height = int(img_height * scale)
                    img = img.resize((new_width, new_height), Image.LANCZOS)

            img_width, img_height = img.size

            left = (img_width - width) / 2
            top = (img_height - height) / 2
            right = (img_width + width) / 2
            bottom = (img_height + height) / 2

            cropped_img = img.crop((left, top, right, bottom))

            return cropped_img
        except FileNotFoundError:
            logger.error(f"今日运势：找不到图片文件：{image_path}")
        except Exception as e:
            logger.error(f"今日运势：裁剪图片时出错：{e}")
        return None

    def add_transparent_layer(
        self,
        base_img: Image.Image,
        box_width: int = 800,
        box_height: int = 400,
        position: Tuple[int, int] = (100, 200),
        layer_color: Tuple[int, int, int, int] = (0, 0, 0, 128),
        radius: int = 50,
    ) -> Image.Image:
        try:
            x1, y1 = position
            x2 = x1 + box_width
            y2 = y1 + box_height

            overlay = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)

            draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=layer_color)

            return Image.alpha_composite(base_img, overlay)
        except Exception as e:
            logger.error(f"今日运势：添加半透明图层时出错: {e}")
            return base_img

    def wrap_text(
        self,
        text: str,
        font: ImageFont.ImageFont,
        draw: Optional[ImageDraw.ImageDraw] = None,
        max_width: int = TEXT_WRAP_WIDTH,
    ) -> List[str]:
        try:
            if draw is None:
                img = Image.new("RGB", (self.image_width, self.image_height))
                draw = ImageDraw.Draw(img)

            lines: List[str] = []
            current_line = ""
            for char in text:
                test_line = current_line + char
                bbox = draw.textbbox((0, 0), test_line, font=font)
                width = bbox[2] - bbox[0]
                if width <= max_width:
                    current_line = test_line
                else:
                    lines.append(current_line)
                    current_line = char
            if current_line:
                lines.append(current_line)
            return lines
        except Exception as e:
            logger.error(f"今日运势：文本换行时出错: {e}")
            return [text]

    def create_gradients_image(
        self, char: str, font: ImageFont.ImageFont, colors: List[Tuple[int, int, int]]
    ) -> Image.Image:
        try:
            bbox = font.getbbox(char)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            if width <= 0 or height <= 0:
                width, height = font.size, font.size
                offset_x, offset_y = 0, 0
            else:
                offset_x = -bbox[0]
                offset_y = -bbox[1]

            gradient = Image.new("RGBA", (width, height), color=0)
            draw = ImageDraw.Draw(gradient)

            mask = Image.new("L", (width, height), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.text((offset_x, offset_y), char, font=font, fill=255)

            num_colors = len(colors)
            if num_colors < 2:
                raise ValueError("至少需要两个颜色进行渐变")

            segement_width = width / (num_colors - 1)
            for i in range(num_colors - 1):
                start_color = colors[i]
                end_color = colors[i + 1]
                start_x = int(i * segement_width)
                end_x = int((i + 1) * segement_width)

                for x in range(start_x, end_x):
                    factor = (x - start_x) / segement_width
                    color = tuple(
                        [
                            int(
                                start_color[j]
                                + (end_color[j] - start_color[j]) * factor
                            )
                            for j in range(3)
                        ]
                    )
                    draw.line([(x, 0), (x, height)], fill=color)

            gradient.putalpha(mask)

            return gradient
        except Exception as e:
            logger.error(f"今日运势：创建渐变字体图像时出错: {e}")
            img = Image.new("RGBA", (max(1, font.size), max(1, font.size)), (255, 255, 255, 0))
            draw = ImageDraw.Draw(img)
            draw.text((0, 0), char, font=font, fill=(255, 255, 255))
            return img

    def get_light_color(self) -> List[Tuple[int, int, int]]:
        light_colors = [
            (255, 250, 205),
            (173, 216, 230),
            (221, 160, 221),
            (255, 182, 193),
            (240, 230, 140),
            (224, 255, 255),
            (245, 245, 220),
            (230, 230, 250),
        ]
        return random.choices(light_colors, k=4)

    def draw_avatar_img(self, avatar_path: str, img: Image.Image) -> Image.Image:
        try:
            avatar = Image.open(avatar_path).convert("RGBA")
            avatar = avatar.resize(self.avatar_size, Image.LANCZOS)

            mask = Image.new("L", avatar.size, 0)
            mask_draw = ImageDraw.Draw(mask)

            mask_draw.ellipse((0, 0, avatar.size[0], avatar.size[1]), fill=255)

            avatar.putalpha(mask)

            img.paste(avatar, self.avatar_position, avatar)

            return img
        except Exception as e:
            logger.error(f"今日运势：绘制头像时出错: {e}")
            return img


class JrysPlugin:
    """今日运势逻辑实现（作为 astrbot_all_char 的内部工具类使用）。"""

    def __init__(self, context: Context, config: dict):
        self.context = context
        self.config = config
        self.resources = ResourceManager(self.config)
        self.painter = FortunePainter(self.config)
        self.jrys_keyword_enabled = self.config.get("jrys_keyword_enabled", True)

    async def jrys_command_handler(self, event: AstrMessageEvent):
        """处理 /jrys, /今日运势, /运势 指令。"""
        logger.info("今日运势：指令处理器被触发")

        setattr(event, "_jrys_processed", True)

        async for result in self.jrys(event):
            yield result

    async def jrys_last_command_handler(self, event: AstrMessageEvent):
        """处理 /jrys_last 指令，发送上一次生成的原图。"""
        import os as _os

        user_id = event.get_sender_id()
        self.jrys_data = await self.resources._load_jrys_data()
        user_last_images = self.jrys_data.get("_user_last_images", {})
        if user_id not in user_last_images:
            yield event.plain_result("你还没有生成过今日运势哦，先发送 jrys 生成一张吧！")
            return

        last_info = user_last_images[user_id]
        path = last_info.get("path")

        if not path or not _os.path.exists(path):
            yield event.plain_result("找不到上一次生成的原图了，可能已被清理，请重新生成～")
            return

        yield event.image_result(path)

    async def jrys(self, event: AstrMessageEvent):
        """
        生成今日运势海报。
        """
        import os as _os

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        self.jrys_data = await self.resources._load_jrys_data()

        logger.info(f"今日运势：正在为用户 {user_name}({user_id}) 生成今日运势")

        background_path: Optional[str] = None
        background_should_cleanup = False

        try:
            results = await asyncio.gather(
                self.resources.get_avatar_img(user_id),
                self.resources.get_background_image(),
                return_exceptions=True,
            )

            avatar_path, background_result = results

            if isinstance(background_result, Exception):
                logger.error(f"今日运势：获取背景图片时出错: {background_result}")
                yield event.plain_result("获取背景图片失败，请稍后再试～")
                return

            if background_result is None:
                logger.error("今日运势：获取背景图片失败，返回为空")
                yield event.plain_result("获取背景图片失败，请稍后再试～")
                return

            background_path, background_should_cleanup = background_result

            if isinstance(avatar_path, Exception):
                logger.error(f"今日运势：获取头像时出错: {avatar_path}")
                yield event.plain_result("获取头像失败，请稍后再试～")
                if (
                    background_should_cleanup
                    and background_path
                    and _os.path.exists(background_path)
                ):
                    try:
                        await aiofiles.os.remove(background_path)
                    except Exception:
                        pass
                return

        except Exception as e:
            logger.error(f"今日运势：获取头像或背景图片时出错: {e}")
            yield event.plain_result("获取头像或背景图片失败，请稍后再试～")
            return

        temp_file_path: Optional[str] = None

        try:
            logger.info(f"今日运势：正在为用户 {user_name}({user_id}) 生成今日运势图片")
            temp_file_path = await asyncio.to_thread(
                self.painter.generate_image_sync,
                user_id,
                avatar_path,  # type: ignore[arg-type]
                background_path,
                self.jrys_data,
            )

            if temp_file_path is None:
                logger.error("今日运势：生成今日运势图片失败")
                yield event.plain_result("生成图片失败，请稍后再试～")
                return

            yield event.image_result(temp_file_path)
            logger.info(f"今日运势：成功为用户 {user_name}({user_id}) 生成今日运势图片")

            if "_user_last_images" not in self.jrys_data:
                self.jrys_data["_user_last_images"] = {}

            user_last_images = self.jrys_data["_user_last_images"]
            if user_id in user_last_images:
                old_info = user_last_images[user_id]
                old_path = old_info.get("path")
                if (
                    old_info.get("should_cleanup")
                    and old_path
                    and old_path != background_path
                    and _os.path.exists(old_path)
                ):
                    try:
                        await aiofiles.os.remove(old_path)
                    except Exception:
                        pass

            user_last_images[user_id] = {
                "path": background_path,
                "should_cleanup": background_should_cleanup,
            }
            await self.resources._save_jrys_data()

            background_should_cleanup = False

        except Exception as e:
            logger.error(f"今日运势：生成运势图片过程中出错: {e}")
            yield event.plain_result("生成图片失败，请稍后再试～")
        finally:
            import os as _os

            if temp_file_path and _os.path.exists(temp_file_path):
                try:
                    await aiofiles.os.remove(temp_file_path)
                    logger.info("今日运势：成功删除临时文件")
                except OSError as e:
                    logger.warning(f"今日运势：删除临时文件 {temp_file_path} 失败: {e}")
                except FileNotFoundError:
                    logger.warning(
                        f"今日运势：临时文件 {temp_file_path} 已经被删除或不存在"
                    )
                except Exception as e:
                    logger.warning(f"今日运势：删除临时文件 {temp_file_path} 失败: {e}")

            if (
                background_should_cleanup
                and background_path
                and _os.path.exists(background_path)
            ):
                try:
                    await aiofiles.os.remove(background_path)
                except Exception:
                    pass

    async def terminate(self):
        """插件终止时的清理工作。"""
        if self.resources._precache_task and not self.resources._precache_task.done():
            self.resources._precache_task.cancel()
            try:
                await self.resources._precache_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"今日运势：预缓存任务清理失败: {e}")

        if self.resources._session:
            await self.resources._session.close()
            logger.info("今日运势：HTTP 会话已关闭")

        logger.info("今日运势：内部实现已终止")


def _get_jrys_plugin(context: Context, all_char_config: AstrBotConfig) -> JrysPlugin:
    """
    初始化并缓存今日运势内部实现实例。
    """
    global _JRYS_PLUGIN
    if _JRYS_PLUGIN is not None:
        return _JRYS_PLUGIN

    jrys_conf = _build_jrys_config(all_char_config)
    plugin = JrysPlugin(context, jrys_conf)
    _JRYS_PLUGIN = plugin
    logger.info("已在 astrbot_all_char 中初始化内置今日运势实现（使用 jrys_assets 资源目录）")
    return _JRYS_PLUGIN


async def handle_jrys_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """处理 /jrys /今日运势 /运势 指令。"""
    try:
        plugin = _get_jrys_plugin(context, config)
    except Exception as e:
        logger.error("今日运势：初始化内部实现失败: %s", e)
        yield event.plain_result("🔮 今日运势核心加载失败，请检查 jrys_assets 资源目录与配置。")
        return

    async for r in plugin.jrys_command_handler(event):
        yield r


async def handle_jrys_last_command(event: AstrMessageEvent, context: Context, config: AstrBotConfig):
    """处理 /jrys_last 指令。"""
    try:
        plugin = _get_jrys_plugin(context, config)
    except Exception as e:
        logger.error("今日运势：初始化内部实现失败: %s", e)
        yield event.plain_result("🔮 今日运势核心加载失败，请检查 jrys_assets 资源目录与配置。")
        return

    async for r in plugin.jrys_last_command_handler(event):
        yield r

