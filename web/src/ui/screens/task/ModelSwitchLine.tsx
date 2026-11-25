// The model switch line of a task (ADR-081 section 5): when the platform moved one of the task's sessions to another
// model access (its access was used up, could not be used or was released again), the newest switch is shown below the
// header – "Model switched: Claude subscription → DFX access over OpenRouter · used up until 14:05". Read from the
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

const ACCESSES: ReadonlySet<string> = new Set(['subscription', 'operator', 'api_key', 'platform']);
const CAUSES: ReadonlySet<string> = new Set(['exhausted', 'unusable', 'released']);

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
  const access = (kind: string, model: string | null) => {
    const name = ACCESSES.has(kind) ? t(`model.switch.access.${kind}`) : kind;
    return model === null ? name : t('model.switch.with-model', { access: name, model });
  };
  const line = t('model.switch.line', {
    from: access(found.fromAccess, found.fromModel),
    to: access(found.toAccess, found.toModel),
  });
  let cause: string | null = null;
  if (found.cause === 'exhausted' && found.until !== null) {
    cause = t('model.switch.cause.exhausted.until', { time: clock(found.until) });
  } else if (CAUSES.has(found.cause)) {
    cause = t(`model.switch.cause.${found.cause}`);
  }
  return (
    <p
      className="text-sm text-gray-700"
      role="status"
      data-testid="task.model-switch"
      data-from={found.fromAccess}
      data-to={found.toAccess}
      data-cause={found.cause}
    >
      {cause === null ? line : `${line} · ${cause}`}
    </p>
  );
}
