"""Draw the ZAC Zentrale desktop icon (frontend/zac-zentrale.ico): red rounded square, white "ZAC"."""
import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, size - 8, size - 8), radius=44, fill="#C62828")
d.rounded_rectangle((8, 196, size - 8, size - 8), radius=44, fill="#1D1D1B")
d.rectangle((8, 196, size - 8, 214), fill="#1D1D1B")
font = None
for name in ("bahnschrift.ttf", "arialbd.ttf", "segoeuib.ttf"):
    try:
        font = ImageFont.truetype(os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", name), 104)
        break
    except OSError:
        continue
font = font or ImageFont.load_default()
box = d.textbbox((0, 0), "ZAC", font=font)
d.text(((size - (box[2] - box[0])) / 2 - box[0], 100 - (box[3] - box[1]) / 2 - box[1]), "ZAC", font=font, fill="white")
img.save(os.path.join(ROOT, "frontend", "zac-zentrale.ico"), sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
print("ok")
