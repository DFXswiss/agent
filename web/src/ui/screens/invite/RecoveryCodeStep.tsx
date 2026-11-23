// A recovery code on screen (06-web.md section 3.2, KOR-22): the grouped code in a monospace block, "Copy",
// "Download as text file" (generated in the browser as a `Blob`, `agent-recovery-code.txt`) and **one** button that
// goes on – no retyping, no skip, no dialog. `children` sit between the code and the button (e.g. "Remember this
// browser"). Used by the registration without PRF, the settings ("Create recovery code") and the recovery.
import { type ReactNode, useState } from 'react';
import { useI18n } from '../../i18n';

export const FILE_NAME = 'agent-recovery-code.txt';

export interface RecoveryCodeStepProps {
  /** `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`. */
  readonly code: string;
  /** One line above the code (what it is for here). */
  readonly explain: string;
  /** Label of the button. */
  readonly continueLabel: string;
  readonly onContinue: () => void;
  /** The button is disabled (work in progress). */
  readonly busy?: boolean;
  readonly children?: ReactNode;
  /** Writes text to the clipboard (default `navigator.clipboard.writeText`). */
  readonly copy?: (text: string) => Promise<void>;
  /** Offers a file for download (default: a `Blob` link). */
  readonly download?: (name: string, text: string) => void;
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
  const { code, explain, continueLabel, onContinue, busy = false, children } = props;
  const { copy = clipboardWrite, download = downloadText } = props;
  const { t } = useI18n();
  const [copied, setCopied] = useState<'copied' | 'copy-failed' | null>(null);

  const doCopy = () => {
    copy(code).then(
      () => setCopied('copied'),
      () => setCopied('copy-failed'),
    );
  };

  return (
    <section className="flex flex-col gap-4" data-testid="recovery-code.step">
      <h2 className="text-xl font-semibold">{t('invite.recovery.title')}</h2>
      <p data-testid="recovery-code.explain">{explain}</p>
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
      {children}
      <button
        type="button"
        className="min-h-11 self-start rounded bg-gray-900 px-4 text-white disabled:opacity-60"
        onClick={onContinue}
        disabled={busy}
        data-testid="recovery-code.continue"
      >
        {continueLabel}
      </button>
    </section>
  );
}
