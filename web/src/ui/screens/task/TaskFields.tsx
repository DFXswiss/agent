// The ticket fields of a task on the task page (E-41, ADR-100 section 7): status (a choice for writers: todo,
// inProgress, completed – completed closes the task, todo/inProgress reopen a closed one; `PUT /v1/tasks/{task}/fields`),
// urgency, person, topic, GitHub organisation and repository and the description (sealed in `task.meta`). "Edit" opens
// the ticket form (`TicketForm.tsx`): the hub fields that changed go to `PUT …/fields`, changed sealed fields as a new
// `task.meta` with the full set. A task without a main session (a captured task) offers "Start agent": its first prompt,
// preset with the title, a blank line and the description, editable.
import { useId, useMemo, useState } from 'react';
import { PROMPT_MAX, sendErrorCode } from '../../../core/tasks/send';
import {
  changedFields,
  EMPTY_META,
  metaBody,
  metaChanged,
  putFields,
  selectMeta,
  startAgent,
  startPrompt,
  STATUSES,
  type TaskStatus,
  taskAssignee,
  taskStatus,
  taskTopic,
  taskUrgency,
  writeMeta,
} from '../../../core/tasks/tickets';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { personName } from '../../names/names';
import { useAppEnv } from '../../shell/context';
import type { Directory } from '../../../core/tasks/directory';
import { topicLabel, UrgencyBadge, useTopics } from '../tasks/tickets';
import { TicketForm, type TicketValues } from '../tasks/TicketForm';

const BUTTON = 'min-h-11 rounded border border-gray-300 px-3 text-sm';
const PRIMARY = 'min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50';

