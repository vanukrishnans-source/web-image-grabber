"""Qt-based image helpers: probing size, making thumbnails, converting.  Safe to call from worker threads."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap


def _checker(w: int, h: int, cell: int = 12) -> QImage:
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    c1, c2 = QColor(236, 238, 242), QColor(210, 214, 222)
    for y in range(0, h, cell):
        for x in range(0, w, cell):
            p.fillRect(x, y, cell, cell, c1 if ((x // cell + y // cell) % 2 == 0) else c2)
    p.end()
    return img


def read_image(path: Path, max_side: int | None = None, for_thumb: bool = False) -> tuple[QImage | None, int, int]:
    """Returns (image scaled to fit max_side or full size, original width, original height).  (None,0,0) if undecodable."""
    r = QImageReader(str(path))
    if r.format().data() in (b"ico", b"cur") and r.imageCount() > 1:
        best, bs = 0, 0                        # multi-size icon: use its largest picture
        for i in range(r.imageCount()):
            if r.jumpToImage(i):
                sz_ = r.size()
                if sz_.width() * sz_.height() > bs:
                    best, bs = i, sz_.width() * sz_.height()
        r.jumpToImage(best)
    else:
        r.setAutoTransform(True)
    sz = r.size()
    ow, oh = sz.width(), sz.height()
    if not r.canRead():
        return None, 0, 0
    if ow > 0 and oh > 0 and max_side and max(ow, oh) > max_side:
        k = max_side / max(ow, oh)
        r.setScaledSize(QSize(max(1, round(ow * k)), max(1, round(oh * k))))
    elif ow <= 0 and max_side and r.format().data() in (b"svg", b"svgz"):
        r.setScaledSize(QSize(max_side, max_side))
    img = r.read()
    if img.isNull():
        # some formats can't report size first / fail with scaled size: retry plain
        r2 = QImageReader(str(path))
        img = r2.read()
        if img.isNull():
            return None, 0, 0
        ow, oh = img.width(), img.height()
        if max_side and max(ow, oh) > max_side:
            img = img.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if ow <= 0 or oh <= 0:
        ow, oh = img.width(), img.height()
    # EXIF-rotated: swap reported dims if orientation transposed (reader.size() is pre-transform)
    if img.width() and img.height() and ((ow > oh) != (img.width() > img.height())) and abs(ow - oh) > 2 and \
            abs(img.width() / img.height() - oh / ow) < 0.05:
        ow, oh = oh, ow
    return img, ow, oh


def flatten(img: QImage, background: str = "checker") -> QImage:
    if not img.hasAlphaChannel():
        return img.convertToFormat(QImage.Format_RGB32)
    out = _checker(img.width(), img.height()) if background == "checker" else QImage(img.size(), QImage.Format_RGB32)
    if background != "checker":
        out.fill(QColor(255, 255, 255))
    p = QPainter(out)
    p.drawImage(0, 0, img)
    p.end()
    return out


def make_thumb(path: Path, max_side: int = 480) -> tuple[int, int, bytes | None]:
    """(width, height, jpeg bytes of thumbnail).  (0, 0, None) if Qt cannot decode this format (e.g. AVIF)."""
    img, w, h = read_image(path, max_side, True)
    if img is None:
        return 0, 0, None
    img = flatten(img)
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPG", 90)
    buf.close()
    return w, h, bytes(ba)


def pixmap_from_bytes(b: bytes) -> QPixmap:
    pm = QPixmap()
    pm.loadFromData(b, "JPG")
    return pm


def convert_to(path: Path, dest: Path, fmt: str) -> None:
    """Re-encode (full resolution) to 'jpg' or 'png'."""
    img, _, _ = read_image(path, None)
    if img is None:
        raise RuntimeError("cannot decode image for conversion")
    if fmt == "jpg":
        img = flatten(img, "white")
        ok = img.save(str(dest), "JPG", 95)
    else:
        ok = img.save(str(dest), "PNG")
    if not ok:
        raise RuntimeError(f"cannot write {dest.name}")
