// Process analysis (ADR-095 sections 1, 5 and 6): a button starts a read-only analysis task; its report can be adopted
// into a whiteboard.
//
// - `PERIODS` / `DEFAULT_DAYS`: the period presets in days (1, 7, 30, 90; default 7); `period(context, days)` is
//   `{since, until}` with `until` = now on the hub's clock (the browser clock plus the offset of the last `hello`; the
//   hub refuses an `until` more than one minute after its time) and `since` `days` days before, ISO-8601 UTC with
//   milliseconds.
// - `analysisTitle` / `analysisPrompt`: the fixed title and prompt per language (`contracts/tasks/analysis.json`, the
//   same text as the agent tool; vector-tested). The placeholders are replaced in one pass, so a workgroup name that
//   contains `{since}` stays as it is.
// - `prepareAnalysis`: the ordinary send path (`prepareTask`, sealed `task.meta` with the fixed title and the prompt)
//   with the plaintext `analysis` in the body; `sendTask` sends it like any task.
// - `analysisOf` / `isAnalysis`: the well-formed `analysis` of a task row, else null (every other task).
// - `adoptLevels` / `adoptOutput`: "Adopt" of a report (section 5) – the person writes an output's text as a new
//   `whiteboard.entry` of the task's workgroup or of the organization, sealed and signed by the person through the
//   existing whiteboard path, only on a board the person may write.
import type { Locale } from '../i18n/locale';
import type { OrgState } from '../log/state';
import { isRecord } from '../registry';
import type { TaskRow } from '../store/slices/tasks';
import { formatTime } from '../encoding/time';
import type { TasksContext } from '../tasks/pull';
import { type PreparedTask, prepareTask, type TaskAnalysis } from '../tasks/send';
import { createEntry } from '../whiteboards/actions';
import { type BoardLocation, canWriteBoard, orgBoard, wgBoard } from '../whiteboards/location';

export type Scope = TaskAnalysis['scope'];

/** The period presets in days, and the default. */
export const PERIODS = [1, 7, 30, 90] as const;
export const DEFAULT_DAYS = 7;
export const DAY_MS = 86_400_000;

/** Title and prompt per language (`contracts/tasks/analysis.json` `templates`). */
export const TEMPLATES: Readonly<Record<Locale, { readonly title: string; readonly prompt: string }>> = {
  en: {
    title: 'Optimize processes',
    prompt: 'Optimize processes: analyse {scope} from {since} to {until} and propose improvements.',
  },
  de: {
    title: 'Prozesse optimieren',
    prompt: 'Prozesse optimieren: Analysiere {scope} von {since} bis {until} und schlage Verbesserungen vor.',
  },
};

/** The text of a scope per language (`contracts/tasks/analysis.json` `scopes`; `{name}`: the workgroup's name). */
export const SCOPES: Readonly<Record<Locale, Readonly<Record<Scope, string>>>> = {
  en: { org: 'the organization', wg: 'workgroup {name}' },
  de: { org: 'die Organisation', wg: 'die Workgroup {name}' },
};

/** Replaces the placeholders of a template (each one given in `values`) in one pass. */
function fill(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{(name|scope|since|until)\}/g, (_match, key: string) => values[key]!);
}

/** `{since, until}` of a period of `days` days ending now on the hub's clock. */
export function period(context: Pick<TasksContext, 'store' | 'now'>, days: number): Omit<TaskAnalysis, 'scope'> {
  const until = context.now() + (context.store.getState().tasks.hubOffsetMs ?? 0);
  return { since: formatTime(until - days * DAY_MS), until: formatTime(until) };
}

export function analysisTitle(language: Locale): string {
  return TEMPLATES[language].title;
}

/** The fixed prompt; `name` is the workgroup's name (scope `wg`). */
export function analysisPrompt(language: Locale, analysis: TaskAnalysis, name: string): string {
  const scope = fill(SCOPES[language][analysis.scope], { name });
  return fill(TEMPLATES[language].prompt, { scope, since: analysis.since, until: analysis.until });
}

/** What the dialog asks for. */
export interface AnalysisRequest {
  readonly org: string;
  /** The workgroup the task lives in (and, for scope `wg`, the one analysed). */
  readonly wg: string;
  /** The workgroup's name (in the prompt of scope `wg`). */
  readonly name: string;
  readonly scope: Scope;
  readonly days: number;
  readonly language: Locale;
}

/** The analysis task prepared through the ordinary path (send it with `sendTask`; keep it to retry). */
export async function prepareAnalysis(context: TasksContext, request: AnalysisRequest): Promise<PreparedTask> {
  const analysis: TaskAnalysis = { scope: request.scope, ...period(context, request.days) };
  return prepareTask(context, {
    org: request.org,
    wg: request.wg,
    prompt: analysisPrompt(request.language, analysis, request.name),
    title: analysisTitle(request.language),
    analysis,
  });
}

/** The well-formed `analysis` of a task row, else null. */
export function analysisOf(value: unknown): TaskAnalysis | null {
  if (!isRecord(value)) return null;
  const { scope, since, until } = value;
  if ((scope !== 'org' && scope !== 'wg') || typeof since !== 'string' || typeof until !== 'string') return null;
  return { scope, since, until };
}

/** Whether a task is an analysis task. */
export function isAnalysis(task: Pick<TaskRow, 'analysis'>): boolean {
  return analysisOf(task.analysis) !== null;
}

export type AdoptLevel = 'workgroup' | 'org';

/** The board of a level for a task. */
export function adoptBoard(task: Pick<TaskRow, 'org_id' | 'wg_id'>, level: AdoptLevel): BoardLocation {
  return level === 'org' ? orgBoard(task.org_id) : wgBoard(task.org_id, task.wg_id);
}

/** The levels the user may adopt a report of `task` to (workgroup first). */
export function adoptLevels(
  state: OrgState | null,
  user: string | null,
  task: Pick<TaskRow, 'org_id' | 'wg_id'>,
): AdoptLevel[] {
  return (['workgroup', 'org'] as const).filter((level) => canWriteBoard(state, user, adoptBoard(task, level)));
}

/** Writes `text` as a new entry of the chosen board (refused with `forbidden` without the right); the entry ID. */
export async function adoptOutput(
  context: TasksContext,
  task: Pick<TaskRow, 'org_id' | 'wg_id'>,
  level: AdoptLevel,
  text: string,
): Promise<string> {
  return createEntry(context, adoptBoard(task, level), text, false);
}
