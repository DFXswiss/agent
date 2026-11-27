// The model switch line of a task (ADR-081 section 5): when the platform moved one of the task's sessions to another
// model access (its access was used up, could not be used or was released again), the newest switch is shown below the
// header – "Model switched: Claude subscription → DFX access over OpenRouter · used up until 14:05"; a session the
// platform continued after its access was renewed (cause `renewed`, HOT-26) gets the quiet line "Access renewed –
// continuing (Claude subscription)"; a session that waits because no subscription covers its model (cause
// `model_unavailable`) shows "Waiting: no subscription with access to claude-… free". Read from the
// audit (`core/credentials/switches.ts`) when the task opens, when the task or its main session changes state (a
// switch pauses and resumes the session) and after a reconnect; nothing is shown without a switch or when the audit
// cannot be read.
import { useEffect, useState } from 'react';
import { loadModelSwitch, type ModelSwitch } from '../../../core/credentials/switches';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useClock } from '../../settings/sections/model-quotas/summary';
import '../../../core/i18n/lazy/model';

export function ModelSwitchLine({ row }: { readonly row: TaskRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const clock = useClock();
  const main = row.main_session_id;
  const sessionState = useStore(
    (state) => (main === null ? null : (state.tasks.sessions[main]?.row.state ?? null)),
    app.store,
  );
  const [found, setFound] = useState<ModelSwitch | null>(null);
  const [round, setRound] = useState(0);
  useLiveReload(app.store, () => setRound((value) => value + 1));
  useEffect(() => {
    let live = true;
    loadModelSwitch(app.tasks, row.org_id, row.task_id).then(
      (latest) => live && setFound(latest),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [app, row.org_id, row.task_id, row.state_at, sessionState, round]);
  if (found === null) return null;
  const access = (kind: string, model: string | null) =>
    t(model === null ? `model.switch.access.${kind}` : 'model.switch.with-model', {
      access: t(`model.switch.access.${kind}`),
      model: model ?? '',
    });
  const timed = found.cause === 'exhausted' && found.until !== null;
  const switched = () =>
    `${t('model.switch.line', {
      from: access(found.fromAccess, found.fromModel),
      to: access(found.toAccess, found.toModel),
    })} · ${t(timed ? 'model.switch.cause.exhausted.until' : `model.switch.cause.${found.cause}`, {
      time: clock(found.until ?? 0),
    })}`;
  const text =
    found.cause === 'renewed'
      ? t('model.switch.renewed', { access: access(found.toAccess, found.toModel) })
      : found.cause === 'model_unavailable'
        ? t('model.switch.model-unavailable', {
            model: found.toModel ?? t(`model.switch.access.${found.toAccess}`),
          })
        : switched();
  return (
    <p
      className="text-sm text-gray-700"
      role="status"
      data-testid="task.model-switch"
      data-from={found.fromAccess}
      data-to={found.toAccess}
      data-cause={found.cause}
    >
      {text}
    </p>
  );
}
