"""Render reel.html with headless Chromium: keyframes for a contact sheet, or every frame.

Usage: python render.py keys|all <index|arc> [out_dir]
"""
import base64
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
FPS = 60
DUR = 19.2
KEYS = [1.8, 5.2, 7.2, 10.0, 14.1, 16.8, 18.8]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "keys"
    v = sys.argv[2] if len(sys.argv) > 2 else "index"
    out = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else HERE / (f"keys_{v}" if mode == "keys" else f"frames_{v}")
    if mode == "keys":
        times = KEYS
    else:
        times = [i / FPS for i in range(round(DUR * FPS))]
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto((HERE / "reel.html").resolve().as_uri() + f"?clean=1&v={v}", wait_until="networkidle")
        pg.evaluate("window.ready")
        for i, t in enumerate(times):
            data = pg.evaluate("(t) => { window.render(t); return document.getElementById('c').toDataURL('image/png'); }", t)
            (out / f"f{i:04d}.png").write_bytes(base64.b64decode(data.split(",", 1)[1]))
            if mode == "all" and i % 60 == 0:
                print(f"  {i}/{len(times)}", flush=True)
        b.close()
    print(f"rendered {len(times)} frames to {out}")


if __name__ == "__main__":
    main()
