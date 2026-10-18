"""Networking: page fetch, stylesheet fetch, image download-to-disk, with clear error messages."""
from __future__ import annotations

import base64
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter

from . import extract

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
PAGE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
IMG_HEADERS = {
    "User-Agent": UA,
    "Accept": "image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_IMAGE_BYTES = 200 * 1024 * 1024
MAX_PAGE_BYTES = 30 * 1024 * 1024


class FetchError(Exception):
    """Message is safe to show to the user as is."""


def normalize_url(text: str) -> str:
    t = text.strip().strip("<>\"'")
    if not t:
        raise FetchError("Type or paste a web page address first.")
    if re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith("\\\\"):
        return Path(t).as_uri()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", t):
        if t.lower().startswith("file:"):
            return t
        t = "https://" + t.lstrip("/")
    return t


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(PAGE_HEADERS)
    ad = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=1)
    s.mount("http://", ad)
    s.mount("https://", ad)
    return s


def explain_exception(e: Exception, url: str) -> str:
    host = urlsplit(url).netloc or url
    if isinstance(e, requests.exceptions.SSLError):
        return f"Secure connection to {host} failed (certificate problem). Check the address or your date/time, or try http://."
    if isinstance(e, requests.exceptions.ConnectTimeout) or isinstance(e, requests.exceptions.ReadTimeout):
        return f"{host} took too long to answer (timeout). Check your connection and try again."
    if isinstance(e, requests.exceptions.TooManyRedirects):
        return f"{host} keeps redirecting (too many redirects). It may need a login or cookies."
    if isinstance(e, requests.exceptions.ConnectionError):
        m = str(e).lower()
        if "name or service not known" in m or "getaddrinfo" in m or "nodename nor servname" in m or "11001" in m or "temporary failure in name" in m:
            return f"Could not find '{host}'. Check the spelling of the address and that you are online."
        if "refused" in m:
            return f"{host} refused the connection."
        return f"Could not connect to {host}. Check your internet connection."
    if isinstance(e, requests.exceptions.InvalidURL) or isinstance(e, requests.exceptions.MissingSchema):
        return f"'{url}' is not a valid web address."
    return f"Could not load the page: {e}"


def explain_status(code: int, url: str, body: str = "") -> str:
    host = urlsplit(url).netloc
    if code in (401,):
        return f"{host} needs a login (HTTP 401). Login-only pages can't be fetched."
    if code == 403:
        low = body.lower()
        if "cloudflare" in low or "just a moment" in low or "captcha" in low or "challenge" in low:
            return f"{host} blocked the request with a bot check (HTTP 403). Try 'Render page (JavaScript)', or open the page in your browser and save from there."
        return f"{host} refused access (HTTP 403) - it blocks automated downloads or needs a login. Try 'Render page (JavaScript)'."
    if code == 404:
        return "Page not found (HTTP 404). Check the address."
    if code == 410:
        return "That page is gone (HTTP 410)."
    if code == 429:
        return f"{host} says too many requests (HTTP 429). Wait a minute and try again."
    if code >= 500:
        return f"{host} has a server problem (HTTP {code}). Try again later."
    return f"{host} answered HTTP {code}."


