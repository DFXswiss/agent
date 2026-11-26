// Working modes of a task (ADR-098 sections 1-3, KOR-75; 03-api.md section 4.15): a named way of working a person
// chooses for a task – never on by default, never remembered from earlier tasks (E-32), always revocable.
//
// - `catalogueEntry(value)`: the fields the web uses of an entry of `GET /v1/modes` (`contracts/modes/mode.json`),
//   else null; `loadModes`: the catalogue once into the `modes` slice (again after a failure where asked to).
// - `taskModes(row)`: the modes that are on for a task (`task_row.modes`; absent from hubs before KOR-75: none);
//   malformed items are left out. `newestMode`: the one switched on last (the notice line of the task page).
// - `modeTitle` / `modeSummary`: the text of the user's language, the name where the catalogue does not know it.
// - `missingNeeds(catalogue, chosen, name)`: the modes `name` needs (transitively) that are not chosen; `withNeeds`:
//   the choice with `name` and those – nothing is switched on implicitly, the web offers it as one step (E-05).
// - `switchModeOn` / `switchModeOff`: `PUT` (body `{}`) / `DELETE /v1/tasks/{task}/modes/{mode}` (writers of the
//   workgroup); the task row in the store follows. `neededModes(error, …)`: what a `409 mode_needs` asks for – the
//   modes the hub names in `detail.modes`, else the catalogue's needs that are off.
import type { Locale } from '../i18n/locale';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { RequestsContext } from '../requests/client';
import { setModes } from '../store/slices/modes';
import { putTasks, type TaskRow } from '../store/slices/tasks';

export const MODE_NAME_RE = /^[a-z][a-z0-9_]{0,31}$/;
export const MODE_WAYS = ['web', 'tool', 'sentence', 'inherited'] as const;
export type ModeWay = (typeof MODE_WAYS)[number];

/** A title or summary per language. */
export interface ModeTexts {
  readonly de: string;
  readonly en: string;
}

/** The fields of a catalogue entry the web uses. */
export interface ModeEntry {
  readonly name: string;
  readonly version: number;
  readonly title: ModeTexts;
  readonly summary: ModeTexts;
  /** Other modes it requires (`needs.modes`). */
  readonly needs: readonly string[];
}

/** `task_mode` of the contracts: a mode that is on for a task. */
export interface TaskMode {
  readonly mode: string;
  readonly version: number;
  readonly chosen_by: string;
  readonly way: ModeWay;
  readonly origin_task: string | null;
  readonly changed_at: string;
}

export const isModeName = (value: unknown): value is string => typeof value === 'string' && MODE_NAME_RE.test(value);
const isText = (value: unknown): value is string => typeof value === 'string' && value !== '';
const isVersion = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 1;
const isTexts = (value: unknown): value is ModeTexts => isRecord(value) && isText(value.de) && isText(value.en);
const isWay = (value: unknown): value is ModeWay => MODE_WAYS.includes(value as ModeWay);

/** A well-formed catalogue entry (its web fields), else null. */
export function catalogueEntry(value: unknown): ModeEntry | null {
  if (!isRecord(value)) return null;
  const { name, version, title, summary, needs } = value;
  if (!isModeName(name) || !isVersion(version) || !isTexts(title) || !isTexts(summary)) return null;
  const modes = isRecord(needs) ? needs.modes : null;
  if (!Array.isArray(modes) || !modes.every(isModeName)) return null;
  const texts = (item: ModeTexts): ModeTexts => ({ de: item.de, en: item.en });
  return { name, version, title: texts(title), summary: texts(summary), needs: modes };
}

/** A well-formed `task_mode`, else null. */
export function taskModeOf(value: unknown): TaskMode | null {
  if (!isRecord(value)) return null;
  const { mode, version, chosen_by: chosenBy, way, origin_task: origin, changed_at: changedAt } = value;
  if (!isModeName(mode) || !isVersion(version) || !isText(chosenBy) || !isWay(way)) return null;
  if (!(origin === null || isText(origin)) || !isText(changedAt) || Number.isNaN(Date.parse(changedAt))) return null;
  return { mode, version, chosen_by: chosenBy, way, origin_task: origin, changed_at: changedAt };
}

/** The modes that are on for a task (module comment). */
export function taskModes(row: TaskRow): readonly TaskMode[] {
  if (!Array.isArray(row.modes)) return [];
  return row.modes.map(taskModeOf).filter((item): item is TaskMode => item !== null);
}

/** The mode switched on last, null without one. */
export function newestMode(modes: readonly TaskMode[]): TaskMode | null {
  let newest: TaskMode | null = null;
  for (const item of modes) {
    if (newest === null || Date.parse(item.changed_at) > Date.parse(newest.changed_at)) newest = item;
  }
  return newest;
}

