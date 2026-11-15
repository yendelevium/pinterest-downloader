from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from .core import atomic_json, report, safe_name
from .discovery import Discovery, canonical_url
from .download import AccessBlocked, Downloader, Pace


class Store:
    def __init__(self, dest: Path, name: str, url: str):
        self.folder = dest / safe_name(name)
        self.folder.mkdir(parents=True, exist_ok=True)
        manifest = self.folder / 'manifest.json'
        if manifest.exists():
            existing = json.loads(manifest.read_text())
            if existing.get('url') != url:
                import hashlib
                self.folder = dest / (safe_name(name) + '-' + hashlib.sha256(url.encode()).hexdigest()[:10])
                self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = (self.folder / '.download.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError(f'Another downloader is using {self.folder}')
        manifest = self.folder / 'manifest.json'
        self.previous = json.loads(manifest.read_text()) if manifest.exists() else {}
        if self.previous and self.previous.get('url') != url:
            raise ValueError('Destination manifest belongs to a different source')
        self.state = {'version': 1, 'url': url, 'expected': None, 'discovery': 'in progress', 'notes': [], 'pins': {}}
        self.last_save = 0.0

    def save(self, force=False):
        if force or time.monotonic() - self.last_save >= 5:
            # Keep resume records for pins not rediscovered during an interrupted scan.
            value = dict(self.state)
            resume = dict(self.previous.get('resume', {}))
            for pin_id, row in self.previous.get('pins', {}).items():
                if pin_id not in resume:
                    resume[pin_id] = row
            for pin_id, row in self.state['pins'].items():
                files = {f['index']: f for f in resume.get(pin_id, {}).get('files', [])}
                files.update({f['index']: f for f in row.get('files', [])})
                resume[pin_id] = {**row, 'files': list(files.values())}
            value['resume'] = resume
            atomic_json(self.folder / 'manifest.json', value)
            self.last_save = time.monotonic()

    def finish(self):
        self.save(True)
        summary = report(self.state)
        target = self.folder / 'report.txt'
        temp = target.with_suffix('.tmp')
        temp.write_text(summary)
        temp.replace(target)
        print('\n' + summary, flush=True)
        print(f'Report: {target.resolve()}')
        self.lock.close()


async def transfer(discovery, store, args, stop):
    queue = asyncio.Queue(maxsize=args.workers * 2)
    pace = Pace(args.request_interval, stop)
    errors = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=15),
                                 limits=httpx.Limits(max_connections=args.workers),
                                 headers={'User-Agent': 'PinterestDownloader/0.1'}) as client:
        downloader = Downloader(client, store.folder, pace)

        async def worker():
            while True:
                pin = await queue.get()
                try:
                    if pin is None:
                        return
                    row = store.state['pins'][pin.id]
                    if stop.is_set():
                        row['reason'] = pace.blocked or discovery.network_block or 'Run stopped before download'
                        continue
                    try:
                        pin = await discovery.resolve(pin)
                        if not pin.assets:
                            row.update(status='skipped' if pin.reason == 'video-only pin' else 'failed',
                                       reason=pin.reason or 'No still images found')
                            continue
                        previous = {**store.previous.get('pins', {}), **store.previous.get('resume', {})}.get(pin.id, {})
                        old_files = {f['index']: f for f in previous.get('files', [])}
                        failures = []
                        reused = 0
                        for index, urls in enumerate(pin.assets, 1):
                            if stop.is_set():
                                failures.append('Stopped before remaining images could be downloaded')
                                break
                            try:
                                entry, existing = await downloader.asset(pin.id, index, urls, old_files.get(index))
                                row['files'].append(entry)
                                reused += existing
                                store.save()
                            except AccessBlocked:
                                raise
                            except (httpx.HTTPError, ValueError, OSError) as exc:
                                failures.append(f'image {index}: {exc}')
                        if not pin.resolved:
                            failures.append(pin.reason or 'Image enumeration unverified')
                        row['status'] = ('partial' if row['files'] else 'failed') if failures else ('existing' if reused == len(pin.assets) else 'complete')
                        row['reason'] = '; '.join(failures)
                    except AccessBlocked as exc:
                        stop.set()
                        pace.blocked = str(exc)
                        row.update(status='partial' if row['files'] else 'failed', reason=str(exc))
                        errors.append(str(exc))
                    except Exception as exc:
                        row.update(status='partial' if row['files'] else 'failed', reason=f'{type(exc).__name__}: {exc}')
                finally:
                    queue.task_done()
                    store.save()

        async def progress():
            while True:
                await asyncio.sleep(5)
                rows = store.state['pins'].values()
                done = sum(p['status'] in ('complete', 'existing') for p in rows)
                files = sum(len(p['files']) for p in rows)
                print(f'Progress: {len(store.state["pins"])} pins found | {done} complete | {files} images | {queue.qsize()} queued', flush=True)

        try:
            async with asyncio.TaskGroup() as group:
                workers = [group.create_task(worker()) for _ in range(args.workers)]
                ticker = group.create_task(progress())
                try:
                    async for pin in discovery.pins():
                        store.state['pins'][pin.id] = {'url': pin.url, 'status': 'failed', 'files': [], 'reason': 'Not finished'}
                        await queue.put(pin)
                        store.save()
                except Exception as exc:
                    errors.append(f'{type(exc).__name__}: {exc}')
                    if isinstance(exc, AccessBlocked):
                        stop.set()
                        pace.blocked = str(exc)
                for _ in workers:
                    await queue.put(None)
                await queue.join()
                ticker.cancel()
        finally:
            for row in store.state['pins'].values():
                if row['reason'] == 'Not finished' and row['files']:
                    row['status'] = 'partial'
            store.state['expected'] = discovery.board.expected
            store.state['notes'] = list(dict.fromkeys(discovery.board.notes + errors))
            count_matches = discovery.board.expected is None or discovery.board.expected == len(store.state['pins'])
            verified = discovery.board.verified and not errors and count_matches
            store.state['discovery'] = 'verified feed exhaustion' if verified else 'incomplete or unverified'
    return errors


