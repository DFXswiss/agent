// The `member.profile` event of a member (04-crypto.md section 5.7). A module of its own so that the self-wraps job
// of the first load (`jobs/self-wraps.ts`) does not pull the join flow (`join.ts`).
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import type { Secret } from '../crypto/secret';
import { ORG } from '../log/state';
import type { Joiner } from './join';

/** The `member.profile` event of `joiner` in `org`, sealed with the org key of `epoch` at log head `lh`. */
export async function memberProfile(
  org: string,
  epoch: number,
  orgKey: Secret,
  joiner: Joiner,
  name: string,
  lh: number,
  at: number,
): Promise<unknown> {
  const header = newHeader({
    id: uuid7(at),
    org,
    scope: ORG,
    scope_id: org,
    epoch,
    author: joiner.userId,
    kind: 'member.profile',
    ts: formatTime(at),
    lh,
    ref: joiner.userId,
  });
  return sealEvent(orgKey, header, bodyJson({ display_name: name }), joiner.identity.sig.privateKey);
}
