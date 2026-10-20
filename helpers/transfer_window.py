"""ACK-based upload trials shared by active files within one bounded manager."""
import asyncio
import time
from contextlib import asynccontextmanager


class UploadWindow:
    def __init__(self, initial, maximum, step, interval=5, cooldown=30, gain=0.10):
        self.limit = self.confirmed = initial
        self.initial = initial
        self.maximum, self.step = maximum, step
        self.interval, self.cooldown, self.gain = interval, cooldown, gain
        self.condition = asyncio.Condition()
        self.active = self.waiting = self.pending = 0
        self.acked = self.busy_acked = 0
        self.samples = []
        self.baseline = None
        self.rate = 0
        self.trial = False
        self.last_sample = time.monotonic()
        self.next_trial = self.last_sample

    def reset(self):
        """Discard rate history between busy periods; retain a throttle cooldown."""
        now = time.monotonic()
        start = min(self.initial, self.confirmed) if now < self.next_trial else self.initial
        self.limit = self.confirmed = start
        self.trial = False
        self.samples.clear()
        self.baseline = None
        self.acked = self.busy_acked = 0
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

    def acknowledge(self, amount, now=None):
        now = time.monotonic() if now is None else now
        self.acked += amount
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
        return self.sample(rate, saturated, now)

    def sample(self, rate, saturated, now):
        self.rate = rate
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
            self.trial = False
            self.baseline = None
            self.next_trial = now + self.cooldown
            return "confirmed"
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
        self.acked = self.busy_acked = 0
        self.last_sample = now
