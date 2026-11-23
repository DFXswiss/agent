// CON-02 (log part, TypeScript side) and CON-03: the log vectors of contracts/vectors/ (`log*.json`,
// `platform-log.json`, CRY-02) checked against src/core/log. `LogContext` is the TypeScript twin of
// `agent_hub.log.vectors.Context`: it resolves the log references of a vector file (`{"log": {name, count}}`,
// `{"platform": {name, count, root}}`) and runs the operations the negative vectors name.
import { b64uDecode, b64uEncode } from '../../../src/core/encoding/b64u';
import { x25519KeyPair } from '../../../src/core/crypto/keys';
import { unwrapKey } from '../../../src/core/crypto/wrap';
import { parseTime } from '../../../src/core/encoding/time';
import { CryptoError } from '../../../src/core/crypto/errors';
import { Secret } from '../../../src/core/crypto/secret';
import { checkExtends, LOG_TYPES, PLATFORM_TYPES } from '../../../src/core/log/entries';
import * as catchup from '../../../src/core/log/catchup';
import * as events from '../../../src/core/log/events';
import * as platform from '../../../src/core/log/platform';
import * as rotation from '../../../src/core/log/rotation';
import * as verify from '../../../src/core/log/verify';
import * as wraps from '../../../src/core/log/wraps';
import { Mismatch, type Outcome, runChecks, same, VECTOR_FILES } from '../crypto/vector-suite';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- vector files are untyped JSON
type Any = any;

/** The log vector files (contracts/vectors/README.md, "Log vectors"). */
export const LOG_CONSUMED = ['log', 'log-events', 'log-rotation', 'log-wraps', 'platform-log'];

const time = (value: Any): number | null => (value ? parseTime(value) : null);

/** The logs of one vector file and the operations that resolve references into them. */
export class LogContext {
  readonly ops: Record<string, (a: Any) => Promise<unknown>>;

  constructor(readonly logs: Record<string, unknown[]>) {
    this.ops = {
      verify_log: async (a) => verify.verifyLog(this.entries(a), a.founder, await this.platform(a)),
      append_log: async (a) =>
        verify.appendEntries(await this.log(a), a.append, a.founder, {
          platform: await this.platform(a),
          now: time(a.now),
          platformAccess: a.platform_access ?? false,
        }),
      check_extends: async (a) => checkExtends(a.head, this.entries(a)),
      require_lh: async (a) => verify.requireLh(await this.log(a), a.lh),
      verify_platform_log: async (a) => platform.verifyPlatformLog(this.entries(a), a.root),
      append_platform_log: async (a) =>
        platform.appendPlatform(await platform.verifyPlatformLog(this.entries(a), a.root), a.append, a.root),
      check_batch: async (a) =>
        rotation.checkBatch(await this.log(a), a.append, a.wraps, a.wrapped_by, {
          platform: await this.platform(a),
          now: parseTime(a.now),
          platformAccess: a.platform_access ?? false,
          excluded: a.excluded ?? [],
        }),
      check_wrap: async (a) =>
        wraps.checkWrap(a.wrap, a.wrapped_by, await this.log(a), a.excluded ?? [], a.require_active ?? false),
      check_cred_wrap: async (a) =>
        wraps.checkCredWrap(a.wrap, a.wrapped_by, [await this.log(a)], a.current_epoch, a.excluded ?? []),
      check_event: async (a) =>
        events.checkEvent(a.event, await this.log(a), {
          sessionDevice: a.session_device ?? null,
          excluded: a.excluded ?? [],
        }),
      check_event_hub: async (a) =>
        events.checkEventHub(
          a.event,
          await this.log(a),
          parseTime(a.now),
          time(a.rotated_at),
          a.session_device ?? null,
          a.excluded ?? [],
        ),
      check_replay: async (a) => events.checkReplay(a.stored, a.incoming),
    };
  }

  /** The prefix `args.log` followed by `args.entries`. */
  entries(args: Any): unknown[] {
    return [...this.logs[args.log.name]!.slice(0, args.log.count), ...(args.entries ?? [])];
  }

  /** The verified platform state named by `args.platform`, if any. */
  async platform(args: Any): Promise<platform.PlatformState | null> {
    const ref = args.platform;
    if (!ref) return null;
    return (await platform.verifyPlatformLog(this.logs[ref.name]!.slice(0, ref.count), ref.root)).state;
  }

