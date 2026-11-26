// The release of workgroups to DFX support as the organization sees it (ADR-096; `GET /v1/orgs/{org}/support`, right
// `org:member`): the active releases and the current support persons with the display names the operator set. Who is
// support, and with which keys, comes from the verified platform log (`./names.ts`); the names only label them. A small
// module of its own: the notification center reads the names too.
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { type Access, type OrgRelease, setOrgSupport } from '../store/slices/support';
import type { TasksContext } from '../tasks/pull';
import { sendErrorCode } from '../tasks/send';

const isAccess = (value: unknown): value is Access => value === 'read' || value === 'write';

const isRelease = (value: unknown): value is OrgRelease =>
  isRecord(value) &&
  typeof value.wg_id === 'string' &&
  isAccess(value.access) &&
  Number.isSafeInteger(value.granted_seq) &&
  typeof value.granted_at === 'string';

const isPerson = (value: unknown): value is { user_id: string; name: string | null } =>
  isRecord(value) && typeof value.user_id === 'string' && (value.name === null || typeof value.name === 'string');

/** Loads the releases and support persons of `org` into the store; failures are recorded, never thrown. */
export async function loadOrgSupport(context: Pick<TasksContext, 'store' | 'client'>, org: string): Promise<void> {
  const { store } = context;
  const generation = store.generation;
  const known = store.getState().support.orgs[org];
  setOrgSupport(store, org, {
    status: 'loading',
    code: null,
    releases: known?.releases ?? [],
    people: known?.people ?? {},
  });
  try {
    const body = await context.client.request('GET', `/v1/orgs/${encodeURIComponent(org)}/support`);
    const valid =
      isRecord(body) &&
      Array.isArray(body.releases) &&
      body.releases.every(isRelease) &&
      Array.isArray(body.people) &&
      body.people.every(isPerson);
    if (!valid) throw new HubError('unavailable', 200);
    if (store.generation !== generation) return;
    const people = Object.fromEntries(
      (body.people as { user_id: string; name: string | null }[]).map((p) => [p.user_id, p.name]),
    );
    setOrgSupport(store, org, { status: 'ready', code: null, releases: body.releases as OrgRelease[], people });
  } catch (error) {
    if (store.generation !== generation) return;
    setOrgSupport(store, org, {
      status: 'error',
      code: sendErrorCode(error),
      releases: known?.releases ?? [],
      people: known?.people ?? {},
    });
  }
}
