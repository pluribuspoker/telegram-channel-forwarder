#!/usr/bin/env python3
"""Regression: XClIdGen bootstrap must survive X's web-build changes.

Pins two total Trent outages, both "every candidate page failed at once":

2026-08-24 — X redeployed its legacy webpack build with 16-hex chunk hashes
(7-hex for years before), so twscrape's hash-map regex (`[0-9a-f]{7}` exact)
matched nothing and every candidate page raised "Failed to parse scripts".
Also pins the subtle half of the fix: the name map must exclude hash-like
values by the SAME length-agnostic pattern, otherwise the 16-hex hashes leak
in as chunk NAMES and every reconstructed URL doubles the hash
(`{hash}.{hash}a.js`).

2026-09-24 — the x-web build stopped linking its chunks in the page HTML (one
entry script only) and the `sign.o-*.js` indices file moved behind a dynamic
import in a chunk the entry references (`sentry-filter-*.js`): two hops from
the HTML, so a level-1 scan found nothing on any page. Pinned over the
verbatim capture from that night: page HTML -> entry chunk -> sentry-filter
slice (byte-exact window [288000:304000] of the 538KB chunk, containing the
sign.o import) -> sign.o file. `_parse_anim_idx` must walk that chain, fetch
the carrier FIRST at depth 2 (priority), and stop early.

Then the self-heal for the NEXT move, over synthetic graphs (pure graph-walk
logic, so synthetic bodies are fine): a reference 3 hops deep, the
content-signature last resort for a renamed file (and that a name hit always
beats it), the cluster test's rejections, the fetch budget, the per-process
chunk-digest cache, and the bootstrap report that tells trent_watcher (and
diagnose_failure) how the bootstrap got through.

Fixtures are verbatim captured bytes, never retyped. Fully offline.

    python scripts/test_xclid_scripts_parse.py
"""
import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import x_client as xc  # noqa: E402
from scripts.x_client import (  # noqa: E402
    _JS_REF_RE,
    _SCAN_BATCH,
    _SCAN_FETCH_BUDGET,
    _get_scripts_list,
    _parse_anim_idx,
    bootstrap_report,
    patch_xclid,
)
from twscrape import xclid as _xclid  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "x_home_webpack_maps_20260824.html"

# The exact indices-chunk URL the 2026-08-24 build serves (verified live: 200,
# INDICES_REGEX yields [15, 34, 11, 27]).
ONDEMAND_URL = "https://abs.twimg.com/responsive-web/client-web/ondemand.s.d03eda01013904f3a.js"

URL_SHAPE = re.compile(
    r"^https://abs\.twimg\.com/responsive-web/client-web/(?P<name>.+)\.(?P<hash>[0-9a-f]{7,})a\.js$"
)

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def _fresh():
    """Back to a clean process: the chunk-digest cache and the bootstrap report
    are process-scoped, and some scenarios below reuse a URL with other bytes
    (impossible live — chunk URLs are content-hashed)."""
    xc._CHUNK_DIGESTS.clear()
    xc._BOOTSTRAP_REPORT.update(create_failures=0, fallback_page=None, heuristic=False)


text = FIXTURE.read_text()

# 1. Stock twscrape (7-hex only) goes blind on this build — the outage.
stock = _xclid.get_scripts_list
if getattr(stock, "__module__", "").startswith("twscrape"):
    try:
        stock(text)
        check("stock parser fails on 16-hex build (outage repro)", False, "parsed fine?!")
    except Exception:
        check("stock parser fails on 16-hex build (outage repro)", True)
else:
    print("SKIP  stock parser already patched in this process")

# 2. Patched parser reconstructs the chunk list, including the indices chunk.
urls = _get_scripts_list(text)
check("patched parser returns URLs", len(urls) > 500, f"got {len(urls)}")
check("indices chunk URL present", ONDEMAND_URL in urls)

# 3. Every URL is well-formed and no NAME is itself a bare hash (the name-map
#    leak would render {hash}.{hash}a.js).
bad_shape = [u for u in urls if not URL_SHAPE.match(u)]
check("all URLs match {name}.{hash}a.js", not bad_shape, f"e.g. {bad_shape[:2]}")
doubled = [
    u for u in urls
    if (m := URL_SHAPE.match(u)) and re.fullmatch(r"[0-9a-f]{7,}", m.group("name"))
]
check("no hash leaked into the name map", not doubled, f"e.g. {doubled[:2]}")

