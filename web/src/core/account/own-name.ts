// The user's own display name for a new membership (KOR-36): the hub cannot know it (it is sealed per org in the
// user's `member.profile`, ADR-003), so the unlocked browser reads it from the user's orgs – the given order (the
// landing org first), the first verified own `member.profile` wins; profiles not in the store yet are pulled once.
// '' if no org has one (no org, nothing readable).
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
