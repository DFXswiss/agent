// Test world of the direct admission (ADR-060): the org world of ./world.ts with a platform log whose root certifies
// the identities of ada, bob and cy (`user_certify` at seq 1, 2, 3), and a hub that answers the identity routes the
// browser calls (`GET /v1/users/{user}/identity`, `GET /v1/users/lookup?code=`, `GET /v1/me/invitees`). bob is not a
// member (the account to admit), cy is a member; the org has the workgroups one and two and the archived workgroup old.
import { accountCode } from '../../../src/core/crypto/identity';
import { ed25519KeyPair } from '../../../src/core/crypto/keys';
import { b64uEncode } from '../../../src/core/encoding/b64u';
import { buildPlatformEntries } from '../../../src/core/log/entries';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { HubError } from '../../../src/core/net/signedFetch';
import { setPlatform } from '../../../src/core/store/slices/orgs';
import { type Actor, idOf, seed, TS } from '../log/world';
import { orgWorld } from './world';

export interface IdentityAnswer {
  user_id: string;
  pub: { kem: string; sig: string };
  platform_seq: number;
  id_sig: string;
  account_code: string;
}

/** ada (admin, founder), cy (member); bob certified but not a member. */
export async function admitWorld(options: { as?: 'ada' | 'cy' } = {}) {
  const one = await idOf('wg one');
  const two = await idOf('wg two');
  const old = await idOf('wg old');
  const w = await orgWorld({
    as: options.as ?? 'ada',
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.batch(a.ada, [{ type: 'wg_create', wg: two, epoch: 1 }]);
      await world.batch(a.ada, [{ type: 'wg_create', wg: old, epoch: 1 }]);
      await world.batch(a.ada, [{ type: 'wg_archive', wg: old }]);
      await world.admitUser(a.ada, a.cy, 'member', {});
    },
  });
  const { ada, bob, cy } = w.others;
  const rootPair = await ed25519KeyPair(await seed('platform root'));
  const root = b64uEncode(rootPair.publicKey);
  const certified = [ada, bob, cy];
  const entries = await buildPlatformEntries(
    null,
    rootPair.privateKey,
    [
      { type: 'root', pub: root },
      ...certified.map((user) => ({ type: 'user_certify', user: user.id, pub: user.identity.public() })),
    ],
    TS,
  );
  const verified = await verifyPlatformLog(entries, root);
  setPlatform(w.store, verified);
  w.hub.platform = verified;
  w.world.platform = verified.state;
  const answers = new Map<string, IdentityAnswer>();
  for (const [index, user] of certified.entries()) {
    const pub = user.identity.public();
    answers.set(user.id, {
      user_id: user.id,
      pub,
      platform_seq: index + 1,
      id_sig: '',
      account_code: await accountCode(user.id, pub),
    });
  }
  const answer = (user: Actor) => answers.get(user.id)!;
  for (const user of certified) w.client.on(`GET /v1/users/${user.id}/identity`, () => answers.get(user.id));
  w.client.on('GET /v1/users/lookup', (_body, target) => {
    const code = new URL(target, 'https://hub.example').searchParams.get('code');
    const found = [...answers.values()].find((item) => item.account_code === code);
    if (found === undefined) throw new HubError('not_found', 404);
    return found;
  });
  const invitees: unknown[] = [];
  w.client.on('GET /v1/me/invitees', () => ({ invitees }));
  return { w, one, two, old, answers, answer, invitees };
}
