"""nodriver_worker — a stealth crawl worker for the crawl4ai image.

One pooled nodriver (AGPL-3.0) Chromium process serves many tabs behind an
asyncio semaphore. Exposes the same contract as the main API's /md endpoint:

    POST /md   {"url": "https://..."}  ->  {"url", "markdown", "title", "success"}
    GET  /health

nodriver is AGPL-3.0 and intentionally confined to this worker process: it
lives in its own venv (/opt/nodriver-worker), separate from the main API.
Nothing in the crawl4ai package or the main API ever imports it — AGPL
isolation by process + network boundary. See LICENSE-NOTICE.md.

Headful (Xvfb-backed) is the default: Cloudflare's managed "Just a moment"
interstitial never clears in plain headless mode on this stack, but resolves
in ~10-30s when the browser runs with a real X display (supervisord runs
Xvfb on :99 and sets DISPLAY for this worker).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel

from nodriver import Browser

log = logging.getLogger("nodriver_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.lower() in ("1", "true", "yes", "on")


STEALTH_ENABLED = _flag("NODRIVER_STEALTH_ENABLED", True)
HEADLESS = _flag("NODRIVER_HEADLESS", False)  # default headful on Xvfb
MAX_PARALLEL = int(os.environ.get("NODRIVER_MAX_PARALLEL", "4"))
CRAWL_TIMEOUT_S = float(os.environ.get("NODRIVER_CRAWL_TIMEOUT_S", "60"))
SETTLE_S = 2.5  # let post-load JS render
# Budget for CF challenges: poll every 5s until cleared or the deadline
# passes (verify_cf each round; harmless on non-interactive ones).
CHALLENGE_BUDGET_S = float(os.environ.get("NODRIVER_CHALLENGE_TIMEOUT_S", "25"))
CHALLENGE_POLL_S = 5.0
CHROME_PATH = os.environ.get("NODRIVER_CHROME_PATH", "/usr/bin/chromium")
USER_DATA_DIR = os.environ.get("NODRIVER_USER_DATA_DIR", "/tmp/nodriver-worker-profile")
DEBUG = _flag("NODRIVER_DEBUG", False)

_BROWSER: Browser | None = None
_SEM = asyncio.Semaphore(MAX_PARALLEL)


class MdRequest(BaseModel):
    url: str


# cfbridge marker list plus the extra bot/challenge markers required here.
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "challenge-platform",
    "cf-chl",
    "pardon our interruption",
    "verify you are human",
    "performing security verification",
    "continue shopping",
    "captcha",
    "robot check",
    "type the characters",
    "enter the characters",
)


def _looks_like_challenge(html: str) -> bool:
    low = html[:6000].lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _to_markdown(html: str) -> str:
    """Hybrid extraction: trafilatura (great on articles) and
    readability-lxml+markdownify (better on front/list pages where trafilatura
    gives up). Take whichever is longer — both are fast on page-sized HTML."""
    out: list[str] = []
    try:
        import trafilatura

        t = trafilatura.extract(html, output_format="markdown", include_links=True)
        if t:
            out.append(t)
    except Exception as e:
        log.warning("trafilatura failed: %r", e)
    try:
        from markdownify import markdownify as mdify
        from readability import Document

        r = mdify(Document(html).summary(), heading_style="ATX")
        if r:
            out.append(r)
    except Exception as e:
        log.warning("readability failed: %r", e)
    return max(out, key=len) if out else ""


async def _click_turnstile_checkbox(tab) -> bool:
    """Best-effort click on the Cloudflare "Verify you are human" checkbox.

    The turnstile widget renders inside a CLOSED shadow root: the checkbox is
    invisible to document.querySelectorAll (no iframe, no canvas) and nodriver's
    cv2 template match (verify_cf) cannot find it. The one DOM-visible landmark
    is the widget's container row — a wide, medium-height div near mid-page.
    We locate that row and click the checkbox at its fixed offset (left edge,
    vertical centre). Returns True if a click was attempted.
    """
    try:
        raw = await tab.evaluate("""(() => {
          const els = Array.from(document.querySelectorAll('div'));
          for (const el of els) {
            const r = el.getBoundingClientRect();
            if (r.width > 600 && r.width < 1000 && r.height > 55 && r.height < 85 && r.y > 100) {
              return JSON.stringify([Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]);
            }
          }
          return null;
        })()""", return_by_value=True)
    except Exception as e:
        log.warning("turnstile box lookup failed: %r", e)
        return False
    box = None
    if isinstance(raw, str):
        try:
            box = json.loads(raw)
        except (ValueError, TypeError):
            box = None
    if not isinstance(box, list) or len(box) != 4:
        return False
    x = box[0] + 24
    y = box[1] + box[3] // 2
    try:
        await tab.mouse_move(x, y)
        await asyncio.sleep(0.4)
        await tab.mouse_click(x, y)
        log.info("clicked turnstile checkbox at (%d, %d) [row=%s]", x, y, box)
        return True
    except Exception as e:
        log.warning("turnstile click failed: %r", e)
        return False


async def _crawl_tab(tab, url: str) -> dict:
    """Drive an already-open tab: settle, resolve challenge, extract markdown."""
    loop = asyncio.get_running_loop()
    await tab.wait(SETTLE_S)
    deadline = loop.time() + CHALLENGE_BUDGET_S
    challenge_seen = False
    round_no = 0
    while True:
        html = await tab.get_content()
        if not _looks_like_challenge(html):
            break
        challenge_seen = True
        round_no += 1
        log.info("challenge present (poll %d, budget %.0fs) — click checkbox + verify_cf + wait",
                 round_no, CHALLENGE_BUDGET_S)
        if DEBUG:
            try:
                await tab.save_screenshot(f"/tmp/challenge_r{round_no}.jpg")
                log.info("debug screenshot saved /tmp/challenge_r%d.jpg", round_no)
            except Exception as e:
                log.warning("debug screenshot failed: %r", e)
        await _click_turnstile_checkbox(tab)  # closed-shadow-root widget: click at computed coords
        try:
            # verify_cf does a CDP screenshot + cv2 match that can HANG on a
            # page mid-challenge (observed on boardgamegeek: it ate the entire
            # 60s crawl budget after a single checkbox click, so the poll loop
            # never got a chance to re-click). The click above already went
            # through — cap verify_cf so the loop keeps polling/re-clicking.
            await asyncio.wait_for(tab.verify_cf(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("verify_cf timed out for %s — continuing (checkbox click already sent)", url)
        except Exception as e:  # no checkbox match, CDP error — keep waiting anyway
            log.warning("verify_cf failed: %r", e)
        if loop.time() + CHALLENGE_POLL_S > deadline:
            break
        await tab.wait(CHALLENGE_POLL_S)
    html = await tab.get_content()
    if _looks_like_challenge(html):
        return {"url": url, "markdown": "", "title": None, "success": False,
                "error": f"cloudflare challenge still present after {CHALLENGE_BUDGET_S:.0f}s budget"}

    await tab.wait(1.0)  # let post-challenge redirect/render settle
    html = await tab.get_content()

    title = None
    try:
        t = await tab.evaluate("document.title", return_by_value=True)
        if isinstance(t, str) and t.strip():
            title = t.strip()
    except Exception:
        pass

    md = _to_markdown(html)
    if not md.strip():
        return {"url": url, "markdown": "", "title": title, "success": False,
                "error": "empty markdown"}
    return {"url": url, "markdown": md, "title": title, "success": True,
            "challenge_cleared": challenge_seen}


async def lifespan(_app: FastAPI):
    global _BROWSER
    if not STEALTH_ENABLED:
        log.warning("NODRIVER_STEALTH_ENABLED is off — worker will refuse crawls")
        yield
        return
    log.info("starting pooled browser (headless=%s, chrome=%s, display=%s)",
             HEADLESS, CHROME_PATH, os.environ.get("DISPLAY"))
    _BROWSER = await Browser.create(
        headless=HEADLESS,
        browser_executable_path=CHROME_PATH,
        user_data_dir=USER_DATA_DIR,
        sandbox=False,  # container runs as appuser in a rootfs where chrome's setuid sandbox is unavailable
        browser_args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1366,900",
            "--lang=en-US",
        ],
    )
    proc = getattr(_BROWSER, "_process", None)
    log.info("browser up (pid=%s)", getattr(proc, "pid", None))
    try:
        yield
    finally:
        log.info("stopping browser")
        try:
            _BROWSER.stop()
        except Exception as e:
            log.warning("browser stop: %r", e)
        _BROWSER = None


app = FastAPI(title="nodriver_worker", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    if not STEALTH_ENABLED:
        return {"status": "disabled", "browser": "down"}
    up = _BROWSER is not None
    return {"status": "ok" if up else "starting", "browser": "up" if up else "down"}


@app.post("/md")
async def md(req: MdRequest) -> dict:
    url = (req.url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"url": url, "markdown": "", "title": None, "success": False,
                "error": "invalid url"}
    if not STEALTH_ENABLED or _BROWSER is None:
        return {"url": url, "markdown": "", "title": None, "success": False,
                "error": "nodriver stealth worker is disabled or not started"}

    loop = asyncio.get_running_loop()
    async with _SEM:
        t0 = loop.time()
        log.info("crawl start %s", url)
        tab = None
        try:
            # new_tab=True: Browser.get() without it navigates the FIRST page
            # target in its (stale-prone) target list — after that tab is
            # closed every later crawl dies with "Session with given id not
            # found". A fresh target per crawl is the pooled-browser contract.
            tab = await _BROWSER.get(url, new_tab=True)
            out = await asyncio.wait_for(_crawl_tab(tab, url), timeout=CRAWL_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("crawl timeout %s (%.0fs)", url, CRAWL_TIMEOUT_S)
            out = {"url": url, "markdown": "", "title": None, "success": False,
                   "error": f"timeout after {CRAWL_TIMEOUT_S:.0f}s"}
        except Exception as e:
            log.exception("crawl error %s", url)
            out = {"url": url, "markdown": "", "title": None, "success": False,
                   "error": f"{type(e).__name__}: {e}"}
        finally:
            if tab is not None:
                try:
                    await asyncio.wait_for(tab.close(), timeout=10)
                except Exception:
                    pass
        out["ms"] = int((loop.time() - t0) * 1000)
        log.info("crawl done %s success=%s ms=%s md_chars=%d",
                 url, out.get("success"), out.get("ms"), len(out.get("markdown") or ""))
        return out
