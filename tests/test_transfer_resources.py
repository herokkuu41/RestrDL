from types import SimpleNamespace as NS

from helpers import transfer_resources as resources


def test_fractional_container_cpu_and_memory_limits_drive_headroom(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(resources.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: NS(total=8 * 1024**3, percent=20))
    (tmp_path / "cpu.max").write_text("150000 100000")
    (tmp_path / "cpu.stat").write_text("usage_usec 0\n")
    (tmp_path / "memory.max").write_text(str(2 * 1024**3))
    (tmp_path / "memory.current").write_text(str(128 * 1024**2))
    monitor = resources.ResourceMonitor(root=tmp_path)
    assert monitor.cores == 1.5
    clock[0] = 10
    (tmp_path / "cpu.stat").write_text("usage_usec 7500000\n")
    assert monitor.has_headroom()
    assert monitor.cpu == 50 and monitor.memory == 6.25
    clock[0] = 20
    (tmp_path / "cpu.stat").write_text("usage_usec 22500000\n")
    assert not monitor.has_headroom() and monitor.cpu == 100
    clock[0] = 30
    (tmp_path / "memory.current").write_text(str(int(1.8 * 1024**3)))
    assert not monitor.has_headroom() and monitor.memory > 89


def test_combined_v1_cpu_controller_is_supported(tmp_path):
    cpu = tmp_path / "cpu,cpuacct"
    cpu.mkdir()
    (cpu / "cpu.cfs_quota_us").write_text("150000")
    (cpu / "cpu.cfs_period_us").write_text("100000")
    (cpu / "cpuacct.usage").write_text("2000000000")
    monitor = resources.ResourceMonitor(root=tmp_path)
    assert monitor.cores == 1.5 and monitor.cpu_seconds() == 2


def test_cpu_busy_is_distinct_from_memory_pressure_and_reports_quota_throttling(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(resources.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: NS(total=8 * 1024**3, percent=20))
    (tmp_path / "cpu.max").write_text("150000 100000")
    (tmp_path / "cpu.stat").write_text("usage_usec 0\nthrottled_usec 0\n")
    (tmp_path / "memory.max").write_text(str(2 * 1024**3))
    (tmp_path / "memory.current").write_text(str(256 * 1024**2))
    monitor = resources.ResourceMonitor(cpu_target=95, root=tmp_path)
    clock[0] = 10
    (tmp_path / "cpu.stat").write_text("usage_usec 15000000\nthrottled_usec 2000000\n")
    assert not monitor.has_headroom()
    assert monitor.cpu == 100 and monitor.throttled == 20
    assert not monitor.memory_pressure()
    clock[0] = 20
    (tmp_path / "memory.current").write_text(str(int(1.9 * 1024**3)))
    assert monitor.memory_pressure()


def test_v1_quota_throttle_counter(tmp_path):
    cpu = tmp_path / "cpu,cpuacct"
    cpu.mkdir()
    (cpu / "cpu.stat").write_text("nr_periods 10\nnr_throttled 2\nthrottled_time 200000000\n")
    monitor = resources.ResourceMonitor(root=tmp_path)
    assert monitor.throttled_seconds() == 0.2
