"""Scan (page -> candidates -> downloaded originals + thumbnails) and Save (selected -> folder).  No widgets here."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import requests

from . import extract, imgutil, net
from .net import FetchError

MAX_CANDIDATES = 2000


@dataclass
class ImageItem:
    order: int
    url: str
    source: str
    path: Path
    kind: str                      # jpg/png/gif/webp/svg/...
    size: int                      # bytes of the original
    w: int = 0
    h: int = 0
    thumb: bytes | None = None
    name: str = "image"
    animated: bool = False
    sha1: str = ""
    selected: bool = False
    final_url: str = ""

    @property
    def tiny(self) -> bool:
        return self.w > 0 and self.h > 0 and self.w < 16 and self.h < 16

    @property
    def dims_text(self) -> str:
        return f"{self.w}\u00d7{self.h}" if self.w and self.h else "size n/a"

    @property
    def type_text(self) -> str:
        t = {"jpg": "JPG", "tiff": "TIFF"}.get(self.kind, self.kind.upper())
        return t + (" (anim)" if self.animated else "")


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


@dataclass
class ScanOptions:
    render: bool = False
    prefer_original: bool = True       # Wikimedia thumbnails -> originals
    include_links: bool = True         # <a href="photo.jpg">
    workers: int = 8


@dataclass
class ScanSummary:
    page_url: str = ""
    title: str = ""
    candidates: int = 0
    loaded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    duplicates: int = 0
    skipped: int = 0
    truncated: bool = False
    cancelled: bool = False
    notes: list[str] = field(default_factory=list)


class Scanner:
    """Runs in a worker thread.  Callbacks (status/progress/item) are invoked from that thread."""

    def __init__(self, tmpdir: Path, on_status: Callable[[str], None], on_progress: Callable[[int, int], None],
                 on_item: Callable[[ImageItem], None]):
        self.tmpdir = tmpdir
        tmpdir.mkdir(parents=True, exist_ok=True)
        self.on_status, self.on_progress, self.on_item = on_status, on_progress, on_item
        self.cancel = threading.Event()
        self.session = net.make_session()
        self.summary = ScanSummary()
        self.referer = ""

    # ---------------------------------------------------------- stage 1: candidates
    def candidates_from_page(self, raw_url: str, opts: ScanOptions) -> list[extract.Candidate]:
        url = net.normalize_url(raw_url)
        S = self.summary
        S.page_url = url
        self.on_status("Loading the page\u2026" if not opts.render else "Opening the page in a hidden browser and scrolling\u2026")
        extra: list[str] = []
        if opts.render:
            from . import render
            rd = render.render_page(url, cancel=self.cancel, log=lambda m: S.notes.append(m))
            for c in rd.cookies:
                try:
                    self.session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
                except Exception:
                    pass
            html, final, extra = rd.html, rd.final_url, rd.urls
            S.title = rd.title
        else:
            pf = net.fetch_page(self.session, url, self.cancel)
            final = pf.final_url
            if pf.is_image:
                S.title = ""
                S.page_url = final
                self.referer = final
                return [extract.Candidate(final, "link")]
            html = pf.html
        if self.cancel.is_set():
            raise FetchError("Cancelled.")
        self.referer = final
        S.page_url = final
        self.on_status("Finding images\u2026")
        res, _, _ = extract.parse_html(html, final, include_links=opts.include_links)
        S.title = S.title or res.title
        cands = list(res.candidates)
        for u in extra:
            c = extract._clean(u, res.base)
            if c:
                cands.append(extract.Candidate(c, "render"))
        # linked stylesheets (+ @import, 2 levels)
        css_queue = list(dict.fromkeys(res.stylesheets))[:30]
        seen_css = set(css_queue)
        depth = 0
        while css_queue and depth < 3 and not self.cancel.is_set():
            self.on_status(f"Reading {len(css_queue)} stylesheet(s)\u2026")
            nxt: list[str] = []
            with ThreadPoolExecutor(8) as ex:
                futs = {ex.submit(net.fetch_text, self.session, u, final): u for u in css_queue}
                for f in as_completed(futs):
                    txt = f.result()
                    if txt:
                        sub = extract.add_css(res, txt, futs[f])
                        for s in sub:
                            if s not in seen_css and len(seen_css) < 60:
                                seen_css.add(s)
                                nxt.append(s)
            cands = list(res.candidates) + [c for c in cands if c.source == "render"]
            css_queue = nxt
            depth += 1
        if depth == 0:
            cands = list(res.candidates) + [c for c in cands if c.source == "render"]
        return cands

    # ---------------------------------------------------------- stage 2: download + thumbnails
    def run(self, raw_urls: list[str] | None, page_url: str | None, opts: ScanOptions,
            html_text: str | None = None, html_base: str | None = None) -> ScanSummary:
        S = self.summary
        try:
            if raw_urls:
                cands = extract.dedupe([extract.Candidate(u, "list") for u in raw_urls])
                S.page_url = ""
                self.referer = ""
            elif html_text is not None:
                S.page_url = html_base or ""
                self.referer = html_base or ""
                res, _, _ = extract.parse_html(html_text, html_base or "file:///", include_links=opts.include_links)
                S.title = res.title
                for u in res.stylesheets[:30]:
                    t = net.fetch_text(self.session, u, self.referer)
                    if t:
                        extract.add_css(res, t, u)
                cands = res.candidates
            else:
                cands = self.candidates_from_page(page_url or "", opts)
            if opts.prefer_original:
                for c in cands:
                    o = extract.wikimedia_original(c.url)
                    if o and o != c.url:
                        c.alt = c.url      # type: ignore[attr-defined]
                        c.url = o
            cands = extract.dedupe(cands)
            S.candidates = len(cands)
            if len(cands) > MAX_CANDIDATES:
                cands = cands[:MAX_CANDIDATES]
                S.truncated = True
            if not cands:
                raise FetchError("No images found on this page. If the site builds its pictures with JavaScript, "
                                 "turn on 'Render page (JavaScript)' and fetch again.")
            self._download_all(cands, opts)
        except FetchError as e:
            if str(e) == "Cancelled.":
                S.cancelled = True
            else:
                raise
        return S

    def _download_all(self, cands: list[extract.Candidate], opts: ScanOptions):
        S = self.summary
        total = len(cands)
        done = 0
        lock = threading.Lock()
        seen_hash: dict[str, int] = {}
        self.on_progress(0, total)
        self.on_status(f"Found {total} image link(s). Loading thumbnails\u2026")

        host_sems: dict[str, threading.Semaphore] = {}
        sem_lock = threading.Lock()

        def host_sem(u: str) -> threading.Semaphore:
            h = urlsplit(u).netloc.lower()
            with sem_lock:
                return host_sems.setdefault(h, threading.Semaphore(5))

        def work(c: extract.Candidate):
            if self.cancel.is_set():
                return None
            tried = [c.url] + ([getattr(c, "alt")] if getattr(c, "alt", None) else [])
            err = None
            for ti, u in enumerate(tried):
                try:
                    with host_sem(u):          # be polite: at most 5 parallel requests per host
                        d = net.download_image(self.session, u, self.referer, self.tmpdir, c.order, self.cancel,
                                               max_bytes=(60 * 1024 * 1024 if len(tried) > 1 and ti == 0 else net.MAX_IMAGE_BYTES))
                    break
                except FetchError as e:
                    err = e
                    d = None
                    if str(e) == "cancelled":
                        return None
                except Exception as e:      # noqa - never let one bad image stop the scan
                    err = FetchError(f"error: {e.__class__.__name__}")
                    d = None
            if d is None:
                return (c, None, str(err))
            try:
                w, h, thumb = imgutil.make_thumb(d.path)
            except Exception:
                w, h, thumb = 0, 0, None
            sha = hashlib.sha1()
            with open(d.path, "rb") as f:
                for ch in iter(lambda: f.read(1 << 20), b""):
                    sha.update(ch)
            it = ImageItem(order=c.order, url=d.url, source=c.source, path=d.path, kind=d.kind, size=d.size, w=w, h=h,
                           thumb=thumb, name=net.finalize_filename(d.suggested_name, d.kind), animated=net.is_animated(d.path, d.kind),
                           sha1=sha.hexdigest(), final_url=d.final_url)
            return (c, it, None)

        with ThreadPoolExecutor(max(1, opts.workers)) as ex:
            futs = [ex.submit(work, c) for c in cands]
            for f in as_completed(futs):
                r = f.result()
                with lock:
                    done += 1
                    if r is not None:
                        c, it, err = r
                        if it is None:
                            if (err or "").startswith("not an image"):
                                S.skipped += 1          # e.g. a link to a web page, not a picture
                            else:
                                S.failed.append((c.url, err or "failed"))
                        else:
                            key = it.sha1
                            if key in seen_hash:
                                S.duplicates += 1
                                it.path.unlink(missing_ok=True)
                            else:
                                seen_hash[key] = it.order
                                S.loaded += 1
                                self.on_item(it)
                    self.on_progress(done, total)
                if self.cancel.is_set():
                    for g in futs:
                        g.cancel()
                    S.cancelled = True
                    break


# ------------------------------------------------------------------ saving

_RESERVED = net._RESERVED


def clean_filename(name: str, maxlen: int = 140) -> str:
    name = net._safe_stem(name) or "image"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem[:maxlen].rstrip(" .") or "image"
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    return stem + (("." + ext) if ext else "")


def unique_path(folder: Path, name: str, taken: set[str]) -> Path:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    cand = name
    n = 0
    while True:
        p = folder / cand
        if cand.lower() not in taken and not p.exists():
            taken.add(cand.lower())
            return p
        n += 1
        cand = f"{stem}-{n}" + (("." + ext) if ext else "")


def page_folder_name(title: str, page_url: str) -> str:
    base = title.strip() or urlsplit(page_url).netloc or "images"
    base = re.sub(r"\s+", " ", net._safe_stem(base))[:70].strip(" .")
    return base or "images"


@dataclass
class SaveResult:
    saved: list[Path] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    folder: Path | None = None
    cancelled: bool = False


def save_items(items: list[ImageItem], folder: Path, convert_webp: str = "keep", cancel: threading.Event | None = None,
               progress: Callable[[int, int, str], None] | None = None,
               session: requests.Session | None = None, referer: str = "") -> SaveResult:
    """Write the ORIGINAL downloaded bytes (full resolution) of every item to folder, never overwriting."""
    res = SaveResult(folder=folder)
    folder.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    for i, it in enumerate(items):
        if cancel is not None and cancel.is_set():
            res.cancelled = True
            break
        if progress:
            progress(i, len(items), it.name)
        try:
            name = clean_filename(net.finalize_filename(it.name, it.kind))
            conv = convert_webp if (it.kind == "webp" and convert_webp in ("jpg", "png") and not it.animated) else None
            if conv:
                stem = name.rpartition(".")[0] or name
                name = f"{stem}.{conv}"
            dest = unique_path(folder, name, taken)
            src = it.path
            if not src.exists():
                if not session:
                    raise FileNotFoundError("original no longer cached")
                import tempfile
                tmp = Path(tempfile.mkdtemp(prefix="wig-resave-"))
                d = net.download_image(session, it.url, referer, tmp, it.order, cancel)
                src = d.path
            if conv:
                imgutil.convert_to(src, dest, conv)
            else:
                shutil.copyfile(src, dest)
            res.saved.append(dest)
        except Exception as e:
            res.failed.append((it.url, str(e)))
    if progress and not res.cancelled:
        progress(len(items), len(items), "")
    return res
