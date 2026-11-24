"""The one administrator step of the agent way (ADR-074 section 3), shared by `agent-platform device add|remove`.

macOS shows one system dialog for `osascript … do shell script … with administrator privileges`. The script text
is a constant of this module; the only variable parts - the package path, its expected SHA-256, the request file and
the dialog's prompt - reach it as `argv` of the AppleScript handler and are quoted with `quoted form of`, so nothing
the agent passes can become shell syntax.

`INSTALL` (as root): a fresh root-only directory, a copy of the package (`cp -P`: a link is copied as a link and then
refused), the digest checked **on that copy**, the request moved into the spool (0700 root) together with the name
of the request file's owner (the account the runner will run as), then `installer`; the directory is removed on
exit. `UNINSTALL` runs the root helper of the installed payload.
"""

from __future__ import annotations

from agent_runner.install.layout import Layout

OSASCRIPT = "/usr/bin/osascript"
INSTALL = """set -eu
umask 077
package=$1
digest=$2
request=$3
work=$(/usr/bin/mktemp -d /private/tmp/dfx-agent-runner.XXXXXXXX)
trap '/bin/rm -rf "$work"' EXIT
/bin/cp -P "$package" "$work/package.pkg"
{ [ -f "$work/package.pkg" ] && [ ! -L "$work/package.pkg" ]; } || exit 1
[ "$(/usr/bin/shasum -a 256 "$work/package.pkg" | /usr/bin/cut -d ' ' -f 1)" = "$digest" ] || exit 1
if [ -n "$request" ]; then
  /bin/mkdir -p "{spool}"
  /bin/chmod 0755 "{payload}"
  /bin/chmod 0700 "{spool}"
  /usr/sbin/chown root:wheel "{spool}"
  owner=$(/usr/bin/stat -f %Su "$request")
  links=$(/usr/bin/stat -f %l "$request")
  { [ -n "$owner" ] && [ "$owner" != root ] && [ "$links" = 1 ]; } || exit 1
  /bin/cp -P "$request" "$work/request"
  { [ -f "$work/request" ] && [ ! -L "$work/request" ]; } || exit 1
  /bin/chmod 0600 "$work/request"
  printf '%s\\n' "$owner" >"$work/owner"
  /bin/mv "$work/owner" "{spool}/owner"
  /bin/mv "$work/request" "{spool}/enroll.request"
fi
/usr/sbin/installer -pkg "$work/package.pkg" -target /
"""
UNINSTALL = """set -eu
"{helper}" uninstall
"""
APPLESCRIPT = (
    "on run argv",
    'do shell script "/bin/sh -c " & quoted form of (item 1 of argv) & " dfx-agent-runner"'
    ' & " " & quoted form of (item 2 of argv) & " " & quoted form of (item 3 of argv)'
    ' & " " & quoted form of (item 4 of argv) with prompt (item 5 of argv) with administrator privileges',
    "end run",
)
INSTALL_PROMPT = "DFX Agent wants to install the DFX execution service on this Mac."
UNINSTALL_PROMPT = "DFX Agent wants to remove the DFX execution service from this Mac."
INSTALL_SENTENCE = "macOS asks once for your password: this installs the DFX execution service on this Mac."
UNINSTALL_SENTENCE = "macOS asks once for your password: this removes the DFX execution service from this Mac."


def install_script(layout: Layout | None = None) -> str:
    used = layout or Layout()
    return INSTALL.replace("{spool}", str(used.spool)).replace("{payload}", str(used.payload))


def uninstall_script(layout: Layout | None = None) -> str:
    used = layout or Layout()
    return UNINSTALL.replace("{helper}", str(used.current / "libexec/root-helper"))


def osascript_argv(script: str, arguments: tuple[str, str, str], prompt: str) -> list[str]:
    """`osascript` with the constant handler; `script` and the three arguments are handed over as `argv`."""
    lines: list[str] = []
    for line in APPLESCRIPT:
        lines += ["-e", line]
    return [OSASCRIPT, *lines, script, *arguments, prompt]


def install_argv(package: str, digest: str, request: str, layout: Layout | None = None) -> list[str]:
    return osascript_argv(install_script(layout), (package, digest, request), INSTALL_PROMPT)


def uninstall_argv(layout: Layout | None = None) -> list[str]:
    return osascript_argv(uninstall_script(layout), ("", "", ""), UNINSTALL_PROMPT)
