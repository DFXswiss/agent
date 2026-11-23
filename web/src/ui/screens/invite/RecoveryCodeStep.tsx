// The recovery code, shown once (06-web.md section 3.2): the grouped code in a monospace block, "Copy", "Download as
// text file" (generated in the browser as a `Blob`, `agent-recovery-code.txt`), and "Continue" only after the last
// group (6 characters, case-insensitive) was typed again. "Skip" (registration only, `allowSkip`) asks first and states
// the consequence; the caller then sets the persistent reminder. Reused by the recovery flow (WEB-14) without skip.
import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from 'react';
import { useI18n } from '../../i18n';

export const FILE_NAME = 'agent-recovery-code.txt';

export interface RecoveryCodeStepProps {
  /** `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`. */
  readonly code: string;
  readonly allowSkip: boolean;
  readonly onConfirmed: () => void;
  readonly onSkipped: () => void;
  /** Writes text to the clipboard (default `navigator.clipboard.writeText`). */
  readonly copy?: (text: string) => Promise<void>;
  /** Offers a file for download (default: a `Blob` link). */
  readonly download?: (name: string, text: string) => void;
}

/** The last group of the code (what the user types to confirm). */
export function lastGroup(code: string): string {
  return code.slice(code.lastIndexOf('-') + 1);
}

/** True if `typed` is the last group of `code` (case and surrounding spaces ignored). */
export function confirmsCode(code: string, typed: string): boolean {
  return typed.trim().toUpperCase() === lastGroup(code);
}

export function downloadText(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.rel = 'noopener';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

const clipboardWrite = (text: string) => navigator.clipboard.writeText(text);
const BUTTON = 'min-h-11 rounded border border-gray-400 px-4';

export function RecoveryCodeStep(props: RecoveryCodeStepProps) {
  const { code, allowSkip, onConfirmed, onSkipped, copy = clipboardWrite, download = downloadText } = props;
  const { t } = useI18n();
  const [typed, setTyped] = useState('');
  const [mismatch, setMismatch] = useState(false);
  const [copied, setCopied] = useState<'copied' | 'copy-failed' | null>(null);
  const [asking, setAsking] = useState(false);
  const confirmSkip = useRef<HTMLButtonElement>(null);
  const cancelSkip = useRef<HTMLButtonElement>(null);
  const skipButton = useRef<HTMLButtonElement>(null);
  const opened = useRef(false);
  useEffect(() => {
    // focus moves into the dialog when it opens and back to "Skip" when it closes
    if (asking) confirmSkip.current?.focus();
    else if (opened.current) skipButton.current?.focus();
    opened.current = asking;
  }, [asking]);
  /** Keyboard of the dialog: Escape closes it, Tab stays on its two buttons (focus trap, 06-web.md section 12.3). */
  const dialogKey = (event: KeyboardEvent) => {
    if (event.key === 'Escape') setAsking(false);
    if (event.key !== 'Tab') return;
    event.preventDefault();
    const next = document.activeElement === confirmSkip.current ? cancelSkip : confirmSkip;
    next.current?.focus();
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (confirmsCode(code, typed)) onConfirmed();
    else setMismatch(true);
  };
  const doCopy = () => {
    copy(code).then(
      () => setCopied('copied'),
      () => setCopied('copy-failed'),
    );
  };

  return (
    <section className="flex flex-col gap-4" data-testid="recovery-code.step">
      <h2 className="text-xl font-semibold">{t('invite.recovery.title')}</h2>
      <p>{t('invite.recovery.explain')}</p>
      <pre
        className="select-all overflow-x-auto rounded bg-gray-100 p-4 text-center font-mono text-lg"
        data-testid="recovery-code.code"
      >
        {code}
      </pre>
      <div className="flex flex-wrap gap-3">
        <button type="button" className={BUTTON} onClick={doCopy} data-testid="recovery-code.copy">
          {t('invite.recovery.copy')}
        </button>
        <button
          type="button"
          className={BUTTON}
          onClick={() => download(FILE_NAME, t('invite.recovery.file', { code }))}
          data-testid="recovery-code.download"
        >
          {t('invite.recovery.download')}
        </button>
      </div>
      {copied !== null && (
        <p role="status" data-testid={`recovery-code.${copied}`}>
          {t(`invite.recovery.${copied}`)}
        </p>
      )}
      <form className="flex flex-col gap-2" onSubmit={submit}>
        <label htmlFor="recovery-code-confirm">
          {t('invite.recovery.confirm.label', { length: lastGroup(code).length })}
        </label>
        <input
          id="recovery-code-confirm"
          className="min-h-11 rounded border border-gray-400 px-3 font-mono uppercase"
          autoComplete="off"
          spellCheck={false}
          value={typed}
          aria-invalid={mismatch}
          onChange={(event) => {
            setTyped(event.target.value);
            setMismatch(false);
          }}
          data-testid="recovery-code.confirm"
        />
        {mismatch && (
          <p role="alert" className="text-red-800" data-testid="recovery-code.mismatch">
            {t('invite.recovery.confirm.mismatch')}
          </p>
        )}
        <div className="flex flex-wrap gap-3">
          <button
            type="submit"
            className="min-h-11 rounded bg-gray-900 px-4 text-white"
            data-testid="recovery-code.continue"
          >
            {t('invite.recovery.continue')}
          </button>
          {allowSkip && (
            <button
              ref={skipButton}
              type="button"
              className="min-h-11 px-4 underline"
              onClick={() => setAsking(true)}
              data-testid="recovery-code.skip"
            >
              {t('invite.recovery.skip')}
            </button>
          )}
        </div>
      </form>
      {asking && (
        <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/40 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="recovery-skip-title"
            className="flex max-w-md flex-col gap-4 rounded bg-white p-6"
            onKeyDown={dialogKey}
            data-testid="recovery-code.skip-dialog"
          >
            <h3 id="recovery-skip-title" className="text-lg font-semibold">
              {t('invite.recovery.skip.title')}
            </h3>
            <p>{t('invite.recovery.skip.body')}</p>
            <div className="flex flex-wrap gap-3">
              <button
                ref={confirmSkip}
                type="button"
                className="min-h-11 rounded bg-red-800 px-4 text-white"
                onClick={onSkipped}
                data-testid="recovery-code.skip-confirm"
              >
                {t('invite.recovery.skip.confirm')}
              </button>
              <button
                ref={cancelSkip}
                type="button"
                className={BUTTON}
                onClick={() => setAsking(false)}
                data-testid="recovery-code.skip-cancel"
              >
                {t('invite.recovery.skip.cancel')}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
