# CloakBrowser stealth-Chromium tier — integration & test report

Branch: `feature/cloak-browser` · commit `b33dbc3` (not pushed)
Date: 2026-08-27 · Stack: wellisearch (crawl4ai primary + cfbridge fallback)

---

## 1. License, free tier, and architecture story

**Two artifacts, two licenses.** The CloakHQ project ships a Python wrapper
(`cloakbrowser/`, MIT) around a **prebuilt, fingerprint-patched Chromium
binary** that is **CloakHQ-proprietary** (their `BINARY-LICENSE.md`):

- Internal use inside a pipeline: **allowed.**
- Redistribution: **not allowed** → the built Docker image must stay private.

**Free tier = the v146 line.** The newest major that ships free on GitHub
Releases is `chromium-v146.0.7680.177.5`. Newer majors (v148+) require a paid
CloakHQ **Pro** license — their release pages expose only `SHA256SUMS` (no
binary download) for those. So v146 is both the newest *free* build and the
one we pinned.

**Architecture fit.** The crawl4ai container is **amd64/x86_64** (it runs
emulated on our Apple Silicon host, but the *container* is x86-64). The v146
free release ships `cloakbrowser-linux-x64.tar.gz` (and `windows-x64`); that
release has **no** `linux-arm64` asset (the previous patch, `...177.4`, did).
Because the container target is x86-64, the linux-x64 binary fits natively
inside the container — no extra emulation cost for the binary itself.

**Pinned artifact (integrity-verified at build):**
- URL: `https://github.com/CloakHQ/CloakBrowser/releases/download/chromium-v146.0.7680.177.5/cloakbrowser-linux-x64.tar.gz`
- SHA256: `4a12bcde95fa1bb1beef2b41ab5e5c27c36be78e3be3d0dac8c64d705216670e`
  (build log: `/tmp/cloakbrowser.tar.gz: OK`)

---

## 2. Integration design

**Off by default.** The standard bundled-Chromium path is byte-identical
unless `CLOAKBROWSER_ENABLED=true`. When on, `enforce_cloakbrowser()` retargets
each `BrowserConfig` at the baked-in binary; `crawl4ai/browser_manager.py`
picks that up at launch.

Runtime switches (all env):
| Var | Default | Meaning |
|---|---|---|
| `CLOAKBROWSER_ENABLED` | `false` | master switch |
| `CLOAKBROWSER_BINARY_PATH` | `/opt/cloakbrowser/chrome` | binary location |
| `CLOAKBROWSER_HEADLESS` | `true` | `false` = headful on Xvfb |

**Files (commit `b33dbc3`, 6 files, +212/−7):**
| File | Change |
|---|---|
| `deploy/docker/cloak_broker.py` | **new** — `enforce_cloakbrowser()` |
| `deploy/docker/api.py` | hook wired into all 5 crawl endpoints (after `enforce_egress`) |
| `crawl4ai/async_configs.py` | `BrowserConfig.executable_path` (field, doc, `to_dict`) |
| `crawl4ai/browser_manager.py` | launch-path handling (both paths) |
| `Dockerfile` | CloakBrowser stage (binary + sha256 pin + xvfb/openbox/fonts) |
| `deploy/docker/entrypoint.sh` | Xvfb `:99` + openbox block before `exec supervisord` |

