"""Deterministic anti-bot fingerprint test via bot.sannysoft.com.

Crawls the detector page and extracts pass/fail for the key checks.
A 'detected as automation' result shows up as FAILED rows.
"""
import asyncio
import json
import re
import sys

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

BROWSER_KWARGS = dict(
    headless=True,
    text_mode=True,
    extra_args=[
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
    ],
)

URL = "https://bot.sannysoft.com/"


async def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    cfg = BrowserConfig(**BROWSER_KWARGS)
    crawler = AsyncWebCrawler(config=cfg, verbose=False)
    await crawler.start()
    out = {"label": label, "url": URL}
    try:
        result = await crawler.arun(
            URL,
            config=CrawlerRunConfig(
                page_timeout=60000,
                wait_for_images=False,
                delay_before_return_html=4.0,
            ),
        )
        html = result.html or ""
        out["success"] = result.success
        out["html_len"] = len(html)
        # bot.sannysoft.com renders a table: each <tr> has a test name + result.
        # Results are in <td> with class 'pass'/'fail' or text 'Passed'/'Failed'.
        rows = re.findall(
            r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I
        )
        checks = []
        for r in rows:
            name_m = re.search(r"<td[^>]*>(.*?)</td>", r, flags=re.S | re.I)
            if not name_m:
                continue
            name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip()
            if not name or name.lower() in ("test", "result"):
                continue
            failed = bool(re.search(r"fail|not passed|✗|×", r, flags=re.I))
            passed = bool(re.search(r"pass|✓|✔", r, flags=re.I))
            checks.append({"test": name, "failed": failed and not passed})
        out["checks"] = checks
        out["failed_count"] = sum(1 for c in checks if c["failed"])
        out["total"] = len(checks)
        # Also capture the raw navigator.webdriver value if present
        wd = re.search(r"navigator\.webdriver[^<]*</td>\s*<td[^>]*>(.*?)</td>",
                       html, flags=re.S | re.I)
        if wd:
            out["navigator_webdriver"] = re.sub(r"<[^>]+>", "", wd.group(1)).strip()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        await crawler.close()
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
