"""U-MON sample: the test stack's textfile comes from the runner's own renderer and carries every metric of the
table, the capacity-low variant, repeated writes with growing counters, and the command line."""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
from agent_monitoring import sample, stack

from agent_runner.metrics import textfile


def values(text: str, name: str) -> list[float]:
    return [float(line.rsplit(" ", 1)[1]) for line in text.splitlines() if re.match(rf"{name}(\{{| )", line)]


def test_writes_every_metric_like_the_runner(tmp_path: Path) -> None:
    path = sample.write(tmp_path, 1795600000.0)
    assert path == tmp_path / textfile.FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == textfile.FILE_MODE
    text = path.read_text(encoding="utf-8")
    names = stack.textfile_metrics(text)
    assert names == {metric.name for metric in textfile.metrics_of(sample.snapshot(0.0), spend=sample.SPEND)}
    # 43 + the work VM slots and their capacity (ADR-059 addendum 4) + the admission per kind (05 section 3.8)
    assert len(names) == 51
    assert values(text, "agent_runner_textfile_timestamp_seconds") == [1795600000.0]
    assert values(text, "agent_runner_device_capacity_low") == [0]
    assert values(text, "agent_runner_vm_probe_ok") == [1, 0]
    assert 'agent_runner_session_model_tokens_total{vm="vm-0a1b2c3d",session="' in text
    assert 'agent_runner_device_model_spend_usd{window="day"} 1.25' in text


def test_capacity_low_variant(tmp_path: Path) -> None:
    text = sample.write(tmp_path, 1.0, capacity_low=True).read_text(encoding="utf-8")
    assert values(text, "agent_runner_device_capacity_low") == [1]
    assert values(text, "agent_runner_device_vm_slots_free") == [0]


def test_repeated_writes_advance_time_and_counters(tmp_path: Path) -> None:
    ticks = iter([100.0, 115.0, 130.0])
    slept: list[float] = []
    path = sample.run(
        tmp_path, capacity_low=False, count=3, interval=15.0, clock=lambda: next(ticks), sleep=slept.append
    )
    text = path.read_text(encoding="utf-8")
    assert slept == [15.0, 15.0]
    assert values(text, "agent_runner_textfile_timestamp_seconds") == [130.0]
    assert values(text, "agent_runner_vm_network_receive_bytes_total")[0] == 1000 + 2 * 4096
    assert sorted(item.name for item in tmp_path.iterdir()) == [textfile.FILE_NAME]


def test_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sample.main(["--dir", str(tmp_path), "--capacity-low", "--count", "0"]) == 0
    assert "wrote" in capsys.readouterr().out
    text = (tmp_path / textfile.FILE_NAME).read_text(encoding="utf-8")
    assert values(text, "agent_runner_device_capacity_low") == [1]
