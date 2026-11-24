"""KOR-01 / AUD-01 A-02 for org invitations (ADR-043): a member reads the open invitation's `invite` entry in the
membership log, but without the code cannot register through it, so it can neither take nor block it; the invitee,
who holds the code, registers and accepts."""

from __future__ import annotations

from tests.platform.hub.auth.authenticator import FakeBrowser, new_invite
from tests.platform.hub.invites.support import accept, create_invitation, registered_actor
from tests.platform.hub.orgs.support import World


def test_a_member_who_reads_the_membership_log_cannot_register_through_an_org_invitation(world: World) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft)
    assert invitation.response.status_code == 200, invitation.response.text
    # what every member reads in the membership log: the invitation's ID and public key
    body = invitation.entry["body"]
    assert (body["type"], body["invite_id"], body["pk_inv"]) == (
        "invite",
        invitation.invite_id,
        invitation.secrets.pk_inv,
    )
    stranger = FakeBrowser(world.hub)
    started = stranger.register_begin(body["invite_id"])
    assert started.status_code == 200, started.text
    begun = started.json()
    credential = stranger.authenticator.create(begun["options"])
    finish, *_ = stranger.registration_body(begun, credential, invite=new_invite())
    refused = world.hub.anon.post("/v1/passkeys/register/finish", json=finish)
    assert (refused.status_code, refused.json()) == (401, {"error": "signature_invalid"})
    assert world.rows("SELECT registered_user FROM hub.invite WHERE id = %s", invitation.invite_id) == [(None,)]
    # the invitee registers through the code and accepts
    actor, _account = registered_actor(FakeBrowser(world.hub), invitation.invite_id)
    world.hub.clock.advance(10)
    assert accept(draft, actor, invitation).status_code == 200
