// The working modes of a task (ADR-098 section 3): the modes that are on as chips – the title in the user's language,
// the tooltip says who chose it (the member's name where known) and how ("in the web", "with the agent tool", "from the
// instruction", "passed on from task #n") – each with "Switch off" for writers (no confirmation: every mode fails safe
// when it stops). Writers may switch on a further mode of the catalogue; a mode that needs another that is off offers
// to switch on both in one step (E-05), and a `409 mode_needs` names the needed modes; a `403` says the right is
// missing. The chat renders no audit lines, so the newest choice is the notice line below the chips, read from the task
// row ("A38 switched on by <name> – from the instruction"); the row follows the live `task` frame. Nothing is shown
// for a task without modes when nothing can be switched on.
import { useState } from 'react';
import {
  missingNeeds,
  modeTitle,
  neededModes,
  newestMode,
  switchModeOff,
  switchModeOn,
  taskModes,
  type TaskMode,
} from '../../../core/modes/modes';
import { HubError } from '../../../core/net/signedFetch';
import type { TaskRow } from '../../../core/store/slices/tasks';
import type { Directory } from '../../../core/tasks/directory';
import { sendErrorCode } from '../../../core/tasks/send';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useModeCatalogue } from '../../modes/catalogue';
import { knownPersonName } from '../../names/names';
import { useAppEnv } from '../../shell/context';

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';

interface Failure {
  readonly code: string;
  readonly forbidden: boolean;
  /** The modes a `409 mode_needs` asks for. */
  readonly needs: readonly string[];
  /** The mode the failed step was about. */
  readonly mode: string;
}

export function TaskModes({
  row,
  writer,
  directory,
}: {
  readonly row: TaskRow;
  readonly writer: boolean;
  readonly directory: Directory;
}) {
  const { app } = useAppEnv();
  const { t, locale, error } = useI18n();
  const modes = taskModes(row);
  const { catalogue } = useModeCatalogue(modes.length > 0 || writer, true);
  const tasks = useStore((state) => state.tasks.tasks, app.store);
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<Failure | null>(null);
  const active = modes.map((item) => item.mode);
  const offered = writer ? catalogue.filter((entry) => !active.includes(entry.name)) : [];
  if (modes.length === 0 && offered.length === 0) return null;
  const title = (name: string) => modeTitle(catalogue, name, locale);
  const titles = (names: readonly string[]) => names.map(title).join(', ');
  const way = (item: TaskMode) => {
    if (item.way !== 'inherited') return t(`modes.way.${item.way}`);
    const number = item.origin_task === null ? undefined : tasks[item.origin_task]?.number;
    return number === undefined ? t('modes.way.inherited.unknown') : t('modes.way.inherited', { number });
  };
  const chosen = (item: TaskMode) => ({
    name: knownPersonName(t, directory, item.chosen_by) ?? t('modes.someone'),
    way: way(item),
  });
  const run = async (step: () => Promise<void>, mode: string) => {
    setFailed(null);
    setBusy(true);
    try {
      await step();
      setChoice('');
    } catch (caught) {
      setFailed({
        code: sendErrorCode(caught),
        forbidden: caught instanceof HubError && caught.status === 403,
        needs: neededModes(caught, catalogue, active, mode),
        mode,
      });
    } finally {
      setBusy(false);
    }
  };
  /** Switches on `names` in order (the needed ones first). */
  const on = (names: readonly string[]) =>
    void run(async () => {
      for (const name of names) await switchModeOn(app.tasks, row.task_id, name);
    }, names.at(-1)!);
  const off = (name: string) => void run(() => switchModeOff(app.tasks, row.task_id, name), name);
  const missing = choice === '' ? [] : missingNeeds(catalogue, active, choice);
  const newest = newestMode(modes);
  return (
    <div className="flex flex-col gap-2 text-sm" data-testid="task.modes">
      {modes.length > 0 && (
        <ul className="flex flex-wrap items-center gap-2" aria-label={t('modes.task.label')}>
          {modes.map((item) => (
            <li
              key={item.mode}
              className="flex items-center gap-1 rounded-full bg-indigo-50 pl-3 text-indigo-900"
              data-testid="task.modes.chip"
              data-mode={item.mode}
              data-way={item.way}
            >
              <span title={t('modes.chosen', chosen(item))} data-testid="task.modes.title">
                {title(item.mode)}
              </span>
              {writer && (
                <button
                  type="button"
                  className="min-h-11 rounded-full px-3 text-indigo-900 underline"
                  aria-label={t('modes.off.label', { title: title(item.mode) })}
                  disabled={busy}
                  onClick={() => off(item.mode)}
                  data-testid="task.modes.off"
                >
                  {t('modes.off')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {newest !== null && (
        <p className="text-gray-700" role="status" data-testid="task.modes.notice" data-mode={newest.mode}>
          {t('modes.notice', { title: title(newest.mode), ...chosen(newest) })}
        </p>
      )}
      {offered.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="min-h-11 rounded border border-gray-300 px-2"
            aria-label={t('modes.on.label')}
            value={choice}
            disabled={busy}
            onChange={(event) => {
              setFailed(null);
              setChoice(event.target.value);
            }}
            data-testid="task.modes.select"
          >
            <option value="">{t('modes.on.choose')}</option>
            {offered.map((entry) => (
              <option key={entry.name} value={entry.name} title={entry.summary[locale]}>
                {entry.title[locale]}
              </option>
            ))}
          </select>
          {choice !== '' && missing.length === 0 && (
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => on([choice])}
              data-testid="task.modes.on"
            >
              {t('modes.on')}
            </button>
          )}
          {missing.length > 0 && (
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => on([...missing].reverse().concat(choice))}
              data-testid="task.modes.on-both"
            >
              {t('modes.on.both', { modes: titles(missing) })}
            </button>
          )}
        </div>
      )}
      {failed !== null && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 text-red-800"
          data-testid="task.modes.error"
          data-code={failed.code}
        >
          <span>
            {failed.needs.length > 0
              ? t('modes.error.needs', { modes: titles(failed.needs) })
              : failed.forbidden
                ? t('modes.error.forbidden')
                : t('modes.error.failed', { reason: error(failed.code).title })}
          </span>
          {failed.needs.length > 0 && writer && (
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => on([...failed.needs, failed.mode])}
              data-testid="task.modes.error.both"
            >
              {t('modes.on.both', { modes: titles(failed.needs) })}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
