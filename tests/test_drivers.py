"""Driver core: strict schema, templates, lane argv and derived lane I/O."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from agent_cli.drivers import (
    DOCUMENT_KEYS,
    DRIVERS_DOC,
    DRIVERS_NOT_CONFIGURED,
    DRIVER_KEYS,
    DRIVER_REFUSED,
    INTERACTIVE_KEYS,
    JUDGMENT_UNSUPPORTED,
    LANE_ARGV_KEYS,
    LANE_KEYS,
    NETWORK_OVERRIDE_ENV,
    NO_DRIVER_CONFIGURATION,
    PANE_KEYS,
    PERIOD_FIELDS,
    SPEND_FIELDS,
    USAGE_KEYS,
    DriverConfigError,
    DriverSpec,
    RecordedNames,
    credential_dir_overlap_error,
    credential_scrub,
    credential_wrapper,
    declared_driver_ids,
    driver_refusal,
    expand_template,
    harden_write_argv,
    home_env_names,
    interactive_driver_ids,
    is_home_default_dir,
    judgment_argv,
    lane_argv,
    lane_driver_ids,
    lane_io,
    lane_scrub_env,
    parse_driver_document,
    redirect_argv,
    require_interactive,
    role_clear_env,
    valid_driver_id,
)
from driver_fixtures import DECLARED_DRIVERS, LANE_DRIVERS, driver_document

pytestmark = pytest.mark.no_pg

SCRUB = ("SCRUB_ONE", "SCRUB_TWO")
SCRUB_PREFIX = ["env", "-u", "SCRUB_ONE", "-u", "SCRUB_TWO"]


def drivers() -> dict[str, DriverSpec]:
    return parse_driver_document(driver_document()).drivers


def spec(driver_id: str) -> DriverSpec:
    return drivers()[driver_id]


# --- document -------------------------------------------------------------------


def test_placeholder_document_parses_every_section() -> None:
    config = parse_driver_document(driver_document())
    assert config.present is True
    assert lane_driver_ids(config.drivers) == LANE_DRIVERS
    assert declared_driver_ids(config.drivers) == DECLARED_DRIVERS
    assert config.recorded_slots == RecordedNames("driver-a", "driver-b")
    assert config.pane_driver == "driver-a"

    a = config.drivers["driver-a"]
    assert a.display_name == "Vendor A"
    assert a.home_env == "TOOL_A_HOME"
    assert a.lane is not None and a.lane.spec == "file-arg"
    assert a.lane.write_hardening == (("--no-shell",), ("--deny", "Shell"))
    assert a.interactive is not None
    assert a.interactive.session_env == "TOOL_A_SESSION_ID"
    assert a.interactive.pane.queued == (("queued", "send now"),)
    assert a.interactive.pane.working[0].search("WORKING...")
    assert a.usage is not None and a.usage.snapshot == "period"
    assert a.usage.pointer("tier") == "/plan/tier"
    assert a.usage.products is not None and a.usage.products.list == "/billing/products"
    assert a.usage.requests[0].headers == (("x-client", "agent-tests"),)
    assert a.usage.credential.entry_prefix == "https://auth.example.test::"

    b = config.drivers["driver-b"]
    assert b.lane is not None and b.lane.may_run_commands is True
    assert b.lane.argv_judgment is None
    assert b.interactive is None and b.usage is None

    d = config.drivers["driver-d"]
    assert d.lane is not None and d.lane.credential_file is not None
    assert d.lane.env == (
        ("TOOL_C_BASE_URL", "https://gateway.example.test/api"),
        ("TOOL_C_API_KEY", ""),
    )
    assert d.usage is not None and d.usage.snapshot == "spend"
    assert d.usage.credential.format == "text"
    assert d.usage.pointer("limit_reset") == "/key/data/limit_reset"

    e = config.drivers["driver-e"]
    assert e == DriverSpec("driver-e")


@pytest.mark.parametrize("raw", [{}, {"drivers": None}, {"drivers": {}}])
def test_empty_document_configures_no_driver(raw: dict[str, Any]) -> None:
    config = parse_driver_document(raw)
    assert config.drivers == {}
    assert config.recorded_slots is None and config.pane_driver is None
    assert config.present is True
    assert NO_DRIVER_CONFIGURATION.present is False


def test_driver_ids() -> None:
    for value in ("driver-a", "A1", "x_y", "a" * 32):
        assert valid_driver_id(value)
    for value in ("", "-a", "a b", "a" * 33, "first", "second", "a/b", None, 3):
        assert not valid_driver_id(value)


def _set(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Mutation that sets (or with ``...`` deletes) a dotted path."""

    def apply(document: dict[str, Any]) -> None:
        parts = path.split(".")
        target: Any = document
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        last = parts[-1]
        if value is ...:
            del target[last]
        elif isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value

    return apply


