// The work VM runtime in the session header (13-work-vm.md section 10, KONZEPT idea 46): the runtime of the work VMs
// of this session and of the whole task from their `workvm.state` events (`core/chat/workvm.ts`); it ticks every
// second while a VM is created and not deleted. Nothing is shown while the task had no work VM.
import { useMemo } from 'react';
import { formatRuntime, selectWorkvmStates, workvmRuntime } from '../../../core/chat/workvm';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

export function WorkvmRuntime({
  wg,
  task,
  session,
}: {
  readonly wg: string;
  readonly task: string;
  readonly session: string;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const select = useMemo(() => selectWorkvmStates(wg, task, session), [wg, task, session]);
  const states = useStore(select, app.store);
  const running = states.task.some((state) => state.state === 'created');
  const now = useNow(running, 1000);
  const ofTask = workvmRuntime(states.task, now);
  if (ofTask.vms === 0) return null;
  const ofSession = workvmRuntime(states.session, now);
  return (
    <span className="text-xs text-gray-600" data-testid="session.workvm-runtime" data-running={ofTask.running}>
      {t('workvm.runtime', { session: formatRuntime(ofSession.ms), task: formatRuntime(ofTask.ms) })}
      {ofTask.running && ` (${t('workvm.runtime.running')})`}
    </span>
  );
}
