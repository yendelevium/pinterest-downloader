from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import BrowserContext, Page

from .core import Pin, parse_pin, srcset_candidates, valid_pin_id
from .download import AccessBlocked, Pace


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def canonical_url(value: str, mode: str) -> str:
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or p.hostname not in ('pinterest.com', 'www.pinterest.com', 'in.pinterest.com') or p.username or p.port:
        raise ValueError('Use a full pinterest.com board or pin URL.')
    parts = [x for x in p.path.split('/') if x]
    if mode == 'pin':
        if len(parts) != 2 or parts[0] != 'pin' or not valid_pin_id(parts[1]):
            raise ValueError('Expected https://www.pinterest.com/pin/PIN_ID/')
    elif len(parts) not in (2, 3) or parts[0] in ('pin', 'search', 'ideas', 'settings', 'login'):
        raise ValueError('Expected https://www.pinterest.com/USERNAME/BOARD/ or /USERNAME/BOARD/SECTION/')
    else:
        parts[0] = parts[0].lower()
    return 'https://www.pinterest.com/' + '/'.join(parts) + '/'


@dataclass
class Board:
    url: str
    name: str = ''
    id: str = ''
    expected: int | None = None
    section_count: int | None = None
    sections: dict[str, str] = field(default_factory=dict)
    verified: bool = False
    notes: list[str] = field(default_factory=list)
    section_id: str = ''
    section_name: str = ''


DOM_PINS = """() => {
  const result = [];
  // Pinterest's pin containers avoid capturing generic links/recommendations.
  for (const card of document.querySelectorAll('[data-test-id="pin"]')) {
    if (card.closest('[data-test-id*="recommend"], [data-test-id*="related"]')) continue;
    const boundary = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
      .find(e => /^(more ideas|find some ideas|more like this|related pins|you might also like)$/i.test(e.textContent.trim()));
    if (boundary && (boundary.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
    const link = card.querySelector('a[href*="/pin/"]');
    const match = link && link.getAttribute('href').match(/\\/pin\\/((?:\\d+|e1_[A-Za-z0-9_-]+))(?:\\/|$|\\?)/);
    if (!match) continue;
    const imgs = [...link.querySelectorAll('img')].map(i => ({src:i.src, srcset:i.srcset}));
    result.push({id:match[1], imgs});
  }
  return result;
}"""


