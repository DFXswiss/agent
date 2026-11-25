// The agents of a task in the situation view (06-web.md section 4.0, ADR-076 section 4): loaded only when an entry is
// opened (`GET /v1/tasks/{task}`, which lists at most 100 sessions), the main agent with its subagents indented below
// the agent they report to, each with its state; at most `TREE_MAX` are shown, the rest are counted ("N more").
import { useEffect, useMemo, useState } from 'react';
import { TREE_MAX } from '../../../core/attention/view';
import { flatten, indentLevel, sessionTree } from '../../../core/sessions/tree';
import { loadTask, sendErrorCode } from '../../../core/tasks/send';
import { formatNumber } from '../../../core/i18n';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

/** Indentation by `indentLevel` (static classes, `INDENT_LEVELS` + 1 entries). */
const INDENT = ['', 'ml-4 sm:ml-6', 'ml-8 sm:ml-12', 'ml-12 sm:ml-20', 'ml-16 sm:ml-24'];

export function SessionTree({ task, subagents }: { readonly task: string; readonly subagents: number }) {
  const { app } = useAppEnv();
  const { t, locale, error } = useI18n();
  const [status, setStatus] = useState<{ kind: 'loading' } | { kind: 'ready' } | { kind: 'error'; code: string }>({
    kind: 'loading',
  });
  useEffect(() => {
    let live = true;
    loadTask(app.tasks, task).then(
      () => live && setStatus({ kind: 'ready' }),
      (failure: unknown) => live && setStatus({ kind: 'error', code: sendErrorCode(failure) }),
    );
    return () => {
      live = false;
    };
  }, [app, task]);
  const sessions = useStore((state) => state.tasks.sessions, app.store);
  const nodes = useMemo(() => flatten(sessionTree(sessions, task)), [sessions, task]);
  const shown = nodes.slice(0, TREE_MAX);
  const mains = nodes.filter((node) => node.depth === 0).length;
  const more = Math.max(nodes.length, mains + subagents) - shown.length;
  return (
    <div className="flex flex-col gap-1 px-2 pb-2" data-testid="attention.tree" data-status={status.kind}>
      {status.kind === 'loading' && nodes.length === 0 && (
        <p className="text-sm text-gray-600">{t('attention.tree.loading')}</p>
      )}
      {status.kind === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="attention.tree.error">
          {error(status.code).title}
        </p>
      )}
      {status.kind === 'ready' && nodes.length === 0 && (
        <p className="text-sm text-gray-600">{t('attention.tree.empty')}</p>
      )}
      {shown.length > 0 && (
        <ul aria-label={t('attention.tree.label')} className="flex flex-col gap-1">
          {shown.map((node) => (
            <li
              key={node.entry.row.session_id}
              className={`flex flex-wrap items-center gap-2 text-sm ${INDENT[indentLevel(node.depth)]}`}
              data-testid="attention.tree.node"
              data-depth={node.depth}
              data-session={node.entry.row.session_id}
            >
              <span>
                {node.depth === 0
                  ? t('attention.tree.main')
                  : t('attention.tree.subagent', { path: node.path.join('.') })}
              </span>
              <span
                className="rounded bg-gray-100 px-2 text-xs text-gray-800"
                data-testid="attention.tree.state"
                data-state={node.entry.row.state}
              >
                {t(`task.state.${node.entry.row.state}`)}
              </span>
            </li>
          ))}
        </ul>
      )}
      {more > 0 && (
        <p className="text-sm text-gray-600" data-testid="attention.tree.more" data-count={more}>
          {t('attention.tree.more', { count: formatNumber(more, locale) })}
        </p>
      )}
    </div>
  );
}
