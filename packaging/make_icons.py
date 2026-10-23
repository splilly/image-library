"""Draw the app icon (a camera on blue) and save it as .png, .ico and .icns."""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent


def draw(size: int) -> Image.Image:
    s = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=52 * s, fill=(59, 111, 216, 255))
    d.rounded_rectangle((44 * s, 84 * s, 212 * s, 196 * s), radius=18 * s, fill=(255, 255, 255, 255))
    d.rounded_rectangle((92 * s, 64 * s, 164 * s, 96 * s), radius=8 * s, fill=(255, 255, 255, 255))
    d.ellipse((96 * s, 108 * s, 160 * s, 172 * s), fill=(59, 111, 216, 255))
    d.ellipse((110 * s, 122 * s, 146 * s, 158 * s), fill=(255, 255, 255, 255))
    return img


if __name__ == "__main__":
    big = draw(1024)
    big.resize((256, 256), Image.Resampling.LANCZOS).save(HERE / "icon.png")
    big.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(HERE / "icon.icns")
    print("icons written")
