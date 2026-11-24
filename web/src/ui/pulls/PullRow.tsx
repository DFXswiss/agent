// One linked pull request (KOR-48, ADR-084; the workgroup's "Pull requests" view and the task's panel): the pull
// request (`owner/name#n` and its title, or "details unavailable" for a link whose body is missing or does not open),
// its state (open, draft, merged, closed) and for an open one the conflict state ("Conflict", "Being checked" – never
// "No conflict" before GitHub computed it – or "No conflict"), the link to GitHub (https only), the task it is linked
// to, "Conflicts fixed, branch updated" with the merge commit, and the controls:
//
// - "Fix conflicts" only for a conflict of a pull request in the same repository and only for users who may write
//   prompts to the task; a fork's pull request says that the button is not available there;
// - "Check again" reads the state once more;
// - a request the device refused with `github_not_connected` offers "Connect GitHub" right in the row (E-05), then
//   reads again;
// - "Unlink" for a link a person made (users with write access to the workgroup).
//
// Text from GitHub (the title, the branch) is untrusted and only ever rendered as text. The row stacks on a phone.
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { pullLabel } from '../../core/pulls/address';
import { fixConflicts } from '../../core/pulls/fix';
import { linkErrorCode, unlinkPull } from '../../core/pulls/links';
import { checkPulls } from '../../core/pulls/status';
import { canFix } from '../../core/pulls/view';
import type { PullLink } from '../../core/pulls/state';
import { sendErrorCode } from '../../core/tasks/send';
import { useI18n } from '../i18n';
import { GithubConnect } from '../settings/sections/github/Connect';
import { useReason } from '../settings/sections/github/reason';
import { useAppEnv } from '../shell/context';
import { usePull, useTaskTitle } from './hooks';

const BUTTON = 'min-h-11 rounded px-4 disabled:opacity-50';
const PRIMARY = `${BUTTON} bg-blue-700 text-white`;
const SECONDARY = `${BUTTON} border border-gray-300`;

const STATE_BADGE: Readonly<Record<string, string>> = {
  open: 'bg-green-100 text-green-900',
  draft: 'bg-gray-200 text-gray-800',
  merged: 'bg-purple-100 text-purple-900',
  closed: 'bg-red-100 text-red-900',
};

const CONFLICT_BADGE: Readonly<Record<string, string>> = {
  conflict: 'bg-red-100 text-red-900',
  checking: 'bg-amber-100 text-amber-900',
  clean: 'bg-green-100 text-green-900',
};

type Action = { kind: 'idle' } | { kind: 'busy' } | { kind: 'sent' } | { kind: 'error'; code: string };

