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
    control.reset()
    assert control.limit == control.confirmed == 4


def test_ack_sampling_recognizes_backlog_while_a_batch_of_rpcs_drains():
    control = window()
    control.active = 1
    control.pending = 5  # ready parts waiting for pool slots, including retry payloads
    control.acknowledge(1000, now=5)
    assert control.acknowledge(1000, now=10) == "trial"
    assert control.limit == 8
