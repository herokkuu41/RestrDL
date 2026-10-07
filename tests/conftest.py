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

import importlib.metadata
_orig_version = importlib.metadata.version
def _shim_version(name):
    if name.lower() == "pyrofork":
        return "2.3.69"
    return _orig_version(name)
importlib.metadata.version = _shim_version

from pyrogram import raw, utils
_orig_messages_init = raw.types.messages.Messages.__init__
_topics_store = {}
raw.types.messages.Messages.topics = property(
    lambda self: _topics_store.get(id(self), []),
    lambda self, val: _topics_store.__setitem__(id(self), val)
)
def _shim_messages_init(self, *args, **kwargs):
    topics = kwargs.pop("topics", None)
    _orig_messages_init(self, *args, **kwargs)
    self.topics = topics or []
raw.types.messages.Messages.__init__ = _shim_messages_init

_orig_get_input_media = utils.get_input_media_from_file_id
def _shim_get_input_media(file_id, *args, **kwargs):
    kwargs.pop("has_spoiler", None)
    return _orig_get_input_media(file_id, *args, **kwargs)
utils.get_input_media_from_file_id = _shim_get_input_media

import test_pin_feature  # noqa: F401
