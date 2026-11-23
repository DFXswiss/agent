"""``python -m agent_doubles.login_cli``: the login-CLI double (`agent_doubles.login_cli.cli`)."""

from __future__ import annotations

import sys

from agent_doubles.login_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
