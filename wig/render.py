"""Optional 'Render page (JavaScript)' mode: drive the Edge/Chrome that Windows already has (headless, via the DevTools
protocol), scroll the page to trigger lazy loading, then hand back the rendered DOM + every image URL the browser saw."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .net import FetchError, UA

WIN_CANDIDATES = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
]
NIX_CANDIDATES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "msedge"]


def find_browser() -> str | None:
    env = os.environ.get("WIG_BROWSER")
    if env and Path(env).exists():
        return env
    if os.name == "nt":
        for c in WIN_CANDIDATES:
            p = os.path.expandvars(c)
            if "%" not in p and Path(p).exists():
                return p
        for n in ("msedge", "chrome"):
            w = shutil.which(n)
            if w:
                return w
        try:  # registry App Paths
            import winreg
            for exe in ("msedge.exe", "chrome.exe"):
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        k = winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}")
                        v, _ = winreg.QueryValueEx(k, "")
                        if v and Path(v).exists():
                            return v
                    except OSError:
                        continue
        except Exception:
            pass
        return None
    for n in NIX_CANDIDATES:
        w = shutil.which(n)
        if w:
            return w
    return None


JS_COLLECT = r"""
(() => {
  const urls = new Set();
  const addCss = (v) => { if (!v || v === 'none') return; const re = /url\((?:"([^"]*)"|'([^']*)'|([^)]*))\)/g; let m;
    while ((m = re.exec(v))) { const u = (m[1] || m[2] || m[3] || '').trim(); if (u) urls.add(u); } };
  let n = 0;
  for (const el of document.querySelectorAll('*')) {
    if (++n > 30000) break;
    try {
      const cs = getComputedStyle(el); addCss(cs.backgroundImage); addCss(cs.listStyleImage); addCss(cs.content);
      if (el.tagName === 'IMG' && el.currentSrc) urls.add(el.currentSrc);
      if (el.tagName === 'VIDEO' && el.poster) urls.add(el.poster);
    } catch (e) {}
    if (n < 4000) { for (const ps of ['::before', '::after']) { try { addCss(getComputedStyle(el, ps).backgroundImage); } catch (e) {} } }
  }
  for (const e of performance.getEntriesByType('resource')) {
    if (['img', 'image', 'css', 'other', 'link', 'fetch', 'xmlhttprequest'].includes(e.initiatorType) &&
        /\.(jpe?g|png|gif|webp|avif|svg|bmp|ico)(\?|#|$)/i.test(e.name) || e.initiatorType === 'img' || e.initiatorType === 'image') urls.add(e.name);
  }
  return JSON.stringify({html: document.documentElement.outerHTML, urls: Array.from(urls), url: location.href,
                         title: document.title, base: document.baseURI});
})()
"""

JS_SCROLL = r"""
(async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let last = -1, same = 0, y = 0, steps = 0;
  const t0 = Date.now();
  while (Date.now() - t0 < %d && steps < 120) {
    const h = Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement.scrollHeight);
    y = Math.min(y + Math.round(window.innerHeight * 0.8), h);
    window.scrollTo(0, y); steps++;
    await sleep(260);
    for (const el of document.querySelectorAll('[loading=lazy]')) { try { el.loading = 'eager'; } catch (e) {} }
    if (y >= h - 2) {
      if (h === last) { same++; } else { same = 0; }
      last = h;
      if (same >= 2) break;
      await sleep(350);
    }
  }
  window.scrollTo(0, 0); await sleep(500);
  return steps;
})()
"""


@dataclass
class Rendered:
    html: str
    urls: list[str]
    final_url: str
    title: str
    cookies: list[dict] = field(default_factory=list)


class _CDP:
    def __init__(self, ws_url: str):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0
        self.events: list[dict] = []

    def send(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        self.ws.settimeout(1.0)
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except Exception as e:
                if e.__class__.__name__ == "WebSocketTimeoutException":
                    continue
                raise
            if msg.get("id") == mid:
                if "error" in msg:
                    raise FetchError(f"Browser error: {msg['error'].get('message')}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise FetchError("The browser did not answer in time.")

    def wait_event(self, name: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if any(e.get("method") == name for e in self.events):
                return True
            self.ws.settimeout(0.5)
            try:
                msg = json.loads(self.ws.recv())
                if "method" in msg:
                    self.events.append(msg)
            except Exception as e:
                if e.__class__.__name__ == "WebSocketTimeoutException":
                    continue
                raise
        return False

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def render_page(url: str, scroll_ms: int = 20000, cancel: threading.Event | None = None, log=None) -> Rendered:
    exe = find_browser()
    if not exe:
        raise FetchError("Render mode needs Microsoft Edge (or Chrome), which was not found on this PC. Use the plain mode instead.")
    try:
        import websocket  # noqa: F401
    except ImportError:
        raise FetchError("Render mode is not available in this build (websocket-client missing).")
    prof = Path(tempfile.mkdtemp(prefix="wig-browser-"))
    args = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--mute-audio",
            f"--user-data-dir={prof}", "--remote-debugging-port=0", "--remote-allow-origins=*", "--hide-scrollbars",
            "--window-size=1366,900", "--disable-extensions", "--disable-background-networking",
            "--disable-features=Translate,MediaRouter", "--blink-settings=imagesEnabled=true", "about:blank"]
    if os.name != "nt" and (os.geteuid() == 0 or os.environ.get("WIG_NO_SANDBOX")):
        args.insert(1, "--no-sandbox")
    flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            creationflags=flags)
    cdp = None
    try:
        port_file = prof / "DevToolsActivePort"
        end = time.time() + 25
        while time.time() < end and not port_file.exists():
            if proc.poll() is not None:
                raise FetchError("The browser closed immediately; render mode could not start.")
            if cancel is not None and cancel.is_set():
                raise FetchError("Cancelled.")
            time.sleep(0.15)
        if not port_file.exists():
            raise FetchError("The browser did not start in time.")
        time.sleep(0.2)
        port = int(port_file.read_text().splitlines()[0])
        import requests
        ws_url = None
        for _ in range(40):
            try:
                tg = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=3).json()
                pages = [t for t in tg if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.15)
        if not ws_url:
            raise FetchError("Could not attach to the browser page.")
        cdp = _CDP(ws_url)
        cdp.send("Page.enable")
        cdp.send("Network.enable")
        cdp.send("Network.setUserAgentOverride", {"userAgent": UA, "acceptLanguage": "en-US,en"})
        cdp.send("Emulation.setDeviceMetricsOverride", {"width": 1366, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        cdp.send("Page.navigate", {"url": url})
        if not cdp.wait_event("Page.loadEventFired", 35):
            if log:
                log("page load event timed out; continuing with what loaded")
        time.sleep(0.6)
        if cancel is not None and cancel.is_set():
            raise FetchError("Cancelled.")
        try:
            cdp.send("Runtime.evaluate", {"expression": JS_SCROLL % scroll_ms, "awaitPromise": True,
                                          "returnByValue": True}, timeout=scroll_ms / 1000 + 20)
        except FetchError as e:
            if log:
                log(f"scroll step: {e}")
        time.sleep(0.5)
        res = cdp.send("Runtime.evaluate", {"expression": JS_COLLECT, "returnByValue": True}, timeout=60)
        val = res.get("result", {}).get("value")
        if not val:
            raise FetchError("The browser returned no page content.")
        data = json.loads(val)
        cookies = []
        try:
            cookies = cdp.send("Network.getAllCookies").get("cookies", [])
        except Exception:
            pass
        if data.get("url", "").startswith("chrome-error:"):
            raise FetchError("The browser could not load that page (network error or blocked).")
        return Rendered(data["html"], data["urls"], data["url"], data.get("title", ""), cookies)
    finally:
        if cdp:
            cdp.close()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(prof, ignore_errors=True)
