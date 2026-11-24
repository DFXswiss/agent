// The registration points of src/core (store slices, background jobs): the real glob, its literal pattern,
// and the collectors proven with fixture modules discovered by the same glob shape.
import { describe, expect, it } from 'vitest';
import { backgroundJobs } from '../../src/core/jobs';
import { storeSlices } from '../../src/core/store/slices';
import { globPatterns } from '../helpers';

describe('store slices', () => {
  it('is discovered from src/core/store/slices/<area>.ts', () => {
    expect(globPatterns('src/core/store/slices/index.ts')).toEqual(["['./*.ts', '!./index.ts']"]);
    // later packages add slices (ADR-027 section 4): the base slices are present, sorted by file name
    const names = storeSlices().map((slice) => slice.name);
    expect(names).toEqual(expect.arrayContaining(['config', 'connection', 'events', 'jobs', 'me', 'orgs']));
    expect(names).toEqual([...names].sort());
  });

  it('collects fixture slices discovered by the same glob shape', () => {
    const modules = import.meta.glob(['../fixtures/registry/store/slices/*.ts'], { eager: true });
    expect(storeSlices(modules).map((slice) => [slice.name, slice.initial()])).toEqual([['demo', { value: 0 }]]);
  });

  it('refuses duplicate slice names', () => {
    const initial = () => null;
    const modules = { './a.ts': { slice: { name: 'x', initial } }, './b.ts': { slice: { name: 'x', initial } } };
    expect(() => storeSlices(modules)).toThrow('duplicate store slice name');
  });

  it('requires an initial state', () => {
    expect(() => storeSlices({ './a.ts': { slice: { name: 'x' } } })).toThrow('missing or invalid export "slice"');
  });
});

describe('background jobs', () => {
  it('is discovered from src/core/jobs/<name>.ts', () => {
    expect(globPatterns('src/core/jobs/index.ts')).toEqual(["['./*.ts', '!./index.ts']"]);
    // the first two in order; later packages add jobs after them
    expect(
      backgroundJobs()
        .map((job) => [job.name, job.order, job.triggers])
        .slice(0, 2),
    ).toEqual([
      ['log-verify', 10, ['login', 'log', 'lh']],
      ['wrap-verify', 20, ['login', 'log', 'wraps']],
    ]);
  });

  it('collects fixture jobs discovered by the same glob shape', () => {
    const modules = import.meta.glob(['../fixtures/registry/jobs/*.ts'], { eager: true });
    expect(backgroundJobs(modules).map((job) => job.name)).toEqual(['demo']);
  });

  it('requires the job name to match the file', () => {
    const job = (name: string, order = 1) => ({ name, order, triggers: [], run: () => Promise.resolve() });
    expect(() => backgroundJobs({ './jobs/a.ts': { job: job('b') } })).toThrow('must match the file');
    expect(() => backgroundJobs({ './jobs/a.ts': { job: { name: 'a' } } })).toThrow('missing or invalid export "job"');
    expect(
      backgroundJobs({ './a.ts': { job: job('a', 5) }, './b.ts': { job: job('b', 2) } }).map((j) => j.name),
    ).toEqual(['b', 'a']);
  });
});