A = "drivers.driver-a"
B = "drivers.driver-b"
D = "drivers.driver-d"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_set("extra", {}), "drivers.json accepts only drivers, recorded_slots, pane_driver"),
        (_set("drivers", []), "drivers must be an object or null"),
        (_set(f"{A}.token", "x"), "drivers.driver-a accepts only display_name"),
        (_set(f"{A}.lane.timeout", 1), "drivers.driver-a.lane accepts only spec, output"),
        (_set(f"{A}.lane.argv.write", ...), "drivers.driver-a.lane.argv.write is required"),
        (_set(f"{A}.lane.spec", ...), "drivers.driver-a.lane.spec is required"),
        (_set(f"{A}.lane.spec", "pipe"), "drivers.driver-a.lane.spec must be one of stdin, file-arg"),
        (_set(f"{A}.lane.may_run_commands", "no"), "may_run_commands must be true or false"),
        (_set(f"{A}.lane.audit_stderr_when_empty", ...), "audit_stderr_when_empty is required"),
        (_set(f"{A}.lane.argv.read.6", "{x}"), "drivers.driver-a.lane.argv.read[6] uses unknown placeholder {x}"),
        (_set(f"{A}.lane.argv.read.6", "--cwd={cwd}"), "read[6] may use a placeholder only as the whole element"),
        (_set(f"{A}.lane.argv.read.6", "a{b"), "read[6] may use a placeholder only as the whole element"),
        (_set(f"{A}.lane.argv.read.4", "fixed-model"), "drivers.driver-a.lane.argv.read must use {model}"),
        (_set(f"{A}.lane.argv.write.2", "spec.md"), "drivers.driver-a.lane.argv.write must use {spec_file}"),
        (_set(f"{A}.lane.argv.read.7", "{output_file}"), "uses {output_file}, which only lane.output last-message-file provides"),
        (_set(f"{A}.lane.argv.read.0", "tool\nx"), "read[0] must be a single-line string"),
        (_set(f"{A}.lane.argv.read", []), "argv.read must be a list of 1 to 200 entries"),
        (_set(f"{B}.lane.argv.read.9", "out.txt"), "drivers.driver-b.lane.argv.read must use {output_file}"),
        (_set(f"{A}.display_name", ...), "drivers.driver-a.display_name is required for a driver with a lane section"),
        (_set(f"{A}.home_env", "tool_home"), "home_env must be an environment variable name"),
        (_set(f"{A}.credential_env", ["OK", "not ok"]), "credential_env[1] must be an environment variable name"),
        (_set(f"{A}.lane.write_hardening", [[]]), "write_hardening[0] must not be empty"),
        (_set("drivers.driver-c.lane.read_dir_args", ["--add-dir"]), "read_dir_args must use {dir}"),
        (_set("drivers.driver-c.lane.read_dir_args", ["{dir}", "{dir}"]), "read_dir_args must use {dir} exactly once"),
        (_set("drivers.driver-c.lane.read_dir_args", ["{model}"]), "read_dir_args[0] uses unknown placeholder {model}"),
        (_set("drivers.driver-c.home_default_dir", "relative/dir"), "home_default_dir must be absolute or start with ~/"),
        (_set("drivers.driver-c.home_default_dir", "/a/../b"), "home_default_dir must not contain parent traversal"),
        (_set(f"{D}.home_env", ...), "drivers.driver-d.lane.credential_file requires home_env"),
        (_set(f"{D}.lane.credential_file.file", "../key"), "credential_file.file must be a plain file name"),
        (_set(f"{D}.lane.credential_file.extra", "x"), "credential_file accepts only file, env"),
        (_set(f"{D}.lane.env", {"lower": "x"}), "lane.env key must be an environment variable name"),
        (_set(f"{B}.lane.credential_file", {"file": "k", "env": "K"}), "credential_file requires lane.output event-stream"),
        (_set(f"{A}.interactive.pane", ...), "drivers.driver-a.interactive.pane is required"),
        (_set(f"{A}.interactive.session_id", "ulid"), "interactive.session_id must be one of uuid"),
        (_set(f"{A}.interactive.argv_new.2", "fixed"), "interactive.argv_new must use {session_id}"),
        (_set(f"{A}.interactive.argv_resume.2", "{spec_file}"), "argv_resume[2] uses unknown placeholder {spec_file}"),
        (_set(f"{A}.interactive.session_env", "x-y"), "interactive.session_env must be an environment variable name"),
        (_set(f"{A}.interactive.pane.working", ["("]), "pane.working[0] is not a valid regular expression"),
        (_set(f"{A}.interactive.pane.idle", ["x" * 501]), "pane.idle[0] must be a non-empty single-line string of at most 500"),
        (_set(f"{A}.interactive.pane.permission", ["x"] * 21), "pane.permission must be a list of 1 to 20 entries"),
        (_set(f"{A}.interactive.pane.chrome", []), "pane.chrome must be a list of 1 to 50 entries"),
        (_set(f"{A}.interactive.pane.queued", [[]]), "pane.queued[0] must be a list of 1 to 50 entries"),
        (_set(f"{A}.interactive.pane.blink", ["x"]), "interactive.pane accepts only working"),
        (_set(f"{A}.usage.requests.0.url", "http://usage.example.test"), "requests[0].url must be an https:// URL"),
        (_set(f"{A}.usage.requests.1.name", "billing"), "requests[1].name repeats request billing"),
        (_set(f"{A}.usage.requests.0.headers", {"bad header": "x"}), "requests[0].headers has an invalid header name"),
        (_set(f"{A}.usage.requests.0.headers", {"authorization": "x"}), "requests[0].headers.authorization is set by the usage adapter itself"),
        (_set(f"{D}.usage.requests.0.headers", {"Accept": "text/plain"}), "requests[0].headers.Accept is set by the usage adapter itself"),
        (_set(f"{A}.usage.fields.tier", "/other/tier"), "fields.tier must start with the name of a configured request"),
        (_set(f"{A}.usage.fields.tier", "plan/tier"), "fields.tier must be a JSON pointer"),
        (_set(f"{A}.usage.fields.tier", ...), "usage.fields.tier is required"),
        (_set(f"{A}.usage.fields.usage_usd", "/plan/x"), "usage.fields accepts only tier"),
        (_set(f"{A}.usage.fields.products.product", "name"), "products.product must be a JSON pointer"),
        (_set(f"{A}.usage.credential.format", "text"), "credential.format must be json-entry for a period snapshot"),
        (_set(f"{A}.usage.credential.account", ...), "usage.credential.account is required"),
        (_set(f"{D}.usage.credential.format", "json-entry"), "credential.format must be text for a spend snapshot"),
        (_set(f"{D}.usage.proxy_from_environment", ...), "usage.proxy_from_environment is required"),
        (_set(f"{D}.usage.snapshot", "monthly"), "usage.snapshot must be one of period, spend"),
        (_set("pane_driver", "driver-b"), "pane_driver driver-b must name a driver with an interactive section"),
        (_set("pane_driver", "first"), "pane_driver must be a driver id"),
        (_set("recorded_slots.second", "driver-a"), "recorded_slots.first and recorded_slots.second must differ"),
        (_set("recorded_slots.first", "second"), "recorded_slots.first must be a driver id"),
        (_set("recorded_slots.third", "driver-c"), "recorded_slots accepts only first, second"),
    ],
)
def test_invalid_documents_fail_with_their_path(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    document = driver_document()
    mutate(document)
    with pytest.raises(DriverConfigError) as excinfo:
        parse_driver_document(document)
    assert message in str(excinfo.value)


@pytest.mark.parametrize("driver_id", ["first", "bad id", "x" * 33])
def test_invalid_driver_ids_are_rejected(driver_id: str) -> None:
    document = driver_document()
    document["drivers"][driver_id] = {}
    with pytest.raises(DriverConfigError, match="drivers key must be a driver id"):
        parse_driver_document(document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            _set(f"{A}.credential_env", ["TOOL_A_API_KEY", "TOOL_B_HOME"]),
            "drivers.driver-a.credential_env[1] names home_env TOOL_B_HOME of driver driver-b",
        ),
        (
            _set(f"{A}.credential_env", ["TOOL_A_HOME"]),
            "drivers.driver-a.credential_env[0] names home_env TOOL_A_HOME of driver driver-a",
        ),
        (
            _set(f"{D}.lane.env", {"TOOL_C_CONFIG": "/elsewhere"}),
            "drivers.driver-d.lane.env.TOOL_C_CONFIG names home_env TOOL_C_CONFIG of driver driver-c",
        ),
        (
            _set(f"{D}.lane.credential_file.env", "TOOL_A_HOME"),
            "drivers.driver-d.lane.credential_file.env names home_env TOOL_A_HOME of driver driver-a",
        ),
        (
            _set(f"{B}.home_env", "HTTPS_PROXY"),
            "drivers.driver-b.home_env HTTPS_PROXY is a network override that every lane clears",
        ),
    ],
)
def test_a_variable_that_clears_or_replaces_a_home_variable_is_refused(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    """The role prefix sets home_env before the lane scrub and lane env run."""
    document = driver_document()
    mutate(document)
    with pytest.raises(DriverConfigError) as excinfo:
        parse_driver_document(document)
    assert str(excinfo.value) == message


def test_recorded_names_derive_stored_names() -> None:
    names = RecordedNames("driver-a", "driver-b")
    assert names.stage_slots() == {"driver-a-pr": "first", "driver-b-pr": "second"}
    assert names.key_slots() == {
        "driver-a_pr_quality": ("first", "quality"),
        "driver-a_pr_logic": ("first", "logic"),
        "driver-b_pr_quality": ("second", "quality"),
        "driver-b_pr_logic": ("second", "logic"),
    }
    assert names.phase_slots() == {
        "pr_gates_driver-a": "first",
        "pr_gates_driver-b": "second",
    }


# --- templates and lane argv --------------------------------------------------------


def test_expand_template_substitutes_whole_elements_only() -> None:
    assert expand_template(["x", "{model}", "", "{cwd}"], {"model": "m", "cwd": "/w"}) == [
        "x",
        "m",
        "",
        "/w",
    ]
    with pytest.raises(SystemExit, match="needs a value for {output_file}"):
        expand_template(["{output_file}"], {"output_file": None})


def test_lane_argv_for_a_file_argument_driver() -> None:
    a = spec("driver-a")
    read = lane_argv(a, "read", scrub=SCRUB, spec_file="/s.md", cwd="/w", model="m")
    assert read == [
        *SCRUB_PREFIX,
        "tool-a", "--prompt-file", "/s.md", "-m", "m", "--read-only", "--cwd", "/w",
    ]
    # A driver without read_dir_args ignores extra directories, also when writing.
    write = lane_argv(
        a, "write", scrub=SCRUB, spec_file="/s.md", cwd="/w", model="m", read_dirs=("/x",)
    )
    assert write == [
        *SCRUB_PREFIX,
        "tool-a", "--prompt-file", "/s.md", "-m", "m", "--edit", "--cwd", "/w",
    ]
    with pytest.raises(SystemExit, match="lane mode must be read\\|write"):
        lane_argv(a, "judge", scrub=(), spec_file="/s.md", cwd="/w", model="m")


def test_lane_argv_for_a_last_message_file_driver() -> None:
    b = spec("driver-b")
    assert lane_argv(
        b, "write", scrub=(), spec_file="/s.md", cwd="/w", model="m", output_file="/o.txt"
    ) == [
        "env",
        "tool-b", "exec", "--model", "m", "--sandbox", "workspace-write", "--cd", "/w",
        "--last-message", "/o.txt", "-",
    ]
    with pytest.raises(SystemExit, match="^driver driver-b requires an output file$"):
        lane_argv(b, "read", scrub=(), spec_file="/s.md", cwd="/w", model="m")


def test_lane_argv_read_directories() -> None:
    c = spec("driver-c")
    assert lane_argv(
        c, "read", scrub=SCRUB, spec_file="/s.md", cwd="/w", model="m",
        read_dirs=("/one", "/two"),
    ) == [
        *SCRUB_PREFIX,
        "tool-c", "--print", "--model", "m", "--tools", "Read", "--mode", "default",
        "--add-dir", "/one", "--add-dir", "/two",
    ]
    with pytest.raises(SystemExit, match="write lane takes no extra directories"):
        lane_argv(
            c, "write", scrub=SCRUB, spec_file="/s.md", cwd="/w", model="m",
            read_dirs=("/one",),
        )


def test_lane_argv_with_key_file_and_fixed_environment() -> None:
    d = spec("driver-d")
    argv = lane_argv(
        d, "read", scrub=SCRUB, spec_file="/s.md", cwd="/w", model="vendor/model",
        output_file="/o.jsonl", read_dirs=("/control",),
    )
    assert argv == [
        *SCRUB_PREFIX,
        "TOOL_C_BASE_URL=https://gateway.example.test/api",
        "TOOL_C_API_KEY=",
        "sh", "-c", credential_wrapper(d), "sh", "/o.jsonl",
        "tool-c", "--print", "--model", "vendor/model", "--tools", "Read", "--mode",
        "default", "--add-dir", "/control",
    ]
    with pytest.raises(SystemExit, match="driver driver-d requires an output file"):
        lane_argv(d, "read", scrub=(), spec_file="/s.md", cwd="/w", model="m")


def test_judgment_argv() -> None:
    assert judgment_argv(
        spec("driver-a"), scrub=SCRUB, spec_file="/s.md", cwd="/w", model="m"
    ) == [
        *SCRUB_PREFIX,
        "tool-a", "--prompt-file", "/s.md", "-m", "m", "--no-tools", "--cwd", "/w",
    ]
    assert judgment_argv(
        spec("driver-c"), scrub=(), spec_file="/s.md", cwd="/w", model="m"
    )[-4:] == ["--tools", "", "--mode", "default"]
    d = judgment_argv(
        spec("driver-d"), scrub=(), spec_file="/s.md", cwd="/w", model="m",
        output_file="/o.jsonl",
    )
    assert d[d.index("sh") : d.index("sh") + 5] == [
        "sh", "-c", credential_wrapper(spec("driver-d")), "sh", "/o.jsonl",
    ]
    with pytest.raises(SystemExit) as excinfo:
        judgment_argv(spec("driver-b"), scrub=(), spec_file="/s.md", cwd="/w", model="m")
    assert str(excinfo.value) == JUDGMENT_UNSUPPORTED.format(driver="driver-b")
    with pytest.raises(SystemExit) as no_lane:
        lane_argv(spec("driver-e"), "read", scrub=(), spec_file="/s.md", cwd="/w", model="m")
    assert str(no_lane.value) == (
        "vendor must be a driver with a lane section "
        "(see drivers.json, docs/ai-accounts.md#drivers)"
    )


def test_require_interactive_refuses_a_driver_without_interactive_section() -> None:
    drivers = parse_driver_document(driver_document()).drivers
    assert require_interactive(drivers["driver-a"]).session_env == "TOOL_A_SESSION_ID"
    for driver in ("driver-b", "driver-e"):
        with pytest.raises(SystemExit) as excinfo:
            require_interactive(drivers[driver])
        assert str(excinfo.value) == DRIVER_REFUSED.format(
            name="provider", choices="a driver with an interactive section"
        )


def test_driver_refusal_reuses_the_vendor_refusal() -> None:
    config = parse_driver_document(driver_document())
    lanes = lane_driver_ids(config.drivers)
    assert interactive_driver_ids(config.drivers) == ("driver-a",)
    assert driver_refusal(config, "driver-a", lanes) is None
    assert driver_refusal(config, "driver-e", lanes) == (
        "vendor must be driver-a|driver-b|driver-c|driver-d "
        "(see drivers.json, docs/ai-accounts.md#drivers)"
    )
    assert driver_refusal(
        config, "driver-c", interactive_driver_ids(config.drivers), name="provider"
    ) == "provider must be driver-a (see drivers.json, docs/ai-accounts.md#drivers)"
    empty = parse_driver_document({})
    assert driver_refusal(empty, "driver-a", ()) == (
        "vendor must be (no driver configured) "
        "(see drivers.json, docs/ai-accounts.md#drivers)"
    )
    assert driver_refusal(NO_DRIVER_CONFIGURATION, "driver-a", ()) == DRIVERS_NOT_CONFIGURED


# --- derived lane I/O -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("driver_id", "tmux", "output_file", "stdin", "redirected"),
    [
        ("driver-a", False, False, False, None),
        ("driver-a", True, True, False, ["sh", "-c", 'out=$1; shift; exec "$@" > "$out"', "sh", "/o"]),
        ("driver-b", False, True, True, None),
        # The last-message-file driver writes its own file; the spec is typed.
        ("driver-b", True, True, True, None),
        ("driver-c", False, False, True, None),
        (
            "driver-c",
            True,
            True,
            False,
            [
                "sh", "-c", 'spec=$1; out=$2; shift 2; exec "$@" < "$spec" > "$out"',
                "sh", "/s.md", "/o",
            ],
        ),
        ("driver-d", False, True, True, None),
        ("driver-d", True, True, False, ["sh", "-c", 'f=$1; shift; exec "$@" < "$f"', "sh", "/s.md"]),
    ],
)
def test_lane_io_rules(
    driver_id: str,
    tmux: bool,
    output_file: bool,
    stdin: bool,
    redirected: list[str] | None,
) -> None:
    io = lane_io(spec(driver_id), tmux=tmux)
    assert io.output_file is output_file
    assert io.stdin is stdin
    assert io.output == spec(driver_id).lane.output  # type: ignore[union-attr]
    wrapped = redirect_argv(io, ["tool", "x"], spec_file="/s.md", output_file="/o")
    if redirected is None:
        assert io.redirect is None
        assert wrapped == ["tool", "x"]
    else:
        assert wrapped == [*redirected, "tool", "x"]


