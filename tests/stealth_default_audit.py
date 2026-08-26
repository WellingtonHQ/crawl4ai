"""Prove use_undetected (patchright) is the default across ALL construction paths.

Covers: direct ctor, from_kwargs, load (TRUSTED + UNTRUSTED), to_dict roundtrip,
and the AsyncWebCrawler entry point. Also confirms the opt-out still works.
"""
import asyncio
import json

from crawl4ai import BrowserConfig, AsyncWebCrawler
from crawl4ai.async_configs import Provenance
from crawl4ai.async_crawler_strategy import _default_adapter
from crawl4ai.browser_adapter import PlaywrightAdapter, UndetectedAdapter


def adapter_name(cfg):
    return type(_default_adapter(cfg)).__name__


def main():
    rows = []

    def check(label, cfg, expect="UndetectedAdapter"):
        name = adapter_name(cfg)
        ok = name == expect
        rows.append({"path": label, "use_undetected": cfg.use_undetected,
                     "adapter": name, "expected": expect, "ok": ok})

    # 1. Direct constructor (no args)
    check("BrowserConfig()", BrowserConfig())

    # 2. from_kwargs with empty dict
    check("BrowserConfig.from_kwargs({})", BrowserConfig.from_kwargs({}))

    # 3. from_kwargs with unrelated kwargs (wellisearch base-config shape)
    check("from_kwargs(headless,text_mode)",
          BrowserConfig.from_kwargs({"headless": True, "text_mode": True}))

    # 4. load TRUSTED, empty
    check("load({}, TRUSTED)", BrowserConfig.load({}))

    # 5. load UNTRUSTED, empty (wellisearch per-request path)
    check("load({}, UNTRUSTED)", BrowserConfig.load({}, provenance=Provenance.UNTRUSTED))

    # 6. load UNTRUSTED, attacker tries to opt OUT -> must be dropped, default wins
    check("load({use_undetected:false}, UNTRUSTED)",
          BrowserConfig.load({"use_undetected": False}, provenance=Provenance.UNTRUSTED))

    # 7. load UNTRUSTED, attacker sends it true -> still patchright
    check("load({use_undetected:true}, UNTRUSTED)",
          BrowserConfig.load({"use_undetected": True}, provenance=Provenance.UNTRUSTED))

    # 8. to_dict roundtrip
    d = BrowserConfig().to_dict()
    check("to_dict() -> from_kwargs", BrowserConfig.from_kwargs(d))

    # 9. OPT-OUT still works (trusted, explicit)
    check("BrowserConfig(use_undetected=False) [opt-out]",
          BrowserConfig(use_undetected=False), expect="PlaywrightAdapter")

    # 10. AsyncWebCrawler entry point (the real client path)
    async def crawler_adapter():
        cr = AsyncWebCrawler(config=BrowserConfig(), verbose=False)
        name = type(cr.crawler_strategy.adapter).__name__
        await cr.close()
        return name
    try:
        name = asyncio.get_event_loop().run_until_complete(crawler_adapter())
    except RuntimeError:
        name = asyncio.new_event_loop().run_until_complete(crawler_adapter())
    rows.append({"path": "AsyncWebCrawler(config=BrowserConfig())",
                 "use_undetected": BrowserConfig().use_undetected,
                 "adapter": name, "expected": "UndetectedAdapter",
                 "ok": name == "UndetectedAdapter"})

    out = {"rows": rows, "all_ok": all(r["ok"] for r in rows)}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
