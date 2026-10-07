import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("SESSION_STRING", "test-session-string")
os.environ.setdefault("FLOOD_WAIT_DELAY", "0")
os.environ.setdefault("BATCH_SIZE", "2")

# Ensure FakeClient is installed before main is imported anywhere
import pyrogram.errors
if not hasattr(pyrogram.errors, "FloodPremiumWait"):
    class FloodPremiumWait(pyrogram.errors.FloodWait):
        def __init__(self, value=0, *args, **kwargs):
            try:
                super().__init__(value, *args, **kwargs)
            except Exception:
                pass
            self.value = value
    pyrogram.errors.FloodPremiumWait = FloodPremiumWait

# Use actual pinned Pyrofork constructors/parser, not version or topics shims.

import test_pin_feature  # noqa: F401