**What `browser_manager.py` does when `executable_path` is set:**
- drops `--disable-gpu` / `--disable-gpu-compositing` / `--disable-software-rasterizer`
  (they fight the binary's GPU/WebGL spoof) — in **both** launch paths, including
  re-filtering `extra_args` from `config.yml` (which re-adds `--disable-gpu`);
- pins the fingerprint profile: `--fingerprint=42424 --fingerprint-platform=windows`
  (fixed seed = one consistent identity per host, instead of a new device every
  launch; `windows` is the wrapper's default profile on Linux);
- **skips** the UA / `sec-ch-ua` override so the binary's native, coherent
  fingerprint is preserved (a mismatched override is itself a detection signal).

**Dockerfile stage:** installs `xvfb xdotool openbox fontconfig` + baseline font
families (per CloakHQ "Font Setup on Linux" — the binary spoofs Windows and
canvas-hash anti-bot needs the emoji + CJK layers); downloads the pinned
release, `sha256sum -c`, extracts to `/opt/cloakbrowser`, `chmod +x`.

**entrypoint.sh:** when `CLOAKBROWSER_ENABLED=true` **and** `CLOAKBROWSER_HEADLESS=false`,
cleans stale X locks, starts `Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp`,
polls `xdotool getdisplaygeometry` until it accepts connections, starts
`openbox`, and `export DISPLAY=:99`. (Same pattern as CloakHQ's own Docker image.)

---

## 3. Per-URL results vs. the reference (nodriver/cfbridge) run

Reference numbers are the earlier independent run of the same 9-URL set with
the **nodriver** (cfbridge) tier. "Cloak" rows are this integration.

### 3a. Isolation — CloakBrowser **headless**, fallback **off**
(Proves CloakBrowser is actually driving the crawl; CF-blocked targets fail
hard, non-CF targets succeed.)

| URL | Cloak headless | ref (nodriver) |
|---|---|---|
| boardgamegeek/hanabi | **BLOCKED** — CF JS challenge (~1.2s) | SOLVED 17.1s |
| stimson.org/2026 | **BLOCKED** — CF JS challenge (~0.9s) | SOLVED 12.2s / 27 chunks |
| freecodecamp.org | **BLOCKED** — empty markdown (~1.8s) | SOLVED 13.2s / 28 chunks |
| carmax.com/mustang-mach-e | **BLOCKED** — Akamai block (~0.9s) | partial 306 chars |
| scrapling.readthedocs | **BLOCKED** — CF JS challenge | 4.3s (served) |
| xhinker | ok 5.4s | 15.7s (c4ai tier) |
| oneuptime | ok 5.0s | 6.2s |
| langchain | ok 4.5s | 4.1s |
| infoworld | ok 5.1s | 3.9s |

### 3b. Headful — CloakBrowser on **Xvfb** (`CLOAKBROWSER_HEADLESS=false`), fallback **off**
(Verified in the live process: `--ozone-platform=x11`, no `--headless`,
`--fingerprint=42424 --fingerprint-platform=windows`, Xvfb answering `1920x1080`.)

| URL | Cloak headful | note |
|---|---|---|
| boardgamegeek/hanabi | **BLOCKED** — CF (6.7s) | headful did not clear it |
| stimson.org/2026 | **BLOCKED** — CF (3.4s) | |
| freecodecamp.org | **BLOCKED** — empty md (3.7s) | |
| carmax.com/mustang-mach-e | **BLOCKED** — Akamai (1.0s) | |
| scrapling.readthedocs | **BLOCKED** — CF (1.4s) | |

**Key finding:** on this stack, the CloakBrowser binary (v146, latest free) does
**not** clear Cloudflare managed challenges or Akamai — in **either** headless or
headful-on-Xvfb mode, even with a coherent fingerprint. Current CF/Turnstile
detection is evidently catching it. This is the honest ceiling of the free
binary; it is not a misconfiguration (the launch args and display were verified).

### 3c. Combo — CloakBrowser primary + **cfbridge fallback ON** (`CF_FALLBACK_ENABLED=true`)
This is the production design. CloakBrowser fails the CF/Akamai targets in ~1s,
cfbridge rescues them. **5/5 solved.**

| URL | Cloak combo (tier, latency) | ref (nodriver) |
|---|---|---|
| boardgamegeek/hanabi | **SOLVED** — via **cfbridge**, 20.6s (999 ch) | SOLVED 17.1s |
| stimson.org/2026 | **SOLVED** — via **cfbridge**, 21.8s (27 chunks) | SOLVED 12.2s / 27 chunks |
| freecodecamp.org | **SOLVED** — via **cfbridge**, 15.7s (3924 ch) | SOLVED 13.2s / 28 chunks |
| carmax.com/mustang-mach-e | **SOLVED** — via **cfbridge**, 10.1s (1057 ch) | partial 306 chars |
| scrapling.readthedocs | **SOLVED** — via **crawl4ai**, 7.4s (27 chunks) | 4.3s (served) |

> Tier attribution is authoritative from `wellisearch` logs
> (`…: ok/unchanged (N ms, K chunks, via=cfbridge|crawl4ai)`); the
> `/api/refresh` JSON body does not echo `via` — it is only in `crawl_log.detail`.
> Note: scrapling is a legit page ("StealthyFetcher class" docs) that merely
> *mentions* Cloudflare in its body — a naive "cloudflare" string match
> false-positives on it; it is genuinely solved.

**carmax** improved from "partial 306 chars" (reference) to "1057 chars" here —
same cfbridge engine, better result this run.

---

## 4. Concurrency & memory

4 CF-blocked URLs (all cfbridge-bound) fired **in parallel**, `docker stats`
sampled every 3s during the run:

- **4/4 SOLVED**, total wall **13.6s**, per-URL spread **8.5s–13.5s** — no
  pathological queueing; cfbridge absorbed the burst.
- Peak memory: **crawl4ai 1.79GiB / 16GiB**, **cfbridge 2.02GiB / 6GiB**,
  **wellisearch 379MiB / 16GiB**.
- **No OOM kills, no restarts, zero OOM log lines** on crawl4ai/cfbridge.

cfbridge CPU spiked to ~650% (multi-core) at the burst, then settled — expected.

---

## 5. Limitations

- **Does not beat current Cloudflare / Akamai.** The free v146 binary is
  detected by CF managed challenges and Akamai in both headless and headful
  modes. It is therefore useful as a *general* stealth tier, but on this
  stack the **cfbridge (nodriver) fallback remains the component that actually
  clears the CF/Akamai walls.**
- **No `linux-arm64` in the free v146.177.5 release** — only linux-x64 and
  windows-x64. (The prior patch, `.177.4`, had an arm64 asset.) The container
  target is x86-64, so this is not a blocker today; it would matter if the
  stack ever moved to native arm64.
- **No real Microsoft fonts** (Segoe UI, etc.) — proprietary, cannot be
  installed from a package repo. Baseline + Noto/CJK/emoji families are
  installed per CloakHQ's guidance, which is the best available on Linux.
- **Shared fixed fingerprint seed** (`42424`) = one consistent identity per
  host by design; all crawls from this host present as the same device.
- **License:** binary is proprietary — the built image must stay private (no
  push to a public registry).

---

## 6. Running-stack state & re-activation / revert

**Current state (restored to known-good):**
- `wellisearch` — healthy, standard config, `CF_FALLBACK_ENABLED=true`.
- `crawl4ai` — **standard** image `wellingtonhq/crawl4ai:0.9.2-wellisearch`
  (CloakBrowser OFF).
- `cfbridge` — healthy.
- The built stealth image is preserved locally as tag
  **`wellisearch-c4ai-cloak:latest`** (7.42GB) — ready to re-activate.
- `wellisearch/` git tree: **clean** (`compose.yml`, `.env` reverted).
- Fork repo: commit `b33dbc3` on `feature/cloak-browser` (unpushed).

**To RE-ACTIVATE the stealth tier** (from `wellisearch/`):
```bash
# 1) point compose at the cloak image + enable the binary (headful for CF)
#    in compose.yml, set the crawl4ai service to:
#      build: ../crawl4ai
#      image: wellisearch-c4ai-cloak:latest
#      environment: CLOAKBROWSER_ENABLED=true
#                   CLOAKBROWSER_HEADLESS=false
# 2) ensure .env: CF_FALLBACK_ENABLED=true
docker compose up -d --no-build crawl4ai wellisearch
```

**To REVERT** to standard (what is running now):
```bash
git checkout -- compose.yml          # restore standard crawl4ai service
# .env: CF_FALLBACK_ENABLED=true
docker compose up -d --no-build crawl4ai
```

**To RE-BUILD the cloak image** (from `crawl4ai/`):
```bash
docker build -t wellisearch-c4ai-cloak:latest .
```

---

## 7. Gotchas (learned this run)

- **`via=` is not in the `/api/refresh` JSON body.** It is only in
  `crawl_log.detail` (and the `wellisearch` log line). Read the log or the DB
  for tier attribution — don't trust a missing `via` key to mean "crawl4ai."
- **`/api/refresh` vs `/api/fetch`:** `/api/refresh` forces a **live crawl**;
  `/api/fetch` serves **stored** content. For live-crawl tests use `/api/refresh`.
  A result of `status:"unchanged"` means the content was already stored — its
  latency is not a clean first-crawl number.
- **`docker exec` does NOT inherit the container env.** `docker exec … env |
  grep DISPLAY` shows empty even when the worker has `DISPLAY=:99`. Check
  `/proc/<pid>/environ` for a specific process, or trust the entrypoint log.
- **`env VAR=x docker compose up` does not reach the container.** Set the var
  in `compose.yml`/`.env` and recreate.
- **The "cloudflare" string match is a false positive** on pages that merely
  discuss Cloudflare (e.g. scrapling's stealthy-fetching docs). Classify on
  status + real challenge markers, not a bare substring.
- **Xvfb socket:** `/tmp/.X11-unix/X99` may appear momentarily absent right
  after start; `xdotool getdisplaygeometry` is the reliable readiness probe
  (used by the entrypoint).
- **GPU flags re-leak:** `config.yml` `extra_args` re-adds `--disable-gpu`
  *after* `build_browser_flags` strips it — the launch path filters a second
  time (both paths handled).
- **Crawl > ~130s** = treat as TIMEOUT and move on (per-stack convention).
- **No secrets in this report / commit.** (The Dockerfile sha256 is a
  published integrity checksum, not a credential.)