class Discovery:
    def __init__(self, context: BrowserContext, url: str, mode: str, stop: asyncio.Event, signed_in=False):
        self.context, self.url, self.mode, self.stop = context, url, mode, stop
        self.signed_in = signed_in
        parts = urlsplit(url).path.strip('/').split('/')
        self.section_slug = parts[2] if mode == 'board' and len(parts) == 3 else None
        self.parent_url = 'https://www.pinterest.com/' + '/'.join(parts[:2]) + '/'
        self.board = Board(url, parts[1] if mode == 'board' else parts[-1])
        self.pace = Pace(5, stop)
        self.page: Page | None = None
        self.detail: Page | None = None
        self.detail_lock = asyncio.Lock()
        self.detail_gate = asyncio.Semaphore(1)
        self.network_block = ''
        self.tasks: set[asyncio.Task] = set()
        self.responses: list[tuple[str, dict, dict]] = []
        self.seen: set[str] = set()
        self.authoritative: set[str] = set()
        self.initial: list[dict] = []

    async def observe(self, response):
        try:
            host = urlsplit(response.url).hostname or ''
            if not (host == 'pinterest.com' or host.endswith('.pinterest.com')):
                return
            if response.status in (403, 429) and (response.request.resource_type == 'document' or '/resource/' in response.url):
                self.network_block = f'Pinterest returned HTTP {response.status}; stopped browser requests. Retry later.'
                self.stop.set()
                return
            if '/resource/' not in response.url or response.status != 200:
                return
            resource = response.url.split('/resource/', 1)[1].split('/')[0]
            if resource not in ('BoardResource', 'BoardFeedResource', 'BoardSectionsResource', 'BoardSectionResource', 'BoardSectionPinsResource'):
                return
            query = parse_qs(urlsplit(response.url).query)
            raw = query.get('data', ['{}'])[0]
            if response.request.method == 'POST':
                raw = parse_qs(response.request.post_data or '').get('data', [raw])[0]
            options = json.loads(raw).get('options', {})
            self.responses.append((resource, options, await response.json()))
        except (ValueError, KeyError, TypeError):
            pass
        except Exception as exc:
            self.board.notes.append(f'Could not inspect a browser response: {type(exc).__name__}')

    def listener(self, response):
        task = asyncio.create_task(self.observe(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def check_access(self, page):
        if self.network_block:
            raise AccessBlocked(self.network_block)
        path = urlsplit(page.url).path
        if path.startswith(('/login', '/auth', '/challenge')):
            raise AccessBlocked('Pinterest requires login or a challenge. Run `pinterest-downloader login` to sign in or refresh your session.')
        text = (await page.locator('body').inner_text())[:12000].lower()
        if any(s in text for s in ('verify you are human', 'verify you’re human', 'access denied', 'too many requests')):
            raise AccessBlocked('Pinterest access challenge detected; stopped.')

    async def navigate(self, page, url):
        if self.stop.is_set():
            raise AccessBlocked(self.network_block or 'Stopped after a download access block.')
        await self.pace.wait()
        response = await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        if response and response.status >= 400:
            raise AccessBlocked(f'Pinterest page returned HTTP {response.status}; stopped.')
        await page.wait_for_timeout(2000)
        await self.check_access(page)

    async def scripts(self, page):
        texts = await page.locator('script[type="application/json"], script#__PWS_DATA__').all_text_contents()
        result = []
        for text in texts:
            try:
                result.append(json.loads(text))
            except ValueError:
                pass
        return result

    def metadata(self, payload):
        for node in walk(payload):
            url = node.get('url', '')
            same_url = isinstance(url, str) and urlsplit(url).path.rstrip('/').casefold() == urlsplit(self.parent_url).path.rstrip('/').casefold()
            if same_url and node.get('type') != 'board_section' and ('pin_count' in node or node.get('type') == 'board'):
                self.board.id = str(node.get('id', self.board.id))
                self.board.name = node.get('name') or self.board.name
                count = node.get('pin_count')
                if not self.section_slug and isinstance(count, int) and not isinstance(count, bool):
                    self.board.expected = count
                section_count = node.get('section_count')
                if isinstance(section_count, int):
                    self.board.section_count = section_count

        if self.section_slug:
            matches = {}
            for node in walk(payload):
                if node.get('type') != 'board_section' or not node.get('id'):
                    continue
                section_url = self.section_url(node)
                if not section_url or urlsplit(section_url).path.rstrip('/').casefold() != urlsplit(self.url).path.rstrip('/').casefold():
                    continue
                matches[str(node['id'])] = node
            if len(matches) > 1:
                raise ValueError('Multiple sections match this URL; cannot determine the requested section safely.')
            if matches:
                section_id, node = next(iter(matches.items()))
                self.board.section_id = section_id
                self.board.section_name = node.get('title') or node.get('name') or self.section_slug
                count = node.get('pin_count')
                if isinstance(count, int) and not isinstance(count, bool):
                    self.board.expected = count

    def section_url(self, node):
        owner = node.get('board')
        if isinstance(owner, dict) and owner.get('id') and self.board.id and str(owner['id']) != self.board.id:
            return None
        url = node.get('url')
        if not url and node.get('slug'):
            url = str(node['slug']).strip('/') + '/'
        if not isinstance(url, str):
            return None
        try:
            full = canonical_url(urljoin(self.parent_url, url), 'board')
        except ValueError:
            return None
        path = urlsplit(full).path
        if len(path.strip('/').split('/')) != 3 or not path.casefold().startswith(urlsplit(self.parent_url).path.casefold()):
            return None
        return full

    def add_sections(self, payload):
        for node in walk(payload):
            if node.get('type') != 'board_section' or not node.get('id'):
                continue
            full = self.section_url(node)
            if full:
                self.board.sections[str(node['id'])] = full

    async def open(self):
        self.page = await self.context.new_page()
        self.page.on('response', self.listener)
        await self.navigate(self.page, self.url)
        self.initial = await self.scripts(self.page)
        self.metadata(self.initial)
        if self.tasks:
            await asyncio.gather(*list(self.tasks))
        for resource, options, payload in self.responses:
            if resource in ('BoardResource', 'BoardSectionResource'):
                self.metadata(payload)
        if self.section_slug and not self.board.section_id:
            raise ValueError('Could not identify the requested section. Check the URL and login; the parent board will not be downloaded instead.')
        if not self.board.name:
            self.board.name = 'board'

    async def pins(self):
        if self.mode == 'pin':
            yield await self.resolve(Pin(urlsplit(self.url).path.strip('/').split('/')[-1]), self.page)
            self.board.verified = True
            self.board.expected = 1
            return
        if not self.board.id:
            self.board.notes.append('Board identity unavailable; DOM results cannot prove board membership or completeness.')
        if self.board.section_id:
            scan = await self.scan_to_queue(self.board.section_id)
            async for pin in scan:
                yield pin
            membership_verified = self.seen <= self.authoritative
            self.board.verified = self._scan_verified and membership_verified and bool(self.board.id)
            if not membership_verified:
                self.board.notes.append('Some pins were found only in the DOM; section membership is unverified for those pins.')
            return
        verified = await self.scan_to_queue(None)
        async for pin in verified:
            yield pin
        main_verified = self._scan_verified
        scanned = set()
        sections_verified = True
        while set(self.board.sections) - scanned:
            section = next(iter(set(self.board.sections) - scanned))
            scanned.add(section)
            await self.navigate(self.page, self.board.sections[section])
            self.initial = await self.scripts(self.page)
            scan = await self.scan_to_queue(section)
            async for pin in scan:
                yield pin
            sections_verified &= self._scan_verified
        sections_known = self.board.section_count is not None and len(scanned) == self.board.section_count
        membership_verified = self.seen <= self.authoritative
        self.board.verified = main_verified and sections_verified and sections_known and membership_verified
        if not membership_verified:
            self.board.notes.append('Some pins were found only in the DOM; board membership is unverified for those pins.')
        if not sections_known:
            self.board.notes.append('Could not verify enumeration of all board sections.')

    async def scan_to_queue(self, section):
        # Return a generator so a caller can read _scan_verified after exhaustion.
        async def generate():
            exhausted = False
            idle = 0
            feed_resource = 'BoardSectionPinsResource' if section else 'BoardFeedResource'
            self._scan_verified = False
            initial_by_id = {}
            # Current public pages embed the first feed page and continuation cursor.
            # Read only the resource belonging to this board/section, not every pin
            # object in Redux (which may include suggestions and duplicates).
            for root in self.initial:
                redux = root.get('initialReduxState', {}) if isinstance(root, dict) else {}
                for resource, entries in redux.get('resources', {}).items():
                    if resource not in (feed_resource, 'BoardSectionsResource'):
                        continue
                    for encoded, cached in entries.items():
                        try:
                            options = dict(json.loads(encoded))
                        except (TypeError, ValueError):
                            continue
                        self.responses.append((resource, options, {'resource_response': {
                            'data': cached.get('data', []), 'bookmark': cached.get('nextBookmark')}}))
            for node in walk(self.initial):
                if node.get('type') == 'pin':
                    pin = parse_pin(node)
                    if pin:
                        initial_by_id[pin.id] = pin
                if node.get('type') == 'board_section':
                    owner = node.get('board', {})
                    if isinstance(owner, dict) and str(owner.get('id')) == self.board.id:
                        self.add_sections(node)
            for _ in range(10000):
                if self.stop.is_set():
                    raise AccessBlocked(self.network_block or 'Stopped after a download access block.')
                await self.check_access(self.page)
                if self.tasks:
                    await asyncio.gather(*list(self.tasks))
                found = []
                responses, self.responses = self.responses, []
                for resource, options, payload in responses:
                    if resource in ('BoardResource', 'BoardSectionResource'):
                        self.metadata(payload)
                    board_match = bool(self.board.id) and str(options.get('board_id')) == self.board.id
                    section_match = section is not None and str(options.get('section_id')) == section
                    if resource == 'BoardSectionsResource' and board_match:
                        self.add_sections(payload.get('resource_response', {}).get('data'))
                    feed = (resource == 'BoardFeedResource' and board_match and section is None) or (resource == 'BoardSectionPinsResource' and section_match)
                    if not feed:
                        continue
                    response = payload.get('resource_response', {})
                    data = response.get('data', [])
                    if isinstance(data, list):
                        for node in data:
                            if isinstance(node, dict) and node.get('type') == 'pin':
                                pin = parse_pin(node)
                                if pin:
                                    found.append(pin)
                                    self.authoritative.add(pin.id)
                    bookmarks = response.get('bookmark') or payload.get('resource', {}).get('options', {}).get('bookmarks')
                    if bookmarks == '-end-' or bookmarks == ['-end-']:
                        exhausted = True
                # DOM fallback is limited to board grids; never use it beyond feed exhaustion.
                if not exhausted:
                    for row in await self.page.evaluate(DOM_PINS):
                        pin = initial_by_id.get(row['id']) or Pin(row['id'])
                        if not pin.assets:
                            pin.assets = [c for i in row['imgs'] if (c := srcset_candidates(i['src'], i['srcset']))]
                        found.append(pin)
                before = len(self.seen)
                for pin in found:
                    if pin.id not in self.seen:
                        self.seen.add(pin.id)
                        yield pin
                if await self.page.locator('input[type="password"]:visible').count():
                    explanation = 'The saved session may have expired or been revoked.' if self.signed_in else 'Public access cannot reach the remaining pins.'
                    self.board.notes.append(f'Pinterest displayed a sign-in wall. {explanation} Run `pinterest-downloader login` and retry to resume.')
                    return
                # Section links are filtered to this board's URL, not arbitrary navigation.
                links = await self.page.locator('a[href]').evaluate_all('(els) => els.map(e => e.href)')
                for link in links:
                    p = urlsplit(link)
                    if p.hostname in ('www.pinterest.com', 'in.pinterest.com') and p.path.casefold().startswith(urlsplit(self.parent_url).path.casefold()):
                        suffix = p.path[len(urlsplit(self.parent_url).path):].strip('/')
                        if suffix and '/' not in suffix:
                            # Need a real section ID from data before requesting this feed.
                            for node in walk(self.initial):
                                section_url = self.section_url(node) if node.get('type') == 'board_section' else None
                                if section_url and urlsplit(section_url).path.rstrip('/').casefold() == p.path.rstrip('/').casefold():
                                    self.add_sections(node)
                if exhausted:
                    self._scan_verified = True
                    return
                idle = idle + 1 if len(self.seen) == before else 0
                if idle >= 5:
                    self.board.notes.append(f"Scan stalled without explicit exhaustion: {self.page.url}")
                    return
                await self.pace.wait()
                advanced = await self.page.evaluate('''() => {
                    const before = scrollY;
                    window.scrollBy(0, Math.max(500, window.innerHeight * 0.85));
                    return scrollY > before;
                }''')
                if advanced:
                    idle = 0
                await asyncio.sleep(random.uniform(2, 3))
            self.board.notes.append('Scroll safety limit reached.')
        return generate()

    async def resolve(self, pin: Pin, page=None) -> Pin:
        if pin.resolved:
            return pin
        async with self.detail_lock:
            if page is None:
                if self.detail is None:
                    self.detail = await self.context.new_page()
                    self.detail.on('response', self.listener)
                page = self.detail
                await self.navigate(page, pin.url)
            scripts = await self.scripts(page)
            matches = [p for node in walk(scripts) if str(node.get('id')) == pin.id
                       and (p := parse_pin(node)) and p.resolved]
            if matches:
                return max(matches, key=lambda p: len(p.assets))
            # Main image can still be useful, but mark it incomplete if structured data is absent.
            if not pin.assets:
                for row in await page.evaluate(DOM_PINS):
                    if row['id'] == pin.id:
                        pin.assets += [c for i in row['imgs'] if (c := srcset_candidates(i['src'], i['srcset']))]
            pin.reason = 'Could not verify all still images from pin metadata'
            return pin

    async def close(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
