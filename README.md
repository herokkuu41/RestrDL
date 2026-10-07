<h1 align="center">Restricted Content Downloader Telegram Bot</h1>

<p align="center">
  <a href="https://github.com/bisnuray/RestrictedContentDL/stargazers"><img src="https://img.shields.io/github/stars/bisnuray/RestrictedContentDL?color=blue&style=flat" alt="GitHub Repo stars"></a>
  <a href="https://github.com/bisnuray/RestrictedContentDL/issues"><img src="https://img.shields.io/github/issues/bisnuray/RestrictedContentDL" alt="GitHub issues"></a>
  <a href="https://github.com/bisnuray/RestrictedContentDL/pulls"><img src="https://img.shields.io/github/issues-pr/bisnuray/RestrictedContentDL" alt="GitHub pull requests"></a>
  <a href="https://github.com/bisnuray/RestrictedContentDL/graphs/contributors"><img src="https://img.shields.io/github/contributors/bisnuray/RestrictedContentDL?style=flat" alt="GitHub contributors"></a>
  <a href="https://github.com/bisnuray/RestrictedContentDL/network/members"><img src="https://img.shields.io/github/forks/bisnuray/RestrictedContentDL?style=flat" alt="GitHub forks"></a>
</p>

<p align="center">
  <em>Restricted Content Downloader: An advanced Telegram bot script to download restricted content such as photos, videos, audio files, or documents from Telegram private chats or channels. This bot can also copy text messages from Telegram posts.</em>
</p>
<hr>

## Features

