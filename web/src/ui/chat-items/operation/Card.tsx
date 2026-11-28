// The operation card (06-web.md sections 5.2 and 5.4), shared by the chat items `operation.params` /
// `operation.result` and the Operations tab: type, live state badge (from the `operation` frames), "Triggered by
// <name>" for the person whose instruction it followed from (`triggered_by`, ADR-083), a short subject from the
// decrypted parameters, the result links and values (PR URL, commit SHA, comment URL, branch, subagent session, issue,
// login), "Renamed by session" for `task.set_title`, a test run's summary with "Open log" (`test.run`, ADR-080), the
// error code, and "Duplicate of …" linking the original. Listed issues and pull requests show number (linked), title,
// state and labels; a read issue shows its text and its page of comments. Text from GitHub is untrusted: it is only
// rendered as React text (never as HTML or Markdown), links are https only (`httpsUrl`). Unverifiable parameters or
// results are shown as such with their code, never as content.
import { useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { LINK_REL } from '../../../core/chat/links';
import { runFailed, type TestRunResult } from '../../../core/chat/testrun';
import { ensureOperations, loadOperation } from '../../../core/operations/load';
import {
  type Fact,
  ISSUE_VALUES,
  type IssueContent,
  type ListEntry,
  type Listing,
  type OperationView,
  STOP_VALUES,
  selectOperation,
} from '../../../core/operations/view';
import { useStore } from '../../hooks/useStore';
import { OpenLog } from '../../logs/LogView';
import { useSummaryText } from '../test.run';
import { TriggeredBy } from '../../task-tabs/people';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

export const KNOWN_TYPES: ReadonlySet<string> = new Set([
  'github.list_repositories',
  'github.checkout',
  'github.get_pull_request',
  'github.create_branch',
  'github.commit',
  'github.push',
  'github.create_pull_request',
  'github.comment',
  'github.list_issues',
  'github.get_issue',
  'github.create_issue',
  'github.update_issue',
  'github.update_pull_request',
  'github.list_pull_requests',
  'github.request_connection',
  'subagent.request',
  'subagent.instruct',
  'subagent.stop',
  'task.set_title',
  'human.question',
  'human.approval',
  'workvm.request',
  'workvm.release',
  'test.run',
  'browser.open',
  'browser.close',
]);

export const STATE_BADGE: Readonly<Record<string, string>> = {
  succeeded: 'bg-green-100 text-green-900',
  executing: 'bg-blue-100 text-blue-900',
  failed: 'bg-red-100 text-red-900',
  rejected: 'bg-red-100 text-red-900',
  denied: 'bg-red-100 text-red-900',
  expired: 'bg-gray-200 text-gray-800',
  duplicate: 'bg-gray-200 text-gray-800',
};

/** The label of an operation type (the raw type for types this client does not know). */
export function useTypeLabel(): (type: string) => string {
  const { t } = useI18n();
  return (type) => (KNOWN_TYPES.has(type) ? t(`operations.type.${type}`) : type);
}

export function StateBadge({ state }: { readonly state: string }) {
  const { t } = useI18n();
  return (
    <span
      className={`rounded px-2 text-xs ${STATE_BADGE[state] ?? 'bg-amber-100 text-amber-900'}`}
      data-testid="operation.state"
      data-state={state}
    >
      {t(`operations.state.${state}`)}
    </span>
  );
}

/** The operations tab of a task with one operation opened. */
export const operationLink = (org: string, task: string, operation: string): string =>
  `/o/${encodeURIComponent(org)}/t/${encodeURIComponent(task)}?tab=operations&op=${encodeURIComponent(operation)}`;

function FactItem({ fact, org }: { readonly fact: Fact; readonly org: string }) {
  const { t } = useI18n();
  const label = t(`operations.fact.${fact.label}`);
  const short = fact.label === 'commit' ? fact.text.slice(0, 12) : fact.text;
  let value;
  if (fact.href !== undefined)
    value = (
      <a className="text-blue-800 underline" href={fact.href} target="_blank" rel={LINK_REL}>
        {short}
      </a>
    );
  else if (fact.session !== undefined)
    value = (
      <Link
        className="text-blue-800 underline"
        to={`/o/${encodeURIComponent(org)}/s/${encodeURIComponent(fact.session)}`}
      >
        {short.slice(0, 8)}
      </Link>
    );
  else if ((fact.label === 'state' || fact.label === 'state_reason') && ISSUE_VALUES.has(fact.text))
    value = <span>{t(`operations.value.${fact.text}`)}</span>;
  else if (fact.label === 'stop_state' && STOP_VALUES.has(fact.text))
    value = <span>{t(`operations.value.${fact.text}`)}</span>;
  else value = <span className={fact.label === 'commit' ? 'font-mono' : ''}>{short}</span>;
  return (
    <li data-testid="operation.fact" data-label={fact.label} title={fact.text}>
      {label}: {value}
    </li>
  );
}

function IssueNumber({ entry }: { readonly entry: ListEntry }) {
  return entry.href === undefined ? (
    <span>#{entry.number}</span>
  ) : (
    <a className="text-blue-800 underline" href={entry.href} target="_blank" rel={LINK_REL}>
      #{entry.number}
    </a>
  );
}

/** Number, title, state (closed, draft) and labels of an issue or pull request. */
function EntryHead({ entry }: { readonly entry: ListEntry }) {
  const { t } = useI18n();
  return (
    <>
      <IssueNumber entry={entry} /> <span data-testid="operation.entry.title">{entry.title}</span>
      {entry.state === 'closed' && (
        <span className="ml-1 rounded bg-gray-200 px-1 text-xs text-gray-800" data-testid="operation.entry.state">
          {t('operations.value.closed')}
        </span>
      )}
      {entry.draft && (
        <span className="ml-1 rounded bg-gray-200 px-1 text-xs text-gray-800" data-testid="operation.entry.draft">
          {t('operations.draft')}
        </span>
      )}
      {entry.labels.map((label) => (
        <span
          key={label}
          className="ml-1 rounded bg-indigo-50 px-1 text-xs text-indigo-900"
          data-testid="operation.entry.label"
        >
          {label}
        </span>
      ))}
    </>
  );
}

function ListingView({ listing }: { readonly listing: Listing }) {
  const { t } = useI18n();
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="operation.listing" data-kind={listing.kind}>
      {listing.entries.length === 0 ? (
        <p className="text-gray-600" data-testid="operation.listing.empty">
          {t(`operations.listing.empty.${listing.kind}`)}
        </p>
      ) : (
        <ul className="flex max-h-80 flex-col gap-1 overflow-y-auto">
          {listing.entries.map((entry) => (
            <li key={entry.number} className="break-words" data-testid="operation.entry" data-number={entry.number}>
              <EntryHead entry={entry} />
              {entry.author !== null && <span className="ml-1 text-xs text-gray-600">{entry.author}</span>}
            </li>
          ))}
        </ul>
      )}
      {listing.nextPage !== null && (
        <p className="text-xs text-gray-600" data-testid="operation.listing.next">
          {t('operations.listing.next', { page: listing.nextPage })}
        </p>
      )}
      {listing.truncated && (
        <p className="text-xs text-gray-600" data-testid="operation.truncated">
          {t('operations.truncated')}
        </p>
      )}
    </div>
  );
}

