"""Douyin (抖音) creator platform driver — creator.douyin.com automation."""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any, Dict, List, Optional

from .base import BaseDriver

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
HOME_URL = "https://creator.douyin.com/creator-micro/home"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv"}


async def _human_delay(lo: float = 0.5, hi: float = 1.5):
    await asyncio.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# Dismiss known overlays / popups (我知道了, guide dialogs, etc.)
# ---------------------------------------------------------------------------
_JS_DISMISS_OVERLAYS = """
() => {
    let dismissed = 0;
    const safeTexts = ['我知道了', '知道了', '关闭', '一律不允许'];
    document.querySelectorAll('button, [role="button"], a').forEach(el => {
        const t = (el.textContent || '').trim();
        if (t.includes('发布') || t.includes('暂存') || t.includes('封面')
            || t.includes('上传') || t.includes('图文') || t.includes('视频')) return;
        for (const dt of safeTexts) {
            if (t === dt || (t.length < 10 && t.includes(dt))) {
                try { el.click(); dismissed++; } catch(e) {}
                break;
            }
        }
    });
    // Close icon in modals
    document.querySelectorAll('.semi-modal-wrap .semi-icon-close, .semi-modal [aria-label="close"]').forEach(el => {
        try { el.click(); dismissed++; } catch(e) {}
    });
    return dismissed;
}
"""


async def _dismiss_overlays(page, label: str = "") -> int:
    total = 0
    for attempt in range(3):
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass
        try:
            n = await page.evaluate(_JS_DISMISS_OVERLAYS)
            total += (n or 0)
            logger.info("[DOUYIN-DISMISS] %s attempt=%d dismissed=%d", label, attempt, n)
        except Exception as exc:
            logger.debug("[DOUYIN-DISMISS] %s attempt=%d err=%s", label, attempt, exc)
        if attempt < 2:
            await asyncio.sleep(0.5)
        try:
            remaining = await page.evaluate(
                "() => document.querySelectorAll('.semi-portal .semi-modal-wrap').length"
            )
            if remaining == 0:
                break
        except Exception:
            break
    return total


