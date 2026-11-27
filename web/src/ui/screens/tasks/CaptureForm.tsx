// "Capture task" (E-41, ADR-102 section 7) beside the composer of a workgroup's task list and on the task overview of
// the organization: a button that opens the ticket form (`TicketForm.tsx`) – on the overview with the workgroup to
// capture in (the writable ones, the default first) – and sends it with `POST /v1/tasks` without a prompt: no agent
// starts. The sealed request is kept while the input is unchanged, so "Capture task" after a lost answer repeats the
// same request (same task ID). After the capture the form closes and links the new task.
import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { sendErrorCode } from '../../../core/tasks/send';
import { type PreparedCapture, prepareCapture, sendCapture } from '../../../core/tasks/tickets';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { EMPTY_VALUES, INPUT, TicketForm, type TicketValues } from './TicketForm';

interface Kept {
  readonly input: string;
  readonly prepared: PreparedCapture;
}

export function CaptureForm({
  org,
  wgs,
  name,
}: {
  readonly org: string;
  /** The workgroups the user may capture in (`wg:write`), the preselected one first. */
  readonly wgs: readonly string[];
  /** The name of a workgroup (shown when there is a choice). */
  readonly name: (wg: string) => string;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const wgId = useId();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<TicketValues>(EMPTY_VALUES);
  const [chosen, setChosen] = useState('');
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const [kept, setKept] = useState<Kept | null>(null);
  const [done, setDone] = useState<string | null>(null);
  if (wgs.length === 0) return null;
  const wg = wgs.includes(chosen) ? chosen : wgs[0]!;
  const submit = async () => {
    setBusy(true);
    setCode(null);
    const input = JSON.stringify([wg, values]);
    try {
      const prepared =
        kept?.input === input
          ? kept.prepared
          : await prepareCapture(app.tasks, {
              org,
              wg,
              meta: values,
              urgency: values.urgency,
              assigneeId: values.assignee === '' ? null : values.assignee,
              topicId: values.topic === '' ? null : values.topic,
            });
      setKept({ input, prepared });
      const row = await sendCapture(app.tasks, prepared);
      setKept(null);
      setValues(EMPTY_VALUES);
      setOpen(false);
      setDone(row.task_id);
    } catch (failure) {
      setCode(sendErrorCode(failure));
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="min-h-11 rounded border border-blue-700 px-4 text-blue-800"
          onClick={() => {
            setOpen(true);
            setDone(null);
          }}
          data-testid="tickets.capture.open"
        >
          {t('tickets.capture.open')}
        </button>
        {done !== null && (
          <p className="flex flex-wrap items-center gap-2 text-sm" role="status" data-testid="tickets.capture.done">
            <span>{t('tickets.capture.done')}</span>
            <Link
              className="flex min-h-11 items-center text-blue-800 underline"
              to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(done)}`}
              data-testid="tickets.capture.show"
            >
              {t('tickets.capture.show')}
            </Link>
          </p>
        )}
      </div>
    );
  }
  return (
    <form
      className="flex flex-col gap-3 rounded border border-gray-300 p-3"
      aria-labelledby={`${wgId}-heading`}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      data-testid="tickets.capture"
    >
      <h2 id={`${wgId}-heading`} className="font-semibold">
        {t('tickets.capture.heading')}
      </h2>
      <p className="text-sm text-gray-700">{t('tickets.capture.intro')}</p>
      {wgs.length > 1 && (
        <div className="flex flex-col gap-1">
          <label htmlFor={wgId} className="text-sm font-medium">
            {t('tickets.field.workgroup')}
          </label>
          <select
            id={wgId}
            className={INPUT}
            value={wg}
            disabled={busy}
            onChange={(event) => setChosen(event.target.value)}
            data-testid="tickets.capture.wg"
          >
            {wgs.map((id) => (
              <option key={id} value={id}>
                {name(id)}
              </option>
            ))}
          </select>
        </div>
      )}
      <TicketForm org={org} wgs={[wg]} values={values} onChange={setValues} disabled={busy} prefix="tickets.capture" />
      {code !== null && (
        <p className="text-sm text-red-800" role="alert" data-testid="tickets.capture.error" data-code={code}>
          {code === 'request_invalid'
            ? t('tickets.capture.invalid')
            : t('tickets.capture.failed', { reason: error(code).title })}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          className="min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50"
          disabled={busy || values.title.trim() === ''}
          data-testid="tickets.capture.submit"
        >
          {t('tickets.capture.submit')}
        </button>
        <button
          type="button"
          className="min-h-11 rounded border border-gray-300 px-4"
          disabled={busy}
          onClick={() => setOpen(false)}
          data-testid="tickets.capture.cancel"
        >
          {t('tickets.capture.cancel')}
        </button>
      </div>
    </form>
  );
}
