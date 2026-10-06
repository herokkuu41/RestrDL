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

    PARALLEL_DOWNLOAD_WORKERS = int(getenv("PARALLEL_DOWNLOAD_WORKERS", "3"))
    MAX_CONCURRENT_TRANSMISSIONS = int(getenv("MAX_CONCURRENT_TRANSMISSIONS", "3"))