- 📥 Download media (photos, videos, audio, documents).
- ✅ Supports downloading from both single media posts and media groups.
- 🔄 Progress bar showing real-time downloading progress.
- ⚡ Parallel streaming downloads and uploads: upload starts as chunks arrive, without saving the full file locally.
- ✍️ Copy text messages or captions from Telegram posts.
- 📌 Batch mode asks whether to pin the first post of the batch (tap a button or reply `yes` / `no`).
- 🗂️ With no destination channel set, everything (text, photos, videos, files) is delivered to the bot chat itself.
- 🧹 Error messages delete themselves automatically after 5 minutes.
- ☁️ **Native Python & Cloud-ready**: Runs directly on [Botkeep Cloud](https://botkeep.cloud) without requiring Docker!
- 🎬 **Bundled FFmpeg support**: Thumbnail generation works out-of-the-box on standard Python environments.

---

## Streaming transfers on a 2 GB server

Install the updated `requirements.txt` and restart the existing bot. The transfer engine
uses four download and four upload connections across the entire bot, with at most two
files active at once. Transfer scheduling is restored to the version before the
07 October 2026 06:11 IST commit: one request in flight per connection (4 download
RPCs / 4 upload RPCs globally), with chunks enqueued in source order. Downloads
and uploads still overlap. Small files retain ordered MD5 calculation.
Albums share these limits. Queued data, upload slices, and retry
payloads have a shared 64 MiB budget; this is a payload budget, not a cap on total process
RAM. TgCrypto is required and checked at startup.

Uploads begin while downloads are still running. Progress shows downloaded bytes and
bytes acknowledged by Telegram separately. Files retain captions, names, source video/
audio metadata, and available thumbnails; streaming does not generate new FFmpeg thumbnails.
The existing server-side copy/forward path remains the first choice when available.

| Variable | Default | Meaning |
|---|---:|---|
| `PARALLEL_DOWNLOAD_WORKERS` | 4 | Global download connection slots |
| `PARALLEL_UPLOAD_WORKERS` | 4 | Global upload connection slots |
| `DOWNLOAD_REQUESTS_PER_CONNECTION` | 1 | Fixed production RPCs per download connection; old overrides ignored |
| `UPLOAD_REQUESTS_PER_CONNECTION` | 1 | Fixed production RPCs per upload connection; old overrides ignored |
| `MAX_ACTIVE_TRANSFERS` | 2 | Active files, including album items |
| `TRANSFER_BUFFER_MIB` | 64 | Shared buffered payload budget |
| `DISK_RESERVE_MIB` | 256 | Minimum free disk after reserving a fallback file |
| `MAX_CONCURRENT_DOWNLOADS` | 3 | Outer post/album processing slots; does not multiply transfer connections |

Existing environment variables override defaults: update any old
`PARALLEL_DOWNLOAD_WORKERS=3` value to `4` to use the new defaults. CDN streaming
needs at least two download slots because it opens both an origin and a CDN session.
With only one or two download connections, the manager admits one active file so CDN
streaming cannot deadlock another producer holding the memory budget. With four download
connections, native CDN reserves two and leaves the other two available for regular files.
Native disk fallback is attempted only for unsupported file identifiers when space
can be reserved. Oversized files are rejected before downloading using the destination
bot's upload limit (normally 2000 MiB), even when the source user has Premium.
Failed/cancelled transfers release their buffers and connection slots. Albums are sent
only after every item is prepared; failure does not silently send an incomplete album.

### Measure real speed (optional)

Stop the production bot before using its session string for the benchmark. Run from the
repository directory with your normal credentials configured:

```sh
python -B scripts/benchmark_transfers.py --run --source https://t.me/channel/123 --target -1001234567890
```

This sends up to **four real test posts** to your chosen destination: native download/
upload, the previous four-connection relay, and pipelined streaming with two and four
connections per direction. The native
baseline is skipped when disk space is insufficient. JSON results include elapsed time,
throughput, and streaming buffer peak. Repeat with representative files before changing
the defaults; account limits, network conditions, and Telegram throttling can dominate
performance. Automated tests establish correctness and overlapping transfers, not a
guaranteed real-world speed increase.

The updated benchmark sends up to **four** posts with the default arguments: native
download/upload, the previous four-connection relay (one request per connection), then
the new pipeline with two and four connections. Add `--connections 4 6` to compare four
and six instead. Results include both decimal MB/s and binary MiB/s; run the same source
and destination several times to assess improvement. Startup and final sends are included.

### Server speed test and transfer diagnostics

Send `/speedtest` in the bot chat while transfers are idle. It measures HTTP latency and
four-connection download/upload bandwidth against [Cloudflare's test endpoints](https://github.com/cloudflare/speedtest).
It uses at most 48 MiB traffic and streams 64 KiB blocks without a disk file. A global
five-minute cooldown and one-test limit prevent repeated tests competing for bandwidth.
`/killall` cancels it; the overall deadline is 75 seconds. This samples the route to
Cloudflare, not Telegram, and is not an Ookla result.

Your target **7.5 MB/s equals 60 Mbps**, or approximately 7.15 MiB/s. A sample above
60 Mbps in each direction shows headroom to Cloudflare, but cannot establish Telegram
throughput. Telegram can limit non-Premium source downloads. A detected
short waits (up to 30 seconds) are handled by the pinned library on the exact refused
request, as in the earlier transfer version. Completed chunks, the upload file ID,
and acknowledged parts are retained. The whole-file restart loop is removed for
both single media and albums. Longer waits stop the batch instead of repeatedly
starting files or moving to the next file while throttled. `/killall` cancels an
in-progress wait. See [Telegram file transfer guidance](https://core.telegram.org/api/files)
and [Premium download limits](https://telegram.org/faq_premium).

`/logs` now includes transfer progress every 15 seconds, plus completion elapsed time,
download rate, acknowledged upload rate, relay rate, source DC, request/connection limits,
native CDN usage and the shared payload buffer peak. Direction rates are averages from
relay start until that direction finishes, including startup and backpressure; they are
not isolated download-only or upload-only benchmarks. Telegram progress edits occur at
most every five seconds, and a slow edit no longer blocks other data workers.

The first successful **source-order** post is pinned as soon as all earlier candidates
finish or fail; a later quick text post cannot take the pin from earlier media. Private
chat pins use the sender's own message ID namespace. Sent responses and album copies
use the required `topics` field for Pyrofork 2.3.69, so completed sends are counted correctly.

After deploying, restart with the new code and check the startup `Transfer settings`
log. Connection-count environment values override defaults, but old 2/4 requests-per-connection
values are ignored so existing deployments use the restored 1/1 scheduling automatically.
Keep four connections. A 64 MiB
payload budget stays well within a 2 GB server without requiring full RAM/CPU utilization.
Increase connection counts only when controlled measurements show a gain without waits.

### Run regression tests without modifying runtime files

Install `pytest` and `pytest-asyncio` in a test environment containing `requirements.txt`.
Run from a temporary working directory so logs/download fixtures stay outside the repo:

```sh
python -B -m pytest /absolute/path/to/RestrDL/tests --rootdir=/absolute/path/to/RestrDL -q -p no:cacheprovider
```

---

## Prerequisites & Telegram Credentials

Before deploying, make sure you have:

1. **Telegram Bot Token**: Get one from [@BotFather](https://t.me/BotFather) on Telegram.
2. **API ID & API Hash**: Get these by creating an application on [my.telegram.org](https://my.telegram.org).
3. **Pyrogram Session String (`SESSION_STRING`)**:
   - Open [@SmartUtilBot](https://t.me/SmartUtilBot) on Telegram.
   - Send `/pyro` command and follow the instructions to generate your string session.

---

## Deploy to Botkeep Cloud (Free 2GB RAM - No Docker Required)

[Botkeep](https://botkeep.cloud) provides a free 2GB RAM founder tier and runs Python applications directly without Docker.

### Step-by-Step Botkeep Deployment:

1. **Fork or Push** this repository to your GitHub account.
2. Sign in or register at **[botkeep.cloud](https://botkeep.cloud)**.
3. Click **New Workload** / **Deploy Service** and connect your GitHub repository.
4. Configure the deployment settings:
   - **Runtime**: `Python` (Python 3.10 / 3.11)
   - **Start Command**: `python3 main.py`
5. Go to the **Environment** / **Variables** section and add:
   | Variable | Description |
   |---|---|
   | `BOT_TOKEN` | Your Telegram Bot Token from `@BotFather` |
   | `SESSION_STRING` | Your Pyrogram String Session from `@SmartUtilBot` |
   | `API_ID` | Your API ID from `my.telegram.org` (e.g. `6`) |
   | `API_HASH` | Your API Hash from `my.telegram.org` |
   | `MAX_CONCURRENT_DOWNLOADS` | *(Optional, default: 3)* Simultaneous downloads |
   | `BATCH_SIZE` | *(Optional, default: 10)* Posts to process in parallel |
   | `FLOOD_WAIT_DELAY` | *(Optional, default: 3)* Flood delay in seconds |
6. Click **Deploy**. Botkeep will install `requirements.txt` and run `python3 main.py` continuously 24/7 without idle sleep!

---

## Direct Python Deployment (Local / VPS)

You can run the bot directly on any machine with Python 3.10+:

1. **Clone the repository**:
   ```sh
   git clone https://github.com/herokkuu52-boop/RestrictedContentDL.git
   cd RestrictedContentDL
   ```

2. **Create a virtual environment (optional but recommended)**:
   ```sh
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```sh
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   Copy the sample configuration file and fill in your credentials:
   ```sh
   cp config.env.sample config.env
   ```
   *(Or edit `.env` / set environment variables directly).*

5. **Start the bot**:
   ```sh
   python3 main.py
   ```

---

## Docker Deployment (Optional)

If you prefer to run inside a Docker container:

1. Copy `config.env.sample` to `config.env` and enter your values.
2. Start the container:
   ```sh
   docker compose up --build -d
   ```
3. Stop the container:
   ```sh
   docker compose down
   ```

---

## Usage

- **`/start`** – Welcomes you and gives a brief introduction.  
- **`/help`** – Shows detailed instructions and examples.  
- **`/dl <post_URL>`** or simply paste a Telegram post link – Fetch photos, videos, audio, or documents from that post.  
- **`/bdl <start_link> <end_link>`** – Batch-download a range of posts in one go.  
  > 💡 Example: `/bdl https://t.me/mychannel/100 https://t.me/mychannel/120`  
- **`/killall`** – Cancel any pending downloads if the bot hangs.  
- **`/logs`** – Download the bot’s logs file.  
- **`/stats`** – View current status (uptime, disk, memory, network, CPU, etc.).  
- **`/speedtest`** – Measure server download/upload bandwidth and HTTP latency while idle.
- **`/batch_watch <Telegram start link> <count>`** (also `/batch_watch_board`) –
  Export lessons linked through **Watch Board & Face**. Send the command without
  arguments for the guided link/count prompts. This is separate from `/batch`.
- **`/batch_watch_video <Telegram start link> <count>`** (also `/batch_watch_pip`) –
  New **board-first** export: original board with a small teacher at bottom-left.
  Supports the guided link/count prompts too; `/batch_watch` is unchanged.

### Faster board-first video mode (new command)

`/batch_watch_video` has its own batch loop and authenticates the supported
player with the password from each source caption or media filename. It downloads
the teacher from the URL (never Telegram's low-quality attachment), retrieves the
original slides/timed handwriting, and exports one full-length MP4. The board
uses native slide detail within the configured ceilings. The teacher is reduced
to at most one quarter of the board width, in a bottom-left footer **outside**
the board, so notes are never obscured. Captions/entities, source-title filenames,
destination settings, counts, `/killall`, disk reserve, upload limits, and confirmed
sends are supported. Wrong passwords, missing assets, and encoder failures do
not send a camera-only replacement. FloodWait stops the batch without retry loops.

Only this new layout caps output/teacher motion at **12 fps** by default; timed
handwriting still updates at `WATCH_BOARD_FPS` (8 by default). Set
`WATCH_BOARD_PIP_FPS=24` or `30` for smoother teacher motion at increased encoding
cost (never above the source frame rate). Audio, full duration, and board spatial
detail are retained. The new mode matches the web player's displayed ink: its
published compiler ignores selected-object deletion (`dlos`) events, leaving
those annotations visible. This compatibility behavior is isolated to the new
mode. The existing `/batch_watch` keeps its own deletion handling, side-by-side layout
and source teacher frame rate. Both layouts share the one-encoder global lock,
bounded slide cache, and frame pipe; no Chromium or full-file RAM buffer is used.

The browser exposed **one 640×360 teacher video plus a canvas**, not two video
files or a separate premium-quality download. Combining the board into a standard
Telegram video therefore still requires encoding; it cannot be an instant
download of a pre-existing combined HD file. No guessed CDN variants or fake
upscaling are used. On this desktop, the same real-source 120-second sample took
**6.70s / 196.4 MiB peak / 4.11 MiB output** in the new 760×552 / 12-fps layout,
versus **18.16s / 262.2 MiB / 14.51 MiB** in the existing 1400×428 layout. These
are local diagnostic measurements, not a guarantee for the 1.5-core server or
Telegram network throughput. Use the probe below with `--layout pip` to compare
on the deployment host. It does not open a Telegram session.

### Watch Board & Face quality

The supported `unacadamy-panel-api.vercel.app` player is **not a single HD video**.
Its `?url=.../output.webm` is the teacher/camera track; slides and timed pen strokes
come from its password-protected `/api/load-video` API. The old downloader only
saved the camera track, which left out the board. `/batch_watch` now reads
`[PASS ABC123]`, `[PASS: ABC123]`, or `Password: ABC123` from the source caption or
media filename, authenticates normally with the player's cookie/request token,
and combines the original slide images, timed vector handwriting, and teacher
video/audio into a full-length MP4. The filename comes from the source title.

Default board panel: **native slide resolution**, up to **1920×1080**, with a
separate camera column (up to 640 pixels wide), H.264 **CRF 16 / veryfast**, and
AAC audio 192 kbps. Teacher frame rate is retained; handwriting updates at 8 fps.
The camera does not cover board text. This is a composed export,
not an untouched original HD recording: if the CDN camera is 640×360 or slides
are 760×427, no extra photographic detail can be invented by enlarging them.
Vector ink is rendered at the chosen board resolution. The bot checks the largest
slide actually selected in the lesson, fetching dimensions with at most four
concurrent requests. It does not download an entire imported deck or enlarge
small slides simply to label the result "1080p". For 760×427 slides and a 640×360
camera the composed output is 1400×428, retaining native slide/camera detail.
There is no verified higher-resolution camera variant at the supplied
`output.webm` URL, so the bot
does not guess “1080p” URLs or silently use Telegram's source attachment.

Board rendering takes CPU time and can be considerably slower than plain
download/upload. It uses **one encoder globally**, two encoder threads, four
decoded slide images, a **32 MiB bounded compressed-slide cache** shared between
preparation and rendering, and a bounded frame pipe—no Chromium or retained frame
sequence. The camera file plus the finished MP4 need disk space. Both are cleaned
up after delivery/failure; incomplete output is never sent. The 256 MiB disk
reserve and destination upload limit are enforced. Progress shows wall-clock
elapsed time, render throughput (multiples of realtime), and an estimated remaining
time, with edits limited to once every five seconds. Authentication, missing slide,
unknown drawing-mode, and changed API errors fail explicitly without sending a
camera-only replacement. Normal Telegram `/batch` concurrency is unchanged.

Optional deployment overrides (defaults shown in `config.env.sample`):
`WATCH_BOARD_WIDTH` (640–1920, even), `WATCH_BOARD_HEIGHT` (360–1080, even),
`WATCH_BOARD_FPS` (1–15), and `WATCH_BOARD_CRF` (0–23; smaller means better
quality/larger files). Width and height are **ceilings** when
`WATCH_BOARD_NATIVE_SIZE=true` (default). Set it to `false` only if an enlarged
canvas is needed; that is much more expensive and adds no photographic detail.
`WATCH_BOARD_PRESET` defaults to `veryfast`; `superfast` or `ultrafast` can save
more encoding CPU but produce substantially larger files, consuming more disk
space and upload time. Supported slower overrides: `faster`, `fast`, `medium`.
These settings do not control the resolution served by the CDN. Native sizing is
on by default even on existing deployments; normal `/batch` transfers are unchanged.

For opt-in live verification without opening a Telegram session, run
`tools/watch_board_probe.py <player-url> --output <temporary-directory>` from a
disposable working directory, set `WATCH_BOARD_TEST_PASSWORD` in the environment,
and use test placeholders for `BOT_TOKEN` / `SESSION_STRING`. The probe exports
a 30-second diagnostic clip by default; `--start` selects its timeline position
and `--seconds` changes its length. **Batch exports always use full duration.**
Live checks replayed all 17,141 events in the supplied two-hour lesson and
visually verified slide/handwriting clips. On the same 120-second sample, the
previous 2560×1080 export took **51.58s / 718.5 MiB peak**, versus native-size
1400×428 at **14.56s / 267.2 MiB peak** (Python plus FFmpeg). A later sample with
the shared compressed cache took **26.56s / 267.4 MiB peak** and produced a
**byte-identical MP4 (matching SHA-256)**. End-to-end times include HTTP asset
fetches and vary; these figures are observations, not a guaranteed multiplier.
Both paths used CRF 16,
the `veryfast` preset, the same local source, and the original teacher frame rate;
timings include fetching selected slide dimensions/images. Output sizes were
13.82 MB and 13.42 MB respectively. `superfast` took 13.27s but produced 24.84 MB,
so it is not the default on a 2 GB disk. These are local export measurements,
**not a guaranteed speed on the 1.5-core server**, and exclude Telegram delivery.
The complete 7,322.14-second lesson was also exported at native size (1400×472,
to preserve the tallest selected slide): **25m17s elapsed, 280.4 MiB peak RAM,
893.27 MiB output**, with its full duration verified and late/tail frames decoded.
That full-length test preceded adding the shared compressed cache; the cache's
reuse, bounds, and identical short-sample output were verified separately.
Combining moving video and a board into one playable MP4 still needs encoding;
an unencoded offline-player bundle would be a different output format.

> **Note:** Make sure that your user session account is a member of the source chat or channel before downloading.

---

## Author

- Name: Bisnu Ray
- Telegram: [@itsSmartDev](https://t.me/itsSmartDev)

> **Note**: If you found this repo helpful, please fork and star it. Also, feel free to share with proper credit!
