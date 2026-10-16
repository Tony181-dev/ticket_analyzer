"""Render packaging/icon.ico (same design as web/icon.svg). Needs Pillow; run once when the logo changes."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # draw large, downscale for crisp small sizes
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
k = S / 32
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(8 * k), fill=(36, 87, 214, 255))
white = (255, 255, 255, 255)
d.line([(11.6 * k, 14.6 * k), (20.4 * k, 10.4 * k)], fill=white, width=int(2 * k))
d.line([(11.6 * k, 17.4 * k), (20.4 * k, 21.6 * k)], fill=white, width=int(2 * k))
for cx, cy in ((9, 16), (23, 9), (23, 23)):
    d.ellipse([(cx - 3) * k, (cy - 3) * k, (cx + 3) * k, (cy + 3) * k], fill=white)

out = Path(__file__).with_name("icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
