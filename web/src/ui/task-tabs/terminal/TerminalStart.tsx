// The start of the main session on the task tab "Claude Code" (KONZEPT E-43, ADR-104), as terminal lines instead of
// the countdown panel's card: the countdown of 06-web.md section 4.4 (`useCountdown`) with the remaining seconds, then
// "Delayed" with the current step and the elapsed time, or the real waiting reason with the step the user can take
// (`ReasonText`, `WaitingAction`); below it the status steps (✓ done, → current, ✕ failed, · pending) with their
// elapsed time. After a failed start the catalog error and – for writers – "Retry" (the same prompt as a follow-up).
// The lines are announced politely (06-web.md section 12.3).
import { type ReactNode, useState } from 'react';
import type { StepStatus } from '../../../core/tasks/countdown';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { sendErrorCode } from '../../../core/tasks/send';
import { THEME } from '../../../core/terminal/screen';
import { useCountdown } from '../../countdown/CountdownPanel';
import { stepId, useSeconds } from '../../countdown/parts';
import { ReasonText, WaitingAction } from '../../countdown/Waiting';
import { useI18n } from '../../i18n';
import { RULE_COLOR } from './ClaudeInput';

/** Claude Code's accent (the ✻ of its spinner). */
export const ACCENT = '#d77757';
const MARK: Record<StepStatus, string> = { done: '✓', current: '→', failed: '✕', pending: '·' };
const COLOR: Record<StepStatus, string> = {
  done: THEME.foreground,
  current: ACCENT,
  failed: THEME.brightRed,
  pending: RULE_COLOR,
};

export function TerminalStart({ task, onRetry }: { readonly task: TaskRow; readonly onRetry?: () => Promise<void> }) {
  const { t, error } = useI18n();
  const seconds = useSeconds();
  const { view } = useCountdown(task);
  const [retrying, setRetrying] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const retry = async () => {
    setRetrying(true);
    setCode(null);
    try {
      await onRetry!();
    } catch (failure) {
      setCode(sendErrorCode(failure));
    }
    setRetrying(false);
  };
  const failed = view.phase === 'failed';
  const message = error(view.reason ?? 'unavailable');
  const reason = view.reason ?? 'unknown';
  let head: ReactNode;
  if (failed) head = <span>{`✕ ${message.title}`}</span>;
  else if (view.phase === 'waiting') head = <ReasonText task={task} reason={reason} />;
  else if (view.phase === 'delayed')
    head = (
      <span>
        {t('task.terminal.start.delayed', {
          step: t(`countdown.step.${view.current!}`),
          seconds: seconds(view.elapsedMs),
        })}
      </span>
    );
  else head = <span>{t('task.terminal.start.counting', { seconds: seconds(view.remainingMs!) })}</span>;
  return (
    <div
      className="whitespace-pre-wrap px-2 py-1 font-mono text-sm leading-5"
      style={{ background: THEME.background, color: THEME.foreground }}
      data-testid="terminal.start"
      data-phase={view.phase}
    >
      {/* the waiting reason's own paragraph takes this line's colour (light on the dark page) */}
      <div
        aria-live="polite"
        className="flex gap-2 [&_p]:font-normal [&_p]:text-inherit"
        style={{ color: failed ? THEME.brightRed : ACCENT }}
        data-testid="terminal.start.head"
      >
        {failed ? null : <span aria-hidden="true">✻</span>}
        {head}
      </div>
      {failed && <p data-testid="terminal.start.body">{`  ⎿  ${message.body}`}</p>}
      {view.phase === 'waiting' && <WaitingAction task={task} reason={reason} />}
      <div data-testid="terminal.start.steps">
        {view.steps.map((step, index) => (
          <div
            key={step.state}
            style={{ color: COLOR[step.status] }}
            data-testid={`terminal.start.step.${stepId(step.state)}`}
            data-status={step.status}
          >
            {`${index === 0 ? '  ⎿  ' : '     '}${MARK[step.status]} ${t(`countdown.step.${step.state}`)}`}
            {step.elapsedMs === null ? '' : `  ${t('countdown.step.elapsed', { seconds: seconds(step.elapsedMs) })}`}
          </div>
        ))}
      </div>
      {failed && onRetry !== undefined && (
        <button
          type="button"
          className="mt-1 min-h-11 px-2 underline disabled:opacity-50"
          style={{ color: ACCENT }}
          onClick={() => void retry()}
          disabled={retrying}
          data-testid="terminal.start.retry"
        >
          {t('countdown.retry')}
        </button>
      )}
      {code !== null && (
        <p role="alert" style={{ color: THEME.brightRed }} data-testid="terminal.start.retry-error">
          {`  ⎿  ${error(code).title}`}
        </p>
      )}
    </div>
  );
}
