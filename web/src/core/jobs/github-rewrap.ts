// Job: re-sealing the user's further GitHub identities (ADR-078 section 1, 06-web.md section 11). A `CK` rotation of
// an older browser leaves the non-primary `github` records under their old epoch; this job re-seals every own
// non-primary record whose epoch is older than the newest own `CK` under it (`rewrapOwnIdentities`: same ID,
// version + 1, author the user), so that devices entitled after the rotation can read them and a device that lost
// eligibility gets no newer version. Like the credential re-wrap it acts only in the queue of the first verified org
// (`CK` belongs to the user) and runs serialized with it (`exclusive`). Company records are not touched: no browser
// can open them (key model KM-35). The identity module is imported when a run needs it (not part of the first load,
// 06-web.md section 12.2).
import type { RequestsContext } from '../requests/client';
import { exclusive, verifiedLogs } from './credential-rewrap';
import { current, type JobContext, type JobRegistration, type JobRun } from './index';

/** One run (module comment); returns how many records were re-sealed (null when this queue does not act). */
export async function rewrapGithub(context: JobContext, run: JobRun): Promise<number | null> {
  if (verifiedLogs(context.store)[0]?.state.org !== run.org) return null;
  const requests: RequestsContext = { store: context.store, client: context.client, now: context.now };
  return exclusive(context.store, async () => {
    if (!current(context, run)) return 0;
    // loaded when the job runs, so the identity code stays out of the first load (06-web.md section 12.2)
    return import('../github/identities').then((identities) => identities.rewrapOwnIdentities(requests));
  });
}

export const job: JobRegistration = {
  name: 'github-rewrap',
  order: 36,
  triggers: ['login', 'interval'],
  run: async (context: JobContext, run: JobRun) => {
    await rewrapGithub(context, run);
  },
};
