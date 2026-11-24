// The user's own display name for a new membership (KOR-36): the hub cannot know it (it is sealed per org in the
// user's `member.profile`, ADR-003), so the unlocked browser reads it from the user's orgs – the given order (the
// landing org first), the first verified own `member.profile` wins; profiles not in the store yet are pulled once.
// '' if no org has one (no org, nothing readable). A name an admin gave the user is never the user's own.
//
// `ensureOwnName` writes the own name into an org the user opens without having one there (ADR-073): no dialog, no
// question – a display name is no access credential. It writes only after the org's profiles were pulled completely
// (a failed pull writes nothing), only with the current org key, and at most once per org and sign-in.
import { memberProfile } from './member-profile';
import { ORG } from '../log/state';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { actorOf, keyRing } from '../org/batch';
import { selectOwnName } from '../org/members';
import { PROFILE_KINDS } from '../tasks/directory';
import { pullOnce, type TasksContext } from '../tasks/pull';

export async function ownDisplayName(context: TasksContext, orgs: readonly string[], userId: string): Promise<string> {
  for (const org of orgs) {
    const read = () => selectOwnName(org, userId)(context.store.getState());
    if (read() === '') await pullOnce(context, org, org, { kinds: PROFILE_KINDS }).catch(() => 0);
    const name = read();
    if (name !== '') return name;
  }
  return '';
}

/** What `ensureOwnName` did. */
export type OwnNameResult = 'present' | 'written' | 'unknown' | 'no-key';

const attempted = new WeakMap<object, Set<string>>();

/**
 * Writes the user's own name into `org` if it has none there (module comment). `orgs`: the user's orgs, the own name
 * is taken from the first other one that has it. Throws the pull's or the hub's error.
 */
export async function ensureOwnName(
  context: TasksContext,
  org: string,
  orgs: readonly string[],
): Promise<OwnNameResult> {
  const actor = actorOf(context.store, org);
  await pullOnce(context, org, org, { kinds: PROFILE_KINDS });
  if (selectOwnName(org, actor.userId)(context.store.getState()) !== '') return 'present';
  const name = await ownDisplayName(
    context,
    orgs.filter((other) => other !== org),
    actor.userId,
  );
  if (name === '') return 'unknown';
  const state = actor.log.state;
  const epoch = state.epoch(ORG, org);
  const orgKey = keyRing(context.store, org)(ORG, org, epoch);
  if (orgKey === null) return 'no-key';
  const event = await memberProfile(org, epoch, orgKey, actor, name, actor.log.head.seq, context.now());
  const answer = await context.client.request('POST', '/v1/events', { body: { events: [{ event }] } });
  const result = isRecord(answer) && Array.isArray(answer.results) ? answer.results[0] : null;
  if (!isRecord(result) || result.scope_id !== org || !Number.isSafeInteger(result.scope_seq)) {
    throw new HubError('unavailable', 200);
  }
  await context.ingest(org, org, [{ scope_seq: result.scope_seq as number, received_at: null, event }]);
  return 'written';
}

/** `ensureOwnName` at most once per org and store (a sign-in) once it succeeded; a failure is tried at the next open. */
export function ensureOwnNameOnce(context: TasksContext, org: string, orgs: readonly string[]): Promise<unknown> {
  let done = attempted.get(context.store);
  if (done === undefined) {
    done = new Set();
    attempted.set(context.store, done);
  }
  const key = `${context.store.getState().me.userId} ${org}`;
  if (done.has(key)) return Promise.resolve(null);
  done.add(key);
  // a failure is tried again at the next open (ADR-073 §3)
  return ensureOwnName(context, org, orgs).catch(() => {
    done!.delete(key);
    return null;
  });
}
