"""Generate the local celebratory trophy animation without external APIs."""

import math
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    """Render a small looping vector-style trophy using Pillow."""
    frames = []
    for frame in range(24):
        image = Image.new("RGB", (360, 360), "#101b35")
        draw = ImageDraw.Draw(image)
        for radius in range(150, 30, -4):
            strength = int(16 + (150 - radius) * 0.22)
            draw.ellipse(
                (180 - radius, 165 - radius, 180 + radius, 165 + radius),
                fill=(strength, strength // 2 + 25, 48),
            )
        draw.arc((67, 92, 164, 210), 75, 285, fill="#ffc83d", width=14)
        draw.arc((196, 92, 293, 210), -105, 105, fill="#ffc83d", width=14)
        draw.polygon(
            [(113, 80), (247, 80), (234, 182), (210, 212), (150, 212), (126, 182)],
            fill="#f7bd30",
        )
        draw.polygon([(122, 89), (173, 89), (161, 193), (142, 177)], fill="#ffe784")
        draw.rectangle((168, 210, 192, 253), fill="#e7a723")
        draw.rounded_rectangle((127, 251, 233, 276), radius=8, fill="#ffd456")
        draw.rounded_rectangle((108, 277, 252, 295), radius=5, fill="#a97122")
        for i in range(9):
            angle = i * math.tau / 9 + frame * 0.015
            x, y = 180 + math.cos(angle) * 142, 170 + math.sin(angle) * 140
            size = 3 + 5 * (1 + math.sin(frame * math.tau / 24 + i)) / 2
            draw.polygon(
                [
                    (x - size, y),
                    (x - 1, y - 1),
                    (x, y - size),
                    (x + 1, y - 1),
                    (x + size, y),
                    (x + 1, y + 1),
                    (x, y + size),
                    (x - 1, y + 1),
                ],
                fill="#fff5be",
            )
        frames.append(image)
    target = (
        Path(__file__).resolve().parents[1]
        / "data/plugins/astrbot_plugin_superbot/assets/mines-trophy-v1.gif"
    )
    frames[0].save(
        target,
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
        optimize=True,
    )


if __name__ == "__main__":
    main()
