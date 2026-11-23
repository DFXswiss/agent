// Settings section "Passkeys and recovery" (06-web.md sections 3.2, 3.3 and 3.6): the passkeys of the account with
// "Add passkey" and "Remove", whether this browser can unlock without passkey PRF ("Forget"), and the recovery-code
// check that clears the "Recovery code not confirmed" reminder, and a new recovery code at any time (KOR-01).
import type { SettingsSection } from '../../../registries/settings-sections';
import { AddPasskey } from './AddPasskey';
import { NewRecoveryCode } from './NewRecoveryCode';
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
      <NewRecoveryCode />
    </div>
  );
}

export const section: SettingsSection = {
  id: 'passkeys',
  order: 10,
  titleKey: 'passkeys.title',
  Component: PasskeysSection,
};
