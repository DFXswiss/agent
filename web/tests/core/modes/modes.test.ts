// Working modes in the web core (ADR-098 sections 1-3, KOR-75): catalogue entries, the modes of a task row, needed
// modes, loading the catalogue, switching a mode on and off, and `modes` on a new task only when chosen.
import { describe, expect, it } from 'vitest';
import {
  catalogueEntry,
  loadModes,
  missingNeeds,
  modeSummary,
  modeTitle,
  neededModes,
  newestMode,
  switchModeOff,
  switchModeOn,
  taskModeOf,
  taskModes,
  withNeeds,
  type ModeEntry,
} from '../../../src/core/modes/modes';
import { HubError } from '../../../src/core/net/signedFetch';
import { putTasks, type TaskRow } from '../../../src/core/store/slices/tasks';
import { prepareTask } from '../../../src/core/tasks/send';
import { taskRow, taskWorld } from '../tasks/world';

const USER = '0190f000-0000-7000-8000-0000000000c1';
const ORIGIN = '0190f000-0000-7000-8000-0000000000c2';

const entry = (name: string, needs: string[] = []) => ({
  name,
  version: 1,
  title: { de: `Titel ${name}`, en: `Title ${name}` },
  summary: { de: `Zusammenfassung ${name}`, en: `Summary ${name}` },
  brief: `Working mode: ${name}`,
  operations: [],
  drivers: [],
  settings: { type: 'object', properties: {}, additionalProperties: false },
  needs: { modes: needs, prerequisites: [] },
  accesses: [],
  passes_on: false,
  test: false,
});

const mode = (name: string, overrides: Record<string, unknown> = {}) => ({
  mode: name,
  version: 1,
  chosen_by: USER,
  way: 'web',
  origin_task: null,
  changed_at: '2026-11-26T10:00:00.000Z',
  ...overrides,
});

const CATALOGUE: ModeEntry[] = [
  catalogueEntry(entry('a38'))!,
  catalogueEntry(entry('merge_lane', ['a38']))!,
  catalogueEntry(entry('train', ['merge_lane', 'a38']))!,
  catalogueEntry(entry('loop', ['loop', 'ghost']))!,
];