# ---------------------------------------------------------------------------
# Discard draft prompt — "有上次未编辑的草稿" → click 放弃
# ---------------------------------------------------------------------------
async def _discard_draft(page, label: str = "") -> bool:
    """If the upload page shows a draft prompt, click 放弃 (discard) to start fresh."""
    try:
        body_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 1000)")
        if "草稿" not in body_text:
            return False
        logger.info("[DOUYIN-DRAFT] %s draft prompt detected", label)

        # Look for 放弃 button
        discard_btn = None
        for sel in [
            'button:has-text("放弃")',
            'text="放弃"',
            'button:has-text("丢弃")',
            'button:has-text("不继续")',
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    discard_btn = el
                    break
            except Exception:
                continue

        if discard_btn:
            await discard_btn.click(force=True, timeout=3000)
            logger.info("[DOUYIN-DRAFT] %s clicked discard button", label)
            await _human_delay(1, 2)
            return True

        # Fallback: JS click any button with 放弃
        clicked = await page.evaluate("""
        () => {
            const btns = document.querySelectorAll('button, [role="button"], a');
            for (const b of btns) {
                const t = (b.textContent || '').trim();
                if (t === '放弃' || t.includes('放弃')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }
        """)
        if clicked:
            logger.info("[DOUYIN-DRAFT] %s JS clicked discard", label)
            await _human_delay(1, 2)
            return True

        logger.warning("[DOUYIN-DRAFT] %s draft detected but discard button not found", label)
        return False
    except Exception as e:
        logger.debug("[DOUYIN-DRAFT] %s error: %s", label, e)
        return False


# ---------------------------------------------------------------------------
# Scroll helpers
# ---------------------------------------------------------------------------
async def _scroll_page_fully(page, label: str = ""):
    """Scroll to bottom then back to top to trigger lazy-loaded content."""
    try:
        await page.evaluate("""
        async () => {
            const delay = ms => new Promise(r => setTimeout(r, ms));
            const totalH = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
            const step = Math.min(800, totalH / 3);
            for (let y = 0; y < totalH; y += step) {
                window.scrollTo(0, y);
                await delay(150);
            }
            window.scrollTo(0, totalH);
            await delay(200);
            window.scrollTo(0, 0);
        }
        """)
        logger.info("[DOUYIN-SCROLL] %s scrolled page fully", label)
    except Exception as exc:
        logger.debug("[DOUYIN-SCROLL] %s error: %s", label, exc)


async def _scroll_and_find(page, selectors, label: str = ""):
    """Try to find element; if not found, scroll down step by step and retry."""
    if isinstance(selectors, str):
        selectors = [selectors]

    for sel in selectors:
        el = await page.query_selector(sel)
        if el:
            try:
                await el.scroll_into_view_if_needed()
            except Exception:
                pass
            return el

    try:
        total_h = await page.evaluate(
            "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        )
    except Exception:
        total_h = 3000

    step = 400
    for y in range(0, total_h + step, step):
        try:
            await page.evaluate(f"() => window.scrollTo(0, {y})")
        except Exception:
            break
        await asyncio.sleep(0.3)
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                logger.info("[DOUYIN-SCROLL] %s found '%s' at scroll y=%d", label, sel, y)
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                return el
    return None


# ---------------------------------------------------------------------------
# Find and click the real 发布 button (red/primary), not 暂存离开
# ---------------------------------------------------------------------------
async def _find_publish_button(page, label: str = ""):
    """Scroll to bottom and find the primary 发布 button. Returns element or None."""
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.8)

    # JS scan: find buttons with exact text "发布", log all button texts for debug
    btn_info = await page.evaluate("""
    () => {
        const btns = Array.from(document.querySelectorAll('button'));
        const allTexts = btns.map(b => (b.textContent || '').trim()).filter(t => t.length < 20);
        const candidates = [];
        for (const b of btns) {
            const t = (b.textContent || '').trim();
            if (t !== '发布') continue;
            const cls = (b.className || '');
            const style = window.getComputedStyle(b);
            const bg = style.backgroundColor || '';
            const isPrimary = cls.includes('primary') || cls.includes('danger')
                || bg.includes('255') || bg.includes('254') || bg.includes('252');
            candidates.push({isPrimary, cls: cls.substring(0, 100)});
            b.scrollIntoView({block: 'center'});
        }
        return {found: candidates.length > 0, count: candidates.length,
                primary: candidates.some(c => c.isPrimary),
                allTexts: allTexts.slice(0, 30)};
    }
    """)
    logger.info("[DOUYIN-%s] button scan: %s", label, btn_info)

    if not btn_info.get("found"):
        return None

    await asyncio.sleep(0.5)

    # Use get_by_role exact match
    try:
        loc = page.get_by_role("button", name="发布", exact=True)
        cnt = await loc.count()
        logger.info("[DOUYIN-%s] get_by_role count=%d", label, cnt)
        if cnt == 1:
            return loc
        if cnt > 1:
            for i in range(cnt):
                el = loc.nth(i)
                txt = (await el.inner_text()).strip()
                if txt == "发布":
                    return el
    except Exception:
        pass

    # Fallback: iterate all buttons
    btns = await page.query_selector_all("button")
    for btn in btns:
        try:
            txt = (await btn.inner_text()).strip()
            if txt == "发布":
                await btn.scroll_into_view_if_needed()
                return btn
        except Exception:
            continue

    return None


async def _click_publish_button(page, publish_btn, label: str = ""):
    """Scroll the publish button into view and click it, with retries."""
    try:
        if hasattr(publish_btn, 'scroll_into_view_if_needed'):
            await publish_btn.scroll_into_view_if_needed()
        elif hasattr(publish_btn, 'first'):
            await publish_btn.first.scroll_into_view_if_needed()
    except Exception:
        pass
    await asyncio.sleep(0.3)

    try:
        await publish_btn.click(timeout=5000)
        return
    except Exception:
        pass

    await _dismiss_overlays(page, f"{label}_publish_retry")
    try:
        if hasattr(publish_btn, 'click'):
            await publish_btn.click(force=True, timeout=5000)
        else:
            await publish_btn.first.click(force=True, timeout=5000)
        return
    except Exception:
        pass

    # JS last resort
    await page.evaluate("""
    () => {
        const btns = document.querySelectorAll('button');
        for (const b of btns) {
            if ((b.textContent || '').trim() === '发布') {
                b.scrollIntoView();
                b.click();
                return;
            }
        }
    }
    """)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _query_any_frame(page: Any, selector: str) -> Any:
    try:
        el = await page.query_selector(selector)
        if el:
            return el
    except Exception:
        pass
    try:
        frames = page.frames
    except Exception:
        frames = []
    for fr in frames or []:
        try:
            el = await fr.query_selector(selector)
            if el:
                return el
        except Exception:
            continue
    return None


async def _query_all_any_frame(page: Any, selector: str) -> List[Any]:
    out: List[Any] = []
    try:
        out.extend(await page.query_selector_all(selector))
    except Exception:
        pass
    try:
        frames = page.frames
    except Exception:
        frames = []
    for fr in frames or []:
        try:
            out.extend(await fr.query_selector_all(selector))
        except Exception:
            continue
    return out


# ===========================================================================
# DouyinDriver
# ===========================================================================
class DouyinDriver(BaseDriver):

    def login_url(self) -> str:
        return "https://creator.douyin.com"

    async def _passive_login_check(self, page: Any) -> bool:
        try:
            url = getattr(page, "url", "") or ""
            if "login" in url or "passport" in url:
                return False
            markers = [
                'text="退出登录"', 'text="作品管理"', 'text="内容管理"',
                'text="发布作品"', 'a[href*="content"]', 'a[href*="upload"]',
            ]
            for sel in markers:
                if await _query_any_frame(page, sel):
                    return True
            if "creator-micro/content/upload" in url:
                if await _query_any_frame(page, 'input[type="file"]'):
                    return True
            return False
        except Exception:
            return False

    async def check_login(self, page: Any, navigate: bool = True) -> bool:
        if not navigate:
            return await self._passive_login_check(page)
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            if await self._passive_login_check(page):
                return True
            try:
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1)
                if await _query_any_frame(page, 'input[type="file"]'):
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Main publish entry
    # -----------------------------------------------------------------------
    async def publish(
        self, page: Any, file_path: str, title: str, description: str,
        tags: str, options: Optional[Dict[str, Any]] = None,
        cover_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        options = options or {}
        applied: Dict[str, Any] = {"steps": []}

        def _step(note: str, ok: bool, **extra):
            entry = {"action": note, "ok": ok, **extra}
            applied["steps"].append(entry)
            logger.info("[DOUYIN-PUBLISH] %s => %s %s", note, "OK" if ok else "FAIL", extra or "")

        try:
            file_ext = os.path.splitext(file_path)[1].lower()
            is_image = file_ext in _IMAGE_EXTS
            logger.info("[DOUYIN-PUBLISH] === START === file=%s ext=%s is_image=%s title=%s",
                        file_path, file_ext, is_image, title)

            if not os.path.isfile(file_path):
                _step("检查文件存在", False, path=file_path)
                return {"ok": False, "error": f"文件不存在: {file_path}", "applied": applied}
            file_size = os.path.getsize(file_path)
            _step("检查文件存在", True, size=file_size)

            media_type = "图片" if is_image else "视频"
            _step(f"自动识别素材类型: {media_type}", True, ext=file_ext, type=media_type)

            # ── Step 1: navigate to upload page (高清发布) ──
            logger.info("[DOUYIN] navigating to upload page: %s", UPLOAD_URL)
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
            await _human_delay(2, 4)

            n = await _dismiss_overlays(page, "upload_page")
            if n:
                _step("关闭弹窗", True, count=n)

            # ── Step 2: discard any lingering draft ──
            if await _discard_draft(page, "upload_page"):
                _step("放弃旧草稿", True)
                await _human_delay(1, 2)
                # May need to re-navigate after discarding draft
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=30000)
                await _human_delay(2, 3)
                await _dismiss_overlays(page, "upload_after_discard")

            if is_image:
                return await self._publish_image(page, file_path, title, description,
                                                 tags, options, applied, _step)
            else:
                return await self._publish_video(page, file_path, title, description,
                                                 tags, options, cover_path, applied, _step)

        except Exception as e:
            logger.exception("[DOUYIN-PUBLISH] publish failed")
            _step("异常", False, error=str(e))
            return {"ok": False, "error": str(e), "applied": applied}

    # ===================================================================
    # IMAGE FLOW: upload page → click "发布图文" tab → use image file input → wait redirect → fill form → publish
    # ===================================================================
    async def _publish_image(self, page, file_path, title, description, tags,
                             options, applied, _step):

        # We are already on /content/upload (from main publish method).
        # The page has tabs: 发布视频 | 发布图文 | 发布全景视频 | 发布文章
        # Default tab is 发布视频. We need to click 发布图文 to switch.

        # ── Click "发布图文" tab ──
        image_tab = await page.query_selector('text="发布图文"')
        if not image_tab:
            image_tab = await page.query_selector('[class*="tab"]:has-text("图文")')
        if not image_tab:
            await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('*')) {
                    if ((el.textContent||'').trim() === '发布图文' && el.offsetParent) {
                        el.click(); return true;
                    }
                }
                return false;
            }
            """)
            _step("切换到发布图文tab", True, method="js")
        else:
            await image_tab.click(timeout=5000)
            _step("切换到发布图文tab", True)

        await _human_delay(2, 3)
        await _dismiss_overlays(page, "image_tab")

        # ── Find the IMAGE file input (accept contains "image") ──
        # After clicking 发布图文, there are 2 file inputs:
        # - video input (hidden, accept="video/*...")
        # - image input (visible, accept="image/png,image/jpeg...")
        uploaded = False

        # Strategy 1: find input[type="file"] with image accept
        all_inputs = await page.query_selector_all('input[type="file"]')
        image_input = None
        for fi in all_inputs:
            try:
                accept = await fi.get_attribute("accept") or ""
                if "image" in accept:
                    image_input = fi
                    break
            except Exception:
                continue

        if image_input:
            try:
                await image_input.set_input_files(file_path)
                logger.info("[DOUYIN-IMAGE] uploaded via image file input (accept=image)")
                _step("上传图片文件", True, method="image-input")
                uploaded = True
            except Exception as e:
                logger.warning("[DOUYIN-IMAGE] set_input_files failed: %s", e)

        # Strategy 2: expect_file_chooser + click "上传图文" button
        if not uploaded:
            upload_btn = await page.query_selector('button:has-text("上传图文")')
            if upload_btn:
                try:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await upload_btn.click(timeout=3000)
                    fc = await fc_info.value
                    await fc.set_files(file_path)
                    logger.info("[DOUYIN-IMAGE] uploaded via filechooser on '上传图文' button")
                    _step("上传图片文件", True, method="filechooser")
                    uploaded = True
                except Exception as e:
                    logger.debug("[DOUYIN-IMAGE] filechooser failed: %s", e)

        # Strategy 3: click "点击上传" text area
        if not uploaded:
            try:
                click_area = await page.query_selector('text="点击上传"')
                if click_area:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await click_area.click(force=True, timeout=3000)
                    fc = await fc_info.value
                    await fc.set_files(file_path)
                    _step("上传图片文件", True, method="click-upload")
                    uploaded = True
            except Exception:
                pass

        if not uploaded:
            diag = await page.evaluate("""
            () => ({
                url: location.href,
                inputs: Array.from(document.querySelectorAll('input')).map(el => ({
                    type: el.type, accept: (el.accept||'').substring(0,60),
                    visible: !!el.offsetParent,
                })),
                buttons: Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent).map(b => ({
                    text: (b.textContent||'').trim().substring(0,30),
                })).slice(0, 20),
            })
            """)
            import json
            logger.error("[DOUYIN-IMAGE] DOM DIAGNOSTICS:\n%s", json.dumps(diag, ensure_ascii=False, indent=2))
            _step("上传图片文件", False, diagnostics=diag)
            return {"ok": False, "error": "找不到图片上传入口", "applied": applied}

        # ── Wait for redirect to image editing page ──
        logger.info("[DOUYIN-IMAGE] waiting for redirect to editing page...")
        redirected = False
        for _ in range(30):
            await asyncio.sleep(2)
            cur = page.url or ""
            if "post/image" in cur or "post/publish" in cur:
                logger.info("[DOUYIN-IMAGE] redirected to: %s", cur)
                redirected = True
                break
            # Check if editing form appeared on same page
            form_el = await page.query_selector('[contenteditable="true"], input[placeholder*="标题"]')
            if form_el:
                logger.info("[DOUYIN-IMAGE] form appeared (URL: %s)", cur)
                redirected = True
                break

        if not redirected:
            _step("等待跳转到编辑页", False, url=page.url)
            return {"ok": False, "error": "上传图片后未跳转到编辑页", "applied": applied}

        _step("进入图文编辑页面", True, url=page.url)
        await _human_delay(2, 3)
        await _dismiss_overlays(page, "image_edit_page")
        if await _discard_draft(page, "image_edit"):
            _step("放弃编辑页草稿", True)

        # ── Fill title ──
        title_input = await _scroll_and_find(page, [
            'input.semi-input[placeholder*="标题"]',
            'input[placeholder*="添加作品标题"]',
            'input.semi-input',
        ], "image_title")
        if title_input and title:
            await title_input.click()
            await title_input.fill("")
            await _human_delay(0.2, 0.4)
            await title_input.fill(title[:20])
            _step("填写标题", True, value=title[:20])
        else:
            _step("填写标题", False, found=bool(title_input))

        # ── Fill description ──
        text = description or title or ""
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            text += " " + " ".join(f"#{t}" for t in tag_list)

        content_editor = await _scroll_and_find(page, [
            '.zone-container[contenteditable="true"]',
            '[contenteditable="true"].notranslate',
            '[contenteditable="true"]',
        ], "image_editor")
        if content_editor:
            await content_editor.click()
            await _human_delay(0.2, 0.4)
            await page.keyboard.press("Control+KeyA")
            await page.keyboard.press("Delete")
            await page.keyboard.type(text[:500])
            _step("填写描述/文案", True, length=len(text))
        else:
            _step("填写描述/文案", False)

        await _human_delay(1, 2)
        await _dismiss_overlays(page, "image_before_publish")

        # ── Find and click publish button ──
        publish_btn = await _find_publish_button(page, "IMAGE")
        if not publish_btn:
            _step("找到发布按钮", False)
            return {"ok": False, "error": "找不到发布按钮", "applied": applied}

        # Verify
        try:
            verify_txt = (await publish_btn.inner_text()).strip()
            logger.info("[DOUYIN-IMAGE] verified button: '%s'", verify_txt)
            if verify_txt != "发布":
                logger.error("[DOUYIN-IMAGE] wrong button text: '%s'", verify_txt)
                _step("找到发布按钮", False, error=f"按钮文字不对: {verify_txt}")
                return {"ok": False, "error": f"找到的按钮文字是'{verify_txt}'而非'发布'", "applied": applied}
        except Exception:
            pass

        _step("找到发布按钮", True)

        if options.get("dry_run"):
            _step("dry_run — 未点击发布", True)
            return {"ok": True, "url": page.url, "applied": applied, "dry_run": True}

        logger.info("[DOUYIN-IMAGE] clicking publish...")
        await _click_publish_button(page, publish_btn, "image")
        _step("点击发布按钮", True)

        return await self._check_publish_result(page, applied, _step)

    # ===================================================================
    # VIDEO FLOW: upload page → upload video → wait redirect → fill form → publish
    # ===================================================================
    async def _publish_video(self, page, file_path, title, description, tags,
                             options, cover_path, applied, _step):

        # We're already on /content/upload from the main publish() method.
        # Find video file input
        file_input = await page.query_selector('input[type="file"]')
        if not file_input:
            try:
                file_input = await page.wait_for_selector(
                    'input[type="file"]', state="attached", timeout=10000
                )
            except Exception:
                pass

        if not file_input:
            for ubs in ['button:has-text("上传视频")', '[class*="upload"] button', 'text="点击上传"']:
                try:
                    ubtn = await page.query_selector(ubs)
                    if ubtn and await ubtn.is_visible():
                        await ubtn.click(timeout=3000)
                        await _human_delay(1, 2)
                        break
                except Exception:
                    continue
            try:
                file_input = await page.wait_for_selector(
                    'input[type="file"]', state="attached", timeout=10000
                )
            except Exception:
                pass

        if not file_input:
            _step("找到视频上传入口", False)
            return {"ok": False, "error": "找不到视频上传入口", "applied": applied}

        _step("找到视频上传入口", True)

        await file_input.set_input_files(file_path)
        _step("上传视频文件", True)

        # Wait for redirect to publish/post page
        logger.info("[DOUYIN-VIDEO] waiting for redirect...")
        redirected = False
        for _ in range(30):
            await asyncio.sleep(2)
            cur = page.url or ""
            if "post/video" in cur or ("publish" in cur and "upload" not in cur):
                logger.info("[DOUYIN-VIDEO] redirected to: %s", cur)
                redirected = True
                break

        if not redirected:
            form_el = await page.query_selector('[contenteditable="true"], input[placeholder*="标题"]')
            if form_el:
                redirected = True
                logger.info("[DOUYIN-VIDEO] form appeared without URL change")

        if not redirected:
            _step("等待视频处理/跳转", False)
            return {"ok": False, "error": "视频上传后未跳转到发布页面", "applied": applied}

        _step("进入视频发布页面", True)
        await _human_delay(1, 2)
        await _dismiss_overlays(page, "video_after_redirect")

        await _scroll_page_fully(page, "video_preload")
        await _dismiss_overlays(page, "video_after_scroll")
        await page.evaluate("() => window.scrollTo(0, 0)")

        # ── Title ──
        title_input = await _scroll_and_find(page, [
            'input[placeholder*="标题"]',
            'input.semi-input',
        ], "video_title")
        if title_input and title:
            await title_input.click()
            await title_input.fill("")
            await _human_delay(0.2, 0.4)
            await title_input.fill(title[:30])
            _step("填写标题", True, value=title[:30])
        else:
            notranslate = await _scroll_and_find(page, [
                '.notranslate[contenteditable]',
            ], "video_title_ce")
            if notranslate and title:
                await notranslate.click()
                await page.keyboard.press("Control+KeyA")
                await page.keyboard.press("Delete")
                await page.keyboard.type(title[:30])
                _step("填写标题", True, value=title[:30])
            else:
                _step("填写标题", False)

        # ── Description ──
        text = description or ""
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            text += " " + " ".join(f"#{t}" for t in tag_list)

        zone = await _scroll_and_find(page, [
            '.zone-container[contenteditable="true"]',
        ], "video_desc")
        if zone and text:
            await zone.click()
            await _human_delay(0.2, 0.4)
            await page.keyboard.press("Control+KeyA")
            await page.keyboard.press("Delete")
            await page.keyboard.type(text[:500])
            _step("填写描述/文案", True, length=len(text))
        else:
            ce = await _scroll_and_find(page, [
                '[contenteditable="true"]',
            ], "video_desc_ce")
            if ce and text:
                await ce.click()
                await _human_delay(0.2, 0.4)
                await page.keyboard.type(text[:500])
                _step("填写描述/文案", True, length=len(text))
            else:
                _step("填写描述/文案", False)

        # ── Wait for video processing ──
        for _w in range(30):
            has_reupload = await page.evaluate(
                "() => (document.body.innerText||'').includes('重新上传')"
            )
            if has_reupload:
                logger.info("[DOUYIN-VIDEO] video processing complete")
                _step("视频处理完成", True)
                break
            has_fail = await page.evaluate(
                "() => (document.body.innerText||'').includes('上传失败')"
            )
            if has_fail:
                _step("视频上传失败", False)
                return {"ok": False, "error": "视频上传失败", "applied": applied}
            await asyncio.sleep(2)

        await _human_delay(1, 2)
        await _dismiss_overlays(page, "video_before_publish")

        # ── Find and click publish button ──
        publish_btn = await _find_publish_button(page, "VIDEO")
        if not publish_btn:
            _step("找到发布按钮", False)
            return {"ok": False, "error": "找不到发布按钮", "applied": applied}

        try:
            verify_txt = (await publish_btn.inner_text()).strip()
            logger.info("[DOUYIN-VIDEO] verified button: '%s'", verify_txt)
            if verify_txt != "发布":
                _step("找到发布按钮", False, error=f"按钮文字不对: {verify_txt}")
                return {"ok": False, "error": f"找到的按钮文字是'{verify_txt}'而非'发布'", "applied": applied}
        except Exception:
            pass

        _step("找到发布按钮", True)

        if options.get("dry_run"):
            return {"ok": True, "url": page.url, "applied": applied, "dry_run": True}

        logger.info("[DOUYIN-VIDEO] clicking publish...")
        await _click_publish_button(page, publish_btn, "video")
        _step("点击发布按钮", True)

        return await self._check_publish_result(page, applied, _step)

    # ===================================================================
    # Check publish result
    # ===================================================================
    async def _check_publish_result(self, page, applied, _step):
        pre_url = page.url or ""
        logger.info("[DOUYIN-PUBLISH] waiting for result, pre_url=%s", pre_url)
        publish_ok = False
        error_msg = ""

        _JS_CHECK = """
        () => {
            const r = {status:'unknown'};
            const body = document.body ? document.body.innerText : '';
            const ok = ['发布成功', '作品发布成功', '发布完成', '已发布', '作品已发布', '提交成功'];
            const fail = ['发布失败', '上传失败', '审核不通过', '内容不符合', '审核未通过'];
            for (const k of ok) { if (body.includes(k)) return {status:'success', keyword:k, src:'body'}; }
            for (const k of fail) { if (body.includes(k)) return {status:'fail', keyword:k, src:'body'}; }

            const toastSels = [
                '.semi-toast-content', '.semi-toast-wrapper',
                '.semi-notification-content', '.semi-notification',
                '[class*="toast"]', '[class*="Toast"]',
                '[class*="notice"]', '[class*="message"]',
            ];
            for (const sel of toastSels) {
                for (const el of document.querySelectorAll(sel)) {
                    const t = (el.textContent || '').trim();
                    if (!t) continue;
                    for (const k of ok) { if (t.includes(k)) return {status:'success', keyword:k, src:'toast'}; }
                    for (const k of fail) { if (t.includes(k)) return {status:'fail', keyword:k, src:'toast'}; }
                }
            }
            return r;
        }
        """

        for ci in range(20):
            if ci < 3:
                await asyncio.sleep(1)
            else:
                await _human_delay(1.5, 3)

            cur = page.url or ""
            logger.info("[DOUYIN-PUBLISH] check[%d] url=%s", ci, cur)

            # URL is on manage page = success
            if "/content/manage" in cur:
                logger.info("[DOUYIN-PUBLISH] on manage page => success")
                publish_ok = True
                break

            # JS text / toast scan
            try:
                jr = await page.evaluate(_JS_CHECK)
                logger.info("[DOUYIN-PUBLISH] JS: %s", jr)
                if jr.get("status") == "success":
                    publish_ok = True
                    break
                elif jr.get("status") == "fail":
                    error_msg = jr.get("keyword", "发布失败")
                    break
            except Exception:
                pass

            if ci in (3, 7, 11, 15):
                await _dismiss_overlays(page, f"result_check_{ci}")

        # Final URL check
        final_url = page.url or ""
        if not publish_ok and not error_msg:
            if "/content/manage" in final_url:
                publish_ok = True
            elif "/content/upload" in final_url and final_url != pre_url:
                error_msg = "页面跳转到上传页(可能点了暂存而非发布)"

        logger.info("[DOUYIN-PUBLISH] === DONE === ok=%s url=%s err=%s", publish_ok, final_url, error_msg)

        if publish_ok:
            _step("发布成功", True, url=final_url)
            return {"ok": True, "url": final_url, "applied": applied}
        elif error_msg:
            _step("发布失败", False, error=error_msg, url=final_url)
            return {"ok": False, "error": f"发布失败: {error_msg}", "url": final_url, "applied": applied}
        else:
            _step("发布状态不确定", False, url=final_url)
            return {
                "ok": False,
                "error": f"点击发布后未检测到成功标志（当前页面: {final_url}），请手动确认",
                "url": final_url,
                "applied": applied,
            }
