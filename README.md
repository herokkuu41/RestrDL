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
defaults to the `throughput` profile: twelve download and twelve upload connections
across the entire bot, with at most two files active at once. Each download connection
handles one outstanding read, preventing stalled reads from trapping sibling requests
on the same connection. Upload connections start with four outstanding requests and
can grow to eight after measured improvement. Large-file ranges enter the upload queue as they finish, so a slow
range does not hold up ready parts. Small files retain ordered MD5 calculation.
Albums share these limits. Queued data, upload slices, and retry
payloads have a shared 256 MiB budget; this is a payload budget, not a cap on total process
RAM. TgCrypto is required and checked at startup.

Uploads begin while downloads are still running. Progress shows downloaded bytes and
bytes acknowledged by Telegram separately. Files retain captions, names, source video/
audio metadata, and available thumbnails; streaming does not generate new FFmpeg thumbnails.
The existing server-side copy/forward path remains the first choice when available.

| Variable | Default | Meaning |
|---|---:|---|
| `TRANSFER_PROFILE` | throughput | Preset: throughput or balanced; explicit overrides take precedence |
| `PARALLEL_DOWNLOAD_WORKERS` | 12 | Global download connection slots |
| `PARALLEL_UPLOAD_WORKERS` | 12 | Global upload connection slots |
| `DOWNLOAD_ISOLATED_READS` | true | One source RPC per connection; takes precedence over source multiplexing counts |
| `DOWNLOAD_REQUESTS_PER_CONNECTION` | 4 | Starting source window per connection when isolation is disabled |
| `UPLOAD_REQUESTS_PER_CONNECTION` | 4 | Starting upload window per connection |
| `TRANSFER_ADAPTIVE` | true | Trial larger download and upload windows using acknowledged throughput |
| `MAX_DOWNLOAD_REQUESTS_PER_CONNECTION` | 8 | Source RPC cap per connection when isolation is disabled |
| `MAX_UPLOAD_REQUESTS_PER_CONNECTION` | 8 | Hard global upload RPC cap per connection |
| `TRANSFER_TUNE_INTERVAL` | 3 | Seconds per ACK rate sample |
| `TRANSFER_TUNE_COOLDOWN` | 10 | Seconds before another capacity trial |
| `TRANSFER_TUNE_MIN_GAIN` | 0.05 | Minimum fractional ACK throughput improvement |
| `TRANSFER_CRYPTO_WORKERS` | 8 | Native encryption/decryption executor workers |
| `TRANSFER_WARM_TTL` | 600 | Seconds confirmed request windows remain reusable for their route |
| `TRANSFER_RPC_TIMEOUT` | 60 | Seconds to wait for one download chunk/upload part request |
| `DOWNLOAD_STALL_TIMEOUT` | 15 | Whole source RPC watchdog, including readiness/encryption/socket drain |
| `DOWNLOAD_CHUNK_ATTEMPTS` | 5 | Maximum attempts for a missing source chunk; other parts are retained |
| `TRANSFER_FINALIZE_TIMEOUT` | 120 | Seconds to wait for Telegram media registration or final sending |
| `TRANSFER_CPU_TARGET` | 95 | CPU percentage ceiling for growth, relative to the container quota |
| `TRANSFER_MEMORY_TARGET` | 80 | Container memory percentage ceiling for growth |
| `MIRROR_SOURCE_PINS` | true | Pin delivered copies of posts pinned in the source |
| `MAX_ACTIVE_TRANSFERS` | 2 | Active files, including album items |
| `TRANSFER_BUFFER_MIB` | 256 | Shared buffered payload budget |
| `DISK_RESERVE_MIB` | 256 | Minimum free disk after reserving a fallback file |
| `MAX_CONCURRENT_DOWNLOADS` | 3 | Outer post/album processing slots; does not multiply transfer connections |