describe('the catalogue and the modes of a task', () => {
  it('reads well-formed catalogue entries only', () => {
    expect(catalogueEntry(entry('test'))).toEqual({
      name: 'test',
      version: 1,
      title: { de: 'Titel test', en: 'Title test' },
      summary: { de: 'Zusammenfassung test', en: 'Summary test' },
      needs: [],
    });
    for (const bad of [
      null,
      'test',
      { ...entry('test'), name: 'Bad' },
      { ...entry('test'), version: 0 },
      { ...entry('test'), title: { de: 'x' } },
      { ...entry('test'), summary: { de: '', en: 'x' } },
      { ...entry('test'), title: 'x' },
      { ...entry('test'), needs: null },
      { ...entry('test'), needs: { modes: 'a38' } },
      { ...entry('test'), needs: { modes: ['A38'] } },
    ]) {
      expect(catalogueEntry(bad)).toBeNull();
    }
  });

  it('reads the modes of a task row (absent from older hubs: none)', () => {
    const t = { org: 'o', wg: 'w', me: { id: USER } } as never;
    expect(taskModes(taskRow(t) as TaskRow)).toEqual([]);
    const row = taskRow(t, {
      modes: [mode('a38'), mode('test', { way: 'inherited', origin_task: ORIGIN }), { mode: 'x' }, 'junk'],
    }) as TaskRow;
    expect(taskModes(row).map((item) => [item.mode, item.way, item.origin_task])).toEqual([
      ['a38', 'web', null],
      ['test', 'inherited', ORIGIN],
    ]);
    for (const bad of [
      null,
      mode('A38'),
      mode('a38', { version: 0 }),
      mode('a38', { chosen_by: '' }),
      mode('a38', { way: 'magic' }),
      mode('a38', { origin_task: 1 }),
      mode('a38', { changed_at: 'yesterday' }),
      mode('a38', { changed_at: 3 }),
    ]) {
      expect(taskModeOf(bad)).toBeNull();
    }
  });

  it('names the newest mode and the titles in the user language', () => {
    const older = taskModeOf(mode('a38'))!;
    const newer = taskModeOf(mode('test', { changed_at: '2026-11-26T11:00:00.000Z' }))!;
    expect(newestMode([])).toBeNull();
    expect(newestMode([older, newer])).toBe(newer);
    expect(newestMode([newer, older])).toBe(newer);
    expect(modeTitle(CATALOGUE, 'a38', 'de')).toBe('Titel a38');
    expect(modeTitle(CATALOGUE, 'a38', 'en')).toBe('Title a38');
    expect(modeTitle(CATALOGUE, 'unknown', 'en')).toBe('unknown');
    expect(modeSummary(CATALOGUE, 'a38', 'en')).toBe('Summary a38');
    expect(modeSummary(CATALOGUE, 'unknown', 'de')).toBeNull();
  });

  it('finds the needed modes that are not chosen and adds them in one step', () => {
    expect(missingNeeds(CATALOGUE, [], 'a38')).toEqual([]);
    expect(missingNeeds(CATALOGUE, [], 'train')).toEqual(['merge_lane', 'a38']);
    expect(missingNeeds(CATALOGUE, ['a38'], 'train')).toEqual(['merge_lane']);
    expect(missingNeeds(CATALOGUE, [], 'loop')).toEqual(['ghost']);
    expect(missingNeeds(CATALOGUE, [], 'unknown')).toEqual([]);
    expect(withNeeds(CATALOGUE, [], 'train')).toEqual(['merge_lane', 'a38', 'train']);
    expect(withNeeds(CATALOGUE, ['a38', 'merge_lane'], 'merge_lane')).toEqual(['a38', 'merge_lane']);
  });

  it('names what a 409 mode_needs asks for', () => {
    const named = new HubError('mode_needs', 409, { modes: ['a38', 'Bad'] });
    expect(neededModes(named, CATALOGUE, [], 'merge_lane')).toEqual(['a38']);
    expect(neededModes(new HubError('mode_needs', 409), CATALOGUE, [], 'train')).toEqual(['merge_lane', 'a38']);
    expect(neededModes(new HubError('mode_needs', 409, { modes: 'a38' }), CATALOGUE, [], 'merge_lane')).toEqual([
      'a38',
    ]);
    expect(neededModes(new HubError('forbidden', 403), CATALOGUE, [], 'merge_lane')).toEqual([]);
    expect(neededModes(new Error('x'), CATALOGUE, [], 'merge_lane')).toEqual([]);
  });
});

describe('loading the catalogue', () => {
  it('loads once, leaves out entries off the contract and asks again after a failure where asked to', async () => {
    const t = await taskWorld();
    let fail = true;
    t.client.on('GET /v1/modes', () => {
      if (fail) throw new HubError('not_found', 404);
      return { modes: [entry('test'), { name: 'Bad' }] };
    });
    await loadModes(t.context);
    expect(t.store.getState().modes).toEqual({ status: 'error', catalogue: [] });
    fail = false;
    await loadModes(t.context, false);
    expect(t.client.of('GET /v1/modes')).toHaveLength(1);
    await loadModes(t.context);
    expect(t.store.getState().modes.status).toBe('ready');
    expect(t.store.getState().modes.catalogue.map((item) => item.name)).toEqual(['test']);
    await loadModes(t.context);
    expect(t.client.of('GET /v1/modes')).toHaveLength(2);
  });

  it('skips while loading, reads a malformed answer as empty and writes nothing after a sign-out', async () => {
    const t = await taskWorld();
    let release: (value: unknown) => void = () => undefined;
    t.client.on('GET /v1/modes', () => new Promise((resolve) => (release = resolve)));
    const first = loadModes(t.context);
    await loadModes(t.context);
    expect(t.store.getState().modes.status).toBe('loading');
    release('odd');
    await first;
    expect(t.store.getState().modes).toEqual({ status: 'ready', catalogue: [] });
    t.store.reset();
    t.client.on('GET /v1/modes', () => {
      t.store.reset();
      return { modes: [entry('test')] };
    });
    await loadModes(t.context);
    expect(t.store.getState().modes.status).toBe('idle');
  });
});

