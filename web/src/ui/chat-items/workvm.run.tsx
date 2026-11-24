// Chat item `workvm.run` (13-work-vm.md sections 8 and 10): a job of the session's work VM – kind, command, a state
// badge, exit code and duration; the output tail in a collapsed monospace block ("show all" opens it). The timeline
// keeps only the latest event of a job, so a running item is replaced by its final one (`core/chat/workvm.ts`).
// A body that does not follow the contract is a neutral row with a code, never content.
import { formatNumber } from '../../core/i18n';
import type { TimelineItem } from '../../core/chat/timeline';
import { outputTruncated, parseWorkvmRun, type RunState } from '../../core/chat/workvm';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useSeconds } from '../countdown/parts';
import { useI18n } from '../i18n';
import { EventTime } from '../task-tabs/chat/EventTime';

const BADGE: Record<RunState, string> = {
  running: 'bg-blue-100 text-blue-900',
  exited: 'bg-green-100 text-green-900',
  killed: 'bg-amber-100 text-amber-900',
  timed_out: 'bg-amber-100 text-amber-900',
  lost: 'bg-red-100 text-red-900',
};

/** A verified body that does not follow the contract: a neutral row with the code (06-web.md section 1). */
export function Invalid() {
  const { t } = useI18n();
  return (
    <p className="text-sm text-gray-500" data-testid="chat.workvm.invalid">
      {t('task.chat.unverifiable', { code: 'body_invalid' })}
    </p>
  );
}

function Run({ item }: ChatItemProps) {
  const { at, body } = item as TimelineItem;
  const { t, locale } = useI18n();
  const seconds = useSeconds();
  const run = parseWorkvmRun(body);
  if (run === null) return <Invalid />;
  const failed = run.state !== 'running' && (run.state !== 'exited' || run.exit_code !== 0);
  return (
    <div
      className="flex flex-col gap-1 rounded border border-gray-200 px-3 py-2 text-sm"
      data-testid="chat.workvm.run"
      data-job={run.job_id}
      data-state={run.state}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-gray-600">{t(`workvm.run.kind.${run.kind}`)}</span>
        <span
          className={`rounded px-1 text-xs ${BADGE[run.state]}`}
          data-testid="chat.workvm.run.state"
          data-failed={failed}
        >
          {t(`workvm.run.state.${run.state}`)}
        </span>
        {run.exit_code !== null && (
          <span className="text-xs text-gray-600" data-testid="chat.workvm.run.exit">
            {t('workvm.run.exit', { code: run.exit_code })}
          </span>
        )}
        {run.state !== 'running' && (
          <span className="text-xs text-gray-500" data-testid="chat.workvm.run.duration">
            {t('workvm.run.duration', { seconds: seconds(run.duration_ms) })}
          </span>
        )}
        <EventTime at={at} />
      </div>
      <code className="break-all font-mono text-xs" data-testid="chat.workvm.run.command">
        {run.command}
      </code>
      {run.output === '' ? (
        <p className="text-xs text-gray-500" data-testid="chat.workvm.run.no-output">
          {t('workvm.run.output.empty')}
        </p>
      ) : (
        <details data-testid="chat.workvm.run.output">
          <summary className="flex min-h-11 cursor-pointer items-center gap-2 text-xs text-blue-800">
            {t('workvm.run.output')}
            {outputTruncated(run) && (
              <span className="text-gray-500" data-testid="chat.workvm.run.tail">
                {t('workvm.run.output.tail', { bytes: formatNumber(run.output_bytes, locale) })}
              </span>
            )}
          </summary>
          <pre
            className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded bg-gray-50 p-2 font-mono text-xs"
            data-testid="chat.workvm.run.text"
          >
            {run.output}
          </pre>
        </details>
      )}
    </div>
  );
}

export const chatItem: ChatItem = { kind: 'workvm.run', Component: Run };
