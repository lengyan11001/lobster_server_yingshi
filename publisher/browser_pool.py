"""Playwright browser context pool — persistent sessions per account.

Each account gets its own user data directory so cookies/localStorage persist.
The pool lazily starts the Playwright instance on first use.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_pw_instance: Any = None
_browser: Any = None
_lock = asyncio.Lock()
_contexts: Dict[str, Any] = {}

_BASE_DIR = Path(__file__).resolve().parent.parent
_CHROMIUM_PATH = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")


async def _ensure_browser() -> Any:
    global _pw_instance, _browser
    async with _lock:
        if _browser and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright 未安装。请运行: pip install playwright && python -m playwright install chromium"
            )
        _pw_instance = await async_playwright().__aenter__()

        launch_kwargs: Dict[str, Any] = {"headless": False}
        if _CHROMIUM_PATH and Path(_CHROMIUM_PATH).exists():
            launch_kwargs["executable_path"] = _CHROMIUM_PATH

        _browser = await _pw_instance.chromium.launch(**launch_kwargs)
        logger.info("Playwright Chromium launched (headless=False)")
        return _browser


async def _acquire_context(profile_dir: str) -> Tuple[Any, bool]:
    """Get (or reuse) a persistent browser context for the given profile directory.

    Returns (context, created_new). If created_new is False, caller MUST NOT close it.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright 未安装")

    global _pw_instance
    async with _lock:
        if not _pw_instance:
            _pw_instance = await async_playwright().__aenter__()

        existing = _contexts.get(profile_dir)
        if existing:
            try:
                if hasattr(existing, "is_closed") and existing.is_closed():
                    _contexts.pop(profile_dir, None)
                else:
                    return existing, False
            except Exception:
                return existing, False

    launch_kwargs: Dict[str, Any] = {
        "headless": False,
        "viewport": {"width": 1280, "height": 800},
        "locale": "zh-CN",
        "permissions": ["geolocation"],
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if _CHROMIUM_PATH and Path(_CHROMIUM_PATH).exists():
        launch_kwargs["executable_path"] = _CHROMIUM_PATH

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    ctx = await _pw_instance.chromium.launch_persistent_context(
        profile_dir, **launch_kwargs,
    )
    async with _lock:
        _contexts[profile_dir] = ctx
    return ctx, True


def _setup_auto_close(ctx: Any, profile_dir: str, page: Any):
    """Register page close handler to release context when user closes window."""
    async def _close_ctx():
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            async with _lock:
                if _contexts.get(profile_dir) is ctx:
                    _contexts.pop(profile_dir, None)
        except Exception:
            pass
    try:
        page.on("close", lambda: asyncio.create_task(_close_ctx()))
    except Exception:
        pass


async def _get_page_and_focus(ctx: Any) -> Any:
    """Get first page (or create one) and bring to front."""
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await _bring_window_to_front(page)
    return page


async def _bring_window_to_front(page: Any) -> None:
    """Aggressively bring the browser window to OS foreground (Windows-friendly)."""
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            target = await cdp.send("Browser.getWindowForTarget")
            wid = target.get("windowId")
            if wid:
                await cdp.send("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"windowState": "normal"},
                })
                await cdp.send("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"windowState": "maximized"},
                })
        finally:
            await cdp.detach()
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────


async def open_login_browser(
    profile_dir: str,
    login_url: str,
    platform: str,
    timeout_sec: int = 120,
) -> Dict[str, Any]:
    """Open browser for user to scan QR code. Returns immediately."""
    from .drivers import DRIVERS

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        return {"logged_in": False, "message": f"不支持的平台: {platform}"}

    ctx, created_new = await _acquire_context(profile_dir)
    try:
        page = await _get_page_and_focus(ctx)
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        logger.info("Login browser opened for %s at %s", platform, login_url)
        _setup_auto_close(ctx, profile_dir, page)
        return {"logged_in": False, "message": "浏览器已打开，请在窗口内扫码登录（不会自动关闭）"}
    except Exception as e:
        if created_new:
            try:
                await ctx.close()
            except Exception:
                pass
            async with _lock:
                if _contexts.get(profile_dir) is ctx:
                    _contexts.pop(profile_dir, None)
        return {"logged_in": False, "message": str(e)}


