# Myuu Multiusage Bot

Telegram media bot for anime/video workflows. It supports direct video tools,
Nyaa/magnet handling, Seedr pipelines, CloudConvert, FreeConvert, hardsub,
resize/convert/compress, stream extraction, thumbnails, screenshots, archives,
and Colab-first deployment.

## Main Workflows

### Seedr + FreeConvert Hardsub

This is the preferred fast path for hardsub jobs.

- Seedr fetches the torrent and exposes the video as a CDN URL.
- **Parallel subtitle fast path:** Erai-raws CRC lookup and direct ffmpeg
  extraction from the CDN URL run simultaneously. Whichever succeeds first
  wins — no serial wait between them.
- Erai-raws lookup is CRC-gated: only triggered when the original Seedr
  filename contains `[XXXXXXXX]` (Erai-raws releases). Non-CRC files skip
  it entirely.
- If both parallel paths fail, ffprobe runs directly on the CDN URL via HTTP
  range requests (reads only container headers, typically < 2 MB). The
  50 MB partial download is a last resort used only when direct URL probing
  fails.
- If a subtitle is found, FreeConvert receives the Seedr URL plus the subtitle
  using `subtitle_add=upload` and `subtitle_mode=hard`.
- Seedr cleanup is deferred until the async FC webhook/poller finishes.
- FreeConvert keys are rotated: if one key returns `402 Insufficient CPU
  minutes`, the bot tries the next configured key before failing.
- Subtitle files are cached by CRC or torrent hash, so repeated hardsub jobs
  skip all extraction steps and reuse the subtitle that already worked.

### Seedr + CloudConvert Hardsub

CloudConvert is available as a backup/alternate engine and uses the same
optimised subtitle pipeline as the FC path above.

- **Parallel subtitle fast path:** same Erai CRC + direct CDN race as FC.
- **Direct URL probe:** ffprobe runs on the Seedr CDN URL directly; the 50 MB
  download is a fallback only.
- Erai-raws lookup is CRC-only. The bot uses the original Seedr filename, not
  the cleaned filename, to find `[XXXXXXXX]` CRC tags.
- Uploaded external subtitles are burned through `command/ffmpeg`, with both
  `import-video` and `import-sub` wired into the command task.
- Smart Job Center dedupe prevents accidentally submitting the same Seedr
  hardsub magnet twice in quick succession.

### Resize / Compress — Engine Fallback

When the primary cloud engine (CloudConvert) fails during a resize or compress
job, the bot now correctly falls through the full cascade:

1. CloudConvert (primary)
2. FreeConvert (if `FC_API_KEY` is configured)
3. Local FFmpeg (always available as final fallback)

Previously a CC failure skipped FreeConvert and jumped straight to local FFmpeg.

### Regular Video Tools

When a user sends a video, the bot shows action buttons for:

- Info, thumbnail, screenshots, no-audio copy.
- Trim, split, sample, metadata, rename.
- Mux subtitle, burn subtitle, merge audio/video, merge videos.
- Audio tools, convert, resize, compress.
- Extract/map/remove streams.

The rename workflow uses a dedicated Pyrogram handler group that runs before
the URL/text routing handlers, so rename prompts are not accidentally swallowed
by other message interceptors.

### Smart Job Center

- `/jobs` shows recent Smart Job Center entries plus CloudConvert and
  FreeConvert jobs.
- Duplicate Seedr hardsub actions are blocked for the same magnet while a
  matching job is active.
- Active Seedr hardsub jobs expose Cancel, Retry, Force FC, and Force CC
  buttons.
- Telegram uploads are queued per chat so final videos upload one at a time,
  reducing FloodWait at the end of FC/CC jobs.

### Seedr Dashboard and Batch Mode

- `/seedr` shows every configured Seedr account, storage, folder count, active
  torrents, and guarded cleanup buttons. It is owner/admin only.
- `/batch_hardsub` accepts several magnet links and submits Auto Seedr Hardsub
  jobs one by one.

## Colab Setup

Open `colab_launcher.py` in Google Colab, fill the values at the top, then run
the notebook.

Required:

```env
API_ID=12345678
API_HASH=your_api_hash
BOT_TOKEN=123456:bot_token
OWNER_ID=123456789
```

Recommended for Seedr + hardsub:

```env
SEEDR_USERNAME=your_seedr_email
SEEDR_PASSWORD=your_seedr_password
FC_API_KEY=fc_key_1,fc_key_2,fc_key_3,fc_key_4,fc_key_5
CC_API_KEY=cc_key_1,cc_key_2,cc_key_3,cc_key_4,cc_key_5
```

You can also use numbered FreeConvert secrets:

```env
FC_API_KEY=fc_key_1
FC_API_KEY_2=fc_key_2
FC_API_KEY_3=fc_key_3
FC_API_KEY_4=fc_key_4
FC_API_KEY_5=fc_key_5
```

CloudConvert supports comma-separated keys in `CC_API_KEY`. FreeConvert now does
the same and also reads `FC_API_KEY_2` through `FC_API_KEY_9`.

Optional:

```env
SEEDR_PROXY=http://user:pass@host:port
ERAI_COOKIE=browser_cookie_if_cloudflare_blocks_colab
ADMINS=111111 222222
LOG_CHANNEL=-1001234567890
GDRIVE_SA_JSON=/path/to/service-account.json
```

## Quality Buttons

The quality buttons control encoder speed and output size:

- Fast: fastest processing, larger files, lower compression.
- Balanced: default profile, good speed/size/quality tradeoff.
- Small file: slower processing, smaller files.
- Best: highest quality, slower and usually larger than Balanced.

These settings feed both FreeConvert and CloudConvert where supported. For FC,
they map to documented video options such as CRF and H.264 encoding speed.

## Useful Commands

```text
/start       Open the bot menu
/help        Show feature help
/settings    Engine, quality, progress panel, and upload preferences
/ccstatus    CloudConvert/FreeConvert job status
/jobs        Smart Job Center and cloud job overview
/debugjob    Job internals for debugging
/runtime     Runtime and git revision info
/seedr       Seedr account dashboard
/batch_hardsub  Queue several magnet hardsub jobs
```

## Development

Install dependencies:

```bash
pip install -r requirements.txt
```

Run syntax checks and tests:

```bash
python -m compileall core services plugins tests main.py colab_launcher.py
python -m unittest discover -s tests -v
```

## Repository Notes

- Colab is the primary target.
- Koyeb/Fly/Docker files are kept because the code still supports those deploys.
- Duplicate legacy docs and cache artifacts are intentionally removed.
