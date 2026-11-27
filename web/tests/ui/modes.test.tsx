// Working modes in the web client (ADR-098 sections 2 and 3, KOR-75): the field "Working modes" of a new task (empty,
// needed modes in one step, `modes` only when chosen), the chips of a task with "Switch off", switching on with the
// needed modes, the refusals, the notice line, and the chips of the task lists and the situation view.
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import type { Store } from '../../src/core/store/store';
import { EMPTY_DIRECTORY, type Directory } from '../../src/core/tasks/directory';
import { Composer } from '../../src/ui/composer/Composer';
import { useStore } from '../../src/ui/hooks/useStore';
import { ModeChips } from '../../src/ui/modes/ModeChips';
import { TaskModes } from '../../src/ui/screens/task/TaskModes';
import { taskRow, taskWorld, type TaskWorld } from '../core/tasks/world';
import { mount, TASK } from './chat-world';

const ADA = '0190f000-0000-7000-8000-0000000000c1';
const ORIGIN = '0190f000-0000-7000-8000-0000000000c2';
const STRANGER = '0190f000-0000-7000-8000-0000000000c3';

const entry = (name: string, en: string, de: string, needs: string[] = []) => ({
  name,
  version: 1,
  title: { de, en },
  summary: { de: `Über ${de}`, en: `About ${en}` },
  brief: `Working mode: ${name}`,
  operations: [],
  drivers: [],
  settings: { type: 'object', properties: {}, additionalProperties: false },
  needs: { modes: needs, prerequisites: [] },
  accesses: [],
  passes_on: false,
  test: false,
});

const CATALOGUE = {
  modes: [
    entry('a38', 'A38', 'A38'),
    entry('merge_lane', 'Merge lane', 'Merge-Spur', ['a38']),
    entry('test', 'Test mode', 'Testmodus'),
  ],
};

const mode = (name: string, overrides: Record<string, unknown> = {}) => ({
  mode: name,
  version: 1,
  chosen_by: ADA,
  way: 'web',
  origin_task: null,
  changed_at: '2026-11-26T10:00:00.000Z',
  ...overrides,
});

const DIRECTORY: Directory = {
  ...EMPTY_DIRECTORY,
  members: new Map([[ADA, 'Ada']]),
  sources: new Map([[ADA, { source: 'own', by: null } as const]]),
};

/** The modes of the task as the store holds it (the task page follows the row). */
function Page({ store, writer }: { readonly store: Store; readonly writer: boolean }) {
  const current = useStore((state) => state.tasks.tasks[TASK]!, store);
  return <TaskModes row={current} writer={writer} directory={DIRECTORY} />;
}

describe('the working modes of a new task', () => {
  async function composer(catalogue: unknown) {
    const t = await taskWorld();
    if (catalogue !== null) t.client.on('GET /v1/modes', () => catalogue);
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    t.client.on('POST /v1/tasks', (body) => ({
      task: taskRow(t, { task_id: (body as { task_id: string }).task_id }),
      session: { session_id: 's1', state: 'requested', device_id: 'd', estimate_ms: 3000 },
      events: [],
    }));
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/', 'de');
    return t;
  }

  it('starts empty, offers the needed modes in one step and sends the chosen ones', async () => {
    const t = await composer(CATALOGUE);
    const field = await screen.findByTestId('composer.modes');
    expect(field).toHaveTextContent('Arbeitsweisen');
    const checks = screen.getAllByTestId('composer.mode.check');
    expect(checks.map((check) => (check as HTMLInputElement).checked)).toEqual([false, false, false]);
    expect(screen.getAllByTestId('composer.mode').map((node) => node.dataset.mode)).toEqual([
      'a38',
      'merge_lane',
      'test',
    ]);
    expect(screen.getAllByTestId('composer.mode')[1]).toHaveTextContent('Über Merge-Spur');
    // the needs of a mode are named, nothing is added without the click
    expect(screen.getByTestId('composer.mode.needs')).toHaveTextContent('Braucht: A38');
    fireEvent.click(checks[1]!);
    expect(checks[0]).not.toBeChecked();
    fireEvent.click(screen.getByTestId('composer.mode.with-needs'));
    expect(checks[0]).toBeChecked();
    expect(checks[1]).toBeChecked();
    expect(screen.queryByTestId('composer.mode.needs')).toBeNull();
    fireEvent.click(checks[0]!);
    expect(checks[0]).not.toBeChecked();
    fireEvent.click(checks[0]!);
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Baue das Feature' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(t.client.of('POST /v1/tasks')).toHaveLength(1));
    expect(t.client.of('POST /v1/tasks')[0]!.body).toMatchObject({ modes: ['merge_lane', 'a38'] });
  });

  it('prepares the task again when the choice changed after a failed send', async () => {
    const t = await composer(CATALOGUE);
    let posts = 0;
    t.client.on('POST /v1/tasks', (body) => {
      posts += 1;
      if (posts === 1) throw new HubError('unavailable', 503);
      return {
        task: taskRow(t, { task_id: (body as { task_id: string }).task_id }),
        session: { session_id: 's1', state: 'requested', device_id: 'd', estimate_ms: 3000 },
        events: [],
      };
    });
    const checks = await screen.findAllByTestId('composer.mode.check');
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Baue' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await screen.findByTestId('composer.error');
    fireEvent.click(checks[2]!);
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(t.client.of('POST /v1/tasks')).toHaveLength(2));
    const [first, second] = t.client.of('POST /v1/tasks').map((call) => call.body as Record<string, unknown>);
    expect(first).not.toHaveProperty('modes');
    expect(second!.modes).toEqual(['test']);
    expect(second!.task_id).not.toBe(first!.task_id);
  });

  it('shows no field and sends no modes where the hub offers none', async () => {
    const t = await composer(null);
    await waitFor(() => expect(t.client.of('GET /v1/modes')).toHaveLength(1));
    expect(screen.queryByTestId('composer.modes')).toBeNull();
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Hallo' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(t.client.of('POST /v1/tasks')).toHaveLength(1));
    expect(t.client.of('POST /v1/tasks')[0]!.body).not.toHaveProperty('modes');
  });
});

