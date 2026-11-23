"""Discovery of the extension points (ADR-027 section 4), tested with throwaway packages, and the route declarations."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter

from agent_hub import registry
from agent_hub.http.routing import (
    EVENT_BATCH_BYTES,
    HubRouter,
    RouteSpec,
    make_spec,
    match_route,
    route_spec,
)
from agent_hub.registry import (
    CLI,
    DEVICE_STATE_SECTIONS,
    JOBS,
    KEY_RESOLVERS,
    OUTBOX_HANDLERS,
    PROJECTION_REBUILDERS,
    ROUTERS,
    SIGNAL_SUBSCRIBERS,
    ExtensionPoint,
    RegistryError,
    areas,
    callables,
    concat_sequences,
    discover,
    merge_mappings,
)


@pytest.fixture
def make_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a throwaway package `<name>` with areas and modules from a dict of file texts."""
    created: list[str] = []

    def build(name: str, files: dict[str, str]) -> str:
        root = tmp_path / name
        root.mkdir()
        (root / "__init__.py").write_text("")
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            init = path.parent / "__init__.py"
            if not init.exists():
                init.write_text("")
            path.write_text(textwrap.dedent(text))
        created.append(name)
        return name

    monkeypatch.syspath_prepend(str(tmp_path))
    yield build
    for module in list(sys.modules):
        if any(module == name or module.startswith(name + ".") for name in created):
            del sys.modules[module]


def test_extension_points_of_adr_027() -> None:
    assert (ROUTERS.module, ROUTERS.attribute) == ("routes", "router")
    assert (JOBS.module, JOBS.attribute) == ("jobs", "JOBS")
    assert (CLI.module, CLI.attribute) == ("cli", "register")
    assert (OUTBOX_HANDLERS.module, OUTBOX_HANDLERS.attribute) == ("outbox", "HANDLERS")
    assert (DEVICE_STATE_SECTIONS.module, DEVICE_STATE_SECTIONS.attribute) == ("device_state", "SECTION")
    assert (PROJECTION_REBUILDERS.module, PROJECTION_REBUILDERS.attribute) == ("projections", "REBUILDERS")
    assert (SIGNAL_SUBSCRIBERS.module, SIGNAL_SUBSCRIBERS.attribute) == ("signals", "SUBSCRIBERS")
    assert (KEY_RESOLVERS.module, KEY_RESOLVERS.attribute) == ("keys", "RESOLVERS")


def test_discovery_is_sorted_and_skips_modules_without_the_attribute(make_package: Any) -> None:
    package = make_package(
        "discover_sorted",
        {
            "zeta/outbox.py": "HANDLERS = {'z': 1}",
            "alpha/outbox.py": "HANDLERS = {'a': 1}",
            "mid/outbox.py": "OTHER = 1",
            "plain/other.py": "HANDLERS = {'p': 1}",
            "notapackage.py": "",
        },
    )
    assert areas(package) == ["alpha", "mid", "plain", "zeta"]
    assert discover(OUTBOX_HANDLERS, package) == [("alpha", {"a": 1}), ("zeta", {"z": 1})]
    assert merge_mappings(OUTBOX_HANDLERS, package) == {"a": 1, "z": 1}


def test_a_key_defined_twice_is_refused(make_package: Any) -> None:
    package = make_package(
        "discover_twice", {"a/outbox.py": "HANDLERS = {'event': 1}", "b/outbox.py": "HANDLERS = {'event': 2}"}
    )
    with pytest.raises(RegistryError, match="defined by a and b"):
        merge_mappings(OUTBOX_HANDLERS, package)


def test_wrong_types_are_refused(make_package: Any) -> None:
    package = make_package(
        "discover_types",
        {"a/outbox.py": "HANDLERS = [1]", "a/jobs.py": "JOBS = 'job'", "a/cli.py": "register = 1"},
    )
    with pytest.raises(RegistryError, match="must be a mapping"):
        merge_mappings(OUTBOX_HANDLERS, package)
    with pytest.raises(RegistryError, match="must be a sequence"):
        concat_sequences(JOBS, package)
    with pytest.raises(RegistryError, match="must be callable"):
        callables(CLI, package)
    other = make_package("discover_types2", {"a/jobs.py": "JOBS = 5", "b/jobs.py": "JOBS = {'x': 1}"})
    with pytest.raises(RegistryError, match="must be a sequence"):
        concat_sequences(ExtensionPoint("jobs", "JOBS"), other)