async def open_and_check_browser(
    profile_dir: str,
    login_url: str,
    platform: str,
) -> Dict[str, Any]:
    """Open browser, bring to front, and check login status. Returns immediately."""
    from .drivers import DRIVERS

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        return {"logged_in": False, "message": f"不支持的平台: {platform}"}

    ctx, created_new = await _acquire_context(profile_dir)
    try:
        page = await _get_page_and_focus(ctx)

        driver = driver_cls()
        logged_in = await driver.check_login(page, navigate=True)

        if not logged_in:
            try:
                await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        _setup_auto_close(ctx, profile_dir, page)

        if logged_in:
            return {"logged_in": True, "message": "浏览器已打开，当前已登录"}
        return {"logged_in": False, "message": "浏览器已打开，请扫码登录"}
    except Exception as e:
        if created_new:
            try:
                await ctx.close()
            except Exception:
                pass
            async with _lock:
                if _contexts.get(profile_dir) is ctx:
                    _contexts.pop(profile_dir, None)
        return {"logged_in": False, "message": str(e)}


async def check_browser_login(
    profile_dir: str,
    platform: str,
) -> bool:
    """Check login status. Opens a context if needed (persistent cookies)."""
    from .drivers import DRIVERS

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        return False

    async with _lock:
        ctx = _contexts.get(profile_dir)

    if ctx:
        try:
            if hasattr(ctx, "is_closed") and ctx.is_closed():
                ctx = None
        except Exception:
            ctx = None

    if not ctx:
        if not Path(profile_dir).exists():
            return False
        try:
            ctx, _ = await _acquire_context(profile_dir)
        except Exception:
            return False

    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        driver = driver_cls()
        logged_in = await driver.check_login(page, navigate=True)
        if logged_in:
            try:
                await page.bring_to_front()
            except Exception:
                pass
        return logged_in
    except Exception:
        return False


async def run_publish_task(
    profile_dir: str,
    platform: str,
    file_path: str,
    title: str,
    description: str,
    tags: str,
    options: Optional[Dict[str, Any]] = None,
    cover_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a publish task. Fails fast if not logged in (no blocking poll)."""
    from .drivers import DRIVERS

    logger.info("[PUBLISH] run_publish_task start: platform=%s file=%s title=%s profile=%s",
                platform, file_path, title, profile_dir)

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        logger.error("[PUBLISH] unsupported platform: %s", platform)
        return {"ok": False, "error": f"不支持的平台: {platform}"}

    driver = driver_cls()
    logger.info("[PUBLISH] acquiring browser context...")
    ctx, created_new = await _acquire_context(profile_dir)
    logger.info("[PUBLISH] context acquired (new=%s)", created_new)
    try:
        page = await _get_page_and_focus(ctx)
        logger.info("[PUBLISH] page ready, checking login...")

        login_ok = await driver.check_login(page, navigate=True)
        logger.info("[PUBLISH] login check result: %s", login_ok)
        if not login_ok:
            try:
                await page.goto(driver.login_url(), wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            _setup_auto_close(ctx, profile_dir, page)
            return {
                "ok": False,
                "need_login": True,
                "error": "未登录，已打开浏览器登录页，请扫码登录后再重试发布",
            }

        await _bring_window_to_front(page)
        logger.info("[PUBLISH] calling driver.publish()...")
        result = await driver.publish(
            page=page,
            file_path=file_path,
            title=title,
            description=description,
            tags=tags,
            options=options or {},
            cover_path=cover_path,
        )
        logger.info("[PUBLISH] driver.publish() returned: ok=%s", result.get("ok"))
        if not result.get("ok"):
            logger.warning("[PUBLISH] publish error: %s", result.get("error"))
        _setup_auto_close(ctx, profile_dir, page)
        return result
    except Exception as exc:
        logger.exception("[PUBLISH] run_publish_task exception")
        return {"ok": False, "error": str(exc)}


async def dryrun_douyin_upload_in_context(
    profile_dir: str,
    file_path: str,
    title: str = "dryrun 标题",
    description: str = "dryrun 文案",
    tags: str = "dryrun,测试",
) -> Dict[str, Any]:
    """Dry-run a douyin publish flow INSIDE the current process."""
    from .drivers.douyin import DouyinDriver, UPLOAD_URL

    driver = DouyinDriver()
    ctx, _created_new = await _acquire_context(profile_dir)
    page = await _get_page_and_focus(ctx)

    await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    frames = []
    try:
        for fr in getattr(page, "frames", []) or []:
            frames.append({"name": getattr(fr, "name", ""), "url": getattr(fr, "url", "")})
    except Exception:
        pass

    result = await driver.publish(
        page=page,
        file_path=file_path,
        title=title,
        description=description,
        tags=tags,
        options={"dry_run": True},
        cover_path=None,
    )
    return {
        "page_url": getattr(page, "url", ""),
        "title": (await page.title()) if hasattr(page, "title") else "",
        "frame_count": len(frames),
        "frames": frames[:12],
        "driver_result": result,
    }
