// The AI agent that an invitation with pairing connects (11-agent-access.md section 5.2): its name and computer as
// the link states them, that it gets full access and becomes the main orchestrator, and the warning of `/connect`.
// Creating the passkey is the confirmation: the sign-in after it is the pairing-tagged ceremony, and `/connect`
// finishes the pairing once the account is ready.
//
// For an agent on another computer (ADR-067 section 6) the notice shows the warning of section 4.2 and asks for the
// agent code before the passkey is created: the screens keep their passkey buttons disabled until it matches.
import type { PairingLink } from '../../../core/agents/link';
import { useI18n } from '../../i18n';
import { AgentCodeField, RemoteWarning, type useAgentCode } from '../connect/AgentCode';
import { RequesterMessage } from '../connect/RequesterMessage';

export interface AgentNoticeProps {
  readonly pairing: PairingLink;
  /** The typed agent code (`useAgentCode`); asked only for a remote pairing. */
  readonly code: ReturnType<typeof useAgentCode>;
  readonly disabled: boolean;
}

export function AgentNotice({ pairing, code, disabled }: AgentNoticeProps) {
  const { t } = useI18n();
  return (
    <section className="flex flex-col gap-2 rounded border border-gray-300 p-3" data-testid="invite.agent">
      <h3 className="font-semibold">{t('invite.agent.title')}</h3>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-gray-700">{t('connect.agent')}</dt>
        <dd className="break-words font-medium" data-testid="invite.agent.name">
          {pairing.name}
        </dd>
        <dt className="text-gray-700">{t('connect.host')}</dt>
        <dd className="break-words font-medium" data-testid="invite.agent.host">
          {pairing.host}
        </dd>
      </dl>
      <p className="text-sm text-gray-700">{t('connect.stated')}</p>
      <RequesterMessage message={pairing.message} caption={t('connect.message')} testId="invite.agent.message" />
      <p className="font-semibold">{t('connect.access')}</p>
      <p>{t('invite.agent.main')}</p>
      {pairing.mode === 'remote' ? (
        <RemoteWarning host={pairing.host} testId="invite.agent.remote-warning" />
      ) : (
        <p role="note" className="rounded border border-amber-600 bg-amber-50 p-3" data-testid="invite.agent.warning">
          {t('connect.warning')}
        </p>
      )}
      {pairing.mode === 'remote' && (
        <AgentCodeField
          typed={code.typed}
          onChange={code.setTyped}
          matches={code.matches}
          disabled={disabled}
          testId="invite.agent.code"
        />
      )}
    </section>
  );
}

/** The display name the agent proposed, shown instead of the name field. */
export function ProposedName({ name }: { readonly name: string }) {
  const { t } = useI18n();
  return <p data-testid="invite.proposed-name">{t('invite.agent.display-name', { name })}</p>;
}
