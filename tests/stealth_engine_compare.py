"""Compare automation-detection signals between the two engines.

Runs the same probe under PlaywrightAdapter (opt-out) and UndetectedAdapter
(default/patchright) and prints the differences.
"""
import asyncio
import json
import sys

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from crawl4ai.browser_adapter import PlaywrightAdapter, UndetectedAdapter

PROBE = r"""
() => {
  const out = {
    webdriver: navigator.webdriver,
    chrome: typeof window.chrome,
    cdc_vars: Object.keys(window).filter(k => k.startsWith('cdc_')).length,
    plugins_len: navigator.plugins.length,
    plugins_ctor: Object.prototype.toString.call(navigator.plugins),
    ua: navigator.userAgent,
    // CDP attach detection: a real browser has no 'Debugger' agent leaks,
    // but the strongest signal is whether console/eval is isolated.
    has_automation_flag: !!(window.__playwright || window.__puppeteer),
  };
  return out;
}
"""


async def probe(adapter) -> dict:
    cfg = BrowserConfig(
        headless=True,
        text_mode=True,
        extra_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
        ],
    )
    cr = AsyncWebCrawler(config=cfg, verbose=False)
    # Force a specific adapter by constructing the strategy manually.
    from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy
    strat = AsyncPlaywrightCrawlerStrategy(browser_config=cfg, browser_adapter=adapter)
    await strat.start()
    try:
        page, _context = await strat.browser_manager.get_page(CrawlerRunConfig())
        sig = await page.evaluate(PROBE)
        sig["engine"] = type(strat.browser_manager.playwright).__module__
        sig["adapter"] = type(adapter).__name__
        return sig
    finally:
        await strat.close()


async def main():
    pw = await probe(PlaywrightAdapter())
    pr = await probe(UndetectedAdapter())
    out = {"playwright": pw, "patchright": pr, "diff": {}}
    for k in set(list(pw.keys()) + list(pr.keys())):
        if pw.get(k) != pr.get(k):
            out["diff"][k] = {"playwright": pw.get(k), "patchright": pr.get(k)}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
