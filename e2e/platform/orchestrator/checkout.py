"""The agent checkout on the runner device: a Python 3.12 venv with the platform packages (doubles, runner) and the
runner's `login` extra (Playwright for the mailbox automation of the subscription login, E2E-28; its Chromium comes
from `PLAYWRIGHT_BROWSERS_PATH`, the browsers of the suite).

The venv is (re)built with uv only when `pyproject.toml` or the extras changed since the last install (the marker
`.venv/.e2e-install` holds both); a venv set up by the work-package tools (`.venv/.wp-install` for the same
`pyproject.toml`, with every extra) is used as it is.
"""

from __future__ import annotations

ENSURE_SH = r"""
set -e
sum="$(shasum -a 256 pyproject.toml | cut -c1-64)"
if [ -x .venv/bin/python ] && { [ "$(cut -c1-64 .venv/.wp-install 2>/dev/null)" = "$sum" ] || \
   [ "$(cat .venv/.e2e-install 2>/dev/null)" = "$sum platform,login" ]; }; then
  echo "venv: up to date"
  exit 0
fi
uv venv -q --allow-existing --python 3.12 .venv
uv pip install -q --python .venv/bin/python -e ".[platform,login]"
echo "$sum platform,login" > .venv/.e2e-install
echo "venv: installed"
"""


def python_of(checkout: str) -> str:
    return f"{checkout}/.venv/bin/python"
