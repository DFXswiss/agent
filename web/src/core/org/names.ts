// Names of people (ADR-073): the name an admin gives a member or an invitation's person (`member.name`, org scope,
// `ref` = the member's user ID or the `invite_id`, sealed with the current org key: every member reads it, the hub
// does not), and the settling of invitations whose person was admitted directly. A member's own name always ranks
// first (`core/tasks/directory.ts`); an admin-given name is only shown until the member's client writes the own one.
import { uuid7 } from '../encoding/ids';
import { bodyJson } from '../encoding/json';
import { formatTime } from '../encoding/time';
import { newHeader, sealEvent } from '../crypto/event';
import { displayName } from '../account/genesis';
import { ORG, USER } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { MEMBER_NAME } from '../tasks/directory';
import type { TasksContext } from '../tasks/pull';
import { actorOf, keyRing, refuse } from './batch';
import { inviteStatuses, revokeInvite } from './invites';

/** Seals and sends one org-scope event of the signed-in admin and reads it into the store at once. */
async function sendName(context: TasksContext, org: string, ref: string, name: string): Promise<void> {
  const actor = actorOf(context.store, org);
  const state = actor.log.state;
  const epoch = state.epoch(ORG, org);
  const orgKey = keyRing(context.store, org)(ORG, org, epoch) ?? refuse('epoch_stale');
  const now = context.now();
  const header = newHeader({
    id: uuid7(now),
    org,
    scope: ORG,
    scope_id: org,
    epoch,
    author: actor.userId,
    kind: MEMBER_NAME,
    ts: formatTime(now),
    lh: actor.log.head.seq,
    ref,
  });
  const event = await sealEvent(orgKey, header, bodyJson({ display_name: name }), actor.identity.sig.privateKey);
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : null;
  if (!isRecord(result) || result.scope_id !== org || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  await context.ingest(org, org, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
}

/**
 * Gives `subject` – a user of the org (active or not) or a user invitation of its log – the name `input` (trimmed,
 * 1–80 characters, `params_invalid` otherwise). Admins only (`forbidden`); an unknown subject is `not_found`.
 */
export async function giveName(context: TasksContext, org: string, subject: string, input: string): Promise<void> {
  const name = displayName(input) ?? refuse('params_invalid');
  const actor = actorOf(context.store, org);
  const state = actor.log.state;
  if (!state.isAdmin(actor.userId)) refuse('forbidden');
  const known = state.principals.get(subject)?.kind === USER || state.invites.get(subject)?.kind === USER;
  if (!known) refuse('not_found');
  await sendName(context, org, subject, name);
}

/**
 * Revokes the open user invitations the hub names `subject` as the registered account of, once `subject` is a member
 * of the org (it was admitted directly, so the invitation is no longer needed); returns their IDs. The link between
 * invitation and account is the hub's word (ADR-073): a lying hub can make the admin revoke another open invitation –
 * a denial it can cause anyway by refusing that invitation. Admins only.
 */
export async function closeSettledInvites(context: TasksContext, org: string, subject: string): Promise<string[]> {
  const rows = await inviteStatuses(context, org);
  const settled = rows
    .filter((row) => row.status === 'settled' && row.acceptedBy === subject)
    .map((row) => row.inviteId);
  for (const id of settled) await revokeInvite(context, org, id);
  return settled;
}
