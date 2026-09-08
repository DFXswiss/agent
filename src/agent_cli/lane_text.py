"""Script-owned CLI calls with no native work tools and isolated configuration.

The provider CLI remains trusted software. This adapter is a model tool
boundary, not an OS sandbox for a malicious provider binary. Its executable
must be explicitly selected and pinned; it never installs or updates a CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from .ai_accounts import AIRole
from .coordinator_exec import run_bounded
from .lane_protocol import ProtocolError, response_schema


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def isolated_env(profile: Path, child_home: Path) -> dict[str, str]:
    """A new child's real home and a minimal environment, never ambient tokens."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(child_home), "LANG": "en_US.UTF-8",
           "TMPDIR": str(child_home), "CODEX_HOME": str(profile), "GROK_HOME": str(profile),
           "GROK_DISABLE_AUTOUPDATER": "1", "GROK_MEMORY": "0", "GROK_SUBAGENTS": "0",
           "GROK_LSP_TOOLS": "0", "GROK_TOOL_SEARCH": "0"}
    for vendor in ("CLAUDE", "CURSOR"):
        for surface in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS"):
            env[f"GROK_{vendor}_{surface}_ENABLED"] = "0"
    return env


class TextCLI:
    """Context-managed, isolated text transport for one bounded model lane."""

    def __init__(self, role: AIRole, *, binary: str, sha256: str, timeout: int):
        self.role = role
        self.binary = Path(binary)
        if not self.binary.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ProtocolError("explicit absolute CLI binary and SHA-256 required")
        if not self.binary.is_file() or file_hash(self.binary) != sha256:
            raise ProtocolError("configured CLI executable hash does not match")
        with self.binary.open("rb") as stream:
            if stream.read(4) not in {b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}:
                raise ProtocolError("pin the native CLI executable, not a launcher or installation script")
        if type(timeout) is not int or timeout < 1:
            raise ProtocolError("positive lane timeout required")
        self.sha256 = sha256
        self.deadline = time.monotonic() + timeout
        self.temp = None

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-text-lane-")
        try:
            self.root = Path(self.temp.name)
            self.profile, self.child_home, self.cwd = (self.root / n for n in ("profile", "home", "work"))
            for p in (self.profile, self.child_home, self.cwd):
                p.mkdir(mode=0o700)
            self.env = isolated_env(self.profile, self.child_home)
            # No user/project plugins, hooks, MCP settings or models are copied.
            self.auth_source = Path(self.role.account.config_dir) / "auth.json"
            info = self.auth_source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ProtocolError("selected account auth.json must be a private regular file")
            auth = self.auth_source.read_bytes()
            if not auth or len(auth) > 1_000_000:
                raise ProtocolError("selected account authentication is unavailable")
            self.auth_hash = hashlib.sha256(auth).hexdigest()
            self.auth_copy = self.profile / "auth.json"
            self.auth_copy.write_bytes(auth)
            self.auth_copy.chmod(0o600)
            self.config = self.profile / "config.toml"
            self.schema = self.root / "response-schema.json"
            self.schema.write_text(json.dumps(response_schema()))
            vendor = self.role.account.provider
            if vendor == "codex":
                self.config.write_text('cli_auth_credentials_store = "file"\nweb_search = "disabled"\napproval_policy = "never"\n')
                version = self._run([str(self.binary), "--version"]).stdout.strip()
                if version not in {"codex-cli 0.147.0", "codex-cli 0.153.4"}:
                    raise ProtocolError("Codex version has no validated text adapter")
                features = self._run([str(self.binary), "features", "list"])
                self.disabled = []
                for line in features.stdout.splitlines():
                    parts = line.split()
                    if len(parts) < 3 or not re.fullmatch(r"[a-z0-9_]+", parts[0]) or parts[-1] not in {"true", "false"}:
                        raise ProtocolError("unrecognized Codex feature inventory")
                    self.disabled.append(parts[0])
                if not {"shell_tool", "unified_exec", "multi_agent", "hooks", "apps", "plugins"}.issubset(self.disabled):
                    raise ProtocolError("incomplete Codex feature inventory")
                self.args = [str(self.binary), "exec", "--model", self.role.model,
                             "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(self.cwd),
                             "-c", 'web_search="disabled"', "--output-schema", str(self.schema)]
                for feature in self.disabled:
                    self.args += ["--disable", feature]
            elif vendor == "grok":
                self.config.write_text('[cli]\nauto_update = false\n[session]\nload_envrc = false\n')
                inspection = json.loads(self._run([str(self.binary), "inspect", "--json"]).stdout)
                if inspection.get("grokVersion") not in {"1.0.5", "1.0.13"}:
                    raise ProtocolError("Grok version has no validated text adapter")
                for key in ("hooks", "skills", "plugins", "mcpServers", "lspServers", "projectInstructions"):
                    if inspection.get(key) != []:
                        raise ProtocolError("unexpected Grok configuration surface: " + key)
                if any(a.get("source", {}).get("type") != "builtin" for a in inspection.get("agents", [])):
                    raise ProtocolError("unexpected external Grok agent definition")
                # --tools Read is the Grok CLI allow-list alias; the canonical
                # native tool name is read_file. --disallowed-tools read_file
                # removes that canonical tool. For the pinned versions above,
                # native fake-provider probes already show an empty work-tool
                # inventory and injected read_file yields "Tool not found".
                # This documents the measured alias/canonical combination, not
                # a new bypass claim.
                self.args = [str(self.binary), "--model", self.role.model, "--verbatim",
                             "--tools", "Read", "--disallowed-tools", "read_file,search_tool,use_tool",
                             "--no-subagents", "--disable-web-search", "--no-plan", "--max-turns", "1",
                             "--cwd", str(self.cwd), "--json-schema", json.dumps(response_schema())]
            else:
                raise ProtocolError("unsupported text transport provider")
            return self
        except BaseException:
            self.temp.cleanup()
            raise

    def _run(self, argv: list[str], text: str | None = None):
        if file_hash(self.binary) != self.sha256:
            raise ProtocolError("CLI executable changed during lane")
        remaining = int(self.deadline - time.monotonic())
        if remaining < 1:
            raise ProtocolError("lane deadline exhausted")
        # Isolation includes the script's Python bridge, before native startup.
        result = run_bounded(argv, timeout=remaining, cwd=str(self.cwd), stdin_text=text,
                             env=self.env, inherit_env=False, clear_ambient_github=False)
        if result.returncode != 0:
            # Provider logs may contain credentials or unrelated local paths.
            raise ProtocolError("configured text CLI failed with exit " + str(result.returncode))
        return result

    def complete(self, prompt: str) -> str:
        if len(prompt.encode()) > 4_000_000:
            raise ProtocolError("lane prompt byte limit exceeded")
        if self.role.account.provider == "codex":
            output = self.root / "result.txt"
            output.unlink(missing_ok=True)
            self._run([*self.args, "--output-last-message", str(output), "-"], prompt)
            result = output.read_text()
        else:
            spec = self.root / "request.txt"
            # Grok expands raw file mentions before calling the model, even
            # with native tools removed. Serialize the entire work input and
            # escape the mention delimiter; only the model decodes this data.
            # This preserves source characters without giving the CLI a host
            # file reference to interpret.
            encoded = json.dumps(prompt, ensure_ascii=True).replace("@", "\\u0040")
            spec.write_text("Decode the following JSON string as your complete work input, then follow it.\n" + encoded)
            raw = self._run([*self.args, "--prompt-file", str(spec)]).stdout
            envelope = json.loads(raw)
            if (not isinstance(envelope, dict) or envelope.get("stopReason") != "end_turn"
                    or type(envelope.get("num_turns")) is not int or envelope["num_turns"] != 1
                    or not isinstance(envelope.get("structuredOutput"), dict)
                    or not isinstance(envelope.get("text"), str)
                    or json.loads(envelope["text"]) != envelope["structuredOutput"]):
                raise ProtocolError("Grok did not return one complete structured work result")
            result = json.dumps(envelope["structuredOutput"])
        if not result.strip() or len(result.encode()) > 1_100_000:
            raise ProtocolError("empty or oversized model response")
        return result

    def __exit__(self, *_):
        # Do not silently discard refreshed credentials, or overwrite a newer
        # account update from a parallel lane. Only the static script persists.
        try:
            if self.auth_copy.is_file():
                updated = self.auth_copy.read_bytes()
                if hashlib.sha256(updated).hexdigest() != self.auth_hash:
                    import fcntl
                    lock = self.auth_source.with_name(".agent-auth.lock")
                    with lock.open("a") as stream:
                        fcntl.flock(stream, fcntl.LOCK_EX)
                        if file_hash(self.auth_source) == self.auth_hash:
                            fd, temporary = tempfile.mkstemp(prefix=".agent-auth-", dir=self.auth_source.parent)
                            try:
                                with os.fdopen(fd, "wb") as target:
                                    target.write(updated)
                                os.replace(temporary, self.auth_source)
                            finally:
                                Path(temporary).unlink(missing_ok=True)
        finally:
            self.temp.cleanup()
