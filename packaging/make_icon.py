"""Рисует значок приложения: три корешка на полке. Запускается один раз, результат лежит в assets."""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
SHELF = "#5d4a36"
SPINES = [
    ("#8a2c38", 86, 150, 118),
    ("#2b4a7d", 214, 60, 92),
    ("#2d6a4e", 316, 104, 110),
]


def draw() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)
    canvas.rounded_rectangle((16, 16, SIZE - 16, SIZE - 16), radius=72, fill="#dfe9de")
    bottom = 410
    for color, x, top, width in SPINES:
        canvas.rectangle((x, top, x + width, bottom), fill=color)
        canvas.rectangle((x + width - 12, top, x + width, bottom), fill=(0, 0, 0, 60))
        canvas.rectangle((x + 22, top + 42, x + width - 30, top + 56), fill="#f4f2e8")
    canvas.rectangle((60, bottom, SIZE - 60, bottom + 30), fill=SHELF)
    return image


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "src" / "knigovert" / "assets" / "icon.ico"
    draw().save(target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(target)
