"""Assemble rasterized terminal frames into a compact GIF with a shared palette."""

import sys
from pathlib import Path

from PIL import Image

target = Path(sys.argv[1])
width = int(sys.argv[2])
# Durations in ms, comma-separated; "170x15" means 170 repeated 15 times.
durations = [
    int(ms)
    for part in sys.argv[3].split(",")
    for ms in ([part.split("x")[0]] * int(part.split("x")[1]) if "x" in part else [part])
]
paths = [Path(p) for p in sys.argv[4:]]
assert len(paths) == len(durations), (len(paths), len(durations))

frames = []
for path in paths:
    image = Image.open(path).convert("RGBA")
    background = Image.new("RGBA", image.size, (13, 13, 17, 255))
    image = Image.alpha_composite(background, image).convert("RGB")
    if width and image.width != width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    frames.append(image)

montage = Image.new("RGB", (frames[0].width, frames[0].height * len(frames)))
for index, frame in enumerate(frames):
    montage.paste(frame, (0, index * frame.height))
palette_image = montage.quantize(
    colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
)
quantized = [frame.quantize(palette=palette_image, dither=Image.Dither.NONE) for frame in frames]
quantized[0].save(
    target,
    save_all=True,
    append_images=quantized[1:],
    duration=durations,
    loop=0,
    optimize=True,
    disposal=1,
)
print(target, target.stat().st_size, "bytes", frames[0].size, len(frames), "frames")
