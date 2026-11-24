// The job runner: trigger filtering and order, serialization per org, coalescing of waiting runs, skipping blocked
// orgs, failure codes and the "Sync" indicator data.
import { describe, expect, it, vi } from 'vitest';
import { CryptoError } from '../../../src/core/crypto/errors';
import { failureCode, type JobContext, type JobRegistration, JobRunner } from '../../../src/core/jobs';
import { HubError } from '../../../src/core/net/signedFetch';
import { createStore } from '../../../src/core/store';
import { blockOrg } from '../../../src/core/store/slices/orgs';

function context() {
  const store = createStore();
  let now = 0;
  return {
    store,
    client: { request: (): Promise<unknown> => Promise.resolve(null) },
    persist: {} as JobContext['persist'],
    rootKey: '',
    now: () => (now += 1),
  } satisfies JobContext;
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => (resolve = r));
  return { promise, resolve };
}

describe('job runner', () => {
  it('runs the jobs of a trigger in order, one at a time per org, in parallel across orgs', async () => {
    const ctx = context();
    const log: string[] = [];
    const gate = deferred();
    const make = (name: string, order: number, triggers: JobRegistration['triggers']): JobRegistration => ({
      name,
      order,
      triggers,
      run: async (_c, run) => {
        log.push(`start ${name} ${run.org} ${run.trigger}`);
        if (name === 'a' && run.org === 'o1') await gate.promise;
        log.push(`end ${name} ${run.org}`);
      },
    });
    const runner = new JobRunner(ctx, [
      make('a', 1, ['login', 'log']),
      make('b', 2, ['login']),
      make('c', 3, ['wraps']),
    ]);
    const first = runner.trigger('login', 'o1');
    const second = runner.trigger('login', 'o2');
    await second;
    expect(ctx.store.getState().jobs.o1).toMatchObject({ running: 'a', waiting: 1 });
    gate.resolve();
    await first;
    expect(log).toEqual([
      'start a o1 login',
      'start a o2 login',
      'end a o2',
      'start b o2 login',
      'end b o2',
      'end a o1',
      'start b o1 login',
      'end b o1',
    ]);
    expect(ctx.store.getState().jobs.o1).toEqual({
      running: null,
      waiting: 0,
      last: { name: 'b', ok: true, code: null, at: 4 },
    });
  });

  it('coalesces a run that is already waiting', async () => {
    const ctx = context();
    let runs = 0;
    const gate = deferred();
    const runner = new JobRunner(ctx, [
      { name: 'slow', order: 1, triggers: ['log'], run: () => gate.promise },
      { name: 'count', order: 2, triggers: ['log'], run: async () => void (runs += 1) },
    ]);
    const all = [runner.trigger('log', 'o'), runner.trigger('log', 'o'), runner.trigger('log', 'o')];
    gate.resolve();
    await Promise.all(all);
    expect(runs).toBe(1);
  });

  it('records failures and skips blocked orgs', async () => {
    const ctx = context();
    const errors = [new HubError('rate_limited', 429), new CryptoError('log_fork'), new Error('bug')];
    let index = 0;
    const runner = new JobRunner(ctx, [
      { name: 'x', order: 1, triggers: ['lh'], run: () => Promise.reject(errors[index++]) },
    ]);
    const codes = [];
    for (let i = 0; i < 3; i += 1) {
      await runner.trigger('lh', 'o');
      codes.push(ctx.store.getState().jobs.o!.last!.code);
    }
    expect(codes).toEqual(['rate_limited', 'log_fork', 'unavailable']);
    expect(ctx.store.getState().jobs.o!.last!.ok).toBe(false);
    blockOrg(ctx.store, 'o', { code: 'log_fork', stored: null, served: null, lh: null });
    await runner.trigger('lh', 'o');
    expect(index).toBe(3);
    await runner.trigger('interval', 'o');
    expect(failureCode('x')).toBe('unavailable');
  });

  it('runs nothing after stop and drops the outcome of a run that a sign-out overtook', async () => {
    const ctx = context();
    const gate = deferred();
    let runs = 0;
    const runner = new JobRunner(ctx, [
      { name: 'x', order: 1, triggers: ['log'], run: async () => void ((runs += 1), await gate.promise) },
    ]);
    const running = runner.trigger('log', 'o');
    while (runs === 0) await Promise.resolve();
    const waiting = runner.trigger('log', 'o');
    ctx.store.reset();
    gate.resolve();
    expect([await running, await waiting]).toEqual([true, false]);
    expect(runs).toBe(1);
    expect(ctx.store.getState().jobs).toEqual({});
    runner.stop();
    expect(await runner.trigger('log', 'o')).toBe(false);
    expect(runs).toBe(1);
  });

  it('uses the discovered jobs by default', async () => {
    const ctx = context();
    const runner = new JobRunner(ctx);
    await runner.trigger('wraps', 'o');
    expect(ctx.store.getState().jobs.o!.last).toMatchObject({ name: 'wrap-verify', ok: true });
  });

  describe('retry with backoff (KOR-05)', () => {
    function fakeTimers() {
      const armed = new Map<number, { callback: () => void; ms: number }>();
      let next = 0;
      return {
        armed,
        cleared: [] as unknown[],
        setTimeout(callback: () => void, ms: number) {
          next += 1;
          armed.set(next, { callback, ms });
          return next;
        },
        clearTimeout(handle: unknown) {
          this.cleared.push(handle);
          armed.delete(handle as number);
        },
        fire() {
          const [handle, timer] = [...armed.entries()][0]!;
          armed.delete(handle);
          timer.callback();
        },
      };
    }

    it('repeats the trigger of a transiently failing retrying job with growing delays until it succeeds', async () => {
      const ctx = context();
      const timers = fakeTimers();
      const outcomes: (Error | null)[] = [new HubError('unavailable', 0), new HubError('rate_limited', 429), null];
      const runs: string[] = [];
      const runner = new JobRunner(
        ctx,
        [
          {
            name: 'flaky',
            order: 1,
            triggers: ['login'],
            retry: true,
            run: async (_c, run) => {
              runs.push(`flaky ${run.trigger}`);
              const outcome = outcomes.shift()!;
              if (outcome !== null) throw outcome;
            },
          },
          { name: 'other', order: 2, triggers: ['login'], run: async () => void runs.push('other') },
        ],
        { timers, random: () => 0.5 },
      );
      expect(await runner.trigger('login', 'o')).toBe(false);
      expect([...timers.armed.values()].map((t) => t.ms)).toEqual([1000]);
      timers.fire();
      await vi.waitFor(() => expect([...timers.armed.values()].map((t) => t.ms)).toEqual([2000]));
      timers.fire();
      await vi.waitFor(() => expect(runs).toHaveLength(6));
      expect(runs).toEqual(['flaky login', 'other', 'flaky login', 'other', 'flaky login', 'other']);
      expect(timers.armed.size).toBe(0);
      // the backoff starts over after a success
      outcomes.push(new HubError('unavailable', 0));
      await runner.trigger('login', 'o');
      expect([...timers.armed.values()].map((t) => t.ms)).toEqual([1000]);
    });

    it('arms one timer per trigger and org, keeps it over a success of another run, and ignores lasting errors', async () => {
      const ctx = context();
      const timers = fakeTimers();
      let fail: Error | null = new HubError('unavailable', 0);
      const runner = new JobRunner(
        ctx,
        [
          {
            name: 'x',
            order: 1,
            triggers: ['log', 'lh'],
            retry: true,
            run: async () => {
              if (fail !== null) throw fail;
            },
          },
          { name: 'plain', order: 1, triggers: ['wraps'], run: () => Promise.reject(new HubError('unavailable', 0)) },
        ],
        { timers, random: () => 0.5 },
      );
      await runner.trigger('log', 'o');
      await runner.trigger('log', 'o');
      expect(timers.armed.size).toBe(1);
      fail = null;
      await runner.trigger('log', 'o');
      expect(timers.armed.size).toBe(1);
      await runner.trigger('wraps', 'o');
      fail = new HubError('forbidden', 403);
      await runner.trigger('lh', 'o');
      expect(timers.armed.size).toBe(1);
      fail = new HubError('unavailable', 0);
      await runner.trigger('lh', 'o');
      expect(timers.armed.size).toBe(2);
      runner.stop();
      expect(timers.armed.size).toBe(0);
      expect(timers.cleared).toHaveLength(2);
      await runner.trigger('log', 'o');
      expect(timers.armed.size).toBe(0);
    });

    it('a retry armed before a sign-out does not run; a run overtaken by a sign-out arms none', async () => {
      const ctx = context();
      const timers = fakeTimers();
      let runs = 0;
      const gate = deferred();
      const runner = new JobRunner(
        ctx,
        [
          {
            name: 'x',
            order: 1,
            triggers: ['log'],
            retry: true,
            run: async () => {
              runs += 1;
              if (runs === 2) await gate.promise;
              throw new HubError('unavailable', 0);
            },
          },
        ],
        { timers, random: () => 0.5 },
      );
      await runner.trigger('log', 'o');
      ctx.store.reset();
      timers.fire();
      await Promise.resolve();
      expect(runs).toBe(1);
      const running = runner.trigger('log', 'o');
      while (runs < 2) await Promise.resolve();
      ctx.store.reset();
      gate.resolve();
      expect(await running).toBe(false);
      expect(timers.armed.size).toBe(0);
      // the fired retry left its backoff state without a timer: nothing to cancel
      runner.stop();
      expect(timers.cleared).toEqual([]);
    });

    it('uses the system timers by default and cancels them on stop', async () => {
      vi.useFakeTimers();
      try {
        const runner = new JobRunner(context(), [
          {
            name: 'x',
            order: 1,
            triggers: ['log'],
            retry: true,
            run: () => Promise.reject(new HubError('unavailable', 0)),
          },
        ]);
        await runner.trigger('log', 'o');
        expect(vi.getTimerCount()).toBe(1);
        runner.stop();
        expect(vi.getTimerCount()).toBe(0);
      } finally {
        vi.useRealTimers();
      }
    });

    it('does not retry a failure that is no transient hub error', async () => {
      const timers = fakeTimers();
      const failures = [new Error('bug'), new HubError('unavailable', 0)];
      const runner = new JobRunner(
        context(),
        [{ name: 'x', order: 1, triggers: ['log'], retry: true, run: () => Promise.reject(failures.shift()) }],
        { timers, random: () => 0.5 },
      );
      await runner.trigger('log', 'o');
      expect(timers.armed.size).toBe(0);
      await runner.trigger('log', 'o');
      expect(timers.armed.size).toBe(1);
    });
  });
});
