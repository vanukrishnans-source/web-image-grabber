"""Independent check of a GUI run: every ticked tile was saved byte-for-byte (fresh download of its URL), nothing else was saved."""
import hashlib, json, sys
from pathlib import Path
import requests
from PIL import Image

run = Path(sys.argv[1])
rep = json.loads((run / "report.json").read_text())
saved_dir = Path(rep["save_folder"])
found = {f["order"]: f for f in rep["found"]}
files = sorted(p for p in saved_dir.iterdir() if p.is_file())
print(f"visible tiles: {len(rep['visible'])}   ticked: {len(rep['selected'])}   files in folder: {len(files)}")
ok = len(files) == len(rep["selected"]) == len(rep["saved"])
sel_sha = {s["sha1"] for s in rep["selected"]}
saved_sha = {hashlib.sha1(p.read_bytes()).hexdigest() for p in files}
ok &= sel_sha == saved_sha
print(f"saved set == ticked set (SHA-1 of bytes): {sel_sha == saved_sha}")
rows = []
for s in rep["selected"]:
    f = found[s["order"]]
    r = requests.get(f["url"], headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0", "Accept": "image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8", "Referer": f["url"]}, timeout=60)
    fresh = hashlib.sha1(r.content).hexdigest()
    p = next((q for q in files if hashlib.sha1(q.read_bytes()).hexdigest() == s["sha1"]), None)
    dims = "svg"
    if p and p.suffix.lower() not in (".svg",):
        with Image.open(p) as im:
            dims = f"{im.width}x{im.height}"
    same = fresh == s["sha1"]
    ok &= same and p is not None
    rows.append((p.name if p else "MISSING", dims, p.stat().st_size if p else 0, f"{s['w']}x{s['h']}", same))
print(f"{'saved file':72} {'pixels':>10} {'bytes':>9} {'tile says':>10}  identical to fresh download")
for n, d, b, t, same in rows:
    print(f"{n[:72]:72} {d:>10} {b:>9} {t:>10}  {same}")
not_saved = [v["name"] for v in rep["visible"] if v["order"] not in {s["order"] for s in rep["selected"]}]
print(f"{len(not_saved)} other visible tiles were NOT saved (e.g. {not_saved[:4]})")
print("RESULT", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
