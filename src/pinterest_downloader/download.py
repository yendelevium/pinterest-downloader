from __future__ import annotations

import asyncio
import hashlib
import random
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .core import image_format, image_url


class AccessBlocked(Exception):
    pass


def retry_after(value: str | None) -> float:
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 60


class Pace:
    def __init__(self, interval: float, stop: asyncio.Event | None = None):
        self.interval = interval
        self.next_at = 0.0
        self.cool_until = 0.0
        self.lock = asyncio.Lock()
        self.blocked = ''
        self.throttles = 0
        self.stop = stop
        self.forbidden_streak = 0

    async def wait(self):
        async with self.lock:
            while True:
                if self.blocked:
                    raise AccessBlocked(self.blocked)
                if self.stop is not None and self.stop.is_set():
                    raise AccessBlocked('Run stopped after an access block.')
                delay = max(self.next_at, self.cool_until) - time.monotonic()
                if delay <= 0:
                    self.next_at = time.monotonic() + self.interval * random.uniform(1, 1.3)
                    return
                await asyncio.sleep(min(delay, 1))

    def throttle(self, header=None):
        self.throttles += 1
        self.interval *= 2
        self.cool_until = max(self.cool_until, time.monotonic() + max(60, retry_after(header)))
        if self.throttles >= 3:
            self.blocked = 'Stopped after repeated HTTP 429 responses; retry later.'

    def forbidden(self, header=None) -> float:
        """Slow all workers on 403 without cancelling unrelated images."""
        self.forbidden_streak += 1
        delay = min(60, 5 * 2 ** min(self.forbidden_streak - 1, 4))
        if header is not None:
            delay = max(delay, retry_after(header))
        self.cool_until = max(self.cool_until, time.monotonic() + delay)
        return delay


def digest(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def valid_existing(folder: Path, entry: dict) -> bool:
    name = entry.get('name', '')
    if not name or Path(name).name != name:
        return False
    path = folder / name
    try:
        return not path.is_symlink() and image_format(path) and digest(path) == entry['sha256']
    except (OSError, ValueError, KeyError):
        return False


class Downloader:
    def __init__(self, client: httpx.AsyncClient, folder: Path, pace: Pace):
        self.client, self.folder, self.pace = client, folder, pace

    async def asset(self, pin_id: str, index: int, urls: list[str], previous: dict | None = None):
        if previous and previous.get('url') in urls and await asyncio.to_thread(valid_existing, self.folder, previous):
            return previous, True
        part = self.folder / f'{pin_id}_{index}.part'
        if part.is_symlink():
            raise ValueError(f'Refusing symlink: {part}')
        errors = []
        forbidden_retries = 2  # Shared across this asset's URL candidates.
        try:
            for url in dict.fromkeys(urls):
                if not image_url(url):
                    continue
                for attempt in range(3):
                    await self.pace.wait()
                    try:
                        async with self.client.stream('GET', url) as response:
                            if response.status_code == 429:
                                self.pace.throttle(response.headers.get('retry-after'))
                                continue
                            if response.status_code == 403:
                                delay = self.pace.forbidden(response.headers.get('retry-after'))
                                errors.append(f'HTTP 403: {url}')
                                retry = forbidden_retries > 0 and attempt < 2
                                action = 'retrying this URL' if retry else 'trying any remaining resolutions; other pins will continue'
                                print(f'Image HTTP 403: {url}; shared cooldown {delay:g}s, {action}.', file=sys.stderr, flush=True)
                                if retry:
                                    forbidden_retries -= 1
                                    continue
                                break
                            if response.status_code == 401:
                                self.pace.blocked = f'Image host returned HTTP 401 for {url}; stopped to avoid repeated unauthorized requests.'
                                raise AccessBlocked(self.pace.blocked)
                            if response.status_code in (404, 410):
                                errors.append(f'{response.status_code}: {url}')
                                break
                            response.raise_for_status()
                            size = 0
                            with part.open('wb') as output:
                                async for chunk in response.aiter_bytes():
                                    size += len(chunk)
                                    if size > 100 * 1024 * 1024:
                                        raise ValueError('Image exceeds 100 MiB limit')
                                    output.write(chunk)
                        extension = await asyncio.to_thread(image_format, part)
                        filename = f'{pin_id}_{index}.{extension}'
                        checksum = await asyncio.to_thread(digest, part)
                        part.replace(self.folder / filename)
                        self.pace.forbidden_streak = 0
                        return {'index': index, 'name': filename, 'url': url, 'sha256': checksum}, False
                    except (httpx.HTTPError, OSError, ValueError) as exc:
                        errors.append(f'{url}: {exc}')
                        if isinstance(exc, (ValueError, OSError)):
                            break
                        if attempt < 2:
                            await asyncio.sleep(2 ** (attempt + 1))
                if self.pace.blocked:
                    raise AccessBlocked(self.pace.blocked)
            raise ValueError('; '.join(dict.fromkeys(errors)) or 'No usable image URLs')
        finally:
            part.unlink(missing_ok=True)