  /** The verified membership log of `args`. */
  async log(args: Any): Promise<verify.VerifiedLog> {
    return verify.verifyLog(this.entries(args), args.founder, await this.platform(args));
  }

  /** Runs `op(args)` and returns the code it fails with (null if it succeeds; other errors propagate). */
  async refusal(op: string, args: Any): Promise<string | null> {
    try {
      await this.ops[op]!(args);
    } catch (error) {
      if (error instanceof CryptoError) return error.code;
      throw error;
    }
    return null;
  }
}

export interface Check {
  readonly name: string;
  readonly run: () => Promise<void>;
}

const file = (name: string): Any => VECTOR_FILES[name];
const contextOf = (name: string) => new LogContext(file(name).logs);

/** One check per negative vector of every log file. */
export function logNegativeChecks(): Check[] {
  return LOG_CONSUMED.flatMap((name) =>
    (file(name).invalid as Any[]).map((vector) => ({
      name: `${name}: invalid ${vector.op}:${vector.name}`,
      run: async () => {
        const ctx = contextOf(name);
        if (!Object.hasOwn(ctx.ops, vector.op)) throw new Mismatch(`unknown op ${vector.op}`);
        same(await ctx.refusal(vector.op, vector.args), vector.error, `${vector.op}:${vector.name}`);
      },
    })),
  );
}

const slots = (items: Any[]) => items.map((item) => wraps.slotOf(item));