describe('the working modes of a task', () => {
  async function page(modes: unknown[] | undefined, options: { writer?: boolean; locale?: 'en' | 'de' } = {}) {
    const t = await taskWorld();
    t.client.on('GET /v1/modes', () => CATALOGUE);
    const row = taskRow(t, modes === undefined ? {} : { modes }) as TaskRow;
    putTasks(t.store, [row, taskRow(t, { task_id: ORIGIN, number: 7 }) as TaskRow]);
    const element = <Page store={t.store} writer={options.writer ?? true} />;
    mount(t, [{ path: '/o/:org/t/:task', element }], `/o/${t.org}/t/${TASK}`, options.locale ?? 'en');
    return { t, row };
  }

  it('shows the chips with who chose them and how, and the newest choice as a notice line', async () => {
    await page(
      [
        mode('a38'),
        mode('test', { way: 'inherited', origin_task: ORIGIN, changed_at: '2026-11-26T11:00:00.000Z' }),
        mode('merge_lane', { way: 'sentence', chosen_by: STRANGER }),
        mode('gone', { way: 'inherited', origin_task: STRANGER }),
        mode('other', { way: 'tool' }),
      ],
      { writer: false, locale: 'de' },
    );
    await waitFor(() => expect(screen.getAllByTestId('task.modes.title')[0]).toHaveTextContent('A38'));
    const titles = screen.getAllByTestId('task.modes.title');
    expect(titles.map((node) => [node.textContent, node.getAttribute('title')])).toEqual([
      ['A38', 'Eingeschaltet von Ada – im Web'],
      ['Testmodus', 'Eingeschaltet von Ada – weitergegeben von Task #7'],
      ['Merge-Spur', 'Eingeschaltet von jemandem – aus der Anweisung'],
      ['gone', 'Eingeschaltet von Ada – weitergegeben von einer anderen Task'],
      ['other', 'Eingeschaltet von Ada – mit dem Agentenwerkzeug'],
    ]);
    expect(screen.getByTestId('task.modes.notice')).toHaveTextContent(
      'Testmodus eingeschaltet von Ada – weitergegeben von Task #7',
    );
    // readers only look
    expect(screen.queryByTestId('task.modes.off')).toBeNull();
    expect(screen.queryByTestId('task.modes.select')).toBeNull();
  });

  it('shows nothing to a reader of a task without modes', async () => {
    const { t } = await page(undefined, { writer: false });
    expect(screen.queryByTestId('task.modes')).toBeNull();
    expect(t.client.of('GET /v1/modes')).toHaveLength(0);
  });

  it('switches a mode off, and says when the right is missing', async () => {
    const { t } = await page([mode('a38'), mode('test')]);
    let refuse = true;
    t.client.on(`DELETE /v1/tasks/${TASK}/modes/a38`, () => {
      if (refuse) throw new HubError('forbidden', 403);
      return {};
    });
    const offs = await screen.findAllByTestId('task.modes.off');
    expect(offs[0]).toHaveAccessibleName('Switch off A38');
    fireEvent.click(offs[0]!);
    expect(await screen.findByTestId('task.modes.error')).toHaveTextContent(
      'You do not have the right to change the working modes of this task.',
    );
    refuse = false;
    fireEvent.click(screen.getAllByTestId('task.modes.off')[0]!);
    await waitFor(() =>
      expect(screen.getAllByTestId('task.modes.chip').map((node) => node.dataset.mode)).toEqual(['test']),
    );
    expect(screen.queryByTestId('task.modes.error')).toBeNull();
  });

  it('switches on a mode together with the modes it needs', async () => {
    const { t } = await page([mode('test')]);
    const order: string[] = [];
    for (const name of ['a38', 'merge_lane']) {
      t.client.on(`PUT /v1/tasks/${TASK}/modes/${name}`, () => {
        order.push(name);
        return { mode: mode(name, { changed_at: '2026-11-26T12:00:00.000Z' }) };
      });
    }
    const select = await screen.findByTestId('task.modes.select');
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Choose a working mode', 'A38', 'Merge lane']);
    expect(screen.queryByTestId('task.modes.on')).toBeNull();
    fireEvent.change(select, { target: { value: 'merge_lane' } });
    expect(screen.queryByTestId('task.modes.on')).toBeNull();
    const both = screen.getByTestId('task.modes.on-both');
    expect(both).toHaveTextContent('Switch on together with A38');
    fireEvent.click(both);
    await waitFor(() => expect(order).toEqual(['a38', 'merge_lane']));
    await waitFor(() =>
      expect(screen.getAllByTestId('task.modes.chip').map((node) => node.dataset.mode)).toEqual([
        'test',
        'a38',
        'merge_lane',
      ]),
    );
    expect(screen.queryByTestId('task.modes.select')).toBeNull();
  });

  it('names the modes a 409 mode_needs asks for and switches them on in one step', async () => {
    const { t } = await page([]);
    let refuse = true;
    t.client.on(`PUT /v1/tasks/${TASK}/modes/test`, () => {
      if (refuse) throw new HubError('mode_needs', 409, { modes: ['a38'] });
      return { mode: mode('test') };
    });
    t.client.on(`PUT /v1/tasks/${TASK}/modes/a38`, () => ({ mode: mode('a38') }));
    fireEvent.change(await screen.findByTestId('task.modes.select'), { target: { value: 'test' } });
    fireEvent.click(screen.getByTestId('task.modes.on'));
    const failed = await screen.findByTestId('task.modes.error');
    expect(failed).toHaveAttribute('data-code', 'mode_needs');
    expect(failed).toHaveTextContent('This working mode needs A38 first.');
    refuse = false;
    fireEvent.click(screen.getByTestId('task.modes.error.both'));
    await waitFor(() => expect(screen.getAllByTestId('task.modes.chip')).toHaveLength(2));
    expect(t.client.of(`PUT /v1/tasks/${TASK}/modes/a38`)).toHaveLength(1);
  });

  it('reports any other failure with its reason', async () => {
    const { t } = await page([]);
    t.client.on(`PUT /v1/tasks/${TASK}/modes/a38`, () => {
      throw new HubError('unavailable', 503);
    });
    const select = await screen.findByTestId('task.modes.select');
    fireEvent.change(select, { target: { value: 'a38' } });
    fireEvent.click(screen.getByTestId('task.modes.on'));
    const failed = await screen.findByTestId('task.modes.error');
    expect(failed).toHaveTextContent('That did not work:');
    expect(screen.queryByTestId('task.modes.error.both')).toBeNull();
    fireEvent.change(select, { target: { value: '' } });
    expect(screen.queryByTestId('task.modes.error')).toBeNull();
  });
});