# 4. Backward compatible with the old 7-hex build.
legacy = '{1:"abc1234",2:"def5678"}...{1:"ondemand.s",2:"vendors~main"}'
legacy_urls = _get_scripts_list(legacy)
check(
    "7-hex legacy build still parses",
    legacy_urls == [
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.abc1234a.js",
        "https://abs.twimg.com/responsive-web/client-web/vendors~main.def5678a.js",
    ],
    f"got {legacy_urls}",
)

# 5. x-web (Vite) pages: directly-linked assets returned as-is.
xweb = '<link rel="modulepreload" href="https://abs.twimg.com/x-web/x-web/entry-client-logged-out-DCPSu4tq.js">'
check(
    "x-web direct links pass through",
    _get_scripts_list(xweb)
    == ["https://abs.twimg.com/x-web/x-web/entry-client-logged-out-DCPSu4tq.js"],
)

# 6. A page with neither scheme still raises (self-heal moves to the next page).
try:
    _get_scripts_list("<html><body>nothing here</body></html>")
    check("script-less page raises", False, "returned instead of raising")
except Exception:
    check("script-less page raises", True)

# 7. patch_xclid() actually rebinds the module-globals twscrape resolves at
#    call time (load_keys/parse_anim_idx look them up on every call).
patch_xclid()
check("patch_xclid rebinds twscrape.xclid.get_scripts_list",
      _xclid.get_scripts_list is _get_scripts_list)
check("patch_xclid rebinds twscrape.xclid.parse_anim_idx",
      _xclid.parse_anim_idx is _parse_anim_idx)

# --- 2026-09-24: two-level indices scan ------------------------------------