def test_lane_io_reports_the_stderr_audit_flag() -> None:
    assert lane_io(spec("driver-c"), tmux=False).audit_stderr_when_empty is True
    assert lane_io(spec("driver-a"), tmux=False).audit_stderr_when_empty is False
    io = lane_io(spec("driver-c"), tmux=True)
    with pytest.raises(SystemExit, match="tmux lane redirect needs output_file"):
        redirect_argv(io, ["tool"], spec_file="/s.md", output_file=None)


def _run_wrapper(
    tmp_path: Path, env: dict[str, str], out: Path
) -> subprocess.CompletedProcess[str]:
    child = ["sh", "-c", 'printf "%s|%s" "$TOOL_C_TOKEN" "$1"', "sh", "arg"]
    return subprocess.run(
        ["sh", "-c", credential_wrapper(spec("driver-d")), "sh", str(out), *child],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", **env},
        cwd=tmp_path,
    )


def test_credential_wrapper_exports_the_key_file_only_after_launch(tmp_path: Path) -> None:
    text = credential_wrapper(spec("driver-d"))
    assert "${TOOL_C_CONFIG:+$TOOL_C_CONFIG/tool-d-key}" in text
    assert 'export TOOL_C_TOKEN="$key"' in text
    config = tmp_path / "config"
    config.mkdir()
    out = tmp_path / "out.txt"

    missing = _run_wrapper(tmp_path, {"TOOL_C_CONFIG": str(config)}, out)
    assert missing.returncode == 2
    assert missing.stderr == "STATUS: unavailable\ndriver-d key missing\n"
    unset = _run_wrapper(tmp_path, {}, out)
    assert unset.returncode == 2 and "driver-d key missing" in unset.stderr

    (config / "tool-d-key").write_text("", encoding="utf-8")
    empty = _run_wrapper(tmp_path, {"TOOL_C_CONFIG": str(config)}, out)
    assert empty.returncode == 2 and "driver-d key missing" in empty.stderr

    (config / "tool-d-key").write_text("key-value", encoding="utf-8")
    present = _run_wrapper(tmp_path, {"TOOL_C_CONFIG": str(config)}, out)
    assert present.returncode == 0, present.stderr
    assert out.read_text(encoding="utf-8") == "key-value|arg"
    with pytest.raises(SystemExit, match="driver driver-c has no lane.credential_file"):
        credential_wrapper(spec("driver-c"))


