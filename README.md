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
- ✍️ Copy text messages or captions from Telegram posts.
- 📌 Batch mode asks whether to pin the first post of the batch (tap a button or reply `yes` / `no`).
- 🗂️ With no destination channel set, everything (text, photos, videos, files) is delivered to the bot chat itself.
- 🧹 Error messages delete themselves automatically after 5 minutes.
- ☁️ **Native Python & Cloud-ready**: Runs directly on [Botkeep Cloud](https://botkeep.cloud) without requiring Docker!
- 🎬 **Bundled FFmpeg support**: Thumbnail generation works out-of-the-box on standard Python environments.

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

> **Note:** Make sure that your user session account is a member of the source chat or channel before downloading.

---

## Author

- Name: Bisnu Ray
- Telegram: [@itsSmartDev](https://t.me/itsSmartDev)

> **Note**: If you found this repo helpful, please fork and star it. Also, feel free to share with proper credit!