function IssueView({ issue }: { readonly issue: IssueContent }) {
  const { t } = useI18n();
  return (
    <div className="flex flex-col gap-1 text-sm" data-testid="operation.issue" data-number={issue.number}>
      <p className="break-words">
        <EntryHead entry={issue} />
      </p>
      <p className="text-xs text-gray-600">
        {issue.author !== null && <span data-testid="operation.issue.author">{issue.author}</span>}
        {issue.stateReason !== null && (
          <span className="ml-1" data-testid="operation.issue.reason">
            ({t(`operations.value.${issue.stateReason}`)})
          </span>
        )}
        {issue.pullRequest && (
          <span className="ml-1" data-testid="operation.issue.pull-request">
            {t('operations.issue.pull-request')}
          </span>
        )}
      </p>
      {issue.body !== null && issue.body !== '' && (
        <p
          className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-2"
          data-testid="operation.issue.body"
        >
          {issue.body}
        </p>
      )}
      <p className="text-xs text-gray-600" data-testid="operation.issue.comments-count">
        {t('operations.issue.comments', { shown: issue.comments.length, total: issue.commentsTotal ?? '?' })}
      </p>
      {issue.comments.length > 0 && (
        <ol className="flex max-h-80 flex-col gap-2 overflow-y-auto">
          {issue.comments.map((comment) => (
            <li key={comment.id} className="rounded border border-gray-100 p-2" data-testid="operation.issue.comment">
              <p className="text-xs text-gray-600">
                {comment.author ?? '?'}
                {comment.createdAt !== null && (
                  <time className="ml-1" dateTime={comment.createdAt}>
                    {comment.createdAt.slice(0, 10)}
                  </time>
                )}
              </p>
              <p className="whitespace-pre-wrap break-words">{comment.body}</p>
            </li>
          ))}
        </ol>
      )}
      {issue.commentsNextPage !== null && (
        <p className="text-xs text-gray-600" data-testid="operation.issue.next">
          {t('operations.issue.next', { page: issue.commentsNextPage })}
        </p>
      )}
      {issue.truncated && (
        <p className="text-xs text-gray-600" data-testid="operation.truncated">
          {t('operations.truncated')}
        </p>
      )}
    </div>
  );
}