def test_sequences_and_callables(make_package: Any) -> None:
    package = make_package(
        "discover_seq",
        {
            "b/jobs.py": "JOBS = (3, 4)",
            "a/jobs.py": "JOBS = [1, 2]",
            "a/cli.py": "def register(subparsers):\n    return None\n",
        },
    )
    assert concat_sequences(JOBS, package) == [1, 2, 3, 4]
    [(area, register)] = callables(CLI, package)
    assert area == "a"
    assert register(None) is None


def test_the_hub_discovers_its_own_extensions() -> None:
    # Later areas add routers and jobs of their own; these are the ones of the core package.
    assert {"config_api", "health"} <= {area for area, _ in discover(ROUTERS)}
    assert "platform-root" in merge_mappings(KEY_RESOLVERS)
    assert {"db", "platform"} <= {area for area, _ in callables(CLI)}
    assert "request_nonce.purge" in [job.name for job in concat_sequences(JOBS)]
    assert registry.ROOT_PACKAGE == "agent_hub"


# Route declarations.


def test_make_spec_rules() -> None:
    spec = make_spec("GET", "/v1/me", auth="browser", right="user", read_resource="me", max_body=10, streaming=False)
    assert spec == RouteSpec(auth="browser", rights=("user",), read_resource="me", max_body=10, streaming=False)
    assert spec.signed
    anon = make_spec("POST", "/v1/x", auth="anon", right=["anon"], read_resource=None, max_body=0, streaming=False)
    assert not anon.signed
    cases: list[tuple[dict[str, Any], str]] = [
        ({"auth": "nobody"}, "unknown auth class"),
        ({"right": "superuser"}, "right must name"),
        ({"right": ()}, "right must name"),
        ({"max_body": EVENT_BATCH_BYTES + 1}, "max_body"),
        ({"max_body": -1}, "max_body"),
        ({"read_resource": None}, "must declare read_resource"),
        ({"read_resource": "no.such.resource"}, "not an audit action"),
        ({"read_resource": "Org"}, "not an audit action"),
        ({"read_resource": "org", "streaming": True}, "only an authenticated"),
        ({"read_resource": "org", "auth": "anon"}, "only an authenticated"),
    ]
    for change, message in cases:
        arguments: dict[str, Any] = {
            "auth": "browser",
            "right": "user",
            "read_resource": "me",
            "max_body": 10,
            "streaming": False,
            **change,
        }
        with pytest.raises(ValueError, match=message):
            make_spec("GET", "/v1/me", **arguments)
    with pytest.raises(ValueError, match="only an authenticated"):
        make_spec("POST", "/v1/me", auth="browser", right="user", read_resource="me", max_body=1, streaming=False)
    streaming = make_spec(
        "GET", "/v1/stream", auth="browser", right="user", read_resource=None, max_body=0, streaming=True
    )
    assert streaming.read_resource is None
    static = make_spec("GET", "/assets/x", auth="anon", right="anon", read_resource=None, max_body=0, streaming=False)
    assert static.auth == "anon"


def test_hub_router_verbs_and_websocket() -> None:
    router = HubRouter()

    @router.get("/v1/a", auth="browser", right="user", read_resource="me")
    async def get_a() -> None: ...

    @router.post("/v1/a", auth="browser", right="user")
    async def post_a() -> None: ...

    @router.put("/v1/a", auth="browser", right="user")
    async def put_a() -> None: ...

    @router.delete("/v1/a", auth="browser", right="user")
    async def delete_a() -> None: ...

    @router.hub_websocket("/v1/ws", auth="device", right="device")
    async def ws() -> None: ...

    specs = [route_spec(route) for route in router.routes]
    assert all(spec is not None for spec in specs)
    assert [getattr(route, "methods", None) for route in router.routes][:4] == [{"GET"}, {"POST"}, {"PUT"}, {"DELETE"}]
    websocket = specs[-1]
    assert websocket is not None
    assert websocket.streaming
    assert websocket.max_body == 0


def test_plain_routes_have_no_declaration() -> None:
    router = APIRouter()

    @router.get("/v1/plain")
    async def plain() -> None: ...

    assert route_spec(router.routes[0]) is None


def test_match_route() -> None:
    router = HubRouter()

    @router.get("/v1/items/{item}", auth="anon", right="anon")
    async def item(item: str) -> None: ...

    scope = {
        "type": "http",
        "path": "/v1/items/7",
        "method": "GET",
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    match = match_route([object(), *router.routes], scope)  # type: ignore[list-item]
    assert match.spec is not None
    assert match.path_params == {"item": "7"}
    assert match_route(router.routes, {**scope, "method": "POST"}).route is None