# --- hardening, scrubbing, overlap -------------------------------------------------------


def test_harden_write_argv_appends_only_missing_groups() -> None:
    a = spec("driver-a")
    assert harden_write_argv(["tool-a"], a) == ["tool-a", "--no-shell", "--deny", "Shell"]
    present = ["tool-a", "--deny", "Shell", "--no-shell"]
    assert harden_write_argv(present, a) == present
    # A split group is not the configured group.
    split = ["tool-a", "--deny", "Edit", "Shell", "--no-shell"]
    assert harden_write_argv(split, a) == [*split, "--deny", "Shell"]
    original = ["tool-c", "--print"]
    hardened = harden_write_argv(original, spec("driver-c"))
    assert hardened == original and hardened is not original


def test_scrub_lists_are_ordered_unions_of_configuration() -> None:
    configured = drivers()
    assert credential_scrub(configured) == (
        "TOOL_A_API_KEY",
        "TOOL_B_API_KEY",
        "TOOL_C_API_KEY",
        "TOOL_C_TOKEN",
        "TOOL_D_API_KEY",
    )
    assert home_env_names(configured) == ("TOOL_A_HOME", "TOOL_B_HOME", "TOOL_C_CONFIG")
    assert lane_scrub_env(configured) == (
        *credential_scrub(configured),
        *NETWORK_OVERRIDE_ENV,
    )
    assert role_clear_env(configured) == (
        *credential_scrub(configured),
        *NETWORK_OVERRIDE_ENV,
        *home_env_names(configured),
    )
    # The lane and pane list is the role list without the home variables. The
    # role prefix runs first, so the lane list never changes the child's
    # environment; it only repeats `-u` pairs in the argv.
    homes = set(home_env_names(configured))
    assert lane_scrub_env(configured) == tuple(
        key for key in role_clear_env(configured) if key not in homes
    )
    assert "HTTPS_PROXY" in NETWORK_OVERRIDE_ENV and "https_proxy" in NETWORK_OVERRIDE_ENV
    assert credential_scrub({}) == () and role_clear_env({}) == NETWORK_OVERRIDE_ENV


