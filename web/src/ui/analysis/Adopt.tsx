// "Adopt" of an analysis report (ADR-097 section 5): on every output of an analysis task the person may write the
// output's whole text as a new entry of the task's workgroup whiteboard or of the organization whiteboard – only the
// boards the person may write are offered, and nothing is shown without one. The small dialog names who will read
// the entry (the organization level repeats the sentence of section 7 for all members of the organization); the entry
// is sealed and signed by the person (`adoptOutput`, the existing whiteboard path). Success links the board.
import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { type AdoptLevel, adoptBoard, adoptLevels, adoptOutput, isAnalysis } from '../../core/analysis/analysis';
import type { TaskRow } from '../../core/store/slices/tasks';
import { selectDirectory } from '../../core/tasks/directory';
import { sendErrorCode } from '../../core/tasks/send';
import { boardPath } from '../../core/whiteboards/location';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { Sheet } from '../responsive/Sheet';
import { useOrgView } from '../screens/tasks/hooks';
import { useAppEnv } from '../shell/context';
import '../../core/i18n/lazy/analysis';

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';

type Status =
  | { readonly kind: 'idle' }
  | { readonly kind: 'saving' }
  | { readonly kind: 'done'; readonly level: AdoptLevel }
  | { readonly kind: 'failed'; readonly code: string };

function AdoptDialog({
  row,
  text,
  levels,
  onClose,
}: {
  readonly row: TaskRow;
  readonly text: string;
  readonly levels: readonly AdoptLevel[];
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const directory = useStore(selectDirectory(row.org_id), app.store);
  const group = useId();
  const [level, setLevel] = useState<AdoptLevel>(levels[0]!);
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  const saving = status.kind === 'saving';
  const save = () => {
    setStatus({ kind: 'saving' });
    adoptOutput(app.tasks, row, level, text).then(
      () => setStatus({ kind: 'done', level }),
      (caught: unknown) => setStatus({ kind: 'failed', code: sendErrorCode(caught) }),
    );
  };
  const name = directory.workgroups.get(row.wg_id) ?? t('tasks.wg.unnamed');
  return (
    <Sheet title={t('analysis.adopt.title')} testId="analysis.adopt.dialog" busy={saving} onClose={onClose}>
      <fieldset className="flex flex-col gap-2" disabled={saving || status.kind === 'done'}>
        <legend id={group} className="text-sm font-medium">
          {t('analysis.adopt.level')}
        </legend>
        {levels.map((item) => (
          <label key={item} className="flex min-h-11 items-center gap-2">
            <input
              type="radio"
              name={group}
              value={item}
              checked={level === item}
              onChange={() => setLevel(item)}
              data-testid={`analysis.adopt.level.${item}`}
            />
            {t(`analysis.adopt.level.${item}`)}
          </label>
        ))}
      </fieldset>
      <p className="text-sm" data-testid="analysis.adopt.visibility" data-level={level}>
        {level === 'org' ? t('analysis.adopt.visibility.org') : t('analysis.visibility.wg', { name })}
      </p>
      {status.kind === 'done' && (
        <p role="status" className="text-sm text-green-800" data-testid="analysis.adopt.done">
          {t('analysis.adopt.done')}{' '}
          <Link className="underline" to={boardPath(adoptBoard(row, status.level))} data-testid="analysis.adopt.open">
            {t('analysis.adopt.open')}
          </Link>
        </p>
      )}
      {status.kind === 'failed' && (
        <p role="alert" className="text-sm text-red-800" data-testid="analysis.adopt.error" data-code={status.code}>
          {t('analysis.adopt.failed', { reason: error(status.code).title })}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {status.kind !== 'done' && (
          <button
            type="button"
            className="min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50"
            onClick={save}
            disabled={saving}
            data-testid="analysis.adopt.save"
          >
            {saving ? t('analysis.adopt.saving') : t('analysis.adopt.save')}
          </button>
        )}
        <button
          type="button"
          className={BUTTON}
          onClick={onClose}
          disabled={saving}
          data-testid="analysis.adopt.cancel"
        >
          {t('analysis.cancel')}
        </button>
      </div>
    </Sheet>
  );
}

function AdoptButton({ row, text }: { readonly row: TaskRow; readonly text: string }) {
  const { t } = useI18n();
  const { state, userId } = useOrgView(row.org_id);
  const [open, setOpen] = useState(false);
  const levels = adoptLevels(state, userId, row);
  if (levels.length === 0) return null;
  return (
    <>
      <button
        type="button"
        className={`${BUTTON} self-start`}
        aria-haspopup="dialog"
        onClick={() => setOpen(true)}
        data-testid="analysis.adopt"
      >
        {t('analysis.adopt')}
      </button>
      {open && <AdoptDialog row={row} text={text} levels={levels} onClose={() => setOpen(false)} />}
    </>
  );
}

/** "Adopt" below an output of session `session`, shown only when its task is an analysis task. */
export function AdoptAction({ session, text }: { readonly session: string | null; readonly text: string }) {
  const { app } = useAppEnv();
  const row = useStore((state) => {
    const task = session === null ? undefined : state.tasks.sessions[session]?.row.task_id;
    return task === undefined ? null : (state.tasks.tasks[task] ?? null);
  }, app.store);
  if (row === null || !isAnalysis(row) || text === '') return null;
  return <AdoptButton row={row} text={text} />;
}
