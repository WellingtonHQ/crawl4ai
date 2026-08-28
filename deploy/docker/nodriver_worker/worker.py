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
import re
from urllib.parse import quote_plus, urlparse

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


def _compile_markers(markers) -> "re.Pattern":
    """Word-boundary-aware marker matcher.

    Plain substring matching false-positives on e.g. "recaptcha.net" inside a
    CSP entry (contains "captcha" but is not a challenge). Anchoring single
    tokens with \\b keeps real challenge text ("px-captcha", standalone
    "Captcha") matching while letting pass-through references go.
    """
    parts = []
    for marker in markers:
        esc = re.escape(marker)
        if marker and marker[0].isalnum():
            esc = r"\b" + esc
        if marker and marker[-1].isalnum():
            esc = esc + r"\b"
        parts.append(esc)
    return re.compile("|".join(parts), re.IGNORECASE)


_CHALLENGE_RE = _compile_markers(_CHALLENGE_MARKERS)


def _looks_like_challenge(html: str) -> bool:
    return _CHALLENGE_RE.search(html[:6000]) is not None


# Walmart serves missing item pages as a full 200 soft-404 shell ("We couldn't
# find this page") — the product was re-listed under a new ID. The shell is a
# complete page, so plain extraction would store it as ok content. Detect it
# and recover the canonical item URL via walmart search instead.
# Anchor to the visible <h1>: the strings "We couldn't find this page" and
# "item404Title" also appear in walmart's site-wide JS config bundle on
# perfectly good item pages, so plain text matching false-positives there.
_WALMART_404_RE = re.compile(
    r">we couldn.{0,2}t find this page</h1>", re.IGNORECASE
)


def _is_walmart_item_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (
        parsed.netloc.lower() in ("walmart.com", "www.walmart.com")
        and parsed.path.startswith("/ip/")
    )


def _is_walmart_item_404(url: str, html: str) -> bool:
    if not _is_walmart_item_url(url):
        return False
    # The 404 h1 sits deep in the shell (~130k chars in) — scan the full page.
    return _WALMART_404_RE.search(html) is not None


async def _walmart_offer_line(tab, md: str) -> str:
    """Ensure the main product's offer (price / availability) is captured.

    The readability/trafilatura hybrid sometimes keeps the related-products
    grid but drops the buy box, leaving the page's own price out of the
    markdown. When that happens, prepend the hero price read from the DOM.
    """
    try:
        raw = await tab.evaluate(
            """(() => {
              const q = (sel) => { const e = document.querySelector(sel); return e ? e.innerText.trim() : null; };
              const hero = q('[data-seo-id="hero-price"]');
              const was = q('[data-seo-id="strike-through-price"]');
              let stock = null;
              const leaf = Array.from(document.querySelectorAll('span, li, p')).find(
                (e) => e.children.length === 0
                  && /^(in stock|out of stock|only \\d+ left|unavailable|not available)/i.test((e.textContent || '').trim()));
              if (leaf) stock = leaf.textContent.trim();
              return JSON.stringify({ hero, was, stock });
            })()""",
            return_by_value=True,
        )
    except Exception as e:
        log.warning("walmart offer lookup failed: %r", e)
        return md
    if not isinstance(raw, str):
        return md
    try:
        offer = json.loads(raw)
    except ValueError:
        return md
    hero = (offer.get("hero") or "").strip()
    if not hero:
        return md
    price_num = re.sub(r"[^0-9.]", "", hero)
    if price_num and price_num in md:
        return md  # the markdown already carries the main price
    bits = [hero]
    if offer.get("was"):
        bits.append("Was " + offer["was"])
    if offer.get("stock"):
        bits.append(offer["stock"])
    return "**Price:** " + " · ".join(bits) + "\n\n" + md


def _slug_tokens(slug: str) -> set:
    return {t for t in re.split(r"[-_]+", slug.lower()) if len(t) > 1}


async def _walmart_recover_item_url(tab, url: str) -> "str | None":
    """Find the canonical walmart item URL for a soft-404 item page.

    Searches walmart with the slug's keywords and returns the best-matching
    item link (slug + numeric id) whose slug shares enough tokens with the
    original slug. Returns None when the search yields no plausible match.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return None
    orig_tokens = _slug_tokens(parts[1])
    if len(orig_tokens) < 3:
        return None
    query = " ".join(parts[1].split("-")[:8])
    search_url = "https://www.walmart.com/search?q=" + quote_plus(query)
    await tab.get(search_url)
    await tab.wait(4.0)
    raw = await tab.evaluate(
        """(() => {
          const seen = new Set();
          const out = [];
          document.querySelectorAll('a[href*="/ip/"]').forEach((a) => {
            const m = (a.getAttribute('href') || '').match(/\\/ip\\/([^/?#]+)\\/([0-9]+)/);
            if (m && !seen.has(m[1] + '/' + m[2])) { seen.add(m[1] + '/' + m[2]); out.push(m[1] + '/' + m[2]); }
          });
          return JSON.stringify(out);
        })()""",
        return_by_value=True,
    )
    if not isinstance(raw, str):
        return None
    try:
        candidates = json.loads(raw)
    except ValueError:
        return None
    best = None
    best_score = 0.0
    for cand in candidates:
        cand_tokens = _slug_tokens(cand.split("/")[0])
        if not cand_tokens:
            continue
        overlap = len(orig_tokens & cand_tokens)
        if overlap < 3:
            continue
        score = overlap / min(len(orig_tokens), len(cand_tokens))
        if score > best_score:
            best, best_score = cand, score
    return f"https://www.walmart.com/ip/{best}" if best else None


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

    # Walmart soft-404 shell: the item was re-listed under a new ID. Recover
    # the canonical item URL via search rather than storing the shell.
    if _is_walmart_item_404(url, html):
        log.info("walmart item page is a 404 shell — recovering via search: %s", url)
        recovered = await _walmart_recover_item_url(tab, url)
        if recovered is None:
            return {"url": url, "markdown": "", "title": None, "success": False,
                    "error": "walmart item page is a 404 shell and no matching item was found in search"}
        log.info("recovered walmart item: %s", recovered)
        await tab.get(recovered)
        await tab.wait(SETTLE_S + 1.5)
        html = await tab.get_content()
        if _is_walmart_item_404(recovered, html):
            return {"url": url, "markdown": "", "title": None, "success": False,
                    "error": f"walmart item 404 shell (recovered item also missing: {recovered})"}

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
    if _is_walmart_item_url(url):
        md = await _walmart_offer_line(tab, md)
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
