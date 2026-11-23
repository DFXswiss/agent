-- 0007_invite_pk_inv: the public invitation key of every invitation the hub can register a user through (KOR-01,
-- ADR-043).
--
-- `register/finish` of a first passkey requires `inv_sig`, the Ed25519 signature of `sk_inv` (derived from the code,
-- 04-crypto.md section 3) over the ceremony's challenge, so that knowing an `invite_id` – which the platform log and
-- the membership log show to their readers – no longer suffices to take an open invitation. The hub keeps `pk_inv`
-- of platform invitations here: from the root-signed `device_invite` / `user_invite` entries (projection) and from the
-- request of an inviter (`POST /v1/platform/invites`). Org invitations carry `pk_inv` in their membership-log
-- `invite` entry, which the hub reads at registration. Rows without a key (platform user invitations created before
-- this migration) cannot be registered through.

ALTER TABLE hub.invite ADD COLUMN pk_inv bytea NULL CHECK (octet_length(pk_inv) = 32);
