"""Рasterization of Tabler Filled icons into PNG silhouettes.

Python runtime (Tkinter/CustomTkinter) cannot read SVG, so the icons used in
the interface are rasterized once into `assets/icons/` and connected in the
code as PNG. The SVG sources stay in `filled/` (Tabler Icons, Filled).

The converter is a development utility: it is not needed at runtime and is
not listed in `requirements.txt`. To repeat the rasterization:

    pip install svglib pypdfium2
    python scripts/rasterize_icons.py

The script writes white silhouettes with an alpha channel; the color of the
icon at runtime is set by the icon cache (`app/ui/icons.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import PROJECT_ROOT  # noqa: E402  — путь к проекту из модуля путей

ROOT = PROJECT_ROOT
FILLED = ROOT / 'filled'
TARGET = ROOT / 'assets' / 'icons'

#: Icons used in the interface: file name without the extension.
ICONS = [
    'brand-messenger',     # Telegram
    'camera',              # camera (ANPR)
    'device-cctv',         # camera (ANPR, surveillance)
    'device-desktop',      # ANPR service
    'send',                # sending
    'chevron-down',        # disclosure
    'x',                   # close
    'file-text',           # service log
    'player-play',         # start
    'player-stop',         # stop
    'player-skip-back',    # restart
    'alert-triangle',      # error
    'circle-check',        # ok
    'settings',            # settings
]

#: Size of the resulting PNG, px (24x24 source, 4x reserve).
SIZE = 96


def render(name: str) -> Path:
    """Rasterize one icon into a white silhouette PNG."""
    from PIL import Image
    from reportlab.graphics import renderPDF
    from svglib.svglib import svg2rlg
    import pypdfium2

    source = FILLED / f'{name}.svg'
    if not source.exists():
        raise FileNotFoundError(source)
    drawing = svg2rlg(str(source))
    pdf = renderPDF.drawToString(drawing)
    document = pypdfium2.PdfDocument(pdf)
    page = document[0]
    scale = SIZE / float(drawing.width or SIZE)
    bitmap = page.render(scale=scale)
    image = bitmap.to_pil().convert('RGBA')

    # The icon is a mask: the alpha channel carries the shape, the color is
    # set at runtime. If the rasterizer filled the background, invert it.
    alpha = image.getchannel('A')
    if alpha.getextrema() == (255, 255):
        alpha = image.convert('L').point(lambda value: 255 - value)
    silhouette = Image.new('RGBA', image.size, (255, 255, 255, 0))
    silhouette.putalpha(alpha)
    TARGET.mkdir(parents=True, exist_ok=True)
    path = TARGET / f'{name}-filled.png'
    silhouette.save(path)
    return path


def main() -> int:
    for name in ICONS:
        try:
            path = render(name)
        except FileNotFoundError as error:
            print(f'! net ishodnika: {error}')
            return 1
        print(f'ok {path.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
