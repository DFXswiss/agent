// A second user joins an org with an invitation code through the API (04-crypto.md sections 5.1, 5.2a; the screen of
// WEB-08 does the same later): lookup, open the package, verify the log from the lookup against the package's founder,
// append `accept` with both signatures and the self-wraps of the package keys, and the `member.profile` event – all as
// the signed-in page of that user (its own session key), with its identity opened from the recovery slot.
import { bodyJson } from '../../src/core/encoding/json';
import { uuid7 } from '../../src/core/encoding/ids';
import { formatTime } from '../../src/core/encoding/time';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { inviteSecrets, openPackage, parseCode } from '../../src/core/crypto/invite';
import { newHeader, sealEvent } from '../../src/core/crypto/event';
import { Secret } from '../../src/core/crypto/secret';
import { verifyPlatformLog } from '../../src/core/log/platform';
import { buildBatch } from '../../src/core/log/rotation';
import { epochKey, ORG } from '../../src/core/log/state';
import { verifyLog } from '../../src/core/log/verify';
import { compiledRootKey } from '../../build/manifest.ts';
import type { PageUser } from './user';

interface Lookup {
  readonly org_id: string;
  readonly package_ct: string;
  readonly log: unknown[];
}

/** `user` joins the org of the invitation `code` and publishes `displayName`; returns the org ID. */
export async function joinWithCode(user: PageUser, origin: string, code: string, displayName: string): Promise<string> {
  const secrets = await inviteSecrets(parseCode(code));
  const response = await fetch(new URL('/v1/invites/lookup', origin), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ invite_id: secrets.inviteId }),
  });
  if (response.status !== 200) throw new Error(`lookup: ${response.status} ${await response.text()}`);
  const lookup = (await response.json()) as Lookup;
  const pkg = (await openPackage(secrets, lookup.package_ct)) as {
    founder: string;
    keys: { scope: string; scope_id: string; epoch: number; key: string }[];
  };
  const platformBody = (await user.request('GET', '/v1/platform/log')) as { entries: { entry: unknown }[] };
  const platform = await verifyPlatformLog(
    platformBody.entries.map((item) => item.entry),
    compiledRootKey(),
  );
  const log = await verifyLog(lookup.log, pkg.founder, platform.state);
  const keys = new Map(pkg.keys.map((k) => [epochKey(k.scope, k.scope_id, k.epoch), Secret.of(b64uDecode(k.key))]));
  const ts = formatTime(Date.now());
  const built = await buildBatch({
    log,
    author: { id: user.userId, identity: user.identity, invite: secrets.signing.privateKey },
    entries: [
      {
        type: 'accept',
        invite_id: secrets.inviteId,
        subject: user.userId,
        kind: 'user',
        pub: user.identity.public(),
      },
    ],
    keys: (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null,
    platform: platform.state,
    ts,
  });
  const org = lookup.org_id;
  const epoch = built.log.state.epoch(ORG, org);
  const header = newHeader({
    id: uuid7(),
    org,
    scope: ORG,
    scope_id: org,
    epoch,
    author: user.userId,
    kind: 'member.profile',
    ts,
    lh: built.log.head.seq,
    ref: user.userId,
  });
  const profile = await sealEvent(
    keys.get(epochKey(ORG, org, epoch))!,
    header,
    bodyJson({ display_name: displayName }),
    user.identity.sig.privateKey,
  );
  await user.request('POST', `/v1/orgs/${org}/log`, { ...built.body, events: [profile] });
  return org;
}