async def run(args):
    store = None
    discovery = None
    stop = asyncio.Event()
    fatal = False
    interrupted = False
    try:
        async with async_playwright() as playwright:
            browser_type = getattr(playwright, args.browser)
            launch_options = {'headless': not args.headed}
            if args.browser == 'chromium':
                launch_options['channel'] = args.browser_channel or 'chromium'
            browser = await browser_type.launch(**launch_options)
            context = await browser.new_context(viewport={'width': 1280, 'height': 900})
            # Browser previews duplicate HTTP downloads; retain scripts/XHR needed for rendering.
            async def route(request):
                if request.request.resource_type in ('image', 'media', 'font'):
                    await request.abort()
                else:
                    await request.continue_()
            await context.route('**/*', route)
            discovery = Discovery(context, args.url, args.command, stop)
            try:
                await discovery.open()
                name = 'pin-' + discovery.board.name if args.command == 'pin' else discovery.board.name
                store = Store(args.dest, name, args.url)
                print(f'Output: {store.folder.resolve()} | {args.workers} download workers', flush=True)
                await transfer(discovery, store, args, stop)
            finally:
                await discovery.close()
                await context.close()
                await browser.close()
    except asyncio.CancelledError:
        interrupted = True
        if store:
            store.state['discovery'] = 'interrupted'
            store.state['notes'].append('Interrupted; rerun the same command to resume.')
    except Exception as exc:
        fatal = True
        message = f'{type(exc).__name__}: {exc}'
        print(message, file=sys.stderr)
        if store is None:
            # Access/setup failures still leave a human-readable report when possible.
            name = args.url.rstrip('/').split('/')[-1]
            if args.command == 'pin':
                name = 'pin-' + name
            try:
                store = Store(args.dest, name, args.url)
            except (OSError, ValueError) as storage_error:
                print(f'Could not write report: {storage_error}', file=sys.stderr)
        if store:
            store.state['discovery'] = 'failed before completion'
            store.state['notes'].append(message)
    finally:
        if store:
            store.finish()
    if fatal:
        return 2
    if interrupted or not store:
        return 1
    bad = any(p['status'] not in ('complete', 'existing', 'skipped') for p in store.state['pins'].values())
    return 1 if bad or store.state['discovery'] != 'verified feed exhaustion' else 0


def main():
    parser = argparse.ArgumentParser(description='Download still images from public Pinterest boards or pins.')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('board', 'pin'):
        child = sub.add_parser(command)
        child.add_argument('url')
        child.add_argument('--dest', type=Path, required=True)
        child.add_argument('--workers', type=int, default=4, help='Parallel downloads, 1–8 (default: 4)')
        child.add_argument('--request-interval', type=float, default=0.5, help='Minimum seconds between image request starts; at least 0.5')
        child.add_argument('--headed', action='store_true', help='Show the browser window')
        child.add_argument('--browser', choices=['firefox', 'chromium'], default='firefox',
                           help='Discovery browser (default: firefox)')
        child.add_argument('--browser-channel', choices=['chrome', 'chromium'], default=None,
                           help='Optional channel for --browser chromium')
    args = parser.parse_args()
    try:
        args.url = canonical_url(args.url, args.command)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.workers <= 8:
        parser.error('--workers must be between 1 and 8')
    if not 0.5 <= args.request_interval <= 3600:
        parser.error('--request-interval must be between 0.5 and 3600 seconds')
    if args.browser == 'firefox' and args.browser_channel:
        parser.error('--browser-channel requires --browser chromium')
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 1
    sys.exit(code)


if __name__ == '__main__':
    main()
