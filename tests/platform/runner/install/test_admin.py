"""U-RUN-install-admin: the one administrator step of the agent way (ADR-074 section 3): a constant AppleScript
handler, the shell script and its three arguments and the prompt as separate argv items (never spliced into the
script), and the layout's paths in the install and uninstall scripts."""

from __future__ import annotations

from pathlib import Path

from agent_runner.install import admin
from agent_runner.install.layout import Layout

PAYLOAD = "/Library/Application Support/DFXAgentRunner"
HANDLER = [
    "-e",
    "on run argv",
    "-e",
    'do shell script "/bin/sh -c " & quoted form of (item 1 of argv) & " dfx-agent-runner"'
    ' & " " & quoted form of (item 2 of argv) & " " & quoted form of (item 3 of argv)'
    ' & " " & quoted form of (item 4 of argv) with prompt (item 5 of argv) with administrator privileges',
    "-e",
    "end run",
]


def test_the_install_script_names_the_layouts_spool_and_payload(tmp_path: Path) -> None:
    script = admin.install_script()
    assert '/bin/mkdir -p "' + PAYLOAD + '/spool"' in script
    assert '/bin/chmod 0755 "' + PAYLOAD + '"' in script
    assert '/bin/mv "$work/request" "' + PAYLOAD + '/spool/enroll.request"' in script
    assert "{spool}" not in script
    assert "{payload}" not in script
    assert script == admin.INSTALL.replace("{spool}", PAYLOAD + "/spool").replace("{payload}", PAYLOAD)
    layout = Layout(tmp_path)
    assert f'/usr/sbin/chown root:wheel "{layout.spool}"' in admin.install_script(layout)
    # the digest is checked on the root-only copy before installer runs it
    lines = script.splitlines()
    copy = lines.index('/bin/cp -P "$package" "$work/package.pkg"')
    digest = next(index for index, line in enumerate(lines) if "shasum -a 256" in line)
    install = lines.index('/usr/sbin/installer -pkg "$work/package.pkg" -target /')
    assert copy < digest < install


def test_every_check_of_the_install_script_ends_it() -> None:
    """`set -e` does not stop at a failing `&&` list: each check ends the script with `|| exit 1` itself."""
    lines = admin.install_script().splitlines()
    checks = [line.strip() for line in lines if line.lstrip().startswith(("[", "{"))]
    assert len(checks) == 4
    assert all(line.endswith("|| exit 1") for line in checks)
    # the request's owner and link count are read before the request is copied; root and a hard link are refused
    owner = lines.index('  owner=$(/usr/bin/stat -f %Su "$request")')
    links = lines.index('  links=$(/usr/bin/stat -f %l "$request")')
    check = lines.index('  { [ -n "$owner" ] && [ "$owner" != root ] && [ "$links" = 1 ]; } || exit 1')
    copy = lines.index('  /bin/cp -P "$request" "$work/request"')
    assert owner < links < check < copy
    # the name of the checked owner is what reaches the spool, one line
    written = lines.index('  printf \'%s\\n\' "$owner" >"$work/owner"')
    assert written > copy
    assert not any("stat -f %Su" in line and "$work/owner" in line for line in lines)


def test_the_uninstall_script_runs_the_installed_root_helper(tmp_path: Path) -> None:
    assert admin.uninstall_script() == f'set -eu\n"{PAYLOAD}/current/libexec/root-helper" uninstall\n'
    layout = Layout(tmp_path)
    assert admin.uninstall_script(layout) == f'set -eu\n"{layout.current}/libexec/root-helper" uninstall\n'


def test_the_arguments_are_separate_argv_items() -> None:
    hostile = "x'; rm -rf / #"
    argv = admin.osascript_argv("script text", (hostile, "b", ""), "prompt text")
    assert argv == ["/usr/bin/osascript", *HANDLER, "script text", hostile, "b", "", "prompt text"]
    assert all(hostile not in line for line in admin.APPLESCRIPT)


def test_the_install_argv(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    argv = admin.install_argv("/tmp/p.pkg", "ab" * 32, "/tmp/r", layout)
    assert argv == [
        "/usr/bin/osascript",
        *HANDLER,
        admin.install_script(layout),
        "/tmp/p.pkg",
        "ab" * 32,
        "/tmp/r",
        admin.INSTALL_PROMPT,
    ]
    assert admin.install_argv("/p", "d", "")[-5] == admin.install_script()


def test_the_uninstall_argv(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    assert admin.uninstall_argv(layout) == [
        "/usr/bin/osascript",
        *HANDLER,
        admin.uninstall_script(layout),
        "",
        "",
        "",
        admin.UNINSTALL_PROMPT,
    ]
    assert admin.uninstall_argv()[-5] == admin.uninstall_script()


def test_the_prompts_and_sentences_name_dfx() -> None:
    for text in (admin.INSTALL_PROMPT, admin.UNINSTALL_PROMPT, admin.INSTALL_SENTENCE, admin.UNINSTALL_SENTENCE):
        assert "DFX" in text
    assert admin.INSTALL_SENTENCE.startswith("macOS asks once for your password")