/** One field of the panel: its label and value. */
function Field({ label, value, testId }: { readonly label: string; readonly value: string; readonly testId: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-gray-600">{label}</dt>
      <dd className="break-words" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

export function TaskFields({
  row,
  writer,
  directory,
}: {
  readonly row: TaskRow;
  readonly writer: boolean;
  readonly directory: Directory;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const statusId = useId();
  const topics = useTopics(row.org_id);
  const meta =
    useStore(
      useMemo(() => selectMeta(row.wg_id, row.task_id), [row.wg_id, row.task_id]),
      app.store,
    ) ?? EMPTY_META;
  const [editing, setEditing] = useState<TicketValues | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<{ readonly kind: 'status' | 'edit'; readonly code: string } | null>(null);
  const status = taskStatus(row);
  const assignee = taskAssignee(row);
  const topic = taskTopic(row);
  const none = t('tickets.panel.none');
  const run = async (kind: 'status' | 'edit', action: () => Promise<unknown>) => {
    setBusy(true);
    setCode(null);
    try {
      await action();
      return true;
    } catch (failure) {
      setCode({ kind, code: sendErrorCode(failure) });
      return false;
    } finally {
      setBusy(false);
    }
  };
  const changeStatus = (next: TaskStatus) => void run('status', () => putFields(app.tasks, row, { status: next }));
  const save = async (values: TicketValues) => {
    const done = await run('edit', async () => {
      const sealed = metaBody(values);
      const fields = changedFields(row, {
        status,
        urgency: values.urgency,
        assignee_id: values.assignee === '' ? null : values.assignee,
        topic_id: values.topic === '' ? null : values.topic,
      });
      if (metaChanged(sealed, meta)) await writeMeta(app.tasks, row, values);
      await putFields(app.tasks, row, fields);
    });
    if (done) setEditing(null);
  };
  const open = () =>
    setEditing({
      title: meta.title,
      description: meta.description,
      assignee: assignee ?? '',
      topic: topic ?? '',
      githubOrg: meta.github_org,
      githubRepo: meta.github_repo,
      urgency: taskUrgency(row),
    });
  return (
    <section
      className="flex flex-col gap-3 rounded border border-gray-200 p-3"
      aria-labelledby={`${statusId}-heading`}
      data-testid="task.fields"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={`${statusId}-heading`} className="font-semibold">
          {t('tickets.panel.heading')}
        </h2>
        {writer && editing === null && (
          <button type="button" className={BUTTON} onClick={open} disabled={busy} data-testid="task.fields.edit">
            {t('tickets.panel.edit')}
          </button>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={statusId} className="text-sm">
          {t('tickets.field.status')}
        </label>
        <select
          id={statusId}
          className="min-h-11 rounded border border-gray-300 px-2"
          value={status}
          disabled={!writer || busy}
          onChange={(event) => changeStatus(event.target.value as TaskStatus)}
          data-testid="task.fields.status"
        >
          {STATUSES.map((value) => (
            <option key={value} value={value}>
              {t(`tickets.status.${value}`)}
            </option>
          ))}
        </select>
        <UrgencyBadge row={row} testId="task.fields.urgency" />
      </div>
      {code?.kind === 'status' && (
        <p className="text-sm text-red-800" role="alert" data-testid="task.fields.status.error" data-code={code.code}>
          {t('tickets.panel.status.failed', { reason: error(code.code).title })}
        </p>
      )}
      {editing === null ? (
        <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
          <Field
            label={t('tickets.field.person')}
            value={assignee === null ? t('tickets.person.none') : personName(t, directory, assignee)}
            testId="task.fields.person"
          />
          <Field
            label={t('tickets.field.topic')}
            value={topic === null ? t('tickets.topic.none') : topicLabel(t, topics, topic)}
            testId="task.fields.topic"
          />
          <Field label={t('tickets.field.github-org')} value={meta.github_org || none} testId="task.fields.github-org" />
          <Field
            label={t('tickets.field.github-repo')}
            value={meta.github_repo || none}
            testId="task.fields.github-repo"
          />
          <div className="flex flex-col sm:col-span-2">
            <dt className="text-xs text-gray-600">{t('tickets.field.description')}</dt>
            <dd className="whitespace-pre-wrap break-words" data-testid="task.fields.description">
              {meta.description || none}
            </dd>
          </div>
        </dl>
      ) : (
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            void save(editing);
          }}
          data-testid="task.fields.form"
        >
          <TicketForm
            org={row.org_id}
            wgs={[row.wg_id]}
            values={editing}
            onChange={setEditing}
            disabled={busy}
            prefix="task.fields.form"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              className={PRIMARY}
              disabled={busy || editing.title.trim() === ''}
              data-testid="task.fields.save"
            >
              {t('tickets.panel.save')}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => {
                setEditing(null);
                setCode(null);
              }}
              data-testid="task.fields.cancel"
            >
              {t('tickets.panel.cancel')}
            </button>
          </div>
        </form>
      )}
      {code?.kind === 'edit' && (
        <p className="text-sm text-red-800" role="alert" data-testid="task.fields.error" data-code={code.code}>
          {code.code === 'request_invalid'
            ? t('tickets.capture.invalid')
            : t('tickets.panel.failed', { reason: error(code.code).title })}
        </p>
      )}
      {row.main_session_id === null && row.state !== 'closed' && (
        <StartAgent row={row} writer={writer} title={meta.title} description={meta.description} />
      )}
    </section>
  );
}

/** "Start agent" of a task without a main session (module comment). */
export function StartAgent({
  row,
  writer,
  title,
  description,
}: {
  readonly row: TaskRow;
  readonly writer: boolean;
  readonly title: string;
  readonly description: string;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const id = useId();
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const start = async (prompt: string) => {
    setBusy(true);
    setCode(null);
    try {
      await startAgent(app.tasks, row, prompt);
      setText(null);
    } catch (failure) {
      setCode(sendErrorCode(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-2 border-t border-gray-200 pt-3" data-testid="task.start">
      <p className="text-sm text-gray-700">{t('tickets.start.none')}</p>
      {writer && text === null && (
        <button
          type="button"
          className={`${PRIMARY} self-start`}
          onClick={() => setText(startPrompt({ title, description }))}
          data-testid="task.start.open"
        >
          {t('tickets.start.open')}
        </button>
      )}
      {text !== null && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void start(text);
          }}
        >
          <p className="text-sm text-gray-700">{t('tickets.start.intro')}</p>
          <label htmlFor={id} className="text-sm font-medium">
            {t('tickets.start.prompt')}
          </label>
          <textarea
            id={id}
            className="min-h-28 w-full rounded border border-gray-300 p-2"
            value={text}
            maxLength={PROMPT_MAX}
            disabled={busy}
            onChange={(event) => setText(event.target.value)}
            data-testid="task.start.prompt"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="submit"
              className={PRIMARY}
              disabled={busy || text.trim() === ''}
              data-testid="task.start.submit"
            >
              {t('tickets.start.submit')}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => setText(null)}
              data-testid="task.start.cancel"
            >
              {t('tickets.start.cancel')}
            </button>
          </div>
        </form>
      )}
      {code !== null && (
        <p className="text-sm text-red-800" role="alert" data-testid="task.start.error" data-code={code}>
          {t('tickets.start.failed', { reason: error(code).title })}
        </p>
      )}
    </div>
  );
}
