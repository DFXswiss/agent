"""VM cleanup of `agent-runner uninstall` (05-runner.md section 1.2; registry `uninstall.py:CLEANERS`).

Deletes every Lima instance this runner created, while the store still exists: the VMs of `runner.vm` (any state
but `deleted`), the goldens of `runner.golden` and every `<prefix>arp-<8 hex>` instance the store does not know (a
crash between clone and row). Without `limactl` there is nothing to delete. Shared golden caches (`cache-golden-*`)
and instances outside the runner's prefix are never touched (`Lima` refuses foreign names). The token directory is
removed with the rest of the runner data.
"""

from __future__ import annotations

import functools
import re
from typing import Any

from agent_runner.registry import Cleaner
from agent_runner.vm import golden, lima


def delete_instances(context: Any, *, wrapper: lima.Lima | None = None) -> list[str]:
    """Delete the runner's instances; returns their names."""
    config = context.config
    machine = wrapper or lima.Lima(
        functools.partial(lima.run_limactl, env=dict(context.host.env)), prefix=config.vm_prefix
    )
    try:
        existing = {instance.name for instance in machine.list()}
    except lima.LimaNotInstalled:
        return []
    names: set[str] = set()
    if existing and context.dsn is not None:
        with golden.connect(context.dsn) as conn:
            names.update(
                row["lima_name"] for row in conn.execute("SELECT lima_name FROM runner.vm WHERE state <> 'deleted'")
            )
            names.update(row["lima_name"] for row in conn.execute("SELECT lima_name FROM runner.golden"))
    pattern = re.compile("^" + re.escape(config.vm_prefix) + r"arp-[0-9a-f]{8}$")
    targets = sorted(name for name in existing if name in names or pattern.fullmatch(name))
    for name in targets:
        machine.delete(name)
    return targets


def _clean(context: Any) -> None:
    delete_instances(context)


CLEANERS = [Cleaner("vm-instances", _clean)]