def test_credential_dir_overlap_error(tmp_path: Path) -> None:
    d = spec("driver-d")
    config = tmp_path / "config"
    work = tmp_path / "work"
    config.mkdir()
    work.mkdir()
    assert credential_dir_overlap_error(d, str(config), str(work)) is None
    assert (
        credential_dir_overlap_error(d, str(config), str(config / "inside"))
        == "driver-d worktree overlaps account config_dir"
    )
    assert (
        credential_dir_overlap_error(d, str(config), str(work), (str(tmp_path),))
        == "driver-d account config_dir overlaps read directory"
    )
    # Only a driver with a key file has a directory to protect.
    assert credential_dir_overlap_error(spec("driver-c"), str(config), str(config)) is None


def test_home_default_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    c = spec("driver-c")
    assert is_home_default_dir(c, str(tmp_path / ".tool-c"))
    assert not is_home_default_dir(c, str(tmp_path / "named-profile"))
    assert not is_home_default_dir(spec("driver-d"), str(tmp_path / ".tool-c"))


# --- documentation ----------------------------------------------------------------


def _drivers_section() -> str:
    doc_path, anchor = DRIVERS_DOC.split("#", 1)
    text = (Path(__file__).resolve().parents[1] / doc_path).read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    # Every error hint links this anchor, so the heading must exist exactly once.
    assert [h for h in headings if h[3:].strip().lower() == anchor] == ["## Drivers"]
    section = text.split("\n## Drivers\n", 1)[1]
    return section.split("\n## ", 1)[0]


def test_documented_example_is_a_valid_driver_document() -> None:
    section = _drivers_section()
    example = section.split("### Example", 1)[1]
    block = example.split("```json\n", 1)[1].split("```", 1)[0]
    config = parse_driver_document(json.loads(block))
    assert lane_driver_ids(config.drivers) == LANE_DRIVERS
    assert declared_driver_ids(config.drivers) == DECLARED_DRIVERS
    assert config.recorded_slots == RecordedNames("driver-a", "driver-b")
    assert config.pane_driver == "driver-a"


def test_every_driver_key_is_documented() -> None:
    section = _drivers_section()
    keys = {
        *DOCUMENT_KEYS,
        *DRIVER_KEYS,
        *(key for key in LANE_KEYS if key != "argv"),
        *INTERACTIVE_KEYS,
        *PANE_KEYS,
        *USAGE_KEYS,
        *PERIOD_FIELDS,
        *SPEND_FIELDS,
    }
    for key in sorted(keys):
        assert f"`{key}`" in section, key
    for key in LANE_ARGV_KEYS:
        assert f"`argv.{key}`" in section, key
