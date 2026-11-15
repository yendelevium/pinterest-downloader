# Pinterest Downloader

A command-line tool for downloading images from Pinterest boards, board sections, or individual pins (video pins not supported yet, will be skipped). It downloads in parallel, prefers original images with lower-resolution fallbacks, resumes previous downloads, and reports missing or failed pins. Why this downloader? because I like saving everything locally as I'm paranoid I'll lose all my pins someday if pinterest dies (already hating the pin to profile default smh)...

Done via web-scraping + playwright for js rendered content and fr infinite scroll circumvension.

## Install

Requires Linux, Python 3.12+, and [uv](https://docs.astral.sh/uv/).

From the project folder:

```bash
uv sync
uv run playwright install firefox
```

## Run

### Sign in (optional)

```bash
uv run pinterest-downloader login
```

Sign in in the Firefox window. The window closes when the session is saved, and future downloads use it automatically. Run `login` again if the session expires.

### Download a board or pin

```bash
uv run pinterest-downloader board "https://www.pinterest.com/USERNAME/BOARD/" --dest ./downloads
uv run pinterest-downloader board "https://www.pinterest.com/USERNAME/BOARD/SECTION/" --dest ./downloads
uv run pinterest-downloader pin "https://www.pinterest.com/pin/PIN_ID/" --dest ./downloads
```

Full `pinterest.com` and `in.pinterest.com` URLs are supported. Quote URLs and paths containing spaces.

Collaborative boards use the same command and your saved login. A URL ending in `/BOARD/SECTION/` downloads only that section; the parent `/BOARD/` URL downloads the whole board, including its sections. For example:

```bash
uv run pinterest-downloader board "https://in.pinterest.com/EnigmaticStardust/animation-reference/yendelevium/" --dest ./downloads
```

Output goes into `<dest>/<board-name>/`, `<dest>/<board-name>/<section-name>/` for a selected section, or `<dest>/pin-<id>/`, alongside `report.txt` and `manifest.json`. Section reports use the section's pin count. The report compares discovered pins with Pinterest's reported count and lists failures and resolution fallbacks. Rerun the same command to resume; verified existing images are reused.

### Download options

These options work with both `board` and `pin`:

| Option | Description |
| --- | --- |
| `--dest PATH` | Required parent folder for downloads. |
| `--workers N` | Parallel download workers: 1–8; default `4`. |
| `--request-interval SECONDS` | Minimum time between image request starts: 0.5–3600 seconds; default `0.5`. |
| `--headed` | Show the browser window while discovering pins. |
| `--browser firefox` | Use Firefox (default). Also accepts `chromium`. |
| `--browser-channel chrome` | Use installed Google Chrome; requires `--browser chromium`. Also accepts `chromium`. |
| `--no-session` | Use public access without the saved login. |
| `--session-file PATH` | Use a specific saved session; cannot be combined with `--no-session`. |
| `--help` | Show command help. |

Slower downloads with a visible browser:

```bash
uv run pinterest-downloader board "BOARD_URL" --dest ./downloads --workers 2 --request-interval 2 --headed
```

Use public access:

```bash
uv run pinterest-downloader board "BOARD_URL" --dest ./downloads --no-session
```

Use Chromium instead of Firefox:

```bash
uv run playwright install chromium --no-shell
uv run pinterest-downloader board "BOARD_URL" --dest ./downloads --browser chromium
```

### Manage saved sessions

The default session is stored at `~/.config/pinterest-downloader/session.json` (under `XDG_CONFIG_HOME` if set). Keep this file private. Login requires a desktop display.

```bash
# Remove the default local session
uv run pinterest-downloader logout

# Save and use a separate session
uv run pinterest-downloader login --session-file ~/.config/pinterest-downloader/other.session.json
uv run pinterest-downloader board "BOARD_URL" --dest ./downloads --session-file ~/.config/pinterest-downloader/other.session.json

# Remove that separate session
uv run pinterest-downloader logout --session-file ~/.config/pinterest-downloader/other.session.json
```

`logout` removes the local session file; it does not sign out your regular browser.
