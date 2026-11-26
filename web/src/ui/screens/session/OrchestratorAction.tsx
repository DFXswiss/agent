// "Make this session my main orchestrator" on the session page (11-agent-access.md section 11, ADR-082 sections 6 and
// 9.6): offered only to the task owner, only for a running main session (`working`, `idle`), and only when the hub
// says the session may become it (`GET /v1/sessions/{id}/orchestrator`: a session of the owner's private organization
// on a platform device; any other session is refused there too). A session that is the main orchestrator already says
// so instead; a session of an analysis task says why it cannot become it (ADR-097 section 2). The dialog
// (`OrchestratorDialog`) asks the session's runner for keys, checks them and runs the confirmation. It is imported
// statically: it lies in the lazy chunk of the session screen anyway, and an import inside one chunk has no chunk of
// its own whose integrity the import could check (`src/core/assets/integrity.ts`).
import { useEffect, useState } from 'react';
import { loadOrchestrator, type OrchestratorView } from '../../../core/agents/orchestrator';
import type { SessionEntry } from '../../../core/store/slices/tasks';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import OrchestratorDialog from './OrchestratorDialog';

/** The session states in which the session's VM runs and can draw the keys. */
export const RUNNING: readonly string[] = ['working', 'idle'];
const BUTTON = 'min-h-11 self-start rounded border border-gray-900 px-4';

export function OrchestratorAction({ entry, depth }: { readonly entry: SessionEntry; readonly depth: number }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const { session_id: session, task_id: task, state } = entry.row;
  const owner = useStore((current) => current.tasks.tasks[task]?.owner_id ?? null, app.store);
  const offered = owner !== null && owner === app.userId && depth === 0 && RUNNING.includes(state);
  const [view, setView] = useState<OrchestratorView | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!offered) return;
    let live = true;
    loadOrchestrator(app.client, session).then(
      (found) => live && setView(found),
      () => live && setView(null),
    );
    return () => {
      live = false;
    };
  }, [app, session, offered, open]);
  if (!offered || view === null) return null;
  if (view.reason === 'analysis') {
    return (
      <p className="text-sm text-gray-700" data-testid="orchestrator.analysis">
        {t('session.orchestrator.reason.analysis')}
      </p>
    );
  }
  if (!view.available) return null;
  if (view.pairing?.state === 'active') {
    return (
      <p className="text-sm font-semibold" data-testid="orchestrator.current">
        {t('session.orchestrator.current')}
      </p>
    );
  }
  return (
    <>
      <button type="button" className={BUTTON} onClick={() => setOpen(true)} data-testid="orchestrator.make">
        {t('session.orchestrator.make')}
      </button>
      {open && <OrchestratorDialog entry={entry} onClose={() => setOpen(false)} />}
    </>
  );
}