describe('the working modes in the lists', () => {
  async function chips(rows: TaskRow[], catalogue: boolean) {
    const t: TaskWorld = await taskWorld();
    if (catalogue) t.client.on('GET /v1/modes', () => CATALOGUE);
    mount(
      t,
      [
        {
          path: '/',
          element: (
            <>
              {rows.map((row) => (
                <ModeChips key={row.task_id} row={row} testId="tasks.row.modes" />
              ))}
            </>
          ),
        },
      ],
      '/',
      'de',
    );
    return t;
  }

  it('shows the titles of a task with modes and nothing for one without', async () => {
    const t = await taskWorld();
    const plain = taskRow(t) as TaskRow;
    const moded = taskRow(t, { task_id: ORIGIN, modes: [mode('merge_lane'), mode('a38')] }) as TaskRow;
    await chips([plain, moded], true);
    await waitFor(() =>
      expect(screen.getAllByTestId('tasks.row.modes.mode').map((node) => node.textContent)).toEqual([
        'Merge-Spur',
        'A38',
      ]),
    );
    expect(screen.getAllByTestId('tasks.row.modes')).toHaveLength(1);
    expect(screen.getByRole('list', { name: 'Arbeitsweisen' })).toBeVisible();
  });

  it('shows the names while the catalogue cannot be read and does not ask again', async () => {
    const t = await taskWorld();
    const rows = [
      taskRow(t, { modes: [mode('a38')] }) as TaskRow,
      taskRow(t, { task_id: ORIGIN, modes: [mode('test')] }) as TaskRow,
    ];
    const world = await chips(rows, false);
    await waitFor(() => expect(world.store.getState().modes.status).toBe('error'));
    expect(screen.getAllByTestId('tasks.row.modes.mode').map((node) => node.textContent)).toEqual(['a38', 'test']);
    expect(world.client.of('GET /v1/modes')).toHaveLength(1);
  });
});
