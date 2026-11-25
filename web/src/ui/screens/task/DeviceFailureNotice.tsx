// The device failure notice of a task (ADR-086 sections 2, 3 and 6, KOR-58): while the current main session is
// interrupted (its device failed while it was active and the session has not ended), a visible notice names since when
// the device is unreachable and that the task continues by itself – once the device is back or another suitable
// device can take it over; a task pinned to that device offers "Choose automatically" to writers (E-05,
// `Waiting.tsx`). A main session that continues a session lost to a device failure gets one unobtrusive line, like a
// restored context (`ContextNotice`). Old hubs report neither, then nothing is shown.
import { localTimeZone, formatEventTime } from '../../../core/i18n';
import { deviceFailure } from '../../../core/sessions/failover';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { WaitingAction } from '../../countdown/Waiting';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

export function DeviceFailureNotice({ row }: { readonly row: TaskRow }) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const now = useNow(false);
  const sessionId = row.main_session_id;
  const entry = useStore((state) => (sessionId === null ? null : (state.tasks.sessions[sessionId] ?? null)), app.store);
  const failure = deviceFailure(entry);
  if (failure === null) return null;
  if (failure.interruptedAt !== null)
    return (
      <div
        className="flex flex-col gap-2 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm break-words text-amber-900"
        role="status"
        data-testid="task.device-failure.interrupted"
      >
        <p>
          {t('resume.interrupted', {
            time: formatEventTime(failure.interruptedAt, { locale, timeZone: localTimeZone(), now }),
          })}
        </p>
        <WaitingAction task={row} reason="device_failed" />
      </div>
    );
  return (
    <p className="text-xs text-gray-600" data-testid="task.device-failure.failover" data-from={failure.failoverOf!}>
      {t('resume.failover')}
    </p>
  );
}
