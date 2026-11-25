// Settings section "My subscriptions" (KOR-68): the subscriptions and their quotas are on their own page and on the
// card of the home page; the settings only lead there.
import { Link } from 'react-router-dom';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useI18n } from '../../../i18n';
import '../../../../core/i18n/lazy/subscriptions';

export function SubscriptionsLink() {
  const { t } = useI18n();
  return (
    <Link
      to="/subscriptions"
      className="flex min-h-11 items-center self-start underline"
      data-testid="settings.subscriptions"
    >
      {t('subscriptions.settings.link')}
    </Link>
  );
}

export const section: SettingsSection = {
  id: 'subscriptions',
  order: 63,
  titleKey: 'subscriptions.title',
  Component: SubscriptionsLink,
};
