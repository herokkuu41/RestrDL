import asyncio

import pytest

from helpers.transfer_window import UploadWindow


def window():
    result = UploadWindow(4, 12, 4, interval=5, cooldown=30)
    result.last_sample = result.next_trial = 0
    return result


def test_tuner_requires_two_baseline_and_two_improved_samples():
    control = window()
    assert control.sample(100, True, 5) is None
    assert control.limit == 4
    assert control.sample(100, True, 10) == "trial"
    assert control.limit == 8 and control.confirmed == 4
    assert control.sample(125, True, 15) is None
    assert control.sample(125, True, 20) == "confirmed"
    assert control.confirmed == 8
    for now in (25, 30, 35, 40):
        control.sample(125, True, now)
    assert control.limit == 8  # cooldown
    control.sample(125, True, 50)
    assert control.sample(125, True, 55) == "trial"
    assert control.limit == 12


@pytest.mark.parametrize("rate", [80, 100, 109])
def test_plateau_rolls_back_and_waits_for_cooldown(rate):
    control = window()
    control.sample(100, True, 5)
    control.sample(100, True, 10)
    control.sample(rate, True, 15)
    assert control.sample(rate, True, 20) == "plateau"
    assert control.limit == control.confirmed == 4
    assert not control.trial and control.next_trial == 50


def test_source_starvation_cannot_confirm_a_trial():
    control = window()
    for now in range(5, 35, 5):
        assert control.sample(1000, False, now) is None
    assert control.limit == 4
    control.sample(100, True, 35)
    control.sample(100, True, 40)
    control.sample(200, True, 45)
    assert control.sample(200, False, 50) == "source-starved"
    assert control.limit == 4


def test_ack_samples_ignore_intervals_without_backlog():
    control = window()
    for now in range(5, 35, 5):
        control.acknowledge(1000, now=now)
    assert control.limit == 4
    control.active = 4
    control.waiting = 1
    control.acknowledge(1000, now=35)
    assert control.acknowledge(1000, now=40) == "trial"
    assert control.limit == 8


async def test_lowered_window_drains_inflight_without_cancelling_parts():
    control = UploadWindow(4, 8, 4)
    held = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def work():
        nonlocal active
        async with control.lease():
            active += 1
            if active == 4:
                held.set()
            await release.wait()

    tasks = [asyncio.create_task(work()) for _ in range(5)]
    try:
        await asyncio.wait_for(held.wait(), 1)
        control.backoff()
        assert control.limit == control.confirmed == 2
        assert control.active == 4 and control.waiting == 1
        assert all(not task.done() for task in tasks)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert control.active == control.waiting == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancelled_waiter_releases_window_state():
    control = UploadWindow(1, 2, 1)
    async with control.lease():
        async def wait():
            async with control.lease():
                pytest.fail("Window overcommitted")
        task = asyncio.create_task(wait())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert control.waiting == 0 and control.active == 1
    assert control.active == 0


def test_idle_reset_discards_learned_window_but_keeps_recent_throttle():
    control = window()
    control.limit = control.confirmed = 8
    control.reset()
    assert control.limit == control.confirmed == 4
    control.backoff()
    control.reset()
    assert control.limit == control.confirmed == 2
    control.next_trial = 0
    control.recovery_until = 0
    control.reset()
    assert control.limit == control.confirmed == 4


def test_ack_sampling_recognizes_backlog_while_a_batch_of_rpcs_drains():
    control = window()
    control.active = 1
    control.pending = 5  # ready parts waiting for pool slots, including retry payloads
    control.acknowledge(1000, now=5)
    assert control.acknowledge(1000, now=10) == "trial"
    assert control.limit == 8


def test_warm_window_survives_an_unhelpful_trial_and_new_file():
    control = window()
    control.confirmed = 8
    control.limit = 12
    control.trial = True
    control.rollback(10, "source-starved")
    control.reset(warm=8)
    assert control.limit == control.confirmed == 8
    assert not control.samples and not control.trial


def test_busy_cpu_blocks_growth_without_reducing_established_work():
    control = UploadWindow(4, 12, 4, headroom=lambda: False)
    for now in range(5, 125, 5):
        assert control.sample(1000, True, now) is None
    assert control.limit == control.confirmed == 4
    assert not control.trial


def test_cpu_spike_rolls_back_only_the_unconfirmed_trial():
    busy = [False]
    control = UploadWindow(4, 12, 4, headroom=lambda: not busy[0])
    control.next_trial = 0
    control.sample(100, True, 5)
    assert control.sample(100, True, 10) == "trial"
    busy[0] = True
    assert control.sample(125, True, 15) == "resource-pressure"
    for now in range(20, 100, 5):
        control.sample(100, True, now)
    assert control.limit == control.confirmed == 4
    busy[0] = False
    control.sample(100, True, 100)
    assert control.sample(100, True, 105) == "trial"
    control.sample(125, True, 110)
    assert control.sample(125, True, 115) == "confirmed"
    assert control.confirmed == 8


def test_memory_pressure_reduces_work_once_per_cooldown_then_recovers():
    pressure = [True]
    control = UploadWindow(12, 20, 4, headroom=lambda: not pressure[0],
                           memory_pressure=lambda: pressure[0])
    assert control.sample(1000, True, 5) == "memory-pressure"
    for now in (10, 15, 20, 25, 30):
        assert control.sample(1000, True, now) is None
    assert control.limit == control.confirmed == 8
    assert control.sample(1000, True, 35) == "memory-pressure"
    assert control.limit == 4
    pressure[0] = False
    control.sample(100, True, 65)
    assert control.sample(100, True, 70) == "trial"
    control.sample(125, True, 75)
    assert control.sample(125, True, 80) == "confirmed"
    assert control.limit == control.confirmed == 8


def test_memory_cooldown_survives_file_reset(monkeypatch):
    import helpers.transfer_window as module
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    control = UploadWindow(12, 20, 4, headroom=lambda: False, memory_pressure=lambda: True)
    control.sample(1000, True, clock[0])
    control.reset()
    assert control.limit == control.confirmed == 8
    clock[0] += 5
    control.sample(1000, True, clock[0])
    assert control.limit == 8


def test_fixed_mode_does_not_start_trials_after_throttle():
    control = UploadWindow(4, 12, 4, adaptive=False)
    control.backoff()
    for now in (50, 55, 60, 65):
        assert control.sample(1000, True, now) is None
    assert control.limit == 2
