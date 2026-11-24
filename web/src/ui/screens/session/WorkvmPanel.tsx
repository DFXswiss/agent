// The work VM panel of a session (13-work-vm.md section 10, KONZEPT ideas 27, 28 and 46): the state of the session's
// work VM – none, starting (countdown to the 10 s target while its `workvm.request` operation is open), running or
// deleted with the reason –, its size, the runtime of this VM (ticking every second while it runs), and the runs of
// the session (command, state, exit code, duration, stored output: the `workvm.run` chat item) in a collapsed list.
// The panel has no "release" button: only the session can request operations (decision UI-1 of KOR-29); the VM is
// torn down when the session releases it, after the idle time or when the session ends.
import { useEffect, useMemo } from 'react';
import {
  formatRuntime,
  selectWorkvmRequests,
  selectWorkvmRuns,
  selectWorkvmStates,
  START_TARGET_MS,
  workvmPanel,
  workvmRuntime,
  type WorkvmState,
} from '../../../core/chat/workvm';
import { parseTime } from '../../../core/encoding/time';
import { ensureOperations } from '../../../core/operations/load';
import { FINAL_STATES } from '../../../core/store/slices/operations';
import { chatItem as runItem } from '../../chat-items/workvm.run';
import { Digits, Ring, useSeconds } from '../../countdown/parts';
import { TICK_MS, useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

function Facts({ vm, now }: { readonly vm: WorkvmState; readonly now: number }) {
  const { t, locale } = useI18n();
  const memory = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(vm.memory_mib / 1024);
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm text-gray-700">
      <span data-testid="workvm.panel.resources">
        {t('workvm.state.resources', {
          size: t(`workvm.state.size.${vm.size}`),
          cpus: vm.cpus,
          memory,
          disk: vm.disk_gib,
        })}
      </span>
      <span data-testid="workvm.panel.runtime">
        {t('workvm.state.runtime', { runtime: formatRuntime(workvmRuntime([vm], now).ms) })}
      </span>
    </div>
  );
}

export function WorkvmPanel({
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
  const seconds = useSeconds();
  const states = useStore(
    useMemo(() => selectWorkvmStates(wg, task, session), [wg, task, session]),
    app.store,
  ).session;
  const runs = useStore(
    useMemo(() => selectWorkvmRuns(wg, task, session), [wg, task, session]),
    app.store,
  );
  const requests = useStore(
    useMemo(() => selectWorkvmRequests(session), [session]),
    app.store,
  );
  useEffect(() => {
    ensureOperations(app.tasks, task).catch(() => undefined);
  }, [app, task]);
  const opening = requests.some((row) => !FINAL_STATES.has(row.state));
  const live = states.some((state) => state.state === 'created');
  const now = useNow(opening || live, opening && !live ? TICK_MS : 1000);
  const panel = workvmPanel(states, requests, now);
  return (
    <section
      className="flex flex-col gap-2 rounded border border-gray-200 p-3"
      aria-labelledby="workvm-panel-title"
      data-testid="workvm.panel"
      data-phase={panel.phase}
    >
      <h2 id="workvm-panel-title" className="flex flex-wrap items-center gap-2 text-base font-semibold">
        {t('workvm.panel.title')}
        <span className="text-sm font-normal text-gray-700" data-testid="workvm.panel.state">
          {panel.phase === 'deleted'
            ? panel.vm!.reason === null
              ? t('workvm.state.deleted.plain')
              : t('workvm.state.deleted', { reason: t(`workvm.state.reason.${panel.vm!.reason}`) })
            : t(`workvm.panel.phase.${panel.phase}`)}
        </span>
      </h2>
      {panel.phase === 'starting' && (
        <div className="flex flex-wrap items-center gap-4" data-testid="workvm.panel.countdown" aria-live="polite">
          <Ring progress={panel.remainingMs / START_TARGET_MS}>
            <Digits value={Math.ceil(panel.remainingMs / 100) / 10} fractionDigits={1} />
          </Ring>
          <p className="text-sm" data-testid="workvm.panel.countdown.text" data-overdue={panel.overdue}>
            {panel.overdue
              ? t('workvm.panel.overdue', { seconds: seconds(panel.elapsedMs) })
              : t('workvm.panel.starting', { seconds: seconds(panel.elapsedMs) })}
          </p>
        </div>
      )}
      {(panel.phase === 'running' || panel.phase === 'deleted') && <Facts vm={panel.vm!} now={now} />}
      <p className="text-xs text-gray-600" data-testid="workvm.panel.note">
        {t(panel.phase === 'none' ? 'workvm.panel.none' : 'workvm.panel.release')}
      </p>
      {runs.length > 0 && (
        <details data-testid="workvm.panel.runs">
          <summary className="flex min-h-11 cursor-pointer items-center text-sm text-blue-800">
            {t('workvm.panel.runs', { count: runs.length })}
          </summary>
          <ul className="flex flex-col gap-2">
            {runs.map((run) => (
              <li key={run.job_id}>
                <runItem.Component
                  item={{
                    key: run.job_id,
                    kind: runItem.kind,
                    at: parseTime(run.started_at),
                    seq: 0,
                    sessionId: session,
                    author: null,
                    body: run,
                  }}
                />
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
