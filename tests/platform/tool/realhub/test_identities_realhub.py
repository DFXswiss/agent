"""GitHub identities of the agent tool against the real hub (ADR-078): a company account connected through a device,
sealed on the tool for the organization's devices and stored by the hub's own checks (admin, fresh epoch, W3 for every
wrap, every reader device covered), then the defaults behind the feature gate, the task's choice and the removal.
The device's part (fetching the request, answering it) is played through the hub's device routes."""

from __future__ import annotations

import json
from typing import Any

from agent_hub.crypto import request as request_format
from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.credential import open_credential
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.crypto.ids import uuid7
from tests.platform.hub.orgs.support import Actor

from .support import Person, RealHub, pair, person, run_ok

COMPANY = {
    "token": "ghp_company",
    "login": "acme-bot",
    "user_id": 8,
    "scopes": ["repo"],
    "created_at": "2026-11-24T08:00:00.000Z",
    "source": "token",
}


def answer(real: RealHub, device: Actor, who: Person, request: str, body: dict[str, Any]) -> None:
    """The device fetches the request and answers it finally with `body`, sealed to the user."""
    client = real.fleet.client(device)
    assert client.get(f"/v1/device/requests/{request}").status_code == 200
    header = {
        "v": 1,
        "id": uuid7(),
        "user": who.id,
        "device": device.id,
        "kind": "github.connect",
        "in_reply_to": request,
        "seq": 1,
        "final": True,
        "ts": format_time(real.harness.clock()),
        "enc_alg": request_format.ENC_ALG,
    }
    envelope = request_format.seal_response(
        header, who.actor.identity.kem_public(), json.dumps(body).encode(), device.identity.sig
    )
    response = client.post(f"/v1/device/requests/{request}/responses", json={"envelope": envelope})
    assert response.status_code == 200, response.text


def connect(real: RealHub, ada: Person, device: Actor, org: str, *extra: str) -> dict[str, Any]:
    started = run_ok(ada.tool, "github", "connect", "--org", org, *extra, "--wait", "0")
    assert (started["step"], started["device_id"]) == ("waiting", device.id)
    done = {"step": "done", "login": "acme-bot", "source": "token", "body": COMPANY}
    answer(real, device, ada, started["request_id"], done)
    return run_ok(ada.tool, "github", "connect", "--org", org, *extra, "--request", started["request_id"])


def device_reads(real: RealHub, device: Actor, org: str, identity: str, author: Person) -> object:
    """The company record as the org's device reads it: the record from its route, the key from its wrap."""
    served = real.fleet.client(device).get(f"/v1/device/github-identities/{identity}")
    assert served.status_code == 200, served.text
    record = served.json()["record"]
    epoch = record["header"]["epoch"]
    (wrap,) = real.fleet.rows(
        "SELECT wrap FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s AND epoch = %s AND recipient_id = %s",
        org,
        epoch,
        device.id,
    )[0]
    key = wrap_format.unwrap_key(
        device.identity.kem, scope="cred", scope_id=org, epoch=epoch, recipient=device.id, wrap=b64u_encode(wrap)
    )
    return json.loads(open_credential(key, record, author.actor.identity.sig_public()))


def test_company_account_defaults_and_task_choice(real: RealHub) -> None:
    ada = person(real, "Ada")
    pair(real, ada)
    org = str(run_ok(ada.tool, "org", "create", "--name", "Acme", "--display-name", "Ada")["org_id"])
    wg = str(run_ok(ada.tool, "wg", "create", "--org", org, "--name", "Research")["wg_id"])
    device, _seq = real.fleet.platform_device()
    real.fleet.platform.post({"type": "org_platform_access", "org": org, "granted": True})
    run_ok(ada.tool, "device", "assign", "--wg", wg)
    # connected through the device, sealed here for the org's device, stored by the hub's checks
    connected = connect(real, ada, device, org)
    identity = connected["identity_id"]
    assert (connected["status"], connected["owner"], connected["version"], connected["login"]) == (
        "connected",
        "org",
        1,
        "acme-bot",
    )
    assert "ghp_company" not in json.dumps(connected)
    assert device_reads(real, device, org, identity, ada) == COMPANY
    listed = run_ok(ada.tool, "github", "accounts")
    assert [(item["id"], item["status"], item["org_id"]) for item in listed["identities"]] == [
        (identity, "company", org)
    ]
    # a reconnect: same record, version 2, the next epoch
    again = connect(real, ada, device, org, "--identity", identity)
    assert (again["identity_id"], again["version"]) == (identity, 2)
    assert device_reads(real, device, org, identity, ada) == COMPANY
    # defaults wait for the runners of the org's devices
    code, refused = ada.tool.run("github", "default", "set", "--org", org, "--identity", identity)
    assert (code, refused["hub_error"], refused["devices"]) == (1, "peer_outdated", [device.id])
    real.fleet.execute("UPDATE hub.device SET features = %s WHERE id = %s", ["github.identities"], device.id)
    run_ok(ada.tool, "github", "default", "set", "--org", org, "--identity", identity)
    run_ok(ada.tool, "github", "default", "set", "--wg", wg, "--identity", identity)
    defaults = run_ok(ada.tool, "github", "accounts")["defaults"]
    assert sorted((item["scope"], item["scope_id"], item["identity_id"]) for item in defaults) == [
        ("org", org, identity),
        ("wg", wg, identity),
    ]
    run_ok(ada.tool, "github", "default", "set", "--wg", wg, "--none")
    # the task's choice at creation and later
    created = run_ok(ada.tool, "task", "create", "--wg", wg, "--text", "Open the PR", "--github-identity", identity)
    task = str(created["task_id"])
    assert created["github_identity"] == identity
    assert run_ok(ada.tool, "api", "GET", f"/v1/tasks/{task}")["answer"]["task"]["github_identity"] == identity
    run_ok(ada.tool, "task", "github-identity", "--task", task, "--none")
    assert run_ok(ada.tool, "api", "GET", f"/v1/tasks/{task}")["answer"]["task"]["github_identity"] is None
    code, forbidden = ada.tool.run("task", "github-identity", "--task", task, "--identity", uuid7())
    assert (code, forbidden["hub_error"]) == (1, "identity_forbidden")
    # removed with its defaults
    removed = run_ok(ada.tool, "github", "remove", "--identity", identity)
    assert (removed["status"], removed["owner_kind"]) == ("removed", "org")
    assert run_ok(ada.tool, "github", "accounts") == {"ok": True, "identities": [], "defaults": []}