describe('switching a mode', () => {
  it('switches a mode on and off and keeps the task row in step', async () => {
    const t = await taskWorld();
    const task = taskRow(t, { modes: [mode('a38')] }) as TaskRow;
    putTasks(t.store, [task]);
    const path = `/v1/tasks/${task.task_id}/modes`;
    t.client.on(`PUT ${path}/test`, () => ({ mode: mode('test', { changed_at: '2026-11-26T12:00:00.000Z' }) }));
    t.client.on(`DELETE ${path}/a38`, () => ({}));
    await switchModeOn(t.context, task.task_id, 'test');
    expect(t.client.of(`PUT ${path}/test`)[0]!.body).toEqual({});
    expect(taskModes(t.store.getState().tasks.tasks[task.task_id]!).map((item) => item.mode)).toEqual(['a38', 'test']);
    await switchModeOff(t.context, task.task_id, 'a38');
    expect(taskModes(t.store.getState().tasks.tasks[task.task_id]!).map((item) => item.mode)).toEqual(['test']);
    // a repetition answers the record unchanged; an answer off the contract leaves the row to the live frame
    t.client.on(`PUT ${path}/test`, () => ({ mode: mode('test', { changed_at: '2026-11-26T12:00:00.000Z' }) }));
    await switchModeOn(t.context, task.task_id, 'test');
    expect(taskModes(t.store.getState().tasks.tasks[task.task_id]!)).toHaveLength(1);
    t.client.on(`PUT ${path}/a38`, () => 'odd');
    await switchModeOn(t.context, task.task_id, 'a38');
    t.client.on(`PUT ${path}/a38`, () => ({ mode: 'odd' }));
    await switchModeOn(t.context, task.task_id, 'a38');
    expect(taskModes(t.store.getState().tasks.tasks[task.task_id]!).map((item) => item.mode)).toEqual(['test']);
  });

  it('writes nothing for an unknown task or after a sign-out, and passes refusals on', async () => {
    const t = await taskWorld();
    t.client.on('PUT /v1/tasks/unknown/modes/test', () => ({ mode: mode('test') }));
    t.client.on('DELETE /v1/tasks/unknown/modes/test', () => ({}));
    await switchModeOn(t.context, 'unknown', 'test');
    await switchModeOff(t.context, 'unknown', 'test');
    expect(t.store.getState().tasks.tasks.unknown).toBeUndefined();
    const task = taskRow(t) as TaskRow;
    putTasks(t.store, [task]);
    t.client.on(`PUT /v1/tasks/${task.task_id}/modes/test`, () => {
      t.store.reset();
      putTasks(t.store, [task]);
      return { mode: mode('test') };
    });
    await switchModeOn(t.context, task.task_id, 'test');
    expect(t.store.getState().tasks.tasks[task.task_id]!.modes).toBeUndefined();
    t.client.on(`PUT /v1/tasks/${task.task_id}/modes/train`, () => {
      throw new HubError('mode_needs', 409);
    });
    await expect(switchModeOn(t.context, task.task_id, 'train')).rejects.toMatchObject({ code: 'mode_needs' });
  });
});

describe('the modes of a new task', () => {
  it('sends modes only when some are chosen', async () => {
    const t = await taskWorld();
    const input = { org: t.org, wg: t.wg, prompt: 'x' };
    expect((await prepareTask(t.context, input)).body).not.toHaveProperty('modes');
    expect((await prepareTask(t.context, { ...input, modes: [] })).body).not.toHaveProperty('modes');
    expect((await prepareTask(t.context, { ...input, modes: ['a38', 'test'] })).body.modes).toEqual(['a38', 'test']);
  });
});
