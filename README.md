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
files active at once. Each download connection now permits two requests in flight and
each upload connection four (8 download RPCs / 16 upload RPCs globally). Sending another
part while awaiting earlier acknowledgements fills latency gaps; the old pipeline allowed
only one request per connection. Large files enqueue completed chunks immediately rather
than waiting for a slower earlier chunk. Small files retain ordered MD5 calculation.
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
| `DOWNLOAD_REQUESTS_PER_CONNECTION` | 2 | Outstanding RPCs per download connection |
| `UPLOAD_REQUESTS_PER_CONNECTION` | 4 | Outstanding RPCs per upload connection |
| `MAX_ACTIVE_TRANSFERS` | 2 | Active files, including album items |
| `SOURCE_RELAY_CONCURRENCY` | 1 | Protected-source files/albums in flight; each retains all 4 download lanes |
| `PREMIUM_WAIT_RETRIES` | 3 | Automatic retries after Telegram's requested non-Premium wait |
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
`FLOOD_PREMIUM_WAIT` now pauses and retries the current protected-source media/album
automatically (up to three times), rather than aborting the whole batch. Protected-source
relays are serialized so several batch items do not multiply that account-side throttle;
each individual relay still uses its four download lanes. See [Telegram file transfer guidance](https://core.telegram.org/api/files)
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
log. Existing environment values override defaults. Keep four connections, two download
requests per connection and four upload requests per connection initially. A 64 MiB
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

> **Note:** Make sure that your user session account is a member of the source chat or channel before downloading.

---

## Author

- Name: Bisnu Ray
- Telegram: [@itsSmartDev](https://t.me/itsSmartDev)

> **Note**: If you found this repo helpful, please fork and star it. Also, feel free to share with proper credit!
