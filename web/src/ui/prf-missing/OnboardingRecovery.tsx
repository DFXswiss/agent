// The recovery-code step of a registration (06-web.md sections 3.1, 3.2, 3.3) as the invitation paths use it: the code
// (WEB-03's `RecoveryCodeStep`; "Skip" only with a passkey slot), then – when gesture 2 gave no PRF output, so no
// passkey slot exists – the "Remember this browser" offer before the flow goes on.
import { useState } from 'react';
import type { Identity } from '../../core/crypto/keys';
import { RecoveryCodeStep } from '../screens/invite/RecoveryCodeStep';
import { RememberBrowser } from './RememberBrowser';

export interface OnboardingRecoveryProps {
  readonly code: string;
  /** A passkey slot was uploaded (PRF). */
  readonly slot: boolean;
  readonly userId: string;
  readonly identity: Identity;
  readonly onConfirmed: () => void;
  readonly onSkipped: () => void;
}

export function OnboardingRecovery({ code, slot, userId, identity, onConfirmed, onSkipped }: OnboardingRecoveryProps) {
  const [offer, setOffer] = useState(false);
  if (offer) return <RememberBrowser userId={userId} identity={identity} onDone={onConfirmed} />;
  return (
    <RecoveryCodeStep
      code={code}
      allowSkip={slot}
      onConfirmed={() => (slot ? onConfirmed() : setOffer(true))}
      onSkipped={onSkipped}
    />
  );
}
