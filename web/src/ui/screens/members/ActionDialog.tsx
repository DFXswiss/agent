// The confirmation dialog of every membership change (06-web.md sections 7.1-7.3): an explanation, optional inputs,
// then the change with a progress view while it runs ("Rotating keys 2/5", "Wrapping for 7 recipients", "Sending",
// "Attempt 2 of 3" after `seq_conflict`), and its error code with "Try again" if it is refused. Exported for the
// device screen (WEB-07: unassign and unpair).
import { type ReactNode, useState } from 'react';
import { actionErrorCode, BATCH_ATTEMPTS, type SubmitProgress } from '../../../core/org/batch';
import { useI18n } from '../../i18n';
import { Dialog } from './Dialog';

export const BUTTON = 'min-h-11 rounded border border-gray-300 px-4';
export const PRIMARY = 'min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50';
export const DANGER = 'min-h-11 rounded bg-red-800 px-4 text-white disabled:opacity-50';

/** What the progress view shows. */
export interface ProgressView {
  readonly rotate: { readonly done: number; readonly total: number } | null;
  readonly recipients: number | null;
  readonly sending: boolean;
  readonly attempt: number;
}

export const NO_PROGRESS: ProgressView = { rotate: null, recipients: null, sending: false, attempt: 1 };

/** The view after one progress report. */
export function advance(view: ProgressView, step: SubmitProgress): ProgressView {
  switch (step.step) {
    case 'rotate':
      return { ...view, rotate: { done: step.done, total: step.total } };
    case 'wrap':
      return { ...view, recipients: step.recipients };
    case 'send':
      return { ...view, sending: true };
    default:
      return { ...NO_PROGRESS, attempt: step.attempt };
  }
}

export function Progress({ view, testId }: { readonly view: ProgressView; readonly testId: string }) {
  const { t } = useI18n();
  return (
    <ul className="flex flex-col gap-1 text-sm" aria-live="polite" data-testid={`${testId}.progress`}>
      {view.attempt > 1 && (
        <li data-testid={`${testId}.progress.attempt`}>
          {t('members.progress.attempt', { attempt: view.attempt, max: BATCH_ATTEMPTS })}
        </li>
      )}
      {view.rotate !== null && (
        <li data-testid={`${testId}.progress.rotate`}>{t('members.progress.rotate', view.rotate)}</li>
      )}
      {view.recipients !== null && (
        <li data-testid={`${testId}.progress.wrap`}>{t('members.progress.wrap', { recipients: view.recipients })}</li>
      )}
      {view.sending && <li data-testid={`${testId}.progress.send`}>{t('members.progress.send')}</li>}
      {view.rotate === null && view.recipients === null && !view.sending && <li>{t('members.progress.prepare')}</li>}
    </ul>
  );
}

export interface ActionDialogProps {
  readonly title: string;
  /** `data-testid` prefix: `<testId>` (dialog), `.confirm`, `.cancel`, `.progress`, `.error`. */
  readonly testId: string;
  readonly confirmLabel: string;
  readonly danger?: boolean;
  /** Inputs complete (the confirm button is enabled). */
  readonly ready?: boolean;
  readonly children?: ReactNode;
  /** Runs the change and reports progress; resolves when it was accepted. */
  readonly run: (progress: (step: SubmitProgress) => void) => Promise<unknown>;
  readonly onDone: () => void;
  readonly onClose: () => void;
}

export function ActionDialog(props: ActionDialogProps) {
  const { title, testId, confirmLabel, danger = false, ready = true, children, run, onDone, onClose } = props;
  const { t, error } = useI18n();
  const [view, setView] = useState<ProgressView | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const confirm = async () => {
    setCode(null);
    setView(NO_PROGRESS);
    try {
      await run((step) => setView((current) => advance(current as ProgressView, step)));
    } catch (failure) {
      setView(null);
      setCode(actionErrorCode(failure));
      return;
    }
    onDone();
  };
  const busy = view !== null;
  return (
    <Dialog title={title} testId={testId} busy={busy} onClose={onClose}>
      {children}
      {busy && <Progress view={view} testId={testId} />}
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid={`${testId}.error`} data-code={code}>
          {error(code).title}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className={danger ? DANGER : PRIMARY}
          disabled={busy || !ready}
          onClick={() => void confirm()}
          data-testid={`${testId}.confirm`}
        >
          {confirmLabel}
        </button>
        <button type="button" className={BUTTON} disabled={busy} onClick={onClose} data-testid={`${testId}.cancel`}>
          {t('members.cancel')}
        </button>
      </div>
    </Dialog>
  );
}
