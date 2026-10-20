import os
from pathlib import Path
import subprocess
import sys

import pytest


def load_config(tmp_path, **settings):
    env = {**os.environ, "BOT_TOKEN": "123456:TEST", "SESSION_STRING": "offline",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "PARALLEL_DOWNLOAD_WORKERS": "4", "PARALLEL_UPLOAD_WORKERS": "4",
           "DOWNLOAD_REQUESTS_PER_CONNECTION": "3", "UPLOAD_REQUESTS_PER_CONNECTION": "5",
           "MAX_UPLOAD_REQUESTS_PER_CONNECTION": "8", "TRANSFER_TUNE_INTERVAL": "5",
           "TRANSFER_TUNE_COOLDOWN": "30", "TRANSFER_TUNE_MIN_GAIN": "0.1",
           "TRANSFER_CRYPTO_WORKERS": "4", "MAX_ACTIVE_TRANSFERS": "2",
           "TRANSFER_BUFFER_MIB": "64", **settings}
    return subprocess.run([sys.executable, "-B", "-c",
                           "from helpers.transfer import TransferManager; "
                           "m=TransferManager(adaptive=False); "
                           "print(m.downloads.capacity, m.uploads.capacity)"],
                          env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)


def test_environment_overrides_reach_production_request_pools(tmp_path):
    result = load_config(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "12 20"


@pytest.mark.parametrize("settings", [
    {"MAX_UPLOAD_REQUESTS_PER_CONNECTION": "4"},
    {"TRANSFER_TUNE_INTERVAL": "nan"},
    {"UPLOAD_REQUESTS_PER_CONNECTION": "0"},
    {"TRANSFER_RPC_TIMEOUT": "0"},
    {"TRANSFER_FINALIZE_TIMEOUT": "nan"},
])
def test_invalid_transfer_settings_fail_before_sessions_open(tmp_path, settings):
    result = load_config(tmp_path, **settings)
    assert result.returncode != 0
    assert "ValueError" in result.stderr


@pytest.mark.parametrize("profile,expected", [("throughput", "8 8 4 128"), ("balanced", "4 4 2 64")])
def test_profile_defaults_without_legacy_overrides(tmp_path, profile, expected):
    env = {**os.environ, "BOT_TOKEN": "123456:TEST", "SESSION_STRING": "offline",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "TRANSFER_PROFILE": profile}
    for key in ("PARALLEL_DOWNLOAD_WORKERS", "PARALLEL_UPLOAD_WORKERS",
                "DOWNLOAD_REQUESTS_PER_CONNECTION", "TRANSFER_BUFFER_MIB"):
        env.pop(key, None)
    result = subprocess.run([sys.executable, "-B", "-c",
        "from config import PyroConf as c; print(c.PARALLEL_DOWNLOAD_WORKERS, "
        "c.PARALLEL_UPLOAD_WORKERS, c.DOWNLOAD_REQUESTS_PER_CONNECTION, c.TRANSFER_BUFFER_MIB)"],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
