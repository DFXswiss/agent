// Chat item `test.run` (ADR-080, 13-work-vm.md section 12): a test run of the session in its own fresh VM – kind,
// a state badge (passed, failed, error, timed out, cancelled, running), exit code, duration, the time until the VM
// was ready, the command, the summary ("Tests: 3 failed, 12 passed"), the reason of an error or cancellation, the
// output tail in a collapsed block and "Open log" for the complete encrypted output (`ui/logs`). The timeline keeps
// only the latest event of a run (`core/chat/workvm.ts` `latestRuns`). A body that does not follow the contract is a
// neutral row with a code, never content.
import type { ReactNode } from 'react';
import type { TimelineItem } from '../../core/chat/timeline';
import {
  parseTestRun,
  runFailed,
  summaryCounts,
  type TestRun,
  type TestState,
  type TestSummary,
} from '../../core/chat/testrun';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useSeconds } from '../countdown/parts';
import { useI18n } from '../i18n';
import { EventLog } from '../logs/LogView';
import { EventTime } from '../task-tabs/chat/EventTime';
import { Invalid } from './workvm.run';

const BADGE: Record<TestState, string> = {
  running: 'bg-blue-100 text-blue-900',
  passed: 'bg-green-100 text-green-900',
  failed: 'bg-red-100 text-red-900',
  error: 'bg-red-100 text-red-900',
  timed_out: 'bg-amber-100 text-amber-900',
  cancelled: 'bg-gray-200 text-gray-800',
};

/** "Tests: 3 failed, 12 passed", or null when the summary has no counts. */
export function useSummaryText(): (summary: TestSummary | null) => string | null {
  const { t } = useI18n();
  return (summary) => {
    const parts = summaryCounts(summary);
    if (parts.length === 0) return null;
    return t('testrun.summary', {
      parts: parts.map(([part, count]) => t(`testrun.summary.${part}`, { count })).join(', '),
    });
  };
}

/** A test run with its result; `log` is the button of its log (or null). */
export function TestRunView({ run, at, log }: { readonly run: TestRun; readonly at: number; readonly log: ReactNode }) {
  const { t } = useI18n();
  const seconds = useSeconds();
  const summary = useSummaryText()(run.summary);
  const failed = runFailed(run);
  return (
    <div
      className="flex flex-col gap-1 rounded border border-gray-200 px-3 py-2 text-sm"
      data-testid="chat.test.run"
      data-run={run.run_id}
      data-state={run.state}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-gray-600">{t(`testrun.kind.${run.kind}`)}</span>
        <span
          className={`rounded px-1 text-xs ${BADGE[run.state]}`}
          data-testid="chat.test.run.state"
          data-failed={failed}
        >
          {t(`testrun.state.${run.state}`)}
        </span>
        {run.exit_code !== null && (
          <span className="text-xs text-gray-600" data-testid="chat.test.run.exit">
            {t('testrun.exit', { code: run.exit_code })}
          </span>
        )}
        {run.state !== 'running' && (
          <span className="text-xs text-gray-500" data-testid="chat.test.run.duration">
            {t('testrun.duration', { seconds: seconds(run.duration_ms) })}
          </span>
        )}
        {run.ready_ms > 0 && (
          <span className="text-xs text-gray-500" data-testid="chat.test.run.ready">
            {t('testrun.ready', { seconds: seconds(run.ready_ms) })}
          </span>
        )}
        <EventTime at={at} />
      </div>
      <code className="break-all font-mono text-xs" data-testid="chat.test.run.command">
        {run.command}
      </code>
      {summary !== null && (
        <p className="font-medium" data-testid="chat.test.run.summary" data-failed={failed}>
          {summary}
        </p>
      )}
      {run.reason !== null && (
        <p className="text-xs text-gray-600" data-testid="chat.test.run.reason">
          {t('testrun.reason', { reason: t(`testrun.reason.${run.reason}`) })}
        </p>
      )}
      {run.tail === '' ? (
        <p className="text-xs text-gray-500" data-testid="chat.test.run.no-tail">
          {t('testrun.tail.empty')}
        </p>
      ) : (
        <details data-testid="chat.test.run.tail">
          <summary className="flex min-h-11 cursor-pointer items-center text-xs text-blue-800">
            {t('testrun.tail')}
          </summary>
          <pre
            className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded bg-gray-50 p-2 font-mono text-xs"
            data-testid="chat.test.run.text"
          >
            {run.tail}
          </pre>
        </details>
      )}
      {log}
    </div>
  );
}

function Item({ item }: ChatItemProps) {
  const { at, body, seq } = item as TimelineItem;
  const { t } = useI18n();
  const run = parseTestRun(body);
  if (run === null) return <Invalid />;
  const log =
    run.log === null ? null : (
      <EventLog seq={seq} body={body} runId={run.run_id} log={run.log} label={t('testrun.log')} />
    );
  return <TestRunView run={run} at={at} log={log} />;
}

export const chatItem: ChatItem = { kind: 'test.run', Component: Item };
