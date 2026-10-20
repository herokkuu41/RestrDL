"""Measure process/container pressure, including fractional Linux CPU quotas."""
import os
from pathlib import Path
import time

import psutil


class ResourceMonitor:
    def __init__(self, cpu_target=85, memory_target=80, root="/sys/fs/cgroup"):
        self.root = Path(root)
        self.process = psutil.Process()
        self.cpu_target, self.memory_target = cpu_target, memory_target
        self.cores = float(os.cpu_count() or 1)
        quota = self.read("cpu.max").split()
        try:
            if len(quota) == 2 and quota[0] != "max":
                if int(quota[0]) > 0 and int(quota[1]) > 0:
                    self.cores = min(self.cores, int(quota[0]) / int(quota[1]))
            else:
                quota, period = self.number("cpu/cpu.cfs_quota_us"), self.number("cpu/cpu.cfs_period_us")
                if quota > 0 and period > 0:
                    self.cores = min(self.cores, quota / period)
        except ValueError:
            pass
        self.last_time = time.monotonic()
        self.last_cpu = self.cpu_seconds()
        self.cpu = self.memory = 0.0
        self.throttled = 0.0
        self.last_throttled = self.throttled_seconds()
        self.last_check = -float("inf")

    def read(self, name):
        try:
            return (self.root / name).read_text().strip()
        except OSError:
            if name.startswith(("cpu/", "cpuacct/")):
                try:
                    return (self.root / "cpu,cpuacct" / name.split("/", 1)[1]).read_text().strip()
                except OSError:
                    pass
            return ""

    def number(self, name):
        try:
            return int(self.read(name))
        except ValueError:
            return 0

    def cpu_seconds(self):
        stat = dict(line.split() for line in self.read("cpu.stat").splitlines() if len(line.split()) == 2)
        if "usage_usec" in stat:
            return int(stat["usage_usec"]) / 1e6
        usage = self.number("cpuacct/cpuacct.usage")
        if usage:
            return usage / 1e9
        cpu = self.process.cpu_times()
        return cpu.user + cpu.system

    def throttled_seconds(self):
        stat = dict(line.split() for line in self.read("cpu.stat").splitlines() if len(line.split()) == 2)
        if "throttled_usec" in stat:
            return int(stat["throttled_usec"]) / 1e6
        stat = dict(line.split() for line in self.read("cpu/cpu.stat").splitlines() if len(line.split()) == 2)
        return int(stat.get("throttled_time", 0)) / 1e9

    def memory_pressure(self):
        self.has_headroom()
        return self.memory >= self.memory_target

    def has_headroom(self):
        now = time.monotonic()
        if now - self.last_check < 5:
            return self.cpu < self.cpu_target and self.memory < self.memory_target
        elapsed = now - self.last_time
        cpu = self.cpu_seconds()
        if elapsed >= 0.1:
            self.cpu = max(0, 100 * (cpu - self.last_cpu) / elapsed / max(self.cores, 0.01))
            throttled = self.throttled_seconds()
            self.throttled = max(0, 100 * (throttled - self.last_throttled) / elapsed)
            self.last_throttled = throttled
            self.last_time, self.last_cpu = now, cpu
        current, limit = self.number("memory.current"), self.number("memory.max")
        if not limit:
            current, limit = self.number("memory/memory.usage_in_bytes"), self.number("memory/memory.limit_in_bytes")
        host = psutil.virtual_memory()
        if limit and limit < host.total:
            self.memory = current / limit * 100
        else:
            self.memory = host.percent
        self.last_check = now
        return self.cpu < self.cpu_target and self.memory < self.memory_target