def decode_body(r: requests.Response, content: bytes) -> str:
    enc = None
    ct = r.headers.get("content-type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        enc = m.group(1)
    if not enc:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", content[:4096], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    for e in (enc, "utf-8"):
        if not e:
            continue
        try:
            return content.decode(e, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode(enc or "utf-8", errors="replace") if enc else content.decode("utf-8", errors="replace")


@dataclass
class PageFetch:
    final_url: str
    html: str
    is_image: bool = False          # the URL itself is an image
    status: int = 200


def fetch_page(session: requests.Session, url: str, cancel: threading.Event | None = None) -> PageFetch:
    if url.lower().startswith("file:"):
        path = file_url_to_path(url)
        if not path.exists():
            raise FetchError(f"File not found: {path}")
        data = path.read_bytes()
        if sniff_type(data[:64]) != "bin":
            return PageFetch(url, "", is_image=True)
        return PageFetch(url, data.decode("utf-8", errors="replace"))
    try:
        r = session.get(url, timeout=(10, 30), allow_redirects=True, stream=True)
    except Exception as e:
        raise FetchError(explain_exception(e, url))
    try:
        ct = r.headers.get("content-type", "").lower()
        if r.status_code >= 400:
            body = ""
            try:
                body = r.raw.read(20000, decode_content=True).decode("utf-8", "ignore")
            except Exception:
                pass
            raise FetchError(explain_status(r.status_code, r.url, body))
        if ct.startswith("image/"):
            return PageFetch(r.url, "", is_image=True)
        chunks, total = [], 0
        try:
            for ch in r.iter_content(65536):
                if cancel is not None and cancel.is_set():
                    raise FetchError("Cancelled.")
                chunks.append(ch)
                total += len(ch)
                if total > MAX_PAGE_BYTES:
                    break
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(explain_exception(e, url))
        content = b"".join(chunks)
        if ct and not any(k in ct for k in ("html", "xml", "text", "json")):
            raise FetchError(f"That address is not a web page (content type '{ct.split(';')[0]}').")
        return PageFetch(r.url, decode_body(r, content), status=r.status_code)
    finally:
        r.close()


def fetch_text(session: requests.Session, url: str, referer: str, limit: int = 2_000_000) -> str | None:
    try:
        if url.lower().startswith("file:"):
            p = file_url_to_path(url)
            return p.read_text("utf-8", errors="replace") if p.exists() else None
        r = session.get(url, timeout=(8, 15), headers={"Referer": referer, "Accept": "text/css,*/*;q=0.1"}, stream=True)
        try:
            if r.status_code >= 400:
                return None
            data = b""
            for ch in r.iter_content(65536):
                data += ch
                if len(data) > limit:
                    break
            return decode_body(r, data)
        finally:
            r.close()
    except Exception:
        return None


def file_url_to_path(url: str) -> Path:
    sp = urlsplit(url)
    p = unquote(sp.path)
    if os.name == "nt":
        if sp.netloc and sp.netloc != "localhost":
            return Path("//" + sp.netloc + p)
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
    return Path(p)


# ---------------------------------------------------------------- image type sniffing

def sniff_type(b: bytes) -> str:
    """Returns jpg/png/gif/webp/svg/bmp/ico/avif/tiff/heic or 'bin'."""
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:2] == b"BM" and len(b) > 14:
        return "bmp"
    if b[:4] == b"\x00\x00\x01\x00" or b[:4] == b"\x00\x00\x02\x00":
        return "ico"
    if b[4:8] == b"ftyp":
        brand = b[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
            return "heic"
    if b[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    head = b[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in b[:2048].lower()) or (
            head.startswith(b"<!doctype svg")):
        return "svg"
    return "bin"


MIME_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
            "image/svg+xml": "svg", "image/bmp": "bmp", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
            "image/avif": "avif", "image/tiff": "tiff", "image/heic": "heic", "image/apng": "png"}


def is_animated(path: Path, kind: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(200_000)
        if kind == "webp":
            return b"ANIM" in head[:200]
        if kind == "gif":
            return head.count(b"\x00\x21\xf9\x04") > 1 or head.count(b"\x21\xf9\x04") > 1
        if kind == "png":
            return b"acTL" in head[:2000]
    except OSError:
        pass
    return False


# ---------------------------------------------------------------- image download

@dataclass
class Downloaded:
    path: Path
    kind: str           # sniffed type
    size: int
    url: str
    suggested_name: str
    final_url: str = ""


def _safe_stem(s: str) -> str:
    s = unquote(s)
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    s = s.strip(" .")
    return s


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def name_from_url(url: str, index: int = 0) -> str:
    if url.lower().startswith("data:"):
        return f"image-{index + 1}"
    sp = urlsplit(url)
    base = sp.path.rstrip("/").rsplit("/", 1)[-1]
    base = _safe_stem(base)
    if not base:
        # e.g. https://x/?img=foo.jpg  -> try the query
        for v in urllib.parse.parse_qsl(sp.query):
            if extract.looks_like_image_url(v[1]):
                base = _safe_stem(v[1].rsplit("/", 1)[-1].split("?")[0])
                break
    if not base:
        base = f"image-{index + 1}"
    return base


def finalize_filename(name: str, kind: str) -> str:
    """Keep the original name, but make sure the extension matches the real image type."""
    name = _safe_stem(name) or "image"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext_l = ext.lower()
    want = {"jpg": "jpg"}.get(kind, kind)
    if want == "bin":
        return name
    same = ext_l == want or (want == "jpg" and ext_l in ("jpeg", "jpe", "jfif")) or (want == "tiff" and ext_l == "tif")
    if same:
        return stem + "." + ext
    if ext_l in extract.IMG_EXTS or ext_l in extract.NON_IMAGE_EXTS or ext_l == "":
        return f"{stem or name}.{want}"
    # unknown ext such as '.1' or '.v2' -> keep whole name and append real ext
    return f"{name}.{want}"


def decode_data_uri(uri: str) -> bytes | None:
    try:
        head, _, payload = uri.partition(",")
        if ";base64" in head.lower():
            return base64.b64decode(payload + "=" * (-len(payload) % 4), validate=False)
        return urllib.parse.unquote_to_bytes(payload)
    except Exception:
        return None


def download_image(session: requests.Session, url: str, referer: str, tmpdir: Path, index: int,
                   cancel: threading.Event | None = None, progress=None, max_bytes: int = MAX_IMAGE_BYTES) -> Downloaded:
    """Download one image to tmpdir (the real original).  Raises FetchError with a short reason."""
    tmp = tmpdir / f"img{index:05d}.part"
    if url.lower().startswith("data:"):
        data = decode_data_uri(url)
        if not data:
            raise FetchError("bad data URI")
        tmp.write_bytes(data)
        kind = sniff_type(data[:2048])
        if kind == "bin":
            tmp.unlink(missing_ok=True)
            raise FetchError("not an image")
        return Downloaded(tmp, kind, len(data), url, name_from_url(url, index))
    if url.lower().startswith("file:"):
        p = file_url_to_path(url)
        if not p.is_file():
            raise FetchError("file missing")
        shutil.copyfile(p, tmp)
        with open(tmp, "rb") as f:
            head = f.read(2048)
        kind = sniff_type(head)
        if kind == "bin":
            tmp.unlink(missing_ok=True)
            raise FetchError("not an image")
        return Downloaded(tmp, kind, tmp.stat().st_size, url, name_from_url(url, index))
    last = None
    for attempt, ref in enumerate((referer, None)):
        headers = dict(IMG_HEADERS)
        if ref:
            headers["Referer"] = ref
        r = None
        for tries in range(6):
            try:
                r = session.get(url, headers=headers, timeout=(10, 30), stream=True, allow_redirects=True)
            except Exception as e:
                last = FetchError(_short_err(e))
                r = None
                break
            if r.status_code in (429, 503) and tries < 5:
                try:
                    wait = max(1.0, min(10.0, float(r.headers.get("Retry-After", ""))))
                except ValueError:
                    wait = 2.0 * (tries + 1)
                r.close()
                if cancel is not None and cancel.wait(wait):
                    raise FetchError("cancelled")
                if cancel is None:
                    import time
                    time.sleep(wait)
                continue
            break
        if r is None:
            continue
        try:
            if r.status_code in (401, 403, 406) and attempt == 0:
                last = FetchError(f"HTTP {r.status_code} (blocked / hotlink protection)")
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}")
            ct = r.headers.get("content-type", "").lower().split(";")[0].strip()
            if ct in ("text/html", "application/json", "text/css", "application/javascript", "text/javascript"):
                raise FetchError("not an image (web page)")
            total = 0
            with open(tmp, "wb") as f:
                for ch in r.iter_content(65536):
                    if cancel is not None and cancel.is_set():
                        raise FetchError("cancelled")
                    f.write(ch)
                    total += len(ch)
                    if progress:
                        progress(total)
                    if total > max_bytes:
                        raise FetchError(f"file too large (>{max_bytes // (1024 * 1024)} MB)")
            with open(tmp, "rb") as f:
                head = f.read(2048)
            kind = sniff_type(head)
            if kind == "bin":
                tmp.unlink(missing_ok=True)
                raise FetchError("not an image")
            name = name_from_url(r.url or url, index)
            cd = r.headers.get("content-disposition", "")
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
            if m and "." in m.group(1) and name.startswith("image-"):
                name = _safe_stem(m.group(1))
            return Downloaded(tmp, kind, total, url, name, r.url)
        except FetchError as e:
            last = e
            if attempt == 0 and "blocked" in str(e):
                continue
            tmp.unlink(missing_ok=True)
            raise
        finally:
            r.close()
    tmp.unlink(missing_ok=True)
    raise last or FetchError("failed")


def _short_err(e: Exception) -> str:
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return "timeout"
    if isinstance(e, requests.exceptions.SSLError):
        return "certificate error"
    return "connection failed"


def make_tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="wig-"))
