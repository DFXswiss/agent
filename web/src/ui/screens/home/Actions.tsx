// The next step of an entry of the situation view, without opening the task (06-web.md section 4.0, ADR-076 section 8;
// E-05: the next step, never only an explanation). Only for users who may write in the task's workgroup:
//
// - `question`: the agent's question (opened and verified in the browser) and an answer field (a follow-up prompt);
// - `approval`: the question, an optional comment and "Approve" / "Decline" (a prompt starting with `Approved.` /
//   `Declined.`);
// - `connection_missing`: "Connect GitHub" (the settings); `model_access_missing`: the model settings;
// - `device_missing`: "Assign a computer" for those who may (an admin, any writer of a private org), else who can;
// - `credit_empty`: the operator sets the balance, "Continue" once it is set;
// - `quota_choice` (ADR-081 section 5): "Continue with DFX access" (the task's consent, `POST
//   /v1/tasks/{task}/model-consent`) and, for the task's owner only, "Always" (the setting `fallback.own_to_dfx`);
//   without an answer the task continues by itself on the own subscription at its release;
// - `session_failed`, `guest_error`, `device_unreachable`, `no_capacity`: "Try again" (the newest prompt again).
//
// Prompts go through the chat's send code (the sealed prompt is kept for a retry after a lost answer). The entry
// leaves its group when the hub's live frame says so.
import { useEffect, useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { loadQuestion } from '../../../core/attention/load';
import { type Action, approvalText, CONTINUE, selectQuestion } from '../../../core/attention/view';
import { sendPrompt } from '../../../core/chat/actions';
import { allowDfxAlways, allowDfxForTask } from '../../../core/credentials/model';
import type { AttentionRow } from '../../../core/store/slices/attention';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { retryPrompt, sendable, sendErrorCode } from '../../../core/tasks/send';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

const BUTTON = 'min-h-11 rounded px-4 disabled:opacity-50';
const PRIMARY = `${BUTTON} bg-blue-700 text-white`;
const SECONDARY = `${BUTTON} border border-gray-300`;
const LINK = 'flex min-h-11 items-center rounded border border-gray-300 px-4';

type Status = { kind: 'idle' } | { kind: 'sending' } | { kind: 'sent' } | { kind: 'error'; code: string };

function Question({ task, row }: { readonly task: TaskRow; readonly row: AttentionRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const [select] = useState(() => selectQuestion(task.wg_id, row));
  const question = useStore(select, app.store);
  useEffect(() => {
    if (row.ref_id !== null) loadQuestion(app.tasks, task, row.ref_id).catch(() => undefined);
  }, [app, task, row.ref_id]);
  return (
    <blockquote
      className="whitespace-pre-wrap rounded border-l-4 border-blue-300 bg-blue-50 px-3 py-2 text-sm"
      data-testid="attention.question"
      data-status={question.status}
    >
      {question.status === 'verified' && question.text}
      {question.status === 'missing' && <span className="text-gray-600">{t('attention.question.loading')}</span>}
      {question.status === 'unverifiable' && (
        <span className="text-gray-600">{t('attention.question.unverifiable', { code: question.code })}</span>
      )}
    </blockquote>
  );
}

export function Actions({
  org,
  task,
  row,
  action,
  assign,
  owner,
}: {
  readonly org: string;
  readonly task: TaskRow;
  readonly row: AttentionRow;
  readonly action: Action;
  /** The user may assign a computer to the task's workgroup. */
  readonly assign: boolean;
  /** The user owns the task (only the owner's own setting can say "always"). */
  readonly owner: boolean;
}) {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const fieldId = useId();
  const [text, setText] = useState('');
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  const [always, setAlways] = useState(false);
  const sending = status.kind === 'sending';
  const run = async (send: () => Promise<unknown>) => {
    setStatus({ kind: 'sending' });
    try {
      await send();
      setText('');
      setStatus({ kind: 'sent' });
    } catch (failure) {
      setStatus({ kind: 'error', code: sendErrorCode(failure) });
    }
  };
  const prompt = (value: string) => run(() => sendPrompt(app.tasks, task, value));
  const devices = `/o/${encodeURIComponent(org)}/devices`;
  return (
    <div className="flex flex-col gap-2 px-2 pb-2" data-testid="attention.actions">
      {(action === 'answer' || action === 'approve') && <Question task={task} row={row} />}
      {action === 'answer' && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!sending && sendable(text)) void prompt(text);
          }}
        >
          <label htmlFor={fieldId} className="text-sm">
            {t('attention.answer.label')}
          </label>
          <textarea
            id={fieldId}
            className="min-h-20 w-full rounded border border-gray-300 p-2"
            value={text}
            onChange={(event) => setText(event.target.value)}
            disabled={sending}
            data-testid="attention.answer"
          />
          <button
            type="submit"
            className={`${PRIMARY} self-start`}
            disabled={sending || !sendable(text)}
            data-testid="attention.answer.send"
          >
            {t('attention.answer.send')}
          </button>
        </form>
      )}
      {action === 'approve' && (
        <div className="flex flex-col gap-2">
          <label htmlFor={fieldId} className="text-sm">
            {t('attention.comment.label')}
          </label>
          <textarea
            id={fieldId}
            className="min-h-16 w-full rounded border border-gray-300 p-2"
            value={text}
            onChange={(event) => setText(event.target.value)}
            disabled={sending}
            data-testid="attention.comment"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={PRIMARY}
              disabled={sending}
              onClick={() => void prompt(approvalText(true, text))}
              data-testid="attention.approve"
            >
              {t('attention.approve')}
            </button>
            <button
              type="button"
              className={SECONDARY}
              disabled={sending}
              onClick={() => void prompt(approvalText(false, text))}
              data-testid="attention.decline"
            >
              {t('attention.decline')}
            </button>
          </div>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {action === 'connect' && (
          <Link className={LINK} to="/settings" data-testid="attention.connect">
            {t('attention.connect')}
          </Link>
        )}
        {action === 'model' && (
          <Link className={LINK} to="/settings" data-testid="attention.model">
            {t('attention.model')}
          </Link>
        )}
        {action === 'assign' &&
          (assign ? (
            <Link className={LINK} to={devices} data-testid="attention.assign">
              {t('attention.assign')}
            </Link>
          ) : (
            <p className="text-sm text-gray-700" data-testid="attention.assign.hint">
              {t('attention.assign.hint')}
            </p>
          ))}
        {action === 'continue' && (
          <>
            <p className="text-sm text-gray-700" data-testid="attention.credit">
              {t('attention.credit.hint')}
            </p>
            <button
              type="button"
              className={SECONDARY}
              disabled={sending}
              onClick={() => void prompt(CONTINUE)}
              data-testid="attention.continue"
            >
              {t('attention.continue')}
            </button>
          </>
        )}
        {action === 'consent' && (
          <>
            <p className="w-full text-sm text-gray-700" data-testid="attention.consent.hint">
              {t('attention.consent.hint')}
            </p>
            <button
              type="button"
              className={PRIMARY}
              disabled={sending}
              onClick={() => void run(() => allowDfxForTask(app.tasks, task.task_id))}
              data-testid="attention.consent"
            >
              {t('attention.consent')}
            </button>
            {owner && (
              <button
                type="button"
                className={SECONDARY}
                disabled={sending || always}
                onClick={() =>
                  void run(async () => {
                    await allowDfxAlways(app.tasks, locale);
                    setAlways(true);
                  })
                }
                data-testid="attention.consent.always"
              >
                {t('attention.consent.always')}
              </button>
            )}
            {owner && (
              <p className="w-full text-xs text-gray-600" data-testid="attention.consent.always.hint">
                {t(always ? 'attention.consent.always.saved' : 'attention.consent.always.hint')}
              </p>
            )}
          </>
        )}
        {action === 'retry' && (
          <button
            type="button"
            className={SECONDARY}
            disabled={sending}
            onClick={() => void run(() => retryPrompt(app.tasks, task))}
            data-testid="attention.retry"
          >
            {t('attention.retry')}
          </button>
        )}
      </div>
      {status.kind === 'sent' && (
        <p role="status" className="text-sm text-green-800" data-testid="attention.sent">
          {t('attention.sent')}
        </p>
      )}
      {status.kind === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="attention.error" data-code={status.code}>
          {error(status.code).title}
        </p>
      )}
    </div>
  );
}