const POSITIVE: Record<string, (data: Any) => Promise<void>> = {
  log: async (data) => {
    const ctx = new LogContext(data.logs);
    const log = await ctx.log({ ...data.valid, founder: data.founder });
    same(
      log.states.map((s) => ({ seq: s.seq, hash: s.head })),
      data.valid.heads,
      'heads',
    );
    same(
      log.states.map((s) => s.projection()),
      data.valid.projections,
      'projections',
    );
    same(new Set(log.entries.map((entry) => entry.body.type)).size, LOG_TYPES.length, 'every entry type');
    for (const item of data.extends) await checkExtends(item.head, ctx.entries(item));
    for (const item of data.excluded) {
      const state = (await ctx.platform(item))!;
      same(state.excluded((await ctx.log({ ...item, founder: data.founder })).state), item.excluded, 'excluded');
    }
    const lhLog = await ctx.log({ log: data.lh.log, founder: data.founder, platform: data.valid.platform });
    for (const item of data.lh.needs_refresh) same(verify.needsRefresh(lhLog, item.lh), item.refresh, 'needs refresh');
  },
  'platform-log': async (data) => {
    const log = await platform.verifyPlatformLog(data.logs.platform, data.root);
    same(
      log.states.map((s) => ({ seq: s.seq, hash: s.head })),
      data.valid.heads,
      'platform heads',
    );
    same(
      log.states.map((s) => s.projection()),
      data.valid.projections,
      'platform projections',
    );
    same([...new Set(log.entries.map((e) => e.body.type))].sort(), [...PLATFORM_TYPES].sort(), 'platform types');
    for (const item of data.valid.platform_access) {
      same(log.states[item.count - 1]!.platformAccess(item.org, item.private), item.access, 'platform access');
    }
  },
  'log-rotation': async (data) => {
    const ctx = new LogContext(data.logs);
    const seen = new Set<string>();
    for (const item of data.cases) {
      const args = { log: item.log, founder: data.founder, platform: data.platform };
      const before = await ctx.log(args);
      const after = await verify.apply(before.state, item.entry, data.founder, { platform: await ctx.platform(args) });
      same(rotation.entrySubject(item.entry.body), item.subject, `subject ${item.name}`);
      same(rotation.asJson(rotation.requiredSet(before.state, after, item.subject)), item.required, item.name);
      seen.add(item.entry.body.type);
    }
    for (const type of ['remove', 'ungrant', 'device_unassign', 'accept', 'grant', 'role', 'rotate', 'wg_create']) {
      same(seen.has(type), true, `rotation case ${type}`);
    }
    for (const item of data.batches) {
      const result = (await ctx.ops.check_batch!(item.args)) as rotation.BatchResult;
      same(rotation.asJson(result.required), item.required, 'batch R');
      same(rotation.asJson(result.log.state.pending), item.pending, 'batch pending');
      same(result.log.head, item.head, 'batch head');
    }
  },
  'log-wraps': async (data) => {
    const ctx = new LogContext(data.logs);
    const kemOf = new Map(Object.values<Any>(data.identities).map((who) => [who.id, who.kem_sk]));
    let opened = 0;
    const opens = async (wrap: Any) => {
      const expected = data.scope_keys[`${wrap.scope}/${wrap.scope_id}/${wrap.epoch}`];
      if (expected === undefined) return; // the file lists the keys of the wraps it seals itself
      const target = { scope: wrap.scope, scope_id: wrap.scope_id, epoch: wrap.epoch, recipient: wrap.recipient_id };
      const key = await unwrapKey(await x25519KeyPair(b64uDecode(kemOf.get(wrap.recipient_id))), target, wrap.wrap);
      same(key.reveal(b64uEncode), expected, 'unwrapped key');
      opened += 1;
    };
    for (const item of data.valid) {
      await ctx.ops.check_wrap!(item.args);
      await opens(item.args.wrap);
    }
    for (const item of data.cred_valid) {
      await ctx.ops.check_cred_wrap!(item.args);
      await opens(item.args.wrap);
    }
    same(opened >= data.valid.length, true, 'wraps opened to the vector keys');
    const log = await ctx.log(data.valid[0].args);
    const existing = slots(data.catch_up.existing);
    for (const plan of data.catch_up.plans) {
      const missing = catchup.missingWraps(log, plan.wrapper, existing, plan.excluded);
      same(missing.map(wraps.slotJson), plan.missing, `catch-up ${plan.wrapper}`);
    }
    // admins reading by inheritance get every epoch (ADR-059 section 2); the browser confirms exactly these slots
    const adminLog = await ctx.log({ log: data.admin_catch_up.log, founder: data.founder });
    const adminExisting = slots(data.admin_catch_up.existing);
    for (const plan of data.admin_catch_up.plans) {
      const missing = catchup.missingWraps(adminLog, plan.wrapper, adminExisting, plan.excluded);
      same(missing.map(wraps.slotJson), plan.missing, `admin catch-up ${plan.wrapper}`);
      const offered = plan.missing.map(wraps.slotOf);
      const confirmed = catchup
        .confirmedSlots(adminLog, plan.wrapper, offered, plan.excluded)
        .concat(offered.filter((slot: wraps.WrapSlot) => catchup.compatSkip(adminLog.state, plan.wrapper, slot)))
        .sort(wraps.compareSlots);
      same(confirmed.map(wraps.slotJson), plan.missing, `admin catch-up confirmed ${plan.wrapper}`);
    }
    const credExisting = slots(data.cred_catch_up.existing);
    for (const plan of data.cred_catch_up.plans) {
      const missing = catchup.missingCredWraps([log], plan.owner, plan.wrapper, plan.current_epoch, credExisting);
      same(missing.map(wraps.slotJson), plan.missing, `cred catch-up ${plan.wrapper}`);
    }
  },
  'log-events': async (data) => {
    const ctx = new LogContext(data.logs);
    for (const item of data.valid) {
      const log = await ctx.log(item.args);
      const opened = await events.openCheckedEvent(item.args.event, log, Secret.of(b64uDecode(item.scope_key)), {
        sessionDevice: item.args.session_device ?? null,
        excluded: item.args.excluded ?? [],
      });
      same(opened.checked.header.id, item.args.event.header.id, `event ${item.name}`);
    }
    for (const item of data.hub_valid) await ctx.ops.check_event_hub!(item.args);
    same(events.checkReplay(data.replay.stored, data.replay.incoming), data.replay.replayed, 'replay');
    same(data.grace_seconds * 1000, events.GRACE_MS, 'grace');
    same(events.matrix(), data.matrix, 'CON-03 matrix (vector)');
  },
};

/** One check per log file (all positive vectors of that file). */
export function logPositiveChecks(): Check[] {
  return LOG_CONSUMED.map((name) => ({ name: `${name}: positive vectors`, run: () => POSITIVE[name]!(file(name)) }));
}

/** Runs every log check and reports each outcome (the browser entry point of e2e/core-interop.spec.ts). */
export async function runAll(): Promise<Outcome[]> {
  return runChecks([...logPositiveChecks(), ...logNegativeChecks()]);
}