export const modeEntry = (catalogue: readonly ModeEntry[], name: string): ModeEntry | undefined =>
  catalogue.find((entry) => entry.name === name);

/** The title in `locale`, the name when the catalogue does not list the mode. */
export function modeTitle(catalogue: readonly ModeEntry[], name: string, locale: Locale): string {
  return modeEntry(catalogue, name)?.title[locale] ?? name;
}

/** The summary in `locale`, null when the catalogue does not list the mode. */
export function modeSummary(catalogue: readonly ModeEntry[], name: string, locale: Locale): string | null {
  return modeEntry(catalogue, name)?.summary[locale] ?? null;
}

/** The modes `name` needs, directly or through another, that `chosen` lacks (in the order they are found). */
export function missingNeeds(catalogue: readonly ModeEntry[], chosen: readonly string[], name: string): string[] {
  const missing: string[] = [];
  const visit = (current: string) => {
    for (const need of modeEntry(catalogue, current)?.needs ?? []) {
      if (need === name || chosen.includes(need) || missing.includes(need)) continue;
      missing.push(need);
      visit(need);
    }
  };
  visit(name);
  return missing;
}

/** `chosen` with `name` and every mode it needs (one step, E-05). */
export function withNeeds(catalogue: readonly ModeEntry[], chosen: readonly string[], name: string): string[] {
  return [...new Set([...chosen, ...missingNeeds(catalogue, chosen, name), name])];
}

/**
 * `GET /v1/modes` into the `modes` slice, unless it is loaded or loading, or failed before and `again` is false (the
 * rows of a list do not ask again after a failure); entries off the contract are left out.
 */
export async function loadModes(context: RequestsContext, again = true): Promise<void> {
  const { store } = context;
  const { status } = store.getState().modes;
  if (status === 'loading' || status === 'ready' || (status === 'error' && !again)) return;
  const generation = store.generation;
  setModes(store, { status: 'loading', catalogue: [] });
  let catalogue: ModeEntry[] | null;
  try {
    const answer = await context.client.request('GET', '/v1/modes');
    const listed: unknown[] = isRecord(answer) && Array.isArray(answer.modes) ? answer.modes : [];
    catalogue = listed.map(catalogueEntry).filter((entry): entry is ModeEntry => entry !== null);
  } catch {
    catalogue = null;
  }
  // an answer after a sign-out writes nothing into the next user's store
  if (store.generation !== generation) return;
  setModes(store, catalogue === null ? { status: 'error', catalogue: [] } : { status: 'ready', catalogue });
}

const modePath = (task: string, mode: string) =>
  `/v1/tasks/${encodeURIComponent(task)}/modes/${encodeURIComponent(mode)}`;

/** Replaces the modes of the stored task row (when it is still the same user's store). */
function follow(context: RequestsContext, generation: number, task: string, change: (modes: TaskMode[]) => TaskMode[]) {
  const row = context.store.getState().tasks.tasks[task];
  if (row === undefined || context.store.generation !== generation) return;
  putTasks(context.store, [{ ...row, modes: change([...taskModes(row)]) }]);
}

/** `PUT /v1/tasks/{task}/modes/{mode}`: the mode is on (an answer off the contract leaves the row to the live frame). */
export async function switchModeOn(context: RequestsContext, task: string, mode: string): Promise<void> {
  const generation = context.store.generation;
  const answer = await context.client.request('PUT', modePath(task, mode), { body: {} });
  const on = isRecord(answer) ? taskModeOf(answer.mode) : null;
  if (on === null) return;
  follow(context, generation, task, (modes) => [...modes.filter((item) => item.mode !== on.mode), on]);
}

/** `DELETE /v1/tasks/{task}/modes/{mode}`: the mode is off (also when it was off already). */
export async function switchModeOff(context: RequestsContext, task: string, mode: string): Promise<void> {
  const generation = context.store.generation;
  await context.client.request('DELETE', modePath(task, mode));
  follow(context, generation, task, (modes) => modes.filter((item) => item.mode !== mode));
}

/** The modes a `409 mode_needs` asks for (module comment); empty for any other failure. */
export function neededModes(
  error: unknown,
  catalogue: readonly ModeEntry[],
  active: readonly string[],
  name: string,
): string[] {
  if (!(error instanceof HubError) || error.code !== 'mode_needs') return [];
  const named = isRecord(error.detail) && Array.isArray(error.detail.modes) ? error.detail.modes : [];
  const valid = named.filter(isModeName);
  return valid.length > 0 ? valid : missingNeeds(catalogue, active, name);
}
