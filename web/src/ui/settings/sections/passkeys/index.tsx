// Settings section "Passkeys and recovery" (06-web.md sections 3.2, 3.3 and 3.6): the passkeys of the account with
// "Add passkey" and "Remove", whether this browser can unlock without passkey PRF ("Forget"), and the recovery code:
// create one, or a new one, at any time (KOR-01, KOR-22); the account code others use to admit the account (ADR-060).
import type { SettingsSection } from '../../../registries/settings-sections';
import { AccountCode } from './AccountCode';
import { AddPasskey } from './AddPasskey';
import { PasskeyList } from './PasskeyList';
import { RecoveryCode } from './RecoveryCode';
import { ThisBrowser } from './ThisBrowser';

export function PasskeysSection() {
  return (
    <div className="flex flex-col gap-6">
      <PasskeyList />
      <AddPasskey />
      <ThisBrowser />
      <RecoveryCode />
      <AccountCode />
    </div>
  );
}

export const section: SettingsSection = {
  id: 'passkeys',
  order: 10,
  titleKey: 'passkeys.title',
  Component: PasskeysSection,
};
