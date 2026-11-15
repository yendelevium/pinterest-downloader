from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PIL import Image


def valid_pin_id(value: str) -> bool:
    return bool(re.fullmatch(r'(?:\d+|e1_[A-Za-z0-9_-]{1,180})', value))


@dataclass
class Pin:
    id: str
    assets: list[list[str]] = field(default_factory=list)
    resolved: bool = False
    reason: str = ""

    @property
    def url(self):
        return f"https://www.pinterest.com/pin/{self.id}/"


def image_url(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme == "https" and p.hostname == "i.pinimg.com" and not p.username


def candidates(values: list[tuple[str, float]]) -> list[str]:
    """Explicit original, synthesized original, then ranked available sources."""
    values = [(u, rank) for u, rank in values if image_url(u)]
    values.sort(key=lambda item: item[1], reverse=True)
    originals = [u for u, _ in values if urlsplit(u).path.startswith('/originals/')]
    synthesized = []
    for url, _ in values:
        p = urlsplit(url)
        if re.match(r"^/\d+x(?:\d+)?(?:_[a-z0-9]+)?/", p.path):
            synthesized.append(urlunsplit(p._replace(path=re.sub(r"^/[^/]+/", "/originals/", p.path))))
    return list(dict.fromkeys(originals + synthesized + [u for u, _ in values]))


def srcset_candidates(src: str, srcset: str) -> list[str]:
    values = [(src, 0.0)] if src else []
    for entry in srcset.split(','):
        parts = entry.strip().split()
        if parts:
            try:
                rank = float(parts[1][:-1]) if len(parts) > 1 else 0
            except ValueError:
                rank = 0
            values.append((parts[0], rank))
    return candidates(values)


def parse_pin(data: dict) -> Pin | None:
    pin_id = str(data.get('id', ''))
    if not valid_pin_id(pin_id):
        return None
    assets = []

    def collect(node):
        if isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, dict):
            # Never treat video posters or unrelated embedded pins as image assets.
            if node.get('type') in ('video', 'video_block') or node.get('video') or node.get('videos'):
                return
            images = node.get('images')
            if isinstance(images, dict):
                urls = candidates([(v['url'], float(v.get('width') or 0))
                                   for v in images.values()
                                   if isinstance(v, dict) and isinstance(v.get('url'), str)])
                if urls:
                    assets.append(urls)
            image = node.get('image')
            if isinstance(image, dict) and isinstance(image.get('url'), str):
                urls = candidates([(image['url'], float(image.get('width') or 0))])
                if urls:
                    assets.append(urls)
            for key in ('pages', 'blocks', 'carousel_data', 'carousel_slots', 'story_pin_data', 'image'):
                if key in node:
                    collect(node[key])

    multi = data.get('carousel_data') or data.get('story_pin_data')
    if multi:
        collect(multi)
    else:
        collect(data)
    unique = {urlsplit(a[0]).path: a for a in assets}
    video = bool(data.get('videos') or data.get('video'))
    # A populated multi-image structure must contain its pages/items to be resolved.
    resolved = bool(unique) or video
    if data.get('is_carousel') and not data.get('carousel_data'):
        resolved = False
    if data.get('is_story_pin') and not data.get('story_pin_data'):
        resolved = False
    return Pin(pin_id, list(unique.values()), resolved,
               'video-only pin' if video and not unique else '')


def safe_name(name: str) -> str:
    return re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', name).strip(' .')[:100] or 'board'


def image_format(path: Path) -> str:
    with Image.open(path) as im:
        fmt = im.format
        im.verify()
    with Image.open(path) as im:
        im.load()
    extensions = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif',
                  'AVIF': 'avif', 'TIFF': 'tiff', 'BMP': 'bmp'}
    if fmt not in extensions:
        raise ValueError(f'Unsupported image format: {fmt}')
    return extensions[fmt]


def atomic_json(path: Path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def report(state: dict) -> str:
    pins = state['pins']
    counts = {status: sum(p['status'] == status for p in pins.values())
              for status in ('complete', 'existing', 'partial', 'skipped', 'failed')}
    expected = state.get('expected')
    lines = [f"Source: {state['url']}", f"Discovery: {state['discovery']}",
             f"Pinterest-reported pins: {expected if expected is not None else 'unknown'}",
             f"Unique pins discovered: {len(pins)}",
             ' | '.join(f'{k}: {v}' for k, v in counts.items()),
             f"Verified image files: {sum(len(p.get('files', [])) for p in pins.values())}"]
    if expected is not None:
        lines.append(f"Undiscovered count gap: {max(0, expected - len(pins))} (URLs unknown)")
        if len(pins) > expected:
            lines.append('Discovered count exceeds reported count; board may have changed.')
    lines += [f"Notice: {x}" for x in state.get('notes', [])]
    problems = [p for p in pins.values() if p['status'] not in ('complete', 'existing')]
    lines.append('\nSkipped / partial / failed pins:')
    lines += [f"{p['url']} [{p['status']}] {p.get('reason', 'unfinished')}" for p in problems]
    if not problems:
        lines.append('None among discovered pins.')
    fallbacks = [f"{p['url']} -> {f['url']}" for p in pins.values()
                 for f in p.get('files', []) if '/originals/' not in f['url']]
    if fallbacks:
        lines += ['\nLower-resolution fallbacks:'] + fallbacks
    return '\n'.join(lines) + '\n'