FIXDIR = Path(__file__).resolve().parent / "fixtures"
PAGE_0924 = (FIXDIR / "x_home_xweb_20260924.html").read_text()
ENTRY_0924 = (FIXDIR / "x_entry_chunk_20260924.js").read_text()
SENTRY_0924 = (FIXDIR / "x_sentry_filter_slice_20260924.js").read_text()
SIGN_0924 = (FIXDIR / "x_sign_o_20260924.js").read_text()


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    """Serves mapped URLs; anything else raises (-> _fetch_text yields "")."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.log: list[str] = []

    async def get(self, url: str, *a, **k):
        self.log.append(url)
        if url in self.mapping:
            return _FakeResp(self.mapping[url])
        raise Exception(f"unmapped {url}")


entry_url = _get_scripts_list(PAGE_0924)[0]
sentry_url = urljoin(entry_url, "./assets/sentry-filter-DA8h2Jwu.js")
sign_url = urljoin(sentry_url, "./sign.o-DjU_k1xX.js")

# 8. The 2026-09-24 page links exactly one script and names nothing directly —
#    the shape that blinded the level-1 scanner.
check("2026-09 page links a single entry script",
      len(_get_scripts_list(PAGE_0924)) == 1, f"got {len(_get_scripts_list(PAGE_0924))}")
check("entry references the carrier chunk", "sentry-filter-DA8h2Jwu.js" in ENTRY_0924)
check("carrier slice holds the sign.o import", "`./sign.o-DjU_k1xX.js`" in SENTRY_0924)

# 9. Full chain over the real bytes: page -> entry -> sentry-filter -> sign.o.
_fresh()
clt = _FakeClient({entry_url: ENTRY_0924, sentry_url: SENTRY_0924, sign_url: SIGN_0924})
items = asyncio.run(_parse_anim_idx(PAGE_0924, clt))
check("indices parsed from sign.o via two-level scan",
      items == [10, 5, 34, 26], f"got {items}")
check("entry fetched first", clt.log[0] == entry_url)
check("carrier prioritized to front of depth 2", clt.log[1] == sentry_url)
check("indices file fetched last", clt.log[-1] == sign_url)
check("early stop bounds fetch count",
      len(clt.log) <= 2 + _SCAN_BATCH, f"{len(clt.log)} fetches")

# 10. No indices reference anywhere in the graph -> raises so the page-level
#     self-heal (next _XCLID_PAGES candidate) still engages.
_fresh()
clt_miss = _FakeClient({entry_url: ENTRY_0924.replace("sentry-filter", "sentry-flitre")})
try:
    asyncio.run(_parse_anim_idx(PAGE_0924, clt_miss))
    check("graph without indices file raises", False, "returned instead of raising")
except Exception as e:
    check("graph without indices file raises", "indices script" in str(e), str(e)[:80])

# 11. Ref collector sees quoted AND backticked imports (Vite emits both).
refs = _JS_REF_RE.findall('import("./assets/a.js");x=`./b.o-C1.js`;"no.txt"')
check("ref regex matches quoted and backticked .js", refs == ["./assets/a.js", "./b.o-C1.js"],
      f"got {refs}")

# --- Self-heal for the next move: synthetic graphs --------------------------

BASE = "https://abs.twimg.com/x-web/x-web/"


def _page(entry: str) -> str:
    return f'<script type="module" crossorigin src="{BASE}{entry}.js"></script>'


def _imports(*paths: str) -> str:
    return ";".join(f'import("{p}")' for p in paths)


def _sites(*idx: int, gap: str = ",") -> str:
    # SEPARATE call sites, the real file's shape: INDICES_REGEX ends in `+`, so
    # adjacent "(t[1],16)(t[2],16)" would collapse into ONE match.
    return gap.join(f"E[h(a.B)](t[{i}],16)" for i in idx)


def _cluster(span: int) -> str:
    """4 sites whose first-start..last-end distance is exactly `span` bytes."""
    s = ["(t[10],16)", "(t[5],16)", "(t[34],16)", "(t[26],16)"]
    fill = span - sum(map(len, s))
    g = [fill // 3, fill // 3, fill - 2 * (fill // 3)]
    return s[0] + "q" * g[0] + s[1] + "q" * g[1] + s[2] + "q" * g[2] + s[3]


class _GenClient:
    """Bodies from a function of the URL (None -> the fetch raises)."""

    def __init__(self, fn):
        self.fn = fn
        self.log: list[str] = []

    async def get(self, url: str, *a, **k):
        self.log.append(url)
        body = self.fn(url)
        if body is None:
            raise Exception(f"unmapped {url}")
        return _FakeResp(body)


def _run(page: str, client):
    try:
        return asyncio.run(_parse_anim_idx(page, client)), None
    except Exception as e:
        return None, e


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


warn_log = _Records()
logging.getLogger(xc.__name__).addHandler(warn_log)

# 12. A reference THREE hops deep (page -> entry -> mid -> leaf names sign.o).
_fresh()
e3, m3, l3, s3 = (BASE + p for p in ("entry-client-logged-out-D3.js", "assets/mid-D3.js",
                                    "assets/leaf-D3.js", "assets/sign.o-D3x.js"))
clt = _FakeClient({
    e3: _imports("./assets/mid-D3.js"),
    m3: _imports("./leaf-D3.js"),
    l3: "const s=()=>import(`./sign.o-D3x.js`)",
    s3: _sites(10, 5, 34, 26),
})
items, err = _run(_page("entry-client-logged-out-D3"), clt)
check("depth-3 sign.o reference resolved and parsed", items == [10, 5, 34, 26], f"{items} / {err}")
check("depth-3 walk: entry -> mid -> leaf -> sign.o", clt.log == [e3, m3, l3, s3], f"{clt.log}")
check("a name hit is not flagged heuristic", bootstrap_report()["heuristic"] is False)

# 13. ...but not four: the depth cap holds (no name, no signature -> raises).
_fresh()
e4, d2, d3, d4 = (BASE + p for p in ("entry-client-logged-out-D4.js", "assets/d2-D4.js",
                                    "assets/d3-D4.js", "assets/d4-D4.js"))
clt = _FakeClient({
    e4: _imports("./assets/d2-D4.js"),
    d2: _imports("./d3-D4.js"),
    d3: _imports("./d4-D4.js"),
    d4: "import(`./sign.o-D4x.js`)",
})
items, err = _run(_page("entry-client-logged-out-D4"), clt)
check("a reference 4 hops deep is out of range (raises)",
      err is not None and "indices script" in str(err), f"{items} / {err}")
check("depth-4 chunk never fetched", d4 not in clt.log, f"{clt.log}")

# 14. X renamed the file away from both known names: no body names it, one
#     body carries the clustered signature -> content heuristic picks it,
#     flags the report, and logs a WARNING naming the file and the regex to fix.
_fresh()
warn_log.lines.clear()
eh, ph, sh = (BASE + p for p in ("entry-client-logged-out-HEU.js", "assets/plain-HEU.js",
                                "assets/tx-signer-HEU.js"))
clt = _FakeClient({
    eh: _imports("./assets/plain-HEU.js", "./assets/tx-signer-HEU.js"),
    ph: "var n=parseInt(v,16);export{n}",
    sh: "function k(t){var[b,x]=[" + _sites(10, 5, 34, 26) + "]}",
})
items, err = _run(_page("entry-client-logged-out-HEU"), clt)
check("renamed indices file found by content signature", items == [10, 5, 34, 26], f"{items} / {err}")
check("heuristic pick flagged in bootstrap_report()", bootstrap_report()["heuristic"] is True)
check("heuristic WARNING names the file and _INDICES_FILE_RE",
      any(sh in line and "_INDICES_FILE_RE" in line for line in warn_log.lines), f"{warn_log.lines}")

# 15. Best candidate = smallest span, then smallest body.
_fresh()
ew, wide, big, tight = (BASE + p for p in ("entry-client-logged-out-SEL.js", "assets/wide-SEL.js",
                                          "assets/big-SEL.js", "assets/tight-SEL.js"))
tight_body = "function k(t){" + _sites(10, 5, 34, 26) + "}"
clt = _FakeClient({
    ew: _imports("./assets/wide-SEL.js", "./assets/big-SEL.js", "./assets/tight-SEL.js"),
    wide: _sites(1, 2, 3, 4, gap=";" + "q" * 300 + ";"),
    big: tight_body + "/*" + "z" * 5000 + "*/",
    tight: tight_body,
})
items, err = _run(_page("entry-client-logged-out-SEL"), clt)
check("tightest cluster wins, smaller body breaks the tie",
      items == [10, 5, 34, 26] and clt.log[-1] == tight, f"{items} / {clt.log[-1:]}")

# 16. A name hit ALWAYS beats a signature, even one seen first: an early depth-2
#     signature body must not short-circuit the scan ahead of a depth-3 name.
_fresh()
en, early, midn, leafn, signn = (BASE + p for p in (
    "entry-client-logged-out-NBC.js", "assets/early-NBC.js", "assets/mid-NBC.js",
    "assets/leaf-NBC.js", "assets/sign.o-NBC.js"))
clt = _FakeClient({
    en: _imports("./assets/early-NBC.js", "./assets/mid-NBC.js"),
    early: "function k(t){" + _sites(10, 5, 34, 26) + "}",
    midn: _imports("./leaf-NBC.js"),
    leafn: "import(`./sign.o-NBC.js`)",
    signn: _sites(1, 2, 3, 4),
})
items, err = _run(_page("entry-client-logged-out-NBC"), clt)
check("named file wins over an earlier signature body", items == [1, 2, 3, 4], f"{items} / {err}")
check("signature body was scanned before the name hit",
      early in clt.log and clt.log.index(early) < clt.log.index(leafn), f"{clt.log}")
check("name hit leaves the heuristic flag unset", bootstrap_report()["heuristic"] is False)

# 17. The cluster test rejects: 2 sites; 4 sites spread over >4KB (-> raises).
_fresh()
er, two, spread = (BASE + p for p in ("entry-client-logged-out-REJ.js", "assets/two-REJ.js",
                                     "assets/spread-REJ.js"))
clt = _FakeClient({
    er: _imports("./assets/two-REJ.js", "./assets/spread-REJ.js"),
    two: "function k(t){" + _sites(10, 5) + "}",
    spread: _sites(10, 5, 34, 26, gap=";" + "q" * 1400 + ";"),
})
items, err = _run(_page("entry-client-logged-out-REJ"), clt)
check("2-site and >4KB-spread bodies are no candidates (raises)",
      err is not None and "indices script" in str(err), f"{items} / {err}")
check("rejected candidates leave the heuristic flag unset", bootstrap_report()["heuristic"] is False)

# 17b. The 4096-byte span bound is inclusive.
for span, want in ((4096, True), (4097, False)):
    _fresh()
    body = _cluster(span)
    ms = list(_xclid.INDICES_REGEX.finditer(body))
    assert len(ms) == 4 and ms[-1].end() - ms[0].start() == span, "bad _cluster fixture"
    eb, cb = BASE + f"entry-client-logged-out-B{span}.js", BASE + f"assets/c-B{span}.js"
    items, err = _run(_page(f"entry-client-logged-out-B{span}"),
                      _FakeClient({eb: _imports(f"./assets/c-B{span}.js"), cb: body}))
    check(f"span {span} {'accepted' if want else 'rejected'}",
          (items == [10, 5, 34, 26]) if want else (err is not None), f"{items} / {err}")

# 18. The budget: an unbounded graph (every chunk references 30 new ones)
#     terminates after exactly _SCAN_FETCH_BUDGET fetches and raises.
_fresh()


def _unbounded(url: str) -> str:
    stem = url.rsplit("/", 1)[-1][:-3]
    return _imports(*(f"./{stem}-{j}.js" for j in range(30)))


clt = _GenClient(_unbounded)
t0 = time.monotonic()
items, budget_err = _run(_page("entry-client-logged-out-BUD"), clt)
took = time.monotonic() - t0
check("graph wider than the budget raises", budget_err is not None and "indices script" in str(budget_err),
      f"{items} / {budget_err}")
check("fetch budget holds exactly", len(clt.log) == _SCAN_FETCH_BUDGET, f"{len(clt.log)} fetches")
check("budget-exhausted scan is fast (no hang)", took < 10, f"{took:.1f}s")

# 19. The digest cache: a failed run rescans ONE graph ~28x (7 pages x 3
#     twscrape create attempts + diagnose_failure) inside the watcher's 90s
#     timeout, so a repeat scan must download nothing and reach the same verdict.
clt = _GenClient(_unbounded)
items, err = _run(_page("entry-client-logged-out-BUD"), clt)
check("repeat scan of a scanned graph downloads nothing", clt.log == [], f"{len(clt.log)} fetches")
check("repeat scan reaches the same verdict", str(err) == str(budget_err), f"{err}")

# 19b. ...but a FAILED fetch is never cached: the next scan retries it.
_fresh()
ef, cf, sf = (BASE + p for p in ("entry-client-logged-out-FLK.js", "assets/sentry-filter-FLK.js",
                                "assets/sign.o-FLK.js"))
graph = {ef: _imports("./assets/sentry-filter-FLK.js"), cf: "import(`./sign.o-FLK.js`)",
         sf: _sites(10, 5, 34, 26)}
flaky = _FakeClient({ef: graph[ef]})  # carrier fetch fails this time
items, err = _run(_page("entry-client-logged-out-FLK"), flaky)
check("scan with a failed carrier fetch misses", err is not None, f"{items}")
clt = _FakeClient(graph)
items, err = _run(_page("entry-client-logged-out-FLK"), clt)
check("next scan retries only the failed fetch, then finds sign.o",
      items == [10, 5, 34, 26] and clt.log == [cf, sf], f"{items} / {clt.log}")

# --- Bootstrap report + diagnose_failure ------------------------------------
# The page loop and the verdict, with twscrape's network pieces stubbed.


class _NullClient:
    async def aclose(self):
        pass


async def _stub_page(url, clt):
    return f"<html><!-- {url} --></html>"


pages_tried: list[int] = []


async def _keys_fail_first(soup, clt):
    pages_tried.append(1)
    if len(pages_tried) == 1:
        raise Exception("Couldn't get XClientTxId indices script (stub)")
    return [0] * 48, "anim"


async def _keys_ok(soup, clt):
    return [0] * 48, "anim"


async def _keys_fail(soup, clt):
    raise Exception("Couldn't get XClientTxId indices script (stub)")


saved = {k: getattr(_xclid, k) for k in ("_make_client", "get_tw_page_text", "load_keys")}
_xclid._make_client = lambda: _NullClient()
_xclid.get_tw_page_text = _stub_page
try:
    # 20. First page fails, second works -> fallback_page, no create failure.
    _fresh()
    _xclid.load_keys = _keys_fail_first
    asyncio.run(_xclid.XClIdGen.create())
    rep = bootstrap_report()
    check("fallback page recorded in bootstrap_report()",
          rep["fallback_page"] == xc._XCLID_PAGES[1], f"{rep}")
    check("a create that succeeds is not a create failure", rep["create_failures"] == 0, f"{rep}")

    # 21. Every page fails -> XClIdBootstrapError, counted once per create().
    _xclid.load_keys = _keys_fail
    try:
        asyncio.run(_xclid.XClIdGen.create())
        raised = False
    except xc.XClIdBootstrapError:
        raised = True
    check("all-pages failure raises XClIdBootstrapError", raised)
    check("create_failures counts the failed create", bootstrap_report()["create_failures"] == 1)
    check("bootstrap_report() is a copy", bootstrap_report() is not xc._BOOTSTRAP_REPORT)

    # 22. diagnose_failure: a heuristic-only bootstrap blames the build, never
    #     the cookies (the historical wrong-way alert); a clean one says auth.
    _fresh()
    _xclid.load_keys = _keys_ok
    kind, _detail = asyncio.run(xc.diagnose_failure())
    check("clean bootstrap diagnoses as auth", kind == "auth", kind)
    xc._BOOTSTRAP_REPORT["heuristic"] = True
    kind, detail = asyncio.run(xc.diagnose_failure())
    check("heuristic bootstrap diagnoses as bootstrap (code fix, not cookies)",
          kind == "bootstrap" and "_INDICES_FILE_RE" in detail, f"{kind}: {detail[:80]}")
    _fresh()
    _xclid.load_keys = _keys_fail
    kind, detail = asyncio.run(xc.diagnose_failure())
    check("failed bootstrap diagnoses as bootstrap",
          kind == "bootstrap" and "XClIdBootstrapError" in detail, f"{kind}: {detail[:80]}")
finally:
    for k, v in saved.items():
        setattr(_xclid, k, v)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
