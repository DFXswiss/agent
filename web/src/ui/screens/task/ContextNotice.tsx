// The resume notice of a task (ADR-059 sections 5 and 7, KOR-33): when the task's current main session could not
// take over the original session's context, a visible warning names the reason ("This task was continued by a new
// AI because …"); a successful restore gets one unobtrusive line, which also says when the workspace came back only
// partly or not at all (too large). Old hubs report neither, then nothing is shown.
import { contextResume } from '../../../core/sessions/context';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

export function ContextNotice({ sessionId }: { readonly sessionId: string | null }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const entry = useStore((state) => (sessionId === null ? null : (state.tasks.sessions[sessionId] ?? null)), app.store);
  const resume = contextResume(entry);
  if (resume === null) return null;
  if (resume.kind === 'restored')
    return (
      <p
        className={resume.workspace === 'full' ? 'text-xs text-gray-600' : 'text-xs text-amber-800'}
        data-testid="task.context.restored"
        data-workspace={resume.workspace}
      >
        {t(resume.workspace === 'full' ? 'resume.restored' : `resume.restored.workspace_${resume.workspace}`)}
      </p>
    );
  return (
    <div
      className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm break-words text-amber-900"
      role="status"
      data-testid="task.context.fallback"
      data-reason={resume.reason}
    >
      {t('resume.fallback', { reason: t(`resume.reason.${resume.reason}`) })}
    </div>
  );
}
