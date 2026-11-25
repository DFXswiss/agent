// The Claude subscriptions of the user with "Add a subscription" (KOR-69, ADR-090; rules E-30, E-31): the list of
// `loadSubscriptions` as cards. A standalone component: the page "Subscriptions" (KOR-68) shows it beside its
// overview of the remaining quotas.
import { useCallback, useEffect, useState } from 'react';
import { loadSubscriptions, type Subscription } from '../../core/credentials/subscriptions';
import { requestErrorCode } from '../../core/requests/client';
import { useI18n } from '../i18n';
import { BUTTON, PRIMARY } from '../settings/sections/credentials/DevicePicker';
import { useReason } from '../settings/sections/github/reason';
import { useAppEnv } from '../shell/context';
import { AddSubscription } from './AddSubscription';
import { SubscriptionCard } from './SubscriptionCard';

type State =
  | { readonly kind: 'loading'; readonly items: readonly Subscription[] | null }
  | { readonly kind: 'ready'; readonly items: readonly Subscription[] }
  | { readonly kind: 'error'; readonly code: string };

export function SubscriptionManager() {
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
    loadSubscriptions(app.tasks).then(
      (items) => setState({ kind: 'ready', items }),
      (error: unknown) => setState({ kind: 'error', code: requestErrorCode(error) }),
    );
  }, [app, state]);
  const added = useCallback(() => {
    setAdding(false);
    reload();
  }, [reload]);
  return (
    <section className="flex flex-col gap-3" data-testid="submanage.manager">
      {adding ? (
        <AddSubscription onAdded={added} onClose={() => setAdding(false)} />
      ) : (
        <button type="button" className={PRIMARY} onClick={() => setAdding(true)} data-testid="submanage.add">
          {t('subscription-login.add')}
        </button>
      )}
      {state.kind === 'loading' && state.items === null && (
        <p aria-live="polite" data-testid="submanage.loading">
          {t('subscription-login.loading')}
        </p>
      )}
      {state.kind === 'error' && (
        <>
          <p role="alert" data-testid="submanage.failed" data-code={state.code}>
            {t('subscription-login.load-failed', { reason: reason(state.code) })}
          </p>
          <button type="button" className={BUTTON} onClick={reload} data-testid="submanage.retry">
            {t('subscription-login.retry')}
          </button>
        </>
      )}
      {state.kind === 'ready' && state.items.length === 0 && (
        <p data-testid="submanage.empty">{t('subscription-login.empty')}</p>
      )}
      {(state.kind === 'ready' || state.kind === 'loading') &&
        state.items?.map((item) => <SubscriptionCard key={item.id} item={item} onChanged={reload} />)}
    </section>
  );
}
