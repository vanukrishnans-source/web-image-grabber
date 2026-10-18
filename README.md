# Web Image Grabber

Portable Windows 10/11 x64 app: **paste a web page address, see a thumbnail of every picture on it, tick the ones you want, and save the full-size originals to a folder you pick.**
Made for the ASUS ROG Ally X (touch + controller friendly, one fixed screen at 1920x1080 @150 %), but it works on any Windows PC.

No AI models, no account, no installer. Unsigned, ~40 MB zip.

## Use

1. Unzip, open the **WebImageGrabber** folder, run **WebImageGrabber.exe**. (SmartScreen: **More info -> Run anyway**.)
2. Paste the page address (or press **Paste**) and press **Fetch images**. Thumbnails appear as they download, each with its real size (`1920x1080`), type and file size.
3. **Choose:** tap a thumbnail to tick it. **Press and hold** it (or hover with a mouse, or double-tap / **Preview**) for a big preview, with Previous / Next / Select buttons.
   *Select all / none / Invert*, filters (**Min width/height**, **JPG PNG WebP GIF SVG Other**), **Sort** (page order, dimensions, file size) and **Tiles** size help narrow a long page down.
4. **Save:** press **Browse...** to pick the folder (it is remembered), then **Save selected (N)**. The originals are copied byte-for-byte (full resolution, not the thumbnails). **Cancel** stops, **Open folder** shows the result.

Other ways in: **Paste URL list...** (a list of image links), drag a `.html` file onto the window, or drag a link from your browser.

Extras: **Render page (JavaScript)** (hidden Edge/Chrome scrolls the page to trigger lazy loading), **Subfolder named after the page**, **WebP -> JPG/PNG** conversion on save, **Include tiny (<16 px)** to show tracking pixels (hidden by default).

Scrolling: the thumbnail grid is the only scrolling area. Mouse wheel, finger/pen **drag** (with momentum), **PgUp / PgDn / Home / End** and arrow keys all work; the slim bar on the right is only an indicator. Space / Enter ticks the highlighted tile, **P** previews it.

## What it finds

`<img src>`, `srcset` (largest candidate), `data-src` / `data-original` / other lazy-load attributes, `<picture><source>`, CSS `background-image` (inline, `<style>` and linked stylesheets, `@import`), `og:image` / `twitter:image`, favicons and touch icons, `<video poster>`, links to image files, JSON-LD / inline JSON image URLs, `data:` URIs. Relative URLs are resolved, duplicates (same address or identical bytes) are merged, `<base href>` is honoured. Wikimedia/Wikipedia resized thumbnails are swapped for the original file (Options).

The page is requested with a normal Edge User-Agent and the page as Referer, redirects, gzip and cookies are handled, and 429 / 503 answers are retried politely (at most 5 parallel requests per host).

## Limits

* **Sites that block bots** (Cloudflare / "are you human" pages, some Unsplash / Pexels / Instagram setups) answer 403 - you get a clear message; try *Render page*, otherwise save from your browser.
* **Login-only pages** (401, or a login redirect) cannot be fetched; the app has no sign-in.
* **JavaScript-heavy pages** (infinite scroll, images created by scripts): tick *Render page (JavaScript)*. It needs Microsoft Edge or Chrome (Edge is built into Windows 11) and is slower. Very long infinite-scroll feeds are only scrolled for ~20 s.
* **Hotlink protection:** images are requested with the page as Referer (retrying without it); some CDNs still refuse - those show up under *Details* as "could not be downloaded".
* **AVIF / HEIC** images are saved fine but show a "No preview" tile (Windows Qt cannot decode them). Animated GIF/WebP show their first frame in the grid but are saved untouched. Videos are not downloaded.
* Thumbnails come from the real downloaded files, so a page with hundreds of large photos downloads them all before the grid is complete (use *Stop* any time; what is loaded can still be saved). Up to 2000 image links per page.
* Respect each site's terms and copyright; only download what you are allowed to use.

## Build / test

```
pip install -r requirements-win.txt
python -m wig                       # run the app
python -m wig --selftest            # local fixture site: fetch -> parse -> download -> save, 30+ assertions
pyinstaller WebImageGrabber.spec    # dist/WebImageGrabber/WebImageGrabber.exe  (+ _cli.exe for tests)
```
GitHub Actions (`.github/workflows/build.yml`) builds the packaged exe on `windows-latest`, runs the selftest and a real windowed GUI test (fetch -> thumbnails -> click tiles -> wheel / PgDn / drag / hold -> save -> verify files) against the packaged exe, zips and publishes the release.

MIT licence - see `LICENSE`.
