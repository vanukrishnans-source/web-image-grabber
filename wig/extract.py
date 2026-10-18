"""Find every image URL in an HTML page (and its stylesheets / inline JSON).  Pure Python, no network here."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin, urldefrag, unquote, urlsplit

IMG_EXTS = ("jpg", "jpeg", "jpe", "jfif", "png", "gif", "webp", "avif", "svg", "bmp", "ico", "tif", "tiff", "heic", "apng")
_EXT_RE = r"(?:%s)" % "|".join(IMG_EXTS)
# something that looks like an image URL / path (extension before ? # or end)
IMG_LIKE = re.compile(r"\.%s(?:$|[?#&;])" % _EXT_RE, re.I)
NON_IMAGE_EXTS = {"woff", "woff2", "ttf", "otf", "eot", "css", "js", "mjs", "json", "html", "htm", "php", "map", "xml",
                  "mp4", "webm", "mp3", "ogg", "pdf", "zip", "cur", "txt"}

SRC_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-lazy", "data-original-src", "data-hi-res-src",
             "data-full", "data-full-src", "data-large", "data-large-src", "data-zoom-image", "data-image",
             "data-img", "data-img-src", "data-echo", "data-lazyload", "data-delayed-url", "data-bg", "data-background",
             "data-background-image", "data-poster", "data-thumb", "data-thumbnail", "data-url", "data-mfp-src",
             "data-flickity-lazyload", "data-lazy-original", "data-orig-src", "data-actualsrc", "data-fullsrc")
META_KEYS = {"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src", "msapplication-tileimage",
             "thumbnail", "image", "og:logo", "twitter:image0", "twitter:image1", "twitter:image2", "twitter:image3"}
LINK_ICON_RELS = ("icon", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon", "image_src", "fluid-icon", "shortcut")
JSON_IMG_KEYS = {"image", "images", "logo", "thumbnailurl", "contenturl", "thumbnail", "photo", "primaryimageofpage",
                 "imageurl", "image_url", "img", "src", "poster", "cover", "avatar", "banner", "picture"}

CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s'"][^)]*?))\s*\)""", re.I)
CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?(?:"([^"]+)"|'([^']+)'|([^)\s;'"]+))""", re.I)
# quoted strings in scripts that look like image URLs
SCRIPT_URL = re.compile(r"""(["'])((?:https?:)?(?:\\?/\\?/)[^"'\s<>]{3,}?|(?:\\?/)[^"'\s<>]{2,}?\.%s(?:[?#][^"'\s<>]*)?|[A-Za-z0-9_./~%%-]+\.%s(?:\?[^"'\s<>]*)?)\1""" % (_EXT_RE, _EXT_RE), re.I)
DATA_URI_PREFIX = "data:image/"


@dataclass
class Candidate:
    url: str            # absolute URL, or data: URI
    source: str         # short label: img, srcset, css, meta, icon, poster, json, link, data ...
    order: int = 0
    alt: str = ""


def _unescape_js(s: str) -> str:
    s = s.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/").replace("\\u0026", "&").replace("&amp;", "&")
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s


def _urlish(v: str) -> bool:
    return bool(re.match(r"^(?:https?:)?//|^/|^\.{0,2}/|^data:image/|^[\w%~.-]+/[\w%~./-]+|^[\w%~-]+\.\w{2,5}(?:[?#]|$)", v))


def looks_like_image_url(u: str) -> bool:
    if not u:
        return False
    if u.lower().startswith(DATA_URI_PREFIX):
        return True
    p = urlsplit(u)
    if IMG_LIKE.search(p.path + ("?" + p.query if p.query else "")):
        # extension in the path, or in a query value (e.g. ?url=a.jpg)
        return True
    return False


def parse_srcset(value: str) -> list[tuple[str, float]]:
    """Spec-style srcset parser (URLs may contain commas).  Returns [(url, weight)] where weight is
    the pixel width ('w') or density ('x' * 1000) so candidates are comparable inside one srcset."""
    out: list[tuple[str, float]] = []
    s = value.strip()
    i, n = 0, len(s)
    while i < n:
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not s[j].isspace():
            j += 1
        url = s[i:j]
        i = j
        desc = ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            # descriptor until next comma (not inside parentheses)
            j = i
            depth = 0
            while j < n:
                c = s[j]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth = max(0, depth - 1)
                elif c == "," and depth == 0:
                    break
                j += 1
            desc = s[i:j].strip()
            i = j + 1
        if not url:
            continue
        w = 0.0
        m = re.match(r"^([\d.]+)\s*([wx])$", desc, re.I)
        if m:
            try:
                v = float(m.group(1))
                w = v if m.group(2).lower() == "w" else v * 1000.0
            except ValueError:
                w = 0.0
        out.append((url, w))
    return out


def largest_of_srcset(value: str) -> tuple[str, float] | None:
    cands = parse_srcset(value)
    if not cands:
        return None
    best = max(enumerate(cands), key=lambda t: (t[1][1], t[0]))
    return best[1]


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raw: list[tuple[str, str]] = []        # (url, source)
        self.base_href: str | None = None
        self.title = ""
        self._in_title = False
        self.styles: list[str] = []
        self.stylesheets: list[str] = []
        self.scripts: list[tuple[str, str]] = []    # (type, text)
        self._cur_script: list[str] | None = None
        self._cur_script_type = ""
        self._cur_style: list[str] | None = None
        self._picture: list[tuple[str, float, str]] | None = None   # (url, weight, kind)
        self._picture_fallback: list[str] = []
        self.include_links = True

    # ---- helpers
    def add(self, url, source):
        if url:
            url = url.strip()
            if url:
                self.raw.append((url, source))

    def _attr_candidates(self, tag: str, a: dict[str, str]):
        """Collect candidate URLs from one element.  Returns ([(url, source)], best_srcset (url, weight) | None)."""
        found: list[tuple[str, str]] = []
        best_set = None
        for k, v in a.items():
            if not v:
                continue
            kl = k.lower()
            if "srcset" in kl:
                r = largest_of_srcset(v)
                if r:
                    if best_set is None or kl in ("data-srcset", "data-lazy-srcset") or r[1] > best_set[1]:
                        best_set = r
                continue
            vs = v.strip()
            if kl == "src":
                if tag not in ("img", "source", "input", "image", "video", "embed", "amp-img"):
                    continue
                if tag in ("source", "video", "embed") and not looks_like_image_url(vs):
                    continue
                found.append((vs, "img"))
            elif kl.startswith("data-") or kl in SRC_ATTRS:
                if vs.lower().startswith("url("):
                    m = CSS_URL.search(vs)
                    if m:
                        vs = next(g for g in m.groups() if g is not None)
                if not vs or " " in vs or "\n" in vs or len(vs) > 4000:
                    continue
                if kl in SRC_ATTRS and kl != "data-url":
                    if _urlish(vs):
                        found.append((vs, "lazy"))
                elif looks_like_image_url(vs):
                    found.append((vs, "lazy"))
        return found, best_set

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        t = tag.lower()
        if t == "base" and a.get("href") and self.base_href is None:
            self.base_href = a["href"]
        elif t == "title":
            self._in_title = True
        elif t == "style":
            self._cur_style = []
        elif t == "script":
            self._cur_script = []
            self._cur_script_type = a.get("type", "").lower()
            if a.get("src") and looks_like_image_url(a["src"]):
                self.add(a["src"], "img")
        elif t == "picture":
            self._picture = []
            self._picture_fallback = []
        elif t == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            c = a.get("content", "")
            if key in META_KEYS and c:
                self.add(c, "meta")
        elif t == "link":
            rel = a.get("rel", "").lower().split()
            href = a.get("href", "")
            if href:
                if any(r in LINK_ICON_RELS for r in rel) or "icon" in " ".join(rel):
                    self.add(href, "icon")
                elif "stylesheet" in rel or (a.get("as") == "style"):
                    self.stylesheets.append(href)
                elif "preload" in rel and a.get("as") == "image":
                    if a.get("imagesrcset"):
                        r = largest_of_srcset(a["imagesrcset"])
                        self.add(r[0] if r else href, "img")
                    else:
                        self.add(href, "img")
        elif t == "a":
            href = a.get("href", "")
            if self.include_links and href and looks_like_image_url(href) and not href.lower().startswith("javascript:"):
                self.add(href, "link")
            for k, v in a.items():
                if k.startswith("data-") and v and looks_like_image_url(v) and " " not in v.strip():
                    self.add(v, "lazy")
        else:
            self._element(t, a)

        st = a.get("style", "")
        if st and "url(" in st.lower():
            for m in CSS_URL.finditer(st):
                self.add(next(g for g in m.groups() if g is not None), "css")

    def _element(self, t, a):
        if t == "video" and a.get("poster"):
            self.add(a["poster"], "poster")
        if t == "object" and a.get("data") and looks_like_image_url(a["data"]):
            self.add(a["data"], "img")
        if t in ("image", "use"):
            for k in ("href", "xlink:href"):
                if a.get(k) and (looks_like_image_url(a[k]) or a[k].lower().startswith(DATA_URI_PREFIX)):
                    self.add(a[k], "img")
        if t == "input" and a.get("type", "").lower() != "image":
            return
        if t == "embed":
            if not looks_like_image_url(a.get("src", "")):
                return
        found, best_set = self._attr_candidates(t, a)
        if self._picture is not None and t in ("source", "img"):
            if best_set:
                self._picture.append((best_set[0], best_set[1], "avif" if ("avif" in a.get("type", "").lower() or best_set[0].lower().split("?")[0].endswith(".avif")) else "set"))
            for u, src in found:
                if t == "img":
                    self._picture_fallback.append(u)
            return
        if best_set:
            # prefer the largest srcset candidate (the plain src is the low-res fallback of the same image)
            self.add(best_set[0], "srcset")
            for u, src in found:
                if src == "lazy" and u != best_set[0] and not u.lower().startswith("data:"):
                    self.add(u, src)
        else:
            for u, src in found:
                self.add(u, src)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title":
            self._in_title = False
        elif t == "style" and self._cur_style is not None:
            self.styles.append("".join(self._cur_style))
            self._cur_style = None
        elif t == "script" and self._cur_script is not None:
            self.scripts.append((self._cur_script_type, "".join(self._cur_script)))
            self._cur_script = None
        elif t == "picture" and self._picture is not None:
            # one image per <picture>: the largest srcset candidate, else the <img> fallback
            if self._picture:
                best = max(enumerate(self._picture), key=lambda x: (x[1][1], x[1][2] != "avif", -x[0]))
                self.add(best[1][0], "srcset")
            for u in self._picture_fallback[:1]:
                if not self._picture:
                    self.add(u, "img")
            self._picture = None

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data
        if self._cur_script is not None:
            self._cur_script.append(data)
        if self._cur_style is not None:
            self._cur_style.append(data)


def css_urls(css: str) -> tuple[list[str], list[str]]:
    """(image-ish urls, @import urls) found in a stylesheet text."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    imgs = []
    for m in CSS_URL.finditer(css):
        u = next((g for g in m.groups() if g is not None), "").strip()
        if not u or u.startswith("#"):
            continue
        if u.lower().startswith("data:"):
            if u.lower().startswith(DATA_URI_PREFIX):
                imgs.append(u)
            continue
        p = urlsplit(u)
        ext = p.path.rsplit(".", 1)[-1].lower() if "." in p.path.rsplit("/", 1)[-1] else ""
        if ext in NON_IMAGE_EXTS:
            continue
        imgs.append(u)
    imps = []
    for m in CSS_IMPORT.finditer(css):
        u = next((g for g in m.groups() if g), "")
        if u:
            imps.append(u)
    return imgs, imps


def _walk_json(o, out: list[str], key: str = ""):
    if isinstance(o, dict):
        for k, v in o.items():
            _walk_json(v, out, k.lower())
    elif isinstance(o, list):
        for v in o:
            _walk_json(v, out, key)
    elif isinstance(o, str):
        s = o.strip()
        if not s or len(s) > 4000 or "\n" in s:
            return
        if looks_like_image_url(s):
            out.append(s)
        elif key in JSON_IMG_KEYS and re.match(r"^(https?:)?//", s) and " " not in s:
            out.append(s)


def script_urls(text: str, ctype: str) -> list[str]:
    out: list[str] = []
    t = text.strip()
    if "json" in ctype or (t.startswith("{") or t.startswith("[")):
        try:
            _walk_json(json.loads(t), out)
            if out or "ld+json" in ctype:
                return out
        except Exception:
            pass
    if len(text) > 3_000_000:
        text = text[:3_000_000]
    for m in SCRIPT_URL.finditer(text):
        raw = _unescape_js(m.group(2))
        if looks_like_image_url(raw):
            out.append(raw)
    return out


def _clean(u: str, base: str) -> str | None:
    u = u.strip().strip("\"'")
    if not u:
        return None
    low = u.lower()
    if low.startswith("data:"):
        return u if low.startswith(DATA_URI_PREFIX) else None
    if low.startswith(("javascript:", "about:", "blob:", "mailto:", "tel:", "#", "chrome:", "file:")) and not (
            low.startswith("file:") and base.lower().startswith("file:")):
        return None
    try:
        absu = urljoin(base, u)
    except ValueError:
        return None
    absu, _ = urldefrag(absu)
    sp = urlsplit(absu)
    if sp.scheme not in ("http", "https", "file"):
        return None
    if sp.scheme in ("http", "https") and not sp.netloc:
        return None
    return absu.replace(" ", "%20")


@dataclass
class PageResult:
    candidates: list[Candidate] = field(default_factory=list)
    title: str = ""
    base: str = ""
    stylesheets: list[str] = field(default_factory=list)   # absolute urls of linked css (to be fetched by caller)


def parse_html(html: str, page_url: str, include_links: bool = True) -> tuple[PageResult, list[tuple[str, str]], list[str]]:
    """Parse HTML. Returns (result with candidates from HTML+inline CSS+scripts, css_pending, []).
    css_pending is (stylesheet_url, '') pairs to fetch later with `add_css`."""
    p = _Page()
    p.include_links = include_links
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    base = urljoin(page_url, p.base_href) if p.base_href else page_url
    res = PageResult(title=re.sub(r"\s+", " ", p.title).strip(), base=base)
    for u, src in p.raw:
        c = _clean(u, base)
        if c:
            res.candidates.append(Candidate(c, src))
    for css in p.styles:
        imgs, _ = css_urls(css)
        for u in imgs:
            c = _clean(u, base)
            if c:
                res.candidates.append(Candidate(c, "css"))
    for ctype, text in p.scripts:
        if not text.strip():
            continue
        for u in script_urls(text, ctype):
            c = _clean(u, base)
            if c:
                res.candidates.append(Candidate(c, "json"))
    for href in p.stylesheets:
        c = _clean(href, base)
        if c and not c.startswith("data:"):
            res.stylesheets.append(c)
    return res, [], []


def add_css(res: PageResult, css_text: str, css_url: str) -> list[str]:
    """Add the images of one fetched stylesheet; returns its @import urls (absolute)."""
    imgs, imps = css_urls(css_text)
    for u in imgs:
        c = _clean(u, css_url)
        if c:
            res.candidates.append(Candidate(c, "css"))
    return [x for x in (_clean(i, css_url) for i in imps) if x]


def parse_url_list(text: str) -> list[str]:
    """Pasted list of image URLs (one per line / whitespace / commas)."""
    out = []
    for tok in re.split(r"[\s,]+", text.strip()):
        tok = tok.strip().strip("<>\"'")
        if tok.lower().startswith(("http://", "https://", "data:image/", "file://")):
            out.append(tok)
    return out


def wikimedia_original(url: str) -> str | None:
    """upload.wikimedia.org/.../thumb/a/ab/Name.jpg/330px-Name.jpg -> .../a/ab/Name.jpg (full resolution)."""
    sp = urlsplit(url)
    if not sp.netloc.endswith("wikimedia.org") and not sp.netloc.endswith("wikipedia.org"):
        return None
    m = re.match(r"^(.*?)/thumb/([0-9a-f]/[0-9a-f]{2}/([^/]+))/[^/]+$", sp.path)
    if not m:
        return None
    host = "upload.wikimedia.org" if sp.netloc.startswith("thumb.") else sp.netloc
    return f"{sp.scheme}://{host}{m.group(1)}/{m.group(2)}"


def dedupe(cands: Iterable[Candidate]) -> list[Candidate]:
    seen = set()
    out = []
    for c in cands:
        key = c.url if not c.url.startswith("data:") else c.url[:60] + str(len(c.url)) + c.url[-40:]
        if key in seen:
            continue
        seen.add(key)
        c.order = len(out)
        out.append(c)
    return out