function TaskLink({ org, task }: { readonly org: string; readonly task: string }) {
  const { t } = useI18n();
  const title = useTaskTitle(task);
  const text =
    title.status === 'verified'
      ? title.text
      : title.status === 'missing'
        ? t('pulls.task.loading')
        : t('pulls.task.unverifiable', { code: title.code });
  return (
    <Link
      className="inline-flex min-h-11 items-center break-words text-blue-800 underline"
      to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(task)}`}
      data-testid="pulls.task"
    >
      {t('pulls.task', { title: text })}
    </Link>
  );
}

export function PullRow({
  org,
  link,
  writer,
  showTask,
}: {
  readonly org: string;
  readonly link: PullLink;
  /** The user may write in the link's workgroup (prompts to the task, link and unlink). */
  readonly writer: boolean;
  /** Name the linked task (the workgroup's view; not inside the task's own panel). */
  readonly showTask: boolean;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const reason = useReason();
  const { pull, wg, taskId: task } = link;
  const { display, fixed } = usePull({ org, wg, task }, pull);
  const [action, setAction] = useState<Action>({ kind: 'idle' });
  const busy = action.kind === 'busy';
  // offered only for a link whose pull request is known ("Check again", Connect GitHub after a failed read)
  const recheck = () => checkPulls(app.tasks, { org, wg, task }, [pull!], { force: true });
  const run = async (step: () => Promise<unknown>, code: (failure: unknown) => string, done: Action) => {
    setAction({ kind: 'busy' });
    try {
      await step();
      setAction(done);
    } catch (failure) {
      setAction({ kind: 'error', code: code(failure) });
    }
  };
  const state = display.kind === 'found' ? display.state : display.kind;
  const conflict = display.kind === 'found' ? display.conflict : null;
  return (
    <div
      className="flex min-w-0 flex-col gap-2 border-b border-gray-100 px-2 py-3"
      data-testid="pulls.item"
      data-pull={pull === null ? '' : pullLabel(pull)}
      data-task={task}
      data-state={state}
      data-conflict={conflict ?? ''}
    >
      <div className="flex min-w-0 flex-col gap-1 sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-3">
        <span className="break-all font-mono" data-testid="pulls.name">
          {pull === null ? t('pulls.unavailable') : pullLabel(pull)}
        </span>
        {display.kind === 'found' && (
          <span className="break-words" data-testid="pulls.title">
            {display.title}
          </span>
        )}
      </div>
      {pull === null && (
        <p className="text-sm text-gray-700" data-testid="pulls.unavailable" data-code={link.code!}>
          {t(link.code === 'legacy' ? 'pulls.unavailable.legacy' : 'pulls.unavailable.body')}
        </p>
      )}
      {pull !== null && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span
            className={`rounded px-2 ${STATE_BADGE[state] ?? 'bg-gray-100 text-gray-800'}`}
            data-testid="pulls.state"
            data-state={state}
          >
            {display.kind === 'failed'
              ? t('pulls.state.failed', { reason: reason(display.code) })
              : t(`pulls.state.${state}`)}
          </span>
          {display.kind === 'missing' && (
            <span className="text-gray-700" data-testid="pulls.missing" data-code={display.code}>
              {t(`pulls.missing.${display.code}`)}
            </span>
          )}
          {conflict !== null && (
            <span
              className={`rounded px-2 ${CONFLICT_BADGE[conflict]}`}
              data-testid="pulls.conflict"
              data-conflict={conflict}
            >
              {t(`pulls.conflict.${conflict}`)}
            </span>
          )}
          {display.kind === 'found' && (
            <a
              className="inline-flex min-h-11 items-center text-blue-800 underline"
              href={display.url}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="pulls.github"
            >
              {t('pulls.github')}
            </a>
          )}
          {showTask && <TaskLink org={org} task={task} />}
        </div>
      )}
      {pull === null && showTask && (
        <div className="text-sm">
          <TaskLink org={org} task={task} />
        </div>
      )}
      {fixed !== null && (
        <p role="status" className="text-sm text-green-800" data-testid="pulls.fixed" data-commit={fixed.commit}>
          {t('pulls.fixed', { commit: fixed.commit.slice(0, 7) })}
        </p>
      )}
      {display.kind === 'found' && display.conflict === 'conflict' && display.fork && (
        <p className="text-sm text-gray-700" data-testid="pulls.fork">
          {t('pulls.fork')}
        </p>
      )}
      {display.kind === 'failed' && display.code === 'github_not_connected' && (
        <div className="flex flex-col gap-2" data-testid="pulls.connect">
          <p className="text-sm">{t('pulls.connect')}</p>
          <GithubConnect onConnected={recheck} />
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {canFix(display, writer) && (
          <button
            type="button"
            className={PRIMARY}
            disabled={busy}
            onClick={() => void run(() => fixConflicts(app.tasks, task, pull!), sendErrorCode, { kind: 'sent' })}
            data-testid="pulls.fix"
          >
            {t('pulls.fix')}
          </button>
        )}
        {pull !== null && display.kind !== 'checking' && (
          <button type="button" className={SECONDARY} onClick={recheck} data-testid="pulls.recheck">
            {t('pulls.recheck')}
          </button>
        )}
        {writer && link.linkedBy !== null && (
          <button
            type="button"
            className={SECONDARY}
            disabled={busy}
            onClick={() => void run(() => unlinkPull(app.tasks, link), linkErrorCode, { kind: 'idle' })}
            data-testid="pulls.unlink"
          >
            {t('pulls.unlink')}
          </button>
        )}
      </div>
      {action.kind === 'sent' && (
        <p role="status" className="text-sm text-green-800" data-testid="pulls.fix.sent">
          {t('pulls.fix.sent')}
        </p>
      )}
      {action.kind === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="pulls.error" data-code={action.code}>
          {error(action.code).title}
        </p>
      )}
    </div>
  );
}
