// Settings section "Mailboxes" (KOR-74, ADR-099 section 9; rules E-04, E-31): every own mailbox the platform reads for
// sessions – name, address, purpose, the workgroups it is released to and the mark of the primary one (the mailbox of
// the automatic login) – with "Add a mailbox", change (the password is write-only), release to workgroups, check on a
// device and delete. Records are opened and sealed in the browser under the user's `CK` (`core/credentials/mailboxes`);
// the password never reaches this UI. Loaded on demand by `index.tsx`, so it is not part of the first load.
import { useCallback, useEffect, useState } from 'react';
import '../../../../core/i18n/lazy/mailbox';
import { loadMailboxes, type Mailbox } from '../../../../core/credentials/mailboxes';
import { requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { BUTTON, PRIMARY } from '../credentials/DevicePicker';
import { useReason } from '../github/reason';
import { MailboxCard } from './MailboxCard';
import { MailboxForm } from './MailboxForm';

type State =
  | { readonly kind: 'loading'; readonly items: readonly Mailbox[] | null }
  | { readonly kind: 'ready'; readonly items: readonly Mailbox[] }
  | { readonly kind: 'error'; readonly code: string };

export function MailboxesSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [state, setState] = useState<State>({ kind: 'loading', items: null });
  const [adding, setAdding] = useState(false);
  // the cards stay while the list is read again, so an open dialog and its outcome stay visible
  const reload = useCallback(
    () => setState((current) => ({ kind: 'loading', items: current.kind === 'ready' ? current.items : null })),
    [],
  );
  useEffect(() => {
    if (state.kind !== 'loading') return;
    loadMailboxes(app.tasks).then(
      (items) => setState({ kind: 'ready', items }),
      (error: unknown) => setState({ kind: 'error', code: requestErrorCode(error) }),
    );
  }, [app, state]);
  const items = state.kind === 'error' ? [] : (state.items ?? []);
  return (
    <section className="flex flex-col gap-3" data-testid="mailboxes.section">
      <p className="text-sm text-gray-600">{t('mailbox.intro')}</p>
      {adding ? (
        <MailboxForm
          item={null}
          items={items}
          onSaved={() => {
            setAdding(false);
            reload();
          }}
          onClose={() => setAdding(false)}
        />
      ) : (
        <button type="button" className={PRIMARY} onClick={() => setAdding(true)} data-testid="mailboxes.add">
          {t('mailbox.add')}
        </button>
      )}
      {state.kind === 'loading' && state.items === null && (
        <p aria-live="polite" data-testid="mailboxes.loading">
          {t('mailbox.loading')}
        </p>
      )}
      {state.kind === 'error' && (
        <>
          <p role="alert" data-testid="mailboxes.failed" data-code={state.code}>
            {t('mailbox.load-failed', { reason: reason(state.code) })}
          </p>
          <button type="button" className={BUTTON} onClick={reload} data-testid="mailboxes.retry">
            {t('mailbox.retry')}
          </button>
        </>
      )}
      {state.kind === 'ready' && items.length === 0 && <p data-testid="mailboxes.empty">{t('mailbox.empty')}</p>}
      {items.map((item) => (
        <MailboxCard key={item.id} item={item} items={items} onChanged={reload} />
      ))}
    </section>
  );
}

export default MailboxesSection;
