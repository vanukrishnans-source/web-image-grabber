"""Self test: a local HTTP fixture site + the real scan/download/save code (and optionally the real render mode)."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    CHECKS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" - " + detail) if detail else ""), flush=True)
    return ok


# ------------------------------------------------------------------ fixture images (generated with Qt)

def _img_bytes(w: int, h: int, fmt: str, seed: str) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QBrush
    hh = int(hashlib.md5(seed.encode()).hexdigest()[:6], 16)
    c1 = QColor((hh >> 16) & 255, (hh >> 8) & 255, hh & 255)
    c2 = QColor(255 - c1.red(), 255 - c1.green(), 255 - c1.blue())
    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, c1)
    g.setColorAt(1, c2)
    p.fillRect(0, 0, w, h, QBrush(g))
    p.setPen(QColor("white"))
    f = p.font()
    f.setPixelSize(max(8, h // 8))
    p.setFont(f)
    p.drawText(10, h // 2, seed)
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    ok = img.convertToFormat(QImage.Format_RGB32 if fmt in ("JPG",) else QImage.Format_ARGB32).save(buf, fmt, 92 if fmt in ("JPG", "WEBP") else -1)
    buf.close()
    if not ok:
        raise RuntimeError(f"cannot encode {fmt}")
    return bytes(ba)


def build_fixture() -> dict[str, tuple[str, bytes]]:
    """path -> (content-type, body)"""
    I = {}

    def add(path, w, h, fmt, ctype):
        I[path] = (ctype, _img_bytes(w, h, fmt, path.rsplit("/", 1)[-1]))

    add("/img/og.jpg", 1200, 630, "JPG", "image/jpeg")
    add("/img/favicon.png", 32, 32, "PNG", "image/png")
    add("/img/photo1.jpg", 800, 600, "JPG", "image/jpeg")
    add("/img/small.jpg", 200, 150, "JPG", "image/jpeg")
    add("/img/pic-400.jpg", 400, 300, "JPG", "image/jpeg")
    add("/img/pic-800.jpg", 800, 600, "JPG", "image/jpeg")
    add("/img/pic-1600.jpg", 1600, 1200, "JPG", "image/jpeg")
    add("/img/lazy1.png", 500, 300, "PNG", "image/png")
    add("/img/bg-inline.png", 640, 360, "PNG", "image/png")
    add("/img/bg-css.jpg", 1024, 512, "JPG", "image/jpeg")
    add("/img/pic-hero.webp", 1200, 800, "WEBP", "image/webp")
    add("/img/photo-fallback.jpg", 300, 200, "JPG", "image/jpeg")
    add("/img/protected.jpg", 700, 500, "JPG", "image/jpeg")
    add("/img/cookie.png", 420, 420, "PNG", "image/png")
    add("/img/noext", 300, 200, "PNG", "image/png")
    add("/a/same.jpg", 360, 240, "JPG", "image/jpeg")
    add("/b/same.jpg", 361, 241, "JPG", "image/jpeg")
    add("/img/ldjson.jpg", 900, 600, "JPG", "image/jpeg")
    add("/img/script.png", 480, 320, "PNG", "image/png")
    add("/img/poster.jpg", 960, 540, "JPG", "image/jpeg")
    add("/img/full-big.jpg", 2400, 1600, "JPG", "image/jpeg")
    add("/img/js-made.jpg", 640, 480, "JPG", "image/jpeg")
    add("/img/js-lazy.jpg", 777, 555, "JPG", "image/jpeg")
    I["/img/logo.svg"] = ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
                          b'<rect width="200" height="100" fill="#3366cc"/><circle cx="100" cy="50" r="30" fill="#ffcc00"/></svg>')
    I["/track/pixel.gif"] = ("image/gif", base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"))
    return I


EXPECTED_NAMES = {"og.jpg", "favicon.png", "photo1.jpg", "pic-1600.jpg", "lazy1.png", "bg-inline.png", "bg-css.jpg",
                  "pic-hero.webp", "protected.jpg", "cookie.png", "noext", "same.jpg", "ldjson.jpg", "script.png",
                  "poster.jpg", "full-big.jpg", "logo.svg"}
# +2 files named same.jpg (a/ and b/) and one data: URI image (image-N)
NOT_EXPECTED = {"small.jpg", "pic-400.jpg", "pic-800.jpg", "photo-fallback.jpg", "missing.jpg"}


class Fixture:
    def __init__(self, port: int = 0):
        self.images = build_fixture()
        self.hits: list[tuple[str, str, str]] = []
        self.port = port
        data_png = _img_bytes(64, 64, "PNG", "datauri")
        self.data_uri = "data:image/png;base64," + base64.b64encode(data_png).decode()
        self.data_png = data_png
        fx = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, ctype, body, extra=None, gz=False):
                if gz:
                    body = gzip.compress(body)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                ref = self.headers.get("Referer", "")
                ua = self.headers.get("User-Agent", "")
                fx.hits.append((path, ref, ua))
                host = f"127.0.0.1:{fx.port}"
                if path == "/redirect":
                    return self._send(302, "text/plain", b"", {"Location": "/page.html"})
                if path == "/page.html":
                    html = fx.page_html().encode()
                    return self._send(200, "text/html; charset=utf-8", html, {"Set-Cookie": "sess=abc123; Path=/"}, gz=True)
                if path == "/js.html":
                    return self._send(200, "text/html; charset=utf-8", fx.js_html().encode())
                if path == "/css/site.css":
                    css = b"@font-face{font-family:x;src:url(../fonts/f.woff2)} .hero{background:url(../img/bg-css.jpg) no-repeat}"
                    return self._send(200, "text/css", css)
                if path == "/blocked.html":
                    return self._send(403, "text/html", b"<html>Just a moment... cloudflare</html>")
                if path == "/login.html":
                    return self._send(401, "text/html", b"login")
                if path == "/notfound.html":
                    return self._send(404, "text/html", b"nope")
                if path == "/empty.html":
                    return self._send(200, "text/html", b"<html><body><p>No pictures here</p></body></html>")
                if path == "/img/protected.jpg" and ("127.0.0.1" not in ref):
                    return self._send(403, "text/plain", b"hotlink denied")
                if path == "/img/cookie.png" and "sess=abc123" not in self.headers.get("Cookie", ""):
                    return self._send(403, "text/plain", b"cookie needed")
                if path in fx.images:
                    ctype, body = fx.images[path]
                    return self._send(200, ctype, body)
                if path == "/img/missing.jpg":
                    return self._send(404, "text/plain", b"nope")
                return self._send(404, "text/plain", b"nope")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.handle_error = lambda *a, **k: None
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()

    def page_html(self) -> str:
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Fixture Page \u2014 Gallery</title>
<meta property="og:image" content="{self.base}/img/og.jpg">
<link rel="icon" href="img/favicon.png">
<link rel="stylesheet" href="/css/site.css">
<style>.inline-style{{background:url('/img/bg-inline.png')}}</style>
<script type="application/ld+json">{{"@type":"Article","image":["{self.base}/img/ldjson.jpg"]}}</script>
<script>var cfg = {{"hero": "{self.base.replace('/', chr(92) + '/')}\\/img\\/script.png"}};</script>
</head><body>
<h1>Fixture</h1>
<img src="img/photo1.jpg" alt="relative">
<img src="/img/small.jpg" srcset="/img/pic-400.jpg 400w, /img/pic-1600.jpg 1600w, /img/pic-800.jpg 800w" alt="srcset">
<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" data-src="/img/lazy1.png" alt="lazy">
<div class="inline-style" style="width:10px">css inline</div>
<div class="hero">css linked</div>
<picture><source srcset="/img/pic-hero.webp 1200w" type="image/webp"><img src="/img/photo-fallback.jpg"></picture>
<img src="{self.data_uri}" alt="data uri">
<img src="img/photo1.jpg" alt="duplicate url">
<img src="/img/photo1.jpg?cachebust=7" alt="same bytes other url">
<img src="/track/pixel.gif" width="1" height="1" alt="">
<img src="/img/protected.jpg" alt="hotlink protected">
<img src="/img/cookie.png" alt="cookie protected">
<img src="/img/noext" alt="no extension">
<img src="/a/same.jpg"><img src="/b/same.jpg">
<video poster="/img/poster.jpg"></video>
<a href="/img/full-big.jpg"><span>download big</span></a>
<img src="/img/logo.svg">
<img src="/img/missing.jpg">
</body></html>"""

    def js_html(self) -> str:
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>JS page</title></head><body style="margin:0">
<div id="top">Loading...</div>
<div style="height:3000px"></div>
<img id="lz" data-lazy="pic-js-lazy" width="300" height="200" alt="loads only when scrolled into view">
<script>
setTimeout(function(){{ var i=new Image(); i.src="/img/js-" + "made" + ".jpg"; i.width=300; document.getElementById('top').appendChild(i); }}, 300);
new IntersectionObserver(function(es){{ es.forEach(function(e){{ if(e.isIntersecting){{ e.target.src="/img/js-" + "lazy" + ".jpg"; }} }}); }}).observe(document.getElementById('lz'));
</script></body></html>"""


def serve_forever(port: int = 0) -> int:
    from PySide6.QtGui import QGuiApplication
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv[:1])
    fx = Fixture(port).start()
    print(fx.base + "/page.html", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


# ------------------------------------------------------------------ the test

def _scan(url=None, urls=None, html_text=None, html_base=None, render=False, tmp: Path | None = None):
    from . import core
    tmp = tmp or Path(tempfile.mkdtemp(prefix="wig-test-"))
    items = []
    sc = core.Scanner(tmp, lambda s: None, lambda a, b: None, items.append)
    opts = core.ScanOptions(render=render)
    summary = sc.run(urls, url, opts, html_text=html_text, html_base=html_base)
    items.sort(key=lambda i: i.order)
    return sc, summary, items


def _sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def run(out: Path, require_render: bool = False) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication, QImageReader
    app = QGuiApplication(sys.argv[:1])
    from . import core, extract, net, render as render_mod
    out = out.resolve()
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    fx = Fixture().start()
    base = fx.base
    report: dict = {"base": base}
    try:
        # ---- 1. plain scan through a redirect (also gzip page + cookie)
        sc, summary, items = _scan(url=base + "/redirect", tmp=out / "tmp1")
        names = [i.name for i in items]
        report["found"] = [{"name": i.name, "url": i.url, "w": i.w, "h": i.h, "type": i.kind, "bytes": i.size, "source": i.source} for i in items]
        check("redirect followed (final page url)", summary.page_url == base + "/page.html", summary.page_url)
        check("page title read", summary.title.startswith("Fixture Page"), summary.title)
        nontiny = [i for i in items if not i.tiny]
        got = {i.name for i in nontiny}
        missing = {n for n in EXPECTED_NAMES if n not in got}
        check("every expected image found", not missing, f"missing={sorted(missing)}")
        check("srcset: only the largest candidate", "pic-1600.jpg" in got and not ({"pic-400.jpg", "pic-800.jpg", "small.jpg"} & got))
        check("picture fallback not duplicated", "photo-fallback.jpg" not in got)
        check("two different same.jpg both kept", names.count("same.jpg") == 2, str(names.count("same.jpg")))
        check("data: URI image found", any(i.url.startswith("data:image/png") and i.w == 64 for i in items))
        tiny = [i for i in items if i.tiny]
        check("tracking pixels flagged tiny (1x1 gif + data gif)", len(tiny) >= 1 and all(i.w < 16 for i in tiny), f"{len(tiny)} tiny")
        check("duplicate url + same bytes merged", names.count("photo1.jpg") == 1 and summary.duplicates >= 1, f"dups={summary.duplicates}")
        check("broken image counted as failed, not shown", any("missing.jpg" in u for u, _ in summary.failed) and "missing.jpg" not in names)
        check("hotlink protected image loaded (Referer sent)", "protected.jpg" in got)
        check("cookie protected image loaded (cookies kept)", "cookie.png" in got)
        check("browser User-Agent sent", any(h[0] == "/page.html" and "Mozilla/5.0" in h[2] and "python" not in h[2].lower() for h in fx.hits))
        by = {i.name: i for i in nontiny if i.name != "same.jpg"}
        dims_ok = all((by[n].w, by[n].h) == d for n, d in {"full-big.jpg": (2400, 1600), "pic-1600.jpg": (1600, 1200), "og.jpg": (1200, 630),
                                                           "pic-hero.webp": (1200, 800), "logo.svg": (200, 100)}.items() if n in by)
        check("dimensions of originals are right (not thumbnails)", dims_ok)
        check("thumbnails generated for every image", all(i.thumb for i in nontiny), f"{sum(1 for i in nontiny if not i.thumb)} without")

        # ---- 2. save a subset; exact files, full resolution, clean names
        pick_names = ["full-big.jpg", "pic-hero.webp", "noext", "same.jpg", "logo.svg"]
        pick = [i for i in items if i.name in pick_names]
        save_dir = out / "saved_subset"
        res = core.save_items(pick, save_dir, "keep")
        files = sorted(p.name for p in save_dir.iterdir())
        report["saved_subset"] = files
        check("exactly the selected subset saved", len(res.saved) == len(pick) == len(files) == 6, str(files))
        check("name clash -> same.jpg + same-1.jpg", "same.jpg" in files and "same-1.jpg" in files)
        check("extension added when missing (noext -> noext.png)", "noext.png" in files)
        orig = fx.images["/img/full-big.jpg"][1]
        check("saved original is byte-identical, full 2400x1600", (save_dir / "full-big.jpg").read_bytes() == orig and
              QImageReader(str(save_dir / "full-big.jpg")).size().width() == 2400)
        check("unselected images not saved", not (save_dir / "og.jpg").exists() and not (save_dir / "photo1.jpg").exists())
        res2 = core.save_items(pick, save_dir, "keep")
        files2 = sorted(p.name for p in save_dir.iterdir())
        check("saving again never overwrites (-1/-2 suffixes)", len(files2) == 12 and "same-3.jpg" in files2 or "same-2.jpg" in files2, str(files2))

        # ---- 3. webp conversion
        cdir = out / "saved_convert"
        core.save_items([i for i in items if i.name == "pic-hero.webp"], cdir, "jpg")
        cj = list(cdir.glob("*.jpg"))
        check("webp -> jpg conversion at full size", len(cj) == 1 and QImageReader(str(cj[0])).size().width() == 1200)
        core.save_items([i for i in items if i.name == "pic-hero.webp"], cdir, "png")
        check("webp -> png conversion", len(list(cdir.glob("*.png"))) == 1)

        # ---- 4. subfolder name
        check("subfolder name from page title", core.page_folder_name(summary.title, summary.page_url) == "Fixture Page \u2014 Gallery", core.page_folder_name(summary.title, summary.page_url))

        # ---- 5. pasted URL list
        urls = [base + "/img/og.jpg", base + "/img/poster.jpg", base + "/img/og.jpg", base + "/img/nothing.png"]
        _, s5, it5 = _scan(urls=urls, tmp=out / "tmp5")
        check("pasted URL list: deduped, 404 reported", len(it5) == 2 and len(s5.failed) == 1, f"{len(it5)} loaded {len(s5.failed)} failed")

        # ---- 6. dropped .html file (local relative paths + absolute http image)
        d6 = out / "dropped"
        (d6 / "pics").mkdir(parents=True)
        (d6 / "pics" / "local.jpg").write_bytes(fx.images["/img/photo1.jpg"][1])
        (d6 / "pics" / "local2.png").write_bytes(fx.images["/img/lazy1.png"][1])
        (d6 / "page.html").write_text(f'<html><title>Dropped</title><img src="pics/local.jpg"><img srcset="pics/local2.png 2x"><img src="{base}/img/poster.jpg"></html>', "utf-8")
        _, s6, it6 = _scan(html_text=(d6 / "page.html").read_text("utf-8"), html_base=(d6 / "page.html").as_uri(), tmp=out / "tmp6")
        check("dropped .html: local + remote images found", sorted(i.name for i in it6) == ["local.jpg", "local2.png", "poster.jpg"], str([i.name for i in it6]))

        # ---- 7. errors give clear messages
        def err_of(path):
            try:
                _scan(url=base + path, tmp=out / "tmp7")
            except net.FetchError as e:
                return str(e)
            return ""
        e403, e401, e404, eno = err_of("/blocked.html"), err_of("/login.html"), err_of("/notfound.html"), err_of("/empty.html")
        check("403 bot-check message", "403" in e403 and "bot" in e403.lower(), e403)
        check("401 login message", "login" in e401.lower(), e401)
        check("404 message", "404" in e404, e404)
        check("no-images message mentions Render mode", "Render" in eno, eno)
        try:
            _scan(url="http://127.0.0.1:1/x.html", tmp=out / "tmp7")
            ec = ""
        except net.FetchError as e:
            ec = str(e)
        check("connection refused message", "refused" in ec.lower() or "connect" in ec.lower(), ec)

        # ---- 8. render mode (real Edge/Chrome)
        br = render_mod.find_browser()
        report["browser"] = br
        if br:
            try:
                _, s8p, it8p = _scan(url=base + "/js.html", tmp=out / "tmp8p")
                n8p = [i.name for i in it8p]
            except net.FetchError:
                n8p = []
            check("plain mode misses JS-built images (as expected)", "js-made.jpg" not in n8p and "js-lazy.jpg" not in n8p)
        if br:
            try:
                _, s8, it8 = _scan(url=base + "/js.html", render=True, tmp=out / "tmp8")
                n8 = [i.name for i in it8]
                check("render mode finds JS-created + scroll-lazy images", "js-made.jpg" in n8 and "js-lazy.jpg" in n8, str(n8))
            except Exception as e:                     # noqa
                check("render mode runs", False, repr(e))
        else:
            check("render mode browser available (skipped: no Edge/Chrome here)", not require_render, "no browser")
    finally:
        fx.stop()
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    report["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS]
    report["ok"] = passed == len(CHECKS)
    (out / "selftest_report.json").write_text(json.dumps(report, indent=2))
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    return 0 if report["ok"] else 1