Existing environment variables override defaults: update any old
request counts, connection counts and buffer settings to match `config.env.sample`.
Setting the profile alone does not replace explicit old overrides. The balanced preset
uses four connections per direction and 64 MiB.
The throughput preset starts with 12 isolated source ranges and 48 upload parts,
with upload growth up to 96 parts. Eight native crypto
executor threads allow encryption and decryption to use available CPU cores. These
are shared limits across the service, not per-file multipliers. Set
`TRANSFER_ADAPTIVE=false` for fixed
upload counts (source congestion/throttle handling remains enabled only as described
below). Startup rejects unsafe or inconsistent limits; at most 16 connections
per direction, 8 requests per connection, 4 active files, and 512 MiB of payload are allowed.
CDN streaming
needs at least two download slots because it opens both an origin and a CDN session.
With only one or two download connections, the manager admits one active file so CDN
streaming cannot deadlock another producer holding the memory budget. With four download
connections, native CDN reserves two and leaves the other two available for regular files.
Native disk fallback is attempted only for unsupported file identifiers when space
can be reserved. Oversized files are rejected before downloading using the destination
bot's upload limit (normally 2000 MiB), even when the source user has Premium.
Failed/cancelled transfers release their buffers and connection slots. Albums are sent
only after every item is prepared; failure does not silently send an incomplete album.

Both direction tuners change only outstanding requests, keeping connection counts fixed.
The source window can recover up to the isolated connection cap; it cannot add more
reads to an already busy connection. Set `DOWNLOAD_ISOLATED_READS=false` to explicitly
benchmark the former multiplexed path. Explicit `download_requests` in the benchmark's
manager constructor also selects that path for controlled comparisons.
Active files share one window and require two source-supplied baseline intervals and
two trial intervals; album items cannot multiply the initial window.
A trial is kept only when average acknowledged throughput improves by at least 5%.
A plateau or source starvation rolls back the trial, followed by a cooldown. All files
share the physical pool and byte budget. Confirmed windows are reused for up to ten
minutes for the same bot, source DC, transfer mode, and relevant configuration. Only
successful transfers with confirmed measurements qualify. Expired profiles, failed
transfers and real throttling invalidate reuse; partial trial settings are never cached.
This avoids rebuilding capacity for each next video while keeping stale route settings
from persisting indefinitely. A recent throttle cooldown is retained.

CPU/memory checks use cgroup v2 or v1 limits when available, including fractional CPU
quotas such as 150%. CPU saturation stops additional growth and rolls back an unconfirmed
trial; it does not keep cutting the established window. Previously the controller could
remove a connection's worth of requests every sample merely because CPU was busy,
eventually collapsing to one outstanding request during a video. Actual memory pressure
still reduces established work, at most once per cooldown. Fixed request mode does not
apply resource tuning, while server throttling always applies. Both directions can grow
again when pressure clears and measured ACK rates justify it. Larger limits
allow the service to use spare resources; they cannot make Telegram accept bytes faster
than its own route or account limits. Payload memory stays bounded independently of file size.

These changes apply the guide's latency hiding, bounded parallelism, native CPU work,
ACK-based tuning and local retry patterns. The existing 1 MiB download / 512 KiB upload
parts remain within Telegram's protocol limits; HTTP-specific range sizes and FFmpeg
transforms from the guide do not apply to the normal Telegram relay. The throughput
profile's 256 MiB payload budget provides more read-ahead on a 2 GiB server. It is a cap,
not a RAM allocation target: memory is used when a source/sink speed mismatch needs
buffering. The process, crypto work, sockets, and library allocations use additional RAM.
Keep the balanced profile on hosts without this headroom. Existing deployment environment
overrides must be updated or removed to use the new throughput values.

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

Add `--adaptive` for one additional real post using the ACK tuner. The benchmark also
supports `--production` for one additional post with the deployed source isolation,
upload connection counts and watchdog/recovery settings. These options send posts.
The benchmark
configures the same native crypto pool as production. To measure scheduler changes
without credentials, network requests or test posts, run:

```sh
python -B scripts/benchmark_pipeline.py --mib 64 --latency-ms 20 --repeats 3
```

