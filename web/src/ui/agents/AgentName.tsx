// The name and handle of a task's main agent (06-web.md section 4.0a, ADR-095 sections 1 and 2): `<name> #<number>` –
// the newest verified `task.agent_name` of the task, else the automatic name of its title (`core/agents/naming.ts`);
// an unverifiable newest name is shown as such, never as text. The name events the task row names are pulled when the
// store does not hold them. "Rename" (for writers of the workgroup) opens a field with the current name, "Save" and
// "Automatic name"; both send a sealed `task.agent_name` (`{name: null}` for the automatic name).
import { useEffect, useId, useMemo, useState } from 'react';
import { checkName, mainHandle, taskNumber } from '../../core/agents/name';
import { type AgentLabel, agentLabel, agentNames, renameAgent, selectAgentName } from '../../core/agents/naming';
import '../../core/i18n/lazy/mainagent';
import type { TaskRow } from '../../core/store/slices/tasks';
import type { StoreState } from '../../core/store/store';
import { selectTitle } from '../../core/tasks/directory';
import { sendErrorCode } from '../../core/tasks/send';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

/** The shown name of the task's main agent; pulls the name event the row names when it is not in the store. */
export function useAgentLabel(row: TaskRow): AgentLabel {
  const { app } = useAppEnv();
  const nameEventId = row.name_event_id ?? null;
  const name = useStore(
    useMemo(() => selectAgentName(row.wg_id, row.task_id, nameEventId), [row.wg_id, row.task_id, nameEventId]),
    app.store,
  );
  const title = useStore(selectTitle(row.wg_id, row.task_id, row.title_event_id), app.store);
  useEffect(() => {
    if (nameEventId !== null) void agentNames(app.tasks, [row]);
  }, [app, row, nameEventId]);
  return agentLabel(name, title);
}

/** The label of the task's main agent as the store holds it now (read once, e.g. to fill the rename field). */
function currentLabel(state: StoreState, row: TaskRow): AgentLabel {
  const name = selectAgentName(row.wg_id, row.task_id, row.name_event_id ?? null)(state);
  return agentLabel(name, selectTitle(row.wg_id, row.task_id, row.title_event_id)(state));
}

/** The text of a label (the given or automatic name, or a neutral word). */
export function useLabelText(): (label: AgentLabel) => string {
  const { t } = useI18n();
  return (label) => {
    if (label.status === 'missing') return t('mainagent.name.loading');
    if (label.status === 'unverifiable') return t('mainagent.name.unverifiable', { code: label.code });
    return label.text;
  };
}

export interface AgentNameIds {
  /** The whole label. */
  readonly root: string;
  readonly name: string;
  readonly handle: string;
}

/** `<name> #<number>` of the task's main agent (no handle from a hub that sends no number). */
export function AgentName({ row, ids, className }: { row: TaskRow; ids: AgentNameIds; className?: string }) {
  const label = useAgentLabel(row);
  const text = useLabelText();
  const number = taskNumber(row);
  return (
    <span className={`inline-flex flex-wrap items-baseline gap-x-1 ${className ?? ''}`} data-testid={ids.root}>
      <span
        className={label.status === 'given' || label.status === 'automatic' ? '' : 'text-gray-500'}
        data-testid={ids.name}
        data-status={label.status}
      >
        {text(label)}
      </span>
      {number !== null && (
        <span className="text-gray-600 tabular-nums" data-testid={ids.handle}>
          {mainHandle(number)}
        </span>
      )}
    </span>
  );
}

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';

/** "Rename" of the task's main agent (callers show it to writers of the workgroup only); test IDs below `prefix`. */
export function RenameAgent({ row, prefix }: { readonly row: TaskRow; readonly prefix: string }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const id = useId();
  const [value, setValue] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const save = async (name: string | null) => {
    setBusy(true);
    setCode(null);
    try {
      await renameAgent(app.tasks, row, name);
      setValue(null);
    } catch (failure) {
      setCode(sendErrorCode(failure));
    } finally {
      setBusy(false);
    }
  };
  if (value === null)
    return (
      <button
        type="button"
        className={BUTTON}
        onClick={() => {
          const label = currentLabel(app.store.getState(), row);
          setValue(label.status === 'given' || label.status === 'automatic' ? label.text : '');
        }}
        data-testid={prefix}
      >
        {t('mainagent.rename')}
      </button>
    );
  const valid = checkName(value) !== null;
  return (
    <form
      className="flex w-full flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) void save(value);
      }}
      data-testid={`${prefix}.form`}
    >
      <label htmlFor={id} className="text-sm text-gray-700">
        {t('mainagent.rename.label')}
      </label>
      <input
        id={id}
        className={`min-h-11 rounded border px-2 ${valid ? 'border-gray-300' : 'border-red-700'}`}
        value={value}
        maxLength={160}
        aria-invalid={!valid}
        aria-describedby={`${id}-hint`}
        disabled={busy}
        onChange={(event) => setValue(event.target.value)}
        data-testid={`${prefix}.input`}
      />
      <p id={`${id}-hint`} className={`text-xs ${valid ? 'text-gray-600' : 'text-red-800'}`}>
        {t(valid ? 'mainagent.rename.hint' : 'mainagent.rename.invalid')}
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          className="min-h-11 rounded bg-blue-700 px-3 text-white disabled:opacity-50"
          disabled={busy || !valid}
          data-testid={`${prefix}.save`}
        >
          {t('mainagent.rename.save')}
        </button>
        <button
          type="button"
          className={BUTTON}
          disabled={busy}
          onClick={() => void save(null)}
          data-testid={`${prefix}.automatic`}
        >
          {t('mainagent.rename.automatic')}
        </button>
        <button
          type="button"
          className={BUTTON}
          disabled={busy}
          onClick={() => {
            setValue(null);
            setCode(null);
          }}
          data-testid={`${prefix}.cancel`}
        >
          {t('mainagent.rename.cancel')}
        </button>
      </div>
      {code !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid={`${prefix}.error`} data-code={code}>
          {t('mainagent.rename.failed', { reason: error(code).title })}
        </p>
      )}
    </form>
  );
}
