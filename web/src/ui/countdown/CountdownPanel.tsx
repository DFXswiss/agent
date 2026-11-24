// The countdown panel of a task's session start (06-web.md section 4.4) – a component for the task page's timeline
// (WEB-05 embeds it): large digits with a progress ring, "Delayed" with the current step and a live elapsed time,
// the real reason while the task waits, the status steps with device time and elapsed time, the one-line summary
// "Ready in x s" once `working` is reached and, after `failed`, the catalog error with "Retry" (the same prompt as a
// follow-up). While the task waits, the reason comes with the step the user can take (`Waiting.tsx`, ADR-075). No spinner. Announced through a polite live region every 5 s and on every step change (section 12.3).
import { useState } from 'react';
import { announcement, countdown, type CountdownView, digits, settled } from '../../core/tasks/countdown';
import type { TaskRow } from '../../core/store/slices/tasks';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { Digits, Ring, StepRow, stepId, useSeconds } from './parts';
import { useNow } from './useNow';
import { ReasonText, WaitingAction } from './Waiting';

export interface CountdownPanelProps {
  readonly task: TaskRow;
  /** "Retry" after `failed`; the panel shows the button only when given. */
  readonly onRetry?: () => Promise<void>;
}

/** The session the countdown follows: the task's main session, else the one this browser just launched. */
function useCountdown(task: TaskRow): { readonly view: CountdownView; readonly now: number } {
  const { app } = useAppEnv();
  const launch = useStore((state) => state.tasks.launches[task.task_id] ?? null, app.store);
  const sessionId = task.main_session_id ?? launch?.sessionId ?? null;
  const session = useStore(
    (state) => (sessionId === null ? null : (state.tasks.sessions[sessionId] ?? null)),
    app.store,
  );
  const hubOffsetMs = useStore((state) => state.tasks.hubOffsetMs, app.store);
  const now = useNow(!settled(task, session));
  const followed = launch !== null && launch.sessionId === sessionId ? launch : null;
  return { view: countdown({ task, session, launch: followed, hubOffsetMs, now }), now };
}

/** The text of the live region (changes on step changes and every 5 s only, `announcement`). */
function useAnnouncement(view: CountdownView): string {
  const { t } = useI18n();
  const seconds = useSeconds();
  const said = announcement(view);
  if (said.phase === 'ready') return t('countdown.ready', { seconds: seconds(view.readyInMs!) });
  // every phase but `ready` has a current step
  return t(`countdown.announce.${said.phase}`, { step: t(`countdown.step.${said.step!}`), seconds: said.seconds });
}

function Status({ view, task }: { readonly view: CountdownView; readonly task: TaskRow }) {
  const { t } = useI18n();
  const seconds = useSeconds();
  const step = t(`countdown.step.${view.current!}`);
  if (view.phase === 'counting') {
    const shown = digits(view.remainingMs!);
    return (
      <Ring progress={view.progress}>
        <Digits value={shown.value} fractionDigits={shown.fractionDigits} />
      </Ring>
    );
  }
  if (view.phase === 'delayed') {
    return (
      <div className="flex flex-col gap-1">
        <p className="text-2xl font-semibold text-amber-800" data-testid="countdown.delayed">
          {t('countdown.delayed')}
        </p>
        <p className="tabular-nums" data-testid="countdown.elapsed">
          {t('countdown.current', { step, seconds: seconds(view.elapsedMs) })}
        </p>
      </div>
    );
  }
  const reason = view.reason ?? 'unknown';
  return (
    <div className="flex flex-col gap-1">
      <ReasonText task={task} reason={reason} />
      <p className="tabular-nums" data-testid="countdown.elapsed">
        {t('countdown.waiting', { seconds: seconds(view.elapsedMs) })}
      </p>
      <WaitingAction task={task} reason={reason} />
    </div>
  );
}

function Failed({ view, onRetry }: { readonly view: CountdownView; readonly onRetry?: () => Promise<void> }) {
  const { t, error } = useI18n();
  const [retrying, setRetrying] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const message = error(view.reason ?? 'unavailable');
  const retry = async () => {
    setRetrying(true);
    setCode(null);
    try {
      await onRetry!();
    } catch (failure) {
      setCode(failure instanceof Error && 'code' in failure ? String(failure.code) : 'unavailable');
    }
    setRetrying(false);
  };
  return (
    <div className="flex flex-col gap-2" data-testid="countdown.failed" data-reason={view.reason ?? ''}>
      <p className="font-semibold text-red-800">{message.title}</p>
      <p>{message.body}</p>
      {onRetry !== undefined && (
        <button
          type="button"
          className="min-h-11 self-start rounded bg-blue-700 px-4 text-white disabled:opacity-50"
          onClick={() => void retry()}
          disabled={retrying}
          data-testid="countdown.retry"
        >
          {t('countdown.retry')}
        </button>
      )}
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="countdown.retry-error">
          {error(code).title}
        </p>
      )}
    </div>
  );
}

export function CountdownPanel({ task, onRetry }: CountdownPanelProps) {
  const { t } = useI18n();
  const seconds = useSeconds();
  const { view, now } = useCountdown(task);
  const announcement = useAnnouncement(view);
  const live = (
    <p className="sr-only" aria-live="polite" data-testid="countdown.announce">
      {announcement}
    </p>
  );
  if (view.phase === 'ready') {
    return (
      <section
        className="rounded border border-green-200 bg-green-50 px-3 py-2 text-green-900"
        data-testid="countdown.panel"
        data-phase="ready"
      >
        <p data-testid="countdown.summary">{t('countdown.ready', { seconds: seconds(view.readyInMs!) })}</p>
        {live}
      </section>
    );
  }
  return (
    <section
      className="flex flex-col gap-4 rounded border border-gray-200 p-4"
      aria-label={t('countdown.label')}
      data-testid="countdown.panel"
      data-phase={view.phase}
    >
      {view.phase === 'failed' ? (
        <Failed view={view} {...(onRetry !== undefined && { onRetry })} />
      ) : (
        <Status view={view} task={task} />
      )}
      <ol className="flex flex-col gap-1" data-testid="countdown.steps">
        {view.assignedMs !== null && (
          <StepRow id="assigning" label={t('countdown.step.assigning')} status="done" elapsedMs={view.assignedMs} />
        )}
        {view.steps.map((step) => (
          <StepRow
            key={step.state}
            id={stepId(step.state)}
            label={t(`countdown.step.${step.state}`)}
            status={step.status}
            at={step.at}
            elapsedMs={step.elapsedMs}
            now={now}
          />
        ))}
      </ol>
      {live}
    </section>
  );
}
