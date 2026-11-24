// The agent code of a pairing from another computer (ADR-058 section 2): the user types the code their AI agent
// shows them; the page never shows it. The typed text is read like the agent tool prints it (lower case, spaces,
// dashes; `I`/`L` as `1`, `O` as `0`), and only an exact match with the code computed from the link and the hub's
// nonce enables the passkey button. The warning of section 4.2 names the computer as the link states it.
import { useId, useState } from 'react';
import { AGENT_CODE_LENGTH, tryParseCode } from '../../../core/crypto/agent';
import { useI18n } from '../../i18n';

/** The typed agent code and whether it equals `expected` (null: no code is asked, always true). */
export function useAgentCode(expected: string | null) {
  const [typed, setTyped] = useState('');
  const matches = expected === null || tryParseCode(typed, AGENT_CODE_LENGTH) === expected;
  return { typed, setTyped, matches };
}

export interface AgentCodeFieldProps {
  readonly typed: string;
  readonly onChange: (text: string) => void;
  readonly matches: boolean;
  readonly disabled: boolean;
  readonly testId: string;
}

/** The input "Type the code your AI agent shows you", with a hint while a complete code does not match. */
export function AgentCodeField({ typed, onChange, matches, disabled, testId }: AgentCodeFieldProps) {
  const { t } = useI18n();
  const id = useId();
  const complete = tryParseCode(typed, AGENT_CODE_LENGTH) !== null;
  return (
    <div className="flex flex-col gap-1" data-testid={testId} data-matches={matches}>
      <label htmlFor={id} className="font-semibold">
        {t('connect.remote.code.label')}
      </label>
      <input
        id={id}
        className="min-h-11 rounded border border-gray-400 px-3 font-mono uppercase tracking-widest"
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        maxLength={24}
        value={typed}
        aria-invalid={complete && !matches}
        aria-describedby={`${id}-hint`}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        data-testid={`${testId}.input`}
      />
      <p id={`${id}-hint`} className="text-sm text-gray-700">
        {complete && !matches ? t('connect.remote.code.wrong') : t('connect.remote.code.hint')}
      </p>
    </div>
  );
}

/** The warning of a pairing from another computer (ADR-058 section 4.2): visually distinct from the loopback one. */
export function RemoteWarning({ host, testId }: { readonly host: string; readonly testId: string }) {
  const { t } = useI18n();
  return (
    <div role="note" className="rounded border-2 border-red-700 bg-red-50 p-3 text-red-900" data-testid={testId}>
      <p className="font-semibold">{t('connect.remote.title')}</p>
      <p>{t('connect.remote.warning', { host })}</p>
    </div>
  );
}
