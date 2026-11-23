// U-WEB-log (WEB-06): the batches of every member action against the rotation vectors of contracts/vectors/
// (`log-rotation.json`, CRY-02). The vector logs are verified, the actor's identity comes from the vector file, and
// the planner of the action (role, grant, ungrant, remove, leave, archive, new workgroup) builds its batch on the log
// prefix of each case: the requested entry must be the vector's entry byte for byte (deterministic Ed25519), the
// rotate set must be the vector's R, and the whole batch must pass the hub's rules (`checkBatch` with receipt time).
import { describe, expect, it } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { Identity } from '../../../src/core/crypto/keys';
import { parseTime } from '../../../src/core/encoding/time';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { buildBatch, checkBatch, SELF_OWED } from '../../../src/core/log/rotation';
import { verifyLog } from '../../../src/core/log/verify';
import type { Planner } from '../../../src/core/org/batch';
import { grantPlan, leavePlan, removePlan, rolePlan, ungrantPlan } from '../../../src/core/org/members';
import { archivePlan, createWorkgroupPlan } from '../../../src/core/org/workgroups';
import { VECTOR_FILES } from '../crypto/vector-suite';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- vector files are untyped JSON
type Any = any;

const file: Any = VECTOR_FILES['log-rotation'];
const byId = new Map<string, string>(Object.entries(file.identities).map(([name, v]: [string, Any]) => [v.id, name]));

async function identity(name: string): Promise<Identity> {
  const raw = file.identities[name];
  return Identity.fromRaw(b64uDecode(raw.kem_sk), b64uDecode(raw.sig_sk));
}

async function platform() {
  const ref = file.platform;
  return (await verifyPlatformLog(file.logs[ref.name].slice(0, ref.count), ref.root)).state;
}

/** The planner of the action a vector entry performs, or null for entries without a member action. */
function plannerOf(body: Any): Planner | null {
  switch (body.type) {
    case 'role':
      return rolePlan(body.subject, body.role);
    case 'grant':
      return grantPlan(body.subject, body.wg, body.access);
    case 'ungrant':
      return ungrantPlan(body.subject, body.wg);
    case 'remove':
      return body.subject === body.by ? leavePlan() : removePlan(body.subject);
    case 'wg_archive':
      return archivePlan(body.wg);
    case 'wg_create':
      return createWorkgroupPlan({ wg: body.wg, name: 'X', grants: {}, devices: [] }, () => 0);
    default:
      return null;
  }
}

const cases: Any[] = file.cases.filter((c: Any) => plannerOf(c.entry.body) !== null);

describe('member action batches against the rotation vectors', () => {
  it('covers every action type', () => {
    expect(new Set(cases.map((c) => c.entry.body.type))).toEqual(
      new Set(['role', 'grant', 'ungrant', 'remove', 'wg_archive', 'wg_create']),
    );
  });

  it.each(cases.map((c) => [c.name, c]))('%s', async (_name, c: Any) => {
    const body = c.entry.body;
    const platformState = await platform();
    const log = await verifyLog(file.logs[c.log.name].slice(0, c.log.count), file.founder, platformState);
    const plan = plannerOf(body)!(log, body.by)!;
    const author = { id: body.by, identity: await identity(byId.get(body.by)!) };
    const built = await buildBatch({
      log,
      author,
      entries: plan.entries,
      keys: () => null,
      platform: platformState,
      ts: body.ts,
    });
    expect(built.body.entries[0]).toEqual(c.entry);
    // a self-remove and a self-demotion owe their rotation to the others (`pending`, ADR-059 section 3)
    const self = SELF_OWED.has(body.type) && body.subject === body.by;
    expect(built.required).toEqual(self ? [] : c.required);
    expect(built.body.entries.slice(1).map((e) => e.body.type)).toEqual(self ? [] : c.required.map(() => 'rotate'));
    await expect(
      checkBatch(log, built.body.entries, built.body.wraps, body.by, {
        platform: platformState,
        now: parseTime(body.ts),
        platformAccess: true,
        excluded: platformState.excluded(log.state),
      }),
    ).resolves.toBeTruthy();
    if (self)
      expect(built.log.state.pendingScopes().map(([scope, id]) => ({ scope, scope_id: id }))).toEqual(c.required);
  });
});
