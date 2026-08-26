"""A/B stealth test: crawl URLs that were blocked in production logs.

Usage: python3 tests/stealth_ab_test.py [label]
Prints a JSON result object to stdout.
"""
import asyncio
import json
import re
import sys
import time

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

# Mirrors /app/api.py production BrowserConfig (crawler section of config.yml)
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

# URLs that failed in the wellisearch/crawl4ai production logs (2026-08-26)
TARGETS = [
    # "crawl error ... network: " (120s timeout) — develeap.com, Cloudflare
    "https://www.develeap.com/news/zero-code-low-cost-data-ingestion-new-bigquery-dts-capabilit/",
    # "crawl http_500 ... Page.goto: Timeout 60000ms exceeded" — arcticwolf.com, Akamai
    "https://arcticwolf.com/resources/blog/cve-2026-21962",
]

CHALLENGE_PATTERNS = [
    (r"just a moment", "cloudflare-challenge"),
    (r"cf-challenge|challenge-platform|cf-browser-verification", "cloudflare-challenge"),
    (r"attention required\|cloudflare", "cloudflare-block"),
    (r"pardon our interruption", "akamai-block"),
    (r"access denied.*reference #|reference #.*akamai", "akamai-block"),
    (r"are you a (human|robot)", "bot-check"),
    (r"unusual traffic", "cloudflare-block"),
    (r"request blocked", "generic-block"),
]


def classify(html: str) -> str:
    if not html:
        return "no-content"
    low = html.lower()
    for pat, label in CHALLENGE_PATTERNS:
        if re.search(pat, low):
            return label
    return "content"


async def crawl_one(url: str, cfg: BrowserConfig) -> dict:
    crawler = AsyncWebCrawler(config=cfg, verbose=False)
    await crawler.start()
    out = {"url": url}
    try:
        t0 = time.monotonic()
        result = await crawler.arun(
            url,
            config=CrawlerRunConfig(
                page_timeout=90000,
                wait_for_images=False,
            ),
        )
        dt = time.monotonic() - t0
        html = result.html or ""
        md = (result.markdown.raw_markdown if result.markdown else "") or ""
        out.update(
            success=result.success,
            status_code=result.status_code,
            seconds=round(dt, 1),
            html_len=len(html),
            md_len=len(md),
            classification=classify(html),
            error=(result.error_message or "")[:300],
        )
    except Exception as e:  # noqa: BLE001
        out.update(success=False, error=f"{type(e).__name__}: {e}"[:300])
    finally:
        await crawler.close()
    return out


async def main() -> None:
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    cfg = BrowserConfig(**BROWSER_KWARGS)
    results = [await crawl_one(u, cfg) for u in TARGETS]
    print(json.dumps({"label": label, "results": results}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
