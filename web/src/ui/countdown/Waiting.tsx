// The real reason of a waiting task in the countdown panel (06-web.md section 4.4, ADR-075 section 7) and the step the
// user can take: "Assign a device" for admins while no device is assigned (the first platform candidate, as in step 1
// of the send sequence; everyone else learns that an admin can assign one), and "Choose automatically" while the
// chosen device is full or offline (`PUT /v1/tasks/{task}/placement` with nulls, writers). A chosen device is named.
import { useState } from 'react';
import type { TaskRow } from '../../core/store/slices/tasks';
import { PLACEMENT_REASONS, setTaskPlacement, taskPlacement } from '../../core/tasks/placement';
import { assignFirstCandidate, sendErrorCode } from '../../core/tasks/send';
import { useI18n } from '../i18n';
import { useDeviceNames } from '../screens/devices/labels';
import { useOrgView } from '../screens/tasks/hooks';
import { useAppEnv } from '../shell/context';

const ACTION = 'min-h-11 self-start rounded bg-blue-700 px-4 text-white disabled:opacity-50';

/** The reason text (with the chosen device's name for `placement_*`). */
export function ReasonText({ task, reason }: { readonly task: TaskRow; readonly reason: string }) {
  const { t } = useI18n();
  const name = useDeviceNames([task.org_id]);
  const chosen = taskPlacement(task);
  const device = chosen === null ? t('countdown.chosen-device') : name(chosen.device_id);
  return (
    <p className="font-semibold text-amber-800" data-testid="countdown.reason" data-reason={reason}>
      {t(`countdown.reason.${reason}`, { device })}
    </p>
  );
}

/** The action of a waiting reason, if the user can take one. */
export function WaitingAction({ task, reason }: { readonly task: TaskRow; readonly reason: string }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const { state, userId } = useOrgView(task.org_id);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setCode(null);
    try {
      await action();
    } catch (failure) {
      setCode(sendErrorCode(failure));
    }
    setBusy(false);
  };
  const failed = code !== null && (
    <div role="alert" className="text-red-800" data-testid="countdown.action-error" data-code={code}>
      <p className="font-semibold">{error(code).title}</p>
      <p>{error(code).body}</p>
    </div>
  );
  if (state === null || userId === null) return null;
  const writer = state.canWriteWg(userId, task.wg_id);
  const kind =
    reason === 'no_device'
      ? state.isAdmin(userId)
        ? 'assign'
        : 'ask'
      : PLACEMENT_REASONS.has(reason) && writer
        ? 'automatic'
        : null;
  return (
    <>
      {kind === 'ask' && (
        <p className="text-sm" data-testid="countdown.ask-admin">
          {t('countdown.ask-admin')}
        </p>
      )}
      {kind === 'assign' && (
        <button
          type="button"
          className={ACTION}
          disabled={busy}
          onClick={() => void run(() => assignFirstCandidate(app.tasks, task.org_id, task.wg_id))}
          data-testid="countdown.assign-device"
        >
          {t('countdown.assign-device')}
        </button>
      )}
      {kind === 'automatic' && (
        <button
          type="button"
          className={ACTION}
          disabled={busy}
          onClick={() => void run(() => setTaskPlacement(app.tasks, task.task_id, null))}
          data-testid="countdown.choose-automatic"
        >
          {t('countdown.choose-automatic')}
        </button>
      )}
      {failed}
    </>
  );
}