/** "Tests: 3 failed" (or the state without a summary) and the button of the run's log. */
function TestRunLine({ run, wg, task }: { readonly run: TestRunResult; readonly wg: string; readonly task: string }) {
  const { t } = useI18n();
  const summary = useSummaryText()(run.summary);
  return (
    <div
      className="flex flex-wrap items-center gap-x-2 text-sm"
      data-testid="operation.test-run"
      data-state={run.state}
      data-failed={runFailed(run)}
    >
      <span data-testid="operation.test-run.summary">{summary ?? t(`testrun.state.${run.state}`)}</span>
      {run.log !== null && <OpenLog source={{ wg, task, ref: run.run_id, log: run.log }} label={t('testrun.log')} />}
    </div>
  );
}

export function CardBody({
  view,
  org,
  wg,
  task,
}: {
  readonly view: OperationView;
  readonly org: string;
  readonly wg: string;
  readonly task: string;
}) {
  const { t, error } = useI18n();
  const typeLabel = useTypeLabel();
  const renamed = view.type === 'task.set_title';
  return (
    <div
      className="flex flex-col gap-1"
      data-testid="operation.card"
      data-operation={view.id}
      data-type={view.type}
      data-state={view.state}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium" data-testid="operation.type">
          {typeLabel(view.type)}
        </span>
        <StateBadge state={view.state} />
        {renamed && (
          <span className="text-xs text-gray-600" data-testid="operation.renamed">
            {t('operations.renamed')}
          </span>
        )}
        <TriggeredBy org={org} user={view.row?.triggered_by} testId="operation.triggered-by" />
      </div>
      {view.subject !== null && (
        <p className="break-all text-sm" data-testid="operation.subject">
          {view.subject}
        </p>
      )}
      {view.facts.length > 0 && (
        <ul className="flex flex-col text-sm" data-testid="operation.facts">
          {view.facts.map((fact) => (
            <FactItem key={`${fact.label} ${fact.text}`} fact={fact} org={org} />
          ))}
        </ul>
      )}
      {view.testRun !== null && <TestRunLine run={view.testRun} wg={wg} task={task} />}
      {view.listing !== null && <ListingView listing={view.listing} />}
      {view.issue !== null && <IssueView issue={view.issue} />}
      {view.duplicateOf !== null && (
        <Link
          className="text-sm text-blue-800 underline"
          to={operationLink(org, task, view.duplicateOf)}
          data-testid="operation.duplicate-of"
        >
          {t('operations.duplicate-of', { id: view.duplicateOf.slice(0, 8) })}
        </Link>
      )}
      {view.errorCode !== null && (
        <p className="text-sm text-red-800" data-testid="operation.error" data-code={view.errorCode}>
          {t('operations.error', { reason: error(view.errorCode).title })}
        </p>
      )}
      {view.params.status === 'unverifiable' && (
        <p className="text-sm text-gray-500" data-testid="operation.params.unverifiable">
          {t('operations.params.unverifiable', { code: view.params.code })}
        </p>
      )}
      {view.result.status === 'unverifiable' && (
        <p className="text-sm text-gray-500" data-testid="operation.result.unverifiable">
          {t('operations.result.unverifiable', { code: view.result.code })}
        </p>
      )}
    </div>
  );
}

/** The card of `operation` in workgroup `wg` with its live row (module comment). */
export function OperationCard({
  org,
  wg,
  task,
  operation,
}: {
  readonly org: string;
  readonly wg: string;
  readonly task: string;
  readonly operation: string;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const view = useStore(
    useMemo(() => selectOperation(wg, operation), [wg, operation]),
    app.store,
  );
  const round = useStore((state) => state.operations.round, app.store);
  // the task's list once per live round (lost frames); an operation beyond its first page by ID
  useEffect(() => {
    ensureOperations(app.tasks, task)
      .then(() => app.store.getState().operations.rows[operation] ?? loadOperation(app.tasks, operation))
      .catch(() => undefined);
  }, [app, task, operation, round]);
  return (
    <div className="flex flex-col gap-1 rounded border border-gray-200 p-3">
      <CardBody view={view} org={org} wg={wg} task={task} />
      <Link
        className="min-h-11 self-start py-2 text-sm text-blue-800 underline"
        to={operationLink(org, task, operation)}
        data-testid="operation.open"
      >
        {t('operations.open')}
      </Link>
    </div>
  );
}
