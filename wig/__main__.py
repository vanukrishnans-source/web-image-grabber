from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import APP_NAME, __version__


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="WebImageGrabber", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="local HTTP fixture test of fetch -> parse -> download -> save (no window)")
    ap.add_argument("--selftest-out", default="selftest-out")
    ap.add_argument("--require-render", action="store_true", help="selftest: fail if no Edge/Chrome for render mode")
    ap.add_argument("--serve-fixture", action="store_true", help="start the fixture web server and print its URL (for manual tests)")
    ap.add_argument("--fixture-port", type=int, default=0)
    # GUI automation (used by CI and by my own screenshots)
    ap.add_argument("--url")
    ap.add_argument("--urls-file")
    ap.add_argument("--html-file")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--tiny", action="store_true")
    ap.add_argument("--min-w", type=int, default=0)
    ap.add_argument("--select", help="'all' or comma separated tile numbers (0-based, in displayed order) to click")
    ap.add_argument("--save", action="store_true", help="after selecting, click Save selected")
    ap.add_argument("--folder", help="destination folder (instead of the Browse dialog)")
    ap.add_argument("--subfolder", action="store_true")
    ap.add_argument("--convert", choices=["keep", "jpg", "png"])
    ap.add_argument("--scroll-test", action="store_true", help="drive wheel / PgUp / PgDn / drag / press-and-hold on the real grid")
    ap.add_argument("--shots-dir")
    ap.add_argument("--shot-prefix", default="")
    ap.add_argument("--size", help="window size WxH")
    ap.add_argument("--report")
    ap.add_argument("--timeout", type=int, default=300)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    if args.selftest or args.serve_fixture:
        from . import selftest
        if args.serve_fixture:
            return selftest.serve_forever(args.fixture_port)
        return selftest.run(Path(args.selftest_out), require_render=args.require_render)

    from PySide6.QtCore import QCoreApplication, QSettings, Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    QCoreApplication.setOrganizationName("WebImageGrabber")
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for cand in (base / "icon.ico", base / "packaging" / "icon.ico"):
        if cand.exists():
            app.setWindowIcon(QIcon(str(cand)))
            break
    from .gui import MainWindow
    auto = {}
    if args.url or args.urls_file or args.html_file:
        sel = None
        if args.select:
            sel = "all" if args.select == "all" else [int(x) for x in args.select.split(",") if x.strip()]
        auto = dict(url=args.url, urls_file=args.urls_file, html_file=args.html_file, render=args.render, tiny=args.tiny,
                    min_w=args.min_w, select=sel, save=args.save, folder=args.folder, subfolder=args.subfolder,
                    convert=args.convert, scroll_test=args.scroll_test, shots_dir=args.shots_dir, shot_prefix=args.shot_prefix, report=args.report,
                    timeout=args.timeout,
                    size=tuple(int(x) for x in args.size.lower().split("x")) if args.size else None)
    w = MainWindow(auto)
    if auto:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, w.run_auto)
        w.show()
        return app.exec()
    w.showMaximized() if os.environ.get("WIG_MAXIMIZE", "1") == "1" else w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
