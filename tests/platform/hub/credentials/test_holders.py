"""U-HUB-credentials (R-1.12, R-5.4, KOR-04): `GET /v1/me` names the devices holding a wrap of the user's current
credential key (`cred_holders`, 03-api.md section 4.1 as amended by KOR-04): device IDs only, of the newest `cred`
epoch, the user's own self-wrap left out, still listed after the user left the device's org - so that the user's
browser rotates the `CK` even when it never held that org's log (AUD-01 A-07)."""

from __future__ import annotations

from tests.platform.hub.credentials.support import CANARY, Desk
from tests.platform.hub.orgs.support import Actor


def _holders(user: Actor) -> object:
    response = user.browser.get("/v1/me")
    assert response.status_code == 200, response.text
    return response.json()["cred_holders"]


def test_the_holders_of_the_current_ck(desk: Desk) -> None:
    writer, device, other = desk.scene.writer, desk.scene.device, desk.scene.other_device
    assert _holders(writer) == {"epoch": 1, "devices": []}
    wraps = [desk.wrap(writer, writer, 1, recipient) for recipient in (writer, device, other)]
    assert writer.browser.post("/v1/wraps", json={"wraps": wraps}).status_code == 200
    assert _holders(writer) == {"epoch": 1, "devices": sorted([device.id, other.id])}
    # another user's wraps are not the writer's holders
    assert _holders(desk.scene.reader) == {"epoch": 1, "devices": []}
    # after a rotation only the recipients of the new epoch count
    desk.ck(writer, 2)
    assert writer.browser.post("/v1/wraps", json={"wraps": [desk.wrap(writer, writer, 2, other)]}).status_code == 200
    assert _holders(writer) == {"epoch": 2, "devices": [other.id]}
    assert CANARY not in str(writer.browser.get("/v1/me").json())


def test_a_user_without_a_ck_epoch(desk: Desk) -> None:
    newcomer = desk.world.user()
    assert _holders(newcomer) == {"epoch": None, "devices": []}


def test_holders_stay_listed_after_the_user_left_their_org(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    wraps = [desk.wrap(writer, writer, 1, recipient) for recipient in (writer, device)]
    assert writer.browser.post("/v1/wraps", json={"wraps": wraps}).status_code == 200
    scene = desk.scene
    draft, admin = scene.draft, scene.admin
    draft.add("remove", admin, subject=writer.id)
    draft.add("rotate", admin, scope="org", scope_id=scene.org, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=draft.default_wg, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=scene.wg, epoch=2)
    readers = [admin, scene.reader, scene.device, scene.other_device]
    wraps = (
        draft.wraps(admin, "org", scene.org, 2, readers)
        + draft.wraps(admin, "wg", draft.default_wg, 2, [admin, scene.reader])
        + draft.wraps(admin, "wg", scene.wg, 2, readers)
    )
    response = draft.post(admin, wraps)
    assert response.status_code == 200, response.text
    assert writer.browser.get("/v1/me").json()["orgs"] == []
    assert _holders(writer) == {"epoch": 1, "devices": [device.id]}


def test_a_removed_device_keeps_its_cred_wrap_listed(desk: Desk) -> None:
    """The browser relies on it: removing a device deletes its `org`/`wg` wraps, never the `cred` ones."""
    writer, device = desk.scene.writer, desk.scene.device
    wraps = [desk.wrap(writer, writer, 1, recipient) for recipient in (writer, device)]
    assert writer.browser.post("/v1/wraps", json={"wraps": wraps}).status_code == 200
    scene = desk.scene
    draft, admin = scene.draft, scene.admin
    draft.add("remove", admin, subject=device.id)
    draft.add("rotate", admin, scope="org", scope_id=scene.org, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=scene.wg, epoch=2)
    readers = [admin, scene.writer, scene.reader, scene.other_device]
    wraps = draft.wraps(admin, "org", scene.org, 2, readers) + draft.wraps(admin, "wg", scene.wg, 2, readers)
    response = draft.post(admin, wraps)
    assert response.status_code == 200, response.text
    assert desk.world.rows(
        "SELECT count(*) FROM hub.key_wrap WHERE recipient_id = %s AND scope <> 'cred'", device.id
    ) == [(0,)]
    assert _holders(writer) == {"epoch": 1, "devices": [device.id]}
