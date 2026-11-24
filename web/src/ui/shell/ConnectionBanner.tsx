// Connection banner (06-web.md section 12.5): Live, Reconnecting… (n s) with the live countdown to the next attempt,
// Offline (`navigator.onLine` false or the channel gave up the network). A polite live region announces each change
// of the state (section 12.3) – not the seconds of the countdown, which would be read out every second.
import { useEffect, useState } from 'react';
import { banner } from '../../core/store/slices/connection';
import { useStore } from '../hooks/useStore';
import { LiveRegion } from '../a11y/LiveRegion';
import { useI18n } from '../i18n';
import { useAppEnv } from './context';

const STYLE = {
  live: 'bg-green-50 text-green-900',
  reconnecting: 'bg-amber-50 text-amber-900',
  connecting: 'bg-gray-50 text-gray-800',
  offline: 'bg-red-50 text-red-900',
} as const;

export function ConnectionBanner({ now = Date.now }: { readonly now?: () => number }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const connection = useStore((state) => state.connection, app.store);
  const [, tick] = useState(0);
  const state = banner(connection, now());
  const counting = state.kind === 'reconnecting';
  useEffect(() => {
    if (!counting) return;
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [counting]);
  if (state.kind === 'none') return null;
  const text =
    state.kind === 'reconnecting' ? t('shell.reconnecting', { seconds: state.seconds }) : t(`shell.${state.kind}`);
  return (
    <>
      <p className={`px-4 py-1 text-sm ${STYLE[state.kind]}`} data-testid={`shell.connection.${state.kind}`}>
        {text}
      </p>
      <LiveRegion text={t(`shell.${state.kind}.announce`)} testId="shell.connection.announce" />
    </>
  );
}
