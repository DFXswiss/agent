"""Explicit accounts for existing protocol tests using a mocked gh transport.

Account isolation and missing-configuration behavior are tested without this
adapter in test_github_accounts.py. This adapter preserves the older protocol
fixtures while supplying their newly required account/authentication context.
"""
import json
from pathlib import Path
from agent_cli.runtime import Completed


def configure_accounts(home: Path, sessions, *, login='alice'):
    (home / 'github-accounts.json').write_text(json.dumps({
        'accounts': {'test': {'login': login, 'gh_config_dir': '/test/gh', 'git': {
            'name': 'Test Worker', 'email': 'test@example.com',
            'signing_format': 'ssh', 'signing_key': '/test/key',
        }}},
        'sessions': {sid: 'test' for sid in sessions},
    }))


def transport(runner, *, login='alice', authenticate=False):
    def scoped(argv):
        assert argv[0] == 'env'
        assert 'GH_CONFIG_DIR=/test/gh' in argv
        command = argv[argv.index('gh'):]
        if command == ['gh', 'api', 'user', '--jq', '.login']:
            if not authenticate:
                return Completed(0, login, '')
            result = runner(['gh', 'api', 'user'])
            if result.returncode:
                return result
            return Completed(0, json.loads(result.stdout)['login'], result.stderr)
        result = runner(command)
        if command[:3] == ['gh', 'pr', 'view'] and result.returncode == 0:
            try:
                data = json.loads(result.stdout)
            except ValueError:
                return result
            if isinstance(data, dict):
                data.setdefault('author', {'login': login})
                return Completed(result.returncode, json.dumps(data), result.stderr)
        return result
    return scoped
