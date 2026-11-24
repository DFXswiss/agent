// "Paste a pull-request address" (KOR-48, ADR-084 section 1, idea 40): links a pull request to a task – the task's
// panel links to its own task, the workgroup's view lets the person choose one of the workgroup's tasks (the newest
// first, decrypted titles). The address is `https://github.com/<owner>/<repo>/pull/<n>` or `<owner>/<repo>#<n>`; a
// pull request linked already (under any epoch the browser holds) is shown with its task and nothing is changed.
// Only for users who may write in the workgroup.
import { useEffect, useId, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { parsePullAddress } from '../../core/pulls/address';
import { linkErrorCode, linkPull, type LinkOutcome } from '../../core/pulls/links';
import type { TaskRow } from '../../core/store/slices/tasks';
import { loadTasks, PAGE_SIZE, selectListRows } from '../../core/tasks/list';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { useTitle, useTitles } from '../screens/tasks/hooks';
import { useTaskTitle } from './hooks';

type Status =
  | { kind: 'idle' }
  | { kind: 'busy' }
  | { kind: 'invalid' }
  | { kind: 'done'; outcome: LinkOutcome }
  | { kind: 'error'; code: string };

function TaskOption({ row }: { readonly row: TaskRow }) {
  const { t } = useI18n();
  const title = useTitle(row);
  return <option value={row.task_id}>{title.status === 'verified' ? title.text : t('pulls.task.untitled')}</option>;
}

/**
 * The workgroup's tasks to choose from: the first page of its list (`PAGE_SIZE`, newest activity first), also when the
 * task list has loaded further pages (ADR-086 section 1: a bounded choice).
 */
function TaskChoice({
  wg,
  value,
  onChange,
}: {
  readonly wg: string;
  readonly value: string;
  readonly onChange: (task: string) => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const id = useId();
  const loaded = useStore(selectListRows(wg, null), app.store);
  const rows = useMemo(() => loaded.slice(0, PAGE_SIZE), [loaded]);
  useTitles(rows);
  useEffect(() => {
    void loadTasks(app.tasks, wg, null);
  }, [app, wg]);
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm">
        {t('pulls.link.task')}
      </label>
      <select
        id={id}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        data-testid="pulls.link.task"
      >
        <option value="">{t('pulls.link.task.choose')}</option>
        {rows.map((row) => (
          <TaskOption key={row.task_id} row={row} />
        ))}
      </select>
    </div>
  );
}

function ExistingTask({ org, task }: { readonly org: string; readonly task: string }) {
  const { t } = useI18n();
  const title = useTaskTitle(task);
  return (
    <Link
      className="inline-flex min-h-11 items-center text-blue-800 underline"
      to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(task)}`}
      data-testid="pulls.link.exists.task"
    >
      {title.status === 'verified' ? title.text : t('pulls.task.untitled')}
    </Link>
  );
}

export function LinkForm({
  org,
  wg,
  task: fixed,
}: {
  readonly org: string;
  readonly wg: string;
  /** The task to link to (the task's panel); without it the person chooses one. */
  readonly task?: string;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const id = useId();
  const [address, setAddress] = useState('');
  const [chosen, setChosen] = useState('');
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  const task = fixed ?? chosen;
  const busy = status.kind === 'busy';
  const submit = async () => {
    const ref = parsePullAddress(address);
    if (ref === null) {
      setStatus({ kind: 'invalid' });
      return;
    }
    setStatus({ kind: 'busy' });
    try {
      const outcome = await linkPull(app.tasks, org, wg, task, ref);
      if (outcome.status === 'created') setAddress('');
      setStatus({ kind: 'done', outcome });
    } catch (failure) {
      setStatus({ kind: 'error', code: linkErrorCode(failure) });
    }
  };
  return (
    <form
      className="flex flex-col gap-2 rounded border border-gray-200 p-3"
      aria-label={t('pulls.link.title')}
      onSubmit={(event) => {
        event.preventDefault();
        if (!busy && task !== '') void submit();
      }}
      data-testid="pulls.link"
    >
      <h2 className="font-semibold">{t('pulls.link.title')}</h2>
      <label htmlFor={id} className="text-sm">
        {t('pulls.link.label')}
      </label>
      <input
        id={id}
        className="min-h-11 w-full min-w-0 rounded border border-gray-300 px-2"
        value={address}
        placeholder={t('pulls.link.placeholder')}
        onChange={(event) => setAddress(event.target.value)}
        disabled={busy}
        data-testid="pulls.link.input"
      />
      {fixed === undefined && <TaskChoice wg={wg} value={chosen} onChange={setChosen} />}
      <button
        type="submit"
        className="min-h-11 self-start rounded bg-blue-700 px-4 text-white disabled:opacity-50"
        disabled={busy || task === '' || address.trim() === ''}
        data-testid="pulls.link.submit"
      >
        {t('pulls.link.submit')}
      </button>
      {status.kind === 'invalid' && (
        <p role="alert" className="text-sm text-red-800" data-testid="pulls.link.invalid">
          {t('pulls.link.invalid')}
        </p>
      )}
      {status.kind === 'done' && status.outcome.status === 'created' && (
        <p role="status" className="text-sm text-green-800" data-testid="pulls.link.created">
          {t('pulls.link.created')}
        </p>
      )}
      {status.kind === 'done' && status.outcome.status === 'exists' && (
        <div role="status" className="flex flex-col gap-1 text-sm" data-testid="pulls.link.exists">
          <p>{t('pulls.link.exists')}</p>
          <ul className="flex flex-col gap-1">
            {[...new Set(status.outcome.links.map((link) => link.taskId))].map((other) => (
              <li key={other}>
                <ExistingTask org={org} task={other} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {status.kind === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="pulls.link.error" data-code={status.code}>
          {error(status.code).title}
        </p>
      )}
    </form>
  );
}
