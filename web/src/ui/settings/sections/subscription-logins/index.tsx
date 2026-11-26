// Settings section "Claude subscriptions" (KOR-69, ADR-090; rules E-30, E-31): every Claude subscription of the user,
// each logged in on a platform device, with "Add a subscription", "Log in now", name and note, use, log out and remove
// (`ui/subscriptions`). The overview of the remaining quotas of all subscriptions is the page of KOR-68.
import type { SettingsSection } from '../../../registries/settings-sections';
import { useI18n } from '../../../i18n';
import { SubscriptionManager } from '../../../subscriptions/SubscriptionManager';
import '../../../../core/i18n/lazy/subscription-login';

export function SubscriptionLoginsSection() {
  const { t } = useI18n();
  return (
    <div className="flex flex-col gap-3" data-testid="subscription-logins.section">
      <p className="text-sm text-gray-600">{t('subscription-login.intro')}</p>
      <SubscriptionManager />
    </div>
  );
}

export const section: SettingsSection = {
  id: 'subscription-logins',
  order: 63,
  titleKey: 'subscription-login.title',
  Component: SubscriptionLoginsSection,
};
