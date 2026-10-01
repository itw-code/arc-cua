"""Render reel.html with headless Chromium: keyframes for a contact sheet, or every frame.

Usage: python render.py keys [out_dir] [t1,t2,...]   |   python render.py all [out_dir]
"""
import base64
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
FPS = 60
DUR = 15
KEYS = [0.7, 1.5, 2.2, 2.9, 3.9, 4.8, 5.9, 6.5, 7.2, 7.8, 8.3, 9.2, 10.2, 10.7, 11.5, 12.1, 12.8, 13.6, 14.2, 14.6]

CLEAR_JS = """
const cv = document.getElementById('c');
const ctx = cv.getContext('2d');
ctx.globalCompositeOperation = 'copy';
ctx.globalAlpha = 1;
ctx.fillStyle = '#000';
ctx.fillRect(0, 0, cv.width, cv.height);
ctx.globalCompositeOperation = 'source-over';
if (typeof OFF !== 'undefined') {
  const offCtx = OFF.getContext('2d');
  offCtx.globalCompositeOperation = 'copy';
  offCtx.globalAlpha = 1;
  offCtx.fillStyle = '#000';
  offCtx.fillRect(0, 0, OFF.width, OFF.height);
  offCtx.globalCompositeOperation = 'source-over';
}
"""

RENDER_JS = """(t) => {
    window.render(t);
    return document.getElementById('c').toDataURL('image/png');
}
"""


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "keys"
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / ("keys" if mode == "keys" else "frames")
    if mode == "keys":
        times = [float(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else KEYS
    else:
        times = [i / FPS for i in range(DUR * FPS)]
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto((HERE / "reel.html").resolve().as_uri() + "?clean=1", wait_until="networkidle")
        pg.evaluate("window.ready.then(() => { window._fontsReady = true; })")
        pg.wait_for_function("window._fontsReady")
        time.sleep(0.1)
        for i, t in enumerate(times):
            pg.evaluate(CLEAR_JS)
            data = pg.evaluate(RENDER_JS, t)
            (out / f"f{i:04d}.png").write_bytes(base64.b64decode(data.split(",", 1)[1]))
            if mode == "all" and i % 60 == 0:
                print(f"  {i}/{len(times)}", flush=True)
        b.close()
    print(f"rendered {len(times)} frames to {out}")


if __name__ == "__main__":
    main()
