// The card "My subscriptions" on the home page (rule E-30, KOR-68): the provider rows of every subscription of the
// user, measured centrally, collapsed and opened per provider; a link to the full page. Without any subscription it
// shows the way to add one. Hidden while the first load runs and when the hub cannot answer (an older release has no
// such route): the situation view stays as it was.
import { Link } from 'react-router-dom';
import { providerGroups } from '../../core/subscriptions/subscriptions';
import { useNow } from '../countdown/useNow';
import { ProviderList, TICK_MS, useTexts } from './ProviderList';
import { useSubscriptions } from './useSubscriptions';

export function SubscriptionsCard() {
  const { t } = useTexts();
  const { rows } = useSubscriptions();
  const now = useNow(true, TICK_MS);
  if (rows === null) return null;
  return (
    <section
      className="flex flex-col gap-2 rounded border border-gray-200 bg-gray-50 p-3"
      aria-labelledby="home-subscriptions"
      data-testid="subscriptions.card"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="home-subscriptions" className="text-lg font-semibold">
          {t('subscriptions.title')}
        </h2>
        <Link to="/subscriptions" className="flex min-h-11 items-center underline" data-testid="subscriptions.card.all">
          {t('subscriptions.all')}
        </Link>
      </div>
      {rows.length === 0 ? (
        <p className="text-gray-700" data-testid="subscriptions.card.empty">
          {t('subscriptions.empty')}{' '}
          <Link to="/subscriptions" className="underline" data-testid="subscriptions.card.add">
            {t('subscriptions.add')}
          </Link>
        </p>
      ) : (
        <ProviderList groups={providerGroups(rows)} now={now} />
      )}
    </section>
  );
}
