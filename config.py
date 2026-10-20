import os
from os import getenv
from time import time
from dotenv import load_dotenv

# Load environment variables from config.env or .env if present
load_dotenv("config.env")
load_dotenv()

raw_bot_token = getenv("BOT_TOKEN", "").strip().strip("'\"")
raw_session_string = getenv("SESSION_STRING", "").strip().strip("'\"")

# Validate strictly from Environment
if not raw_bot_token or not raw_bot_token.count(":") == 1:
    print("Error: BOT_TOKEN must be set in environment and in format '123456:abcdefghijklmnopqrstuvwxyz'")
    exit(1)

if (
    not raw_session_string
    or raw_session_string == "xxxxxxxxxxxxxxxxxxxxxxx"
):
    print("Error: SESSION_STRING must be set in environment with a valid string")
    exit(1)


# Pyrogram setup
class PyroConf(object):
    API_ID = int(getenv("API_ID", "6"))
    API_HASH = getenv("API_HASH", "eb06d4abfb49dc3eeb1aeb98ae0f581e").strip().strip("'\"")
    BOT_TOKEN = raw_bot_token
    SESSION_STRING = raw_session_string
    BOT_START_TIME = time()

    MAX_CONCURRENT_DOWNLOADS = int(getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
    BATCH_SIZE = int(getenv("BATCH_SIZE", "10"))
    FLOOD_WAIT_DELAY = int(getenv("FLOOD_WAIT_DELAY", "3"))

    # Seconds the bot waits for the "pin the first post?" answer of a /batch run
    # before it continues without pinning.
    PIN_PROMPT_TIMEOUT = int(getenv("PIN_PROMPT_TIMEOUT", "60"))

    # How long an error message stays visible before the bot deletes it again.
    ERROR_MESSAGE_TTL = int(getenv("ERROR_MESSAGE_TTL", "300"))

    TRANSFER_PROFILE = getenv("TRANSFER_PROFILE", "throughput").lower()
    if TRANSFER_PROFILE not in ("balanced", "throughput"):
        raise ValueError("TRANSFER_PROFILE must be balanced or throughput")
    PARALLEL_DOWNLOAD_WORKERS = int(getenv("PARALLEL_DOWNLOAD_WORKERS", "12" if TRANSFER_PROFILE == "throughput" else "4"))
    MAX_CONCURRENT_TRANSMISSIONS = int(getenv("MAX_CONCURRENT_TRANSMISSIONS", "3"))
    PARALLEL_UPLOAD_WORKERS = int(getenv("PARALLEL_UPLOAD_WORKERS", "12" if TRANSFER_PROFILE == "throughput" else "4"))
    DOWNLOAD_REQUESTS_PER_CONNECTION = int(getenv("DOWNLOAD_REQUESTS_PER_CONNECTION", "4" if TRANSFER_PROFILE == "throughput" else "2"))
    UPLOAD_REQUESTS_PER_CONNECTION = int(getenv("UPLOAD_REQUESTS_PER_CONNECTION", "4"))
    TRANSFER_ADAPTIVE = getenv("TRANSFER_ADAPTIVE", "true").lower() in ("true", "1", "yes")
    MAX_DOWNLOAD_REQUESTS_PER_CONNECTION = int(getenv("MAX_DOWNLOAD_REQUESTS_PER_CONNECTION", "8"))
    MAX_UPLOAD_REQUESTS_PER_CONNECTION = int(getenv("MAX_UPLOAD_REQUESTS_PER_CONNECTION", "8"))
    TRANSFER_TUNE_INTERVAL = float(getenv("TRANSFER_TUNE_INTERVAL", "3"))
    TRANSFER_TUNE_COOLDOWN = float(getenv("TRANSFER_TUNE_COOLDOWN", "10"))
    TRANSFER_TUNE_MIN_GAIN = float(getenv("TRANSFER_TUNE_MIN_GAIN", "0.05"))
    TRANSFER_CRYPTO_WORKERS = int(getenv("TRANSFER_CRYPTO_WORKERS", "8" if TRANSFER_PROFILE == "throughput" else "4"))
    TRANSFER_WARM_TTL = float(getenv("TRANSFER_WARM_TTL", "600"))
    TRANSFER_RPC_TIMEOUT = float(getenv("TRANSFER_RPC_TIMEOUT", "60"))
    TRANSFER_FINALIZE_TIMEOUT = float(getenv("TRANSFER_FINALIZE_TIMEOUT", "120"))
    TRANSFER_CPU_TARGET = float(getenv("TRANSFER_CPU_TARGET", "95"))
    TRANSFER_MEMORY_TARGET = float(getenv("TRANSFER_MEMORY_TARGET", "80"))
    MIRROR_SOURCE_PINS = getenv("MIRROR_SOURCE_PINS", "true").lower() in ("true", "1", "yes")
    MAX_ACTIVE_TRANSFERS = int(getenv("MAX_ACTIVE_TRANSFERS", "2"))
    TRANSFER_BUFFER_MIB = int(getenv("TRANSFER_BUFFER_MIB", "256" if TRANSFER_PROFILE == "throughput" else "64"))
    DISK_RESERVE_MIB = int(getenv("DISK_RESERVE_MIB", "256"))
    # Board vector rendering, separate from unchanged Telegram transfer settings.
    WATCH_BOARD_WIDTH = int(getenv("WATCH_BOARD_WIDTH", "1920"))
    WATCH_BOARD_HEIGHT = int(getenv("WATCH_BOARD_HEIGHT", "1080"))
    WATCH_BOARD_FPS = int(getenv("WATCH_BOARD_FPS", "8"))
    WATCH_BOARD_CRF = int(getenv("WATCH_BOARD_CRF", "16"))
    WATCH_BOARD_PRESET = getenv("WATCH_BOARD_PRESET", "veryfast")
    WATCH_BOARD_NATIVE_SIZE = getenv("WATCH_BOARD_NATIVE_SIZE", "true").lower() in ("true", "1", "yes")
    # Previous footer cadence is retained only for diagnostic comparisons.
    WATCH_BOARD_PIP_FPS = int(getenv("WATCH_BOARD_PIP_FPS", "12"))
    WATCH_BOARD_VIDEO_PRESET = getenv("WATCH_BOARD_VIDEO_PRESET", "superfast")
    if WATCH_BOARD_VIDEO_PRESET not in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium"):
        raise ValueError("Unsupported Watch Board video encoding preset")
    if not 1 <= WATCH_BOARD_PIP_FPS <= 30:
        raise ValueError("Watch Board PiP FPS must be 1..30")
    if WATCH_BOARD_PRESET not in ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium"):
        raise ValueError("Unsupported Watch Board encoding preset")
    if (not 640 <= WATCH_BOARD_WIDTH <= 1920 or not 360 <= WATCH_BOARD_HEIGHT <= 1080
            or WATCH_BOARD_WIDTH % 2 or WATCH_BOARD_HEIGHT % 2):
        raise ValueError("Watch Board dimensions must be even, within 640..1920 by 360..1080")
    if not 1 <= WATCH_BOARD_FPS <= 15 or not 0 <= WATCH_BOARD_CRF <= 23:
        raise ValueError("Watch Board FPS must be 1..15 and CRF 0..23")

    for _name in ("PARALLEL_DOWNLOAD_WORKERS", "PARALLEL_UPLOAD_WORKERS",
                  "DOWNLOAD_REQUESTS_PER_CONNECTION", "UPLOAD_REQUESTS_PER_CONNECTION",
                  "MAX_UPLOAD_REQUESTS_PER_CONNECTION", "MAX_DOWNLOAD_REQUESTS_PER_CONNECTION", "TRANSFER_CRYPTO_WORKERS",
                  "MAX_ACTIVE_TRANSFERS",
                  "TRANSFER_BUFFER_MIB", "DISK_RESERVE_MIB"):
        if locals()[_name] <= 0:
            raise ValueError(f"{_name} must be positive")
    if max(DOWNLOAD_REQUESTS_PER_CONNECTION, UPLOAD_REQUESTS_PER_CONNECTION,
           MAX_UPLOAD_REQUESTS_PER_CONNECTION, MAX_DOWNLOAD_REQUESTS_PER_CONNECTION) > 8:
        raise ValueError("Requests per connection must not exceed 8")
    if TRANSFER_CRYPTO_WORKERS > 8:
        raise ValueError("TRANSFER_CRYPTO_WORKERS must not exceed 8")
    if max(PARALLEL_DOWNLOAD_WORKERS, PARALLEL_UPLOAD_WORKERS) > 16 or MAX_ACTIVE_TRANSFERS > 4:
        raise ValueError("Use at most 16 connections per direction and 4 active transfers")
    if MAX_UPLOAD_REQUESTS_PER_CONNECTION < UPLOAD_REQUESTS_PER_CONNECTION:
        raise ValueError("Maximum upload requests must be at least the starting value")
    if MAX_DOWNLOAD_REQUESTS_PER_CONNECTION < DOWNLOAD_REQUESTS_PER_CONNECTION:
        raise ValueError("Maximum download requests must be at least the starting value")
    if not (0 < TRANSFER_TUNE_INTERVAL <= 60 and 0 < TRANSFER_TUNE_COOLDOWN <= 600
            and 0 < TRANSFER_TUNE_MIN_GAIN <= 1):
        raise ValueError("Invalid transfer tuning interval, cooldown or minimum gain")
    if TRANSFER_BUFFER_MIB > 512:
        raise ValueError("TRANSFER_BUFFER_MIB must not exceed 512")
    if not (0 < TRANSFER_WARM_TTL <= 3600 and 10 <= TRANSFER_CPU_TARGET <= 95
            and 10 <= TRANSFER_MEMORY_TARGET <= 95):
        raise ValueError("Invalid warm profile TTL or resource targets")
    if not (0 < TRANSFER_RPC_TIMEOUT <= 300 and 0 < TRANSFER_FINALIZE_TIMEOUT <= 600):
        raise ValueError("Transfer request timeouts must be positive and within 300/600 seconds")
    if TRANSFER_BUFFER_MIB < 4 * MAX_ACTIVE_TRANSFERS:
        raise ValueError("TRANSFER_BUFFER_MIB must provide at least 4 MiB per active transfer")

