"""Local browser session storage. Never send Pinterest cookies to the image CDN."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error, async_playwright


def default_session_file() -> Path:
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return config / 'pinterest-downloader' / 'session.json'


def pinterest_host(host: str) -> bool:
    host = host.lstrip('.').lower()
    return host == 'pinterest.com' or host.endswith('.pinterest.com')


def pinterest_state(state: dict) -> dict:
    return {
        'cookies': [c for c in state.get('cookies', []) if pinterest_host(c.get('domain', ''))],
        'origins': [o for o in state.get('origins', [])
                    if pinterest_host(urlsplit(o.get('origin', '')).hostname or '')],
    }


def authenticated(state: dict) -> bool:
    now = time.time()
    cookies = {c['name']: c for c in state.get('cookies', [])
               if pinterest_host(c.get('domain', ''))
               and (c.get('expires', -1) == -1 or c.get('expires', 0) > now)}
    return cookies.get('_auth', {}).get('value') == '1' and bool(cookies.get('_pinterest_sess', {}).get('value'))


def save_session(path: Path, state: dict):
    state = pinterest_state(state)
    if not authenticated(state):
        raise ValueError('No signed-in Pinterest session detected; session file was not changed.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('Refusing to write a session file through a symlink.')
    filename = None
    try:
        # mkstemp creates the file with mode 0600 before any cookie is written.
        fd, filename = tempfile.mkstemp(prefix='.pinterest-session-', dir=path.parent)
        with os.fdopen(fd, 'w') as output:
            json.dump(state, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(filename, path)
    finally:
        if filename:
            Path(filename).unlink(missing_ok=True)


def load_session(path: Path, *, required=False) -> dict | None:
    if path.is_symlink():
        raise ValueError('Session file must not be a symlink.')
    if not path.exists():
        if required:
            raise ValueError(f'No session at {path}. Run the login command first.')
        return None
    if path.stat().st_mode & 0o077:
        raise ValueError(f'Session file permissions are too broad. Run: chmod 600 "{path}"')
    try:
        state = pinterest_state(json.loads(path.read_text()))
        valid = authenticated(state)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('Invalid session file. Run the login command again.') from None
    if not valid:
        raise ValueError('Saved Pinterest session is expired or signed out. Run the login command again, or use --no-session.')
    return state


def logout(path: Path):
    if path.is_symlink():
        raise ValueError('Session file must not be a symlink.')
    if path.exists():
        path.unlink()
        print(f'Removed local session: {path}')
    else:
        print('No saved session to remove.')
    print('This removes the local copy; it does not sign out other browsers or revoke Pinterest sessions.')


async def login(path: Path, timeout: float = 600) -> int:
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=False)
        context = None
        try:
            context = await browser.new_context()
            page = await context.new_page()
            response = await page.goto('https://www.pinterest.com/login/', wait_until='domcontentloaded', timeout=60000)
            if response and response.status >= 400:
                raise ValueError(f'Pinterest login returned HTTP {response.status}; stopped without retrying.')
            print('Sign in directly in Firefox. Complete any verification there. No password is entered in this terminal.', flush=True)
            print('The browser will close automatically when the Pinterest session is saved. Close it or press Ctrl+C to cancel.', flush=True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not browser.is_connected() or not context.pages:
                    print('Login cancelled; existing saved session was not changed.')
                    return 1
                cookies = await context.cookies()
                if authenticated({'cookies': cookies}):
                    state = await context.storage_state(indexed_db=True)
                    save_session(path, state)
                    print(f'Pinterest session saved to {path}. Future downloads will use it automatically.')
                    return 0
                await asyncio.sleep(1)
            print('Login timed out after 10 minutes; existing saved session was not changed.')
            return 1
        except Error:
            if not browser.is_connected() or (context is not None and not context.pages):
                print('Login cancelled; existing saved session was not changed.')
                return 1
            raise
        finally:
            await browser.close()
