"""ACK-based request windows shared by active files within one bounded manager."""
import asyncio
import time
from contextlib import asynccontextmanager


class UploadWindow:
    def __init__(self, initial, maximum, step, interval=5, cooldown=30, gain=0.10,
                 headroom=None, adaptive=True, memory_pressure=None, latency_guard=False):
        self.limit = self.confirmed = initial
        self.initial = initial
        self.maximum, self.step = maximum, step
        self.interval, self.cooldown, self.gain = interval, cooldown, gain
        self.headroom = headroom or (lambda: True)
        self.memory_pressure = memory_pressure or (lambda: False)
        self.latency_guard = latency_guard
        self.latency_seconds = self.latency_samples = 0
        self.healthy_latency = None
        self.healthy_rate = 0
        self.pressure_until = 0
        self.adaptive = adaptive
        self.recovery_until = 0
        self.confirmed_rate = 0
        self.condition = asyncio.Condition()
        self.active = self.waiting = self.pending = 0
        self.acked = self.busy_acked = 0
        self.samples = []
        self.baseline = None
        self.rate = 0
        self.trial = False
        self.last_sample = time.monotonic()
        self.next_trial = self.last_sample

    def reset(self, warm=None):
        """Discard rate history between busy periods; retain a throttle cooldown."""
        now = time.monotonic()
        start = min(warm or self.initial, self.maximum)
        if now < max(self.recovery_until, self.pressure_until):
            start = min(start, self.confirmed)
        self.limit = self.confirmed = start
        if warm is None:
            self.confirmed_rate = 0
        self.trial = False
        self.samples.clear()
        self.baseline = None
        self.acked = self.busy_acked = 0
        self.latency_seconds = self.latency_samples = 0
        self.healthy_latency = None
        self.healthy_rate = 0
        self.last_sample = now
        self.next_trial = max(now, self.next_trial)

    @asynccontextmanager
    async def lease(self):
        async with self.condition:
            self.waiting += 1
            try:
                await self.condition.wait_for(lambda: self.active < self.limit)
                self.active += 1
            finally:
                self.waiting -= 1
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def acknowledge(self, amount, now=None, latency=None):
        now = time.monotonic() if now is None else now
        self.acked += amount
        if latency is not None:
            self.latency_seconds += latency
            self.latency_samples += 1
        if self.pending > self.active or self.waiting or self.active >= self.limit:
            self.busy_acked += amount
        elapsed = now - self.last_sample
        if elapsed < self.interval:
            return None
        rate = self.acked / elapsed
        # Exclude source starvation, startup, and the file's draining tail.
        saturated = self.busy_acked >= 0.8 * self.acked
        self.acked = self.busy_acked = 0
        self.last_sample = now
        latency = self.latency_seconds / self.latency_samples if self.latency_samples else None
        self.latency_seconds = self.latency_samples = 0
        if (self.latency_guard and self.adaptive and saturated and latency is not None):
            if (self.healthy_latency is not None and now >= self.recovery_until
                    and latency > max(2.0, self.healthy_latency * 2.5)
                    and rate < self.healthy_rate * 0.75):
                self.stall(now)
                self.healthy_latency, self.healthy_rate = None, 0
                return "source-latency"
            if self.healthy_latency is None or rate > self.healthy_rate:
                self.healthy_latency, self.healthy_rate = latency, rate
        return self.sample(rate, saturated, now)

    def sample(self, rate, saturated, now):
        self.rate = rate
        if not self.adaptive:
            return None
        if not self.headroom():
            self.samples.clear()
            # Busy CPU is useful work, not evidence that the established window
            # is too large. Previously each sample removed another connection's
            # worth of work, eventually reducing sustained transfers to one RPC.
            # Only actual memory pressure shrinks the established window, with
            # a cooldown so one high reading cannot repeatedly cut capacity.
            if self.memory_pressure() and now >= self.pressure_until:
                self.confirmed = max(1, self.confirmed - self.step)
                self.confirmed_rate = 0
                self.pressure_until = now + self.cooldown
                return self.rollback(now, "memory-pressure")
            if self.trial:
                return self.rollback(now, "resource-pressure")
            return None
        if not saturated or rate <= 0:
            self.samples.clear()
            if self.trial:
                return self.rollback(now, "source-starved")
            return None
        self.samples.append(rate)
        if len(self.samples) < 2:
            return None
        average = sum(self.samples) / len(self.samples)
        self.samples.clear()
        if self.trial:
            if average < self.baseline * (1 + self.gain):
                return self.rollback(now, "plateau")
            self.confirmed = self.limit
            self.confirmed_rate = average
            self.trial = False
            self.baseline = None
            self.next_trial = now + self.cooldown
            return "confirmed"
        self.confirmed_rate = average
        if now >= self.next_trial and self.limit < self.maximum:
            self.baseline = average
            self.limit = min(self.maximum, self.limit + self.step)
            self.trial = True
            return "trial"
        return None

    def rollback(self, now, reason):
        self.limit = self.confirmed
        self.trial = False
        self.baseline = None
        self.samples.clear()
        self.next_trial = now + self.cooldown
        return reason

    def backoff(self):
        # A server throttle invalidates even a previously confirmed setting.
        self.confirmed = max(1, self.limit // 2)
        now = time.monotonic()
        self.rollback(now, "throttle")
        self.recovery_until = now + self.cooldown
        self.confirmed_rate = 0
        self.acked = self.busy_acked = 0
        self.last_sample = now

    def stall(self, now=None):
        """One congestion response per timeout wave; do not cancel other reads."""
        now = time.monotonic() if now is None else now
        if now < self.recovery_until or not self.adaptive:
            return False
        # A slow/missing response is network pressure, unlike busy CPU. Keep at
        # least two lanes for recovery, unless the configured cap is only one.
        self.confirmed = max(min(2, self.maximum), self.confirmed // 2)
        self.confirmed_rate = 0
        self.rollback(now, "source-stall")
        self.recovery_until = now + max(self.cooldown, 15)
        self.next_trial = self.recovery_until
        self.acked = self.busy_acked = 0
        self.last_sample = now
        return True