This compares one-request scheduling, the prior four-connection pipeline, the previous
eight-connection profile, and the twelve-connection throughput profile with simulated
per-RPC latency. Each mode reuses
one manager for consecutive files to catch leaked buffer credits or shrinking capacity.
It verifies every payload and reports ACK
throughput, first acknowledgement, CPU time, peak reserved payload, and ACK rates for
each quarter of the file to expose a draining slowdown. It excludes
encryption, real network conditions and Telegram limits, so its speedup is not a
prediction of production throughput.

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
throughput. Telegram can limit non-Premium source downloads. Short waits (up to 30
seconds) are handled by the transfer scheduler on the exact refused request, with at
most three attempts. Waits are exposed instead of hidden inside the library; they
pause new requests across files in the affected direction. One upload throttle episode
reduces the window once even when many outstanding parts receive the same error. Source
throttles reduce only the download window, which can recover through measured trials;
they no longer reduce the upload window. This removes a code-induced capacity
ratchet across videos; genuine repeated service limits still require waiting.
Completed chunks, the upload file ID,
and acknowledged parts are retained. The whole-file restart loop is removed for
both single media and albums. Longer waits stop the batch instead of repeatedly
starting files or moving to the next file while throttled. `/killall` cancels an
in-progress wait. See [Telegram file transfer guidance](https://core.telegram.org/api/files)
and [Premium download limits](https://telegram.org/faq_premium).

`/logs` now includes transfer progress every 15 seconds, plus completion elapsed time,
download rate, acknowledged upload rate, relay rate, source DC, request/connection limits,
native CDN usage and the shared payload buffer peak. Pipeline logs include a transfer
ID, queue estimate, both request windows, in-flight work and tuning decisions. They also
include recent download/upload throughput, actual RPC response latency per direction,
process RSS, and CPU quota throttling. Rising RPC latency with low CPU and memory pressure
points toward the network/Telegram route; high quota throttling indicates a host CPU limit.
Direction rates are averages from
relay start until that direction finishes, including startup and backpressure; they are
not isolated download-only or upload-only benchmarks. Telegram progress edits occur at
most every five seconds, and a slow edit no longer blocks other data workers.
Progress clocks start when a file gets a transfer slot, excluding time queued behind
earlier videos. Progress displays both recent interval speed and active-transfer average.

Chunk/part requests explicitly use a 60-second response timeout instead of the library's
15-second default. Source reads additionally have a 15-second watchdog around the entire
RPC: the library's response timeout excludes readiness, encryption and socket drain.
A timed-out worker is retired before retry; media-session shutdown is bounded to five
seconds and aborts a stuck transport. The missing range gets up to five attempts while
upload parts keep their three-attempt policy. Offsets, part numbers, upload handles and
acknowledged data are retained. These deadlines apply to individual requests, not the
whole video. Source latency above 2.5 times its healthy sample (and above two seconds),
together with at least a 25% goodput drop, reduces source concurrency. A timeout also
reduces the source window once per recovery period, at least 15 seconds; CPU activity
alone never triggers this network backoff. Real FloodWait handling still applies.
After the bytes are acknowledged, Telegram media
registration and final sending have a separate 120-second response timeout and bounded
application retries with stable random IDs for deduplication. Library transport retries
are disabled on these calls to avoid stacking hidden retry loops. Progress switches to
waiting for Telegram to finish sending; delivery is counted only after its final response.
Status-message cleanup is bounded and cannot turn successful delivery into a failure.
Timeout logs identify the direction, RPC, offset/part, and configured deadline.

The first successful **source-order** post is pinned as soon as all earlier candidates
finish or fail; a later quick text post cannot take the pin from earlier media. Private
chat pins use the sender's own message ID namespace. Sent responses and album copies
use the required `topics` field for Pyrofork 2.3.69, so completed sends are counted correctly.

After deploying, restart with the new code and check the startup `Transfer settings`
log. Connection-count environment values override defaults, but old 2/4 requests-per-connection
values now control real pool limits. Apply the updated sample's twelve connections,
four starting requests per connection, eight crypto workers and 256 MiB budget on
an existing deployment. `/logs` reports the effective settings, warm window reuse,
resource pressure, and Telegram waits. More CPU/RAM use is useful only while ACK
throughput improves.

### Mirror pins from the source

`MIRROR_SOURCE_PINS=true` automatically pins destination copies of source-pinned
posts, including the specific pinned members of an album. It works with server-side
copies and streamed reuploads, in channels and the bot chat. Source state is read when
the post is fetched; this does not watch for later pin/unpin changes in the original chat.
The library's discarded raw `pinned` flag is preserved during the existing source
metadata request rather than adding one lookup per video. In private chats, pinning
uses the account that sent the copy and that account's message ID namespace.

The batch's existing Yes/No question controls the optional first-post pin; source pins
are mirrored independently. Pinning a mirrored first post is not repeated, and changing
the first-post choice to No does not remove a source pin. Set `MIRROR_SOURCE_PINS=false`
to disable mirroring. The destination account needs the applicable pin/edit permissions;
a failed pin is reported after delivery and never causes the video to be sent again.

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
  Export the original board on the left and full-size teacher on the right.
  Supports the guided link/count prompts too; `/batch_watch` is unchanged.

### Faster board-first video mode (new command)

`/batch_watch_video` has its own batch loop and authenticates the supported
player with the password from each source caption or media filename. It downloads
the teacher from the URL (never Telegram's low-quality attachment), retrieves the
original slides/timed handwriting, and exports one full-length MP4. The board
uses native slide detail within the configured ceilings. The teacher
is shown in a separate column on the right at its source resolution and frame
rate, so notes are never obscured. Captions/entities, source-title filenames,
destination settings, counts, `/killall`, disk reserve, upload limits, and confirmed
sends are supported. Wrong passwords, missing assets, and encoder failures do
not send a camera-only replacement. FloodWait stops the batch without retry loops.

Output keeps the camera's source frame rate, the original board resolution, the
existing `WATCH_BOARD_CRF` quality setting (16 by default), and AAC 192 kbps audio.
There is no new frame-rate cap, reduced teacher resolution, or raised CRF.
`WATCH_BOARD_VIDEO_PRESET=superfast` reduces encoder analysis; compression is
less efficient, so files can grow even at the same quality setting. Board frames
are converted to video-range YUV420 only when the slide/ink changes, then reused.
This halves board pipe traffic and avoids repeated RGB conversion in FFmpeg.
Timed handwriting still updates at `WATCH_BOARD_FPS` (8 by default).
Audio, full duration, and board spatial detail are retained. The new mode matches the web player's displayed ink: its
published compiler ignores selected-object deletion (`dlos`) events, leaving
those annotations visible. This compatibility behavior is isolated to the new
mode. The existing `/batch_watch` keeps its own deletion handling, side-by-side layout
and source teacher frame rate. Both layouts share the one-encoder global lock,
bounded slide cache, and frame pipe; no Chromium or full-file RAM buffer is used.

The browser exposed **one 640×360 teacher video plus a canvas**, not two video
files or a separate premium-quality download. Combining the board into a standard
Telegram video therefore still requires encoding; it cannot be an instant
download of a pre-existing combined HD file. No guessed CDN variants or fake
upscaling are used. A real-source 120-second sample at **1400×428 / 23.98 fps**
took **11.55s / 245.7 MiB peak / 31.29 MiB output** with the new profile, versus
**17.09s / 268.5 MiB / 17.35 MiB** with the standard side-by-side path on this
desktop. The faster `ultrafast` experiment took 6.98s but produced 47.43 MiB;
it was not selected by default because the larger upload can negate the saving.
These measurements do not demonstrate an eight-minute end-to-end limit on the
1.5-core server. Encoding, download/upload bandwidth, and CPU contention all
affect that limit. Per-post logs now show download, render, and upload durations
separately. Use `--layout fast_side_by_side` with the probe below on the deployment
host for a representative measurement. It does not open a Telegram session.

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
