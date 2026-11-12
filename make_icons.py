"""Draw the PNG app icons (iPhone needs PNG for the home screen). Run at image build time."""
import sys
from PIL import Image, ImageDraw

out = sys.argv[1]
GREEN, PAPER = (47, 107, 79), (244, 241, 234)
for size in (180, 192, 512):
    s = size / 64
    img = Image.new("RGB", (size, size), GREEN)   # full bleed: iOS and Android round the corners
    d = ImageDraw.Draw(img)
    d.polygon([(20 * s, 12 * s), (44 * s, 12 * s), (44 * s, 52 * s), (38 * s, 48 * s), (32 * s, 52 * s), (26 * s, 48 * s), (20 * s, 52 * s)], fill=PAPER)
    for y, x2 in ((22, 38), (30, 38), (38, 33)):
        d.line([(26 * s, y * s), (x2 * s, y * s)], fill=GREEN, width=max(2, round(3 * s)))
    img.save(f"{out}/icon-{size}.png")
