// The app header: brand (link to `/`), the discovered header items (`shell/header/<area>.tsx`, e.g. org switcher,
// notification bell) and the settings link while unlocked, else the sign-in link. Mobile first: one row that wraps.
import { Link } from 'react-router-dom';
import { headerItems, type HeaderItem } from '../registries/header-items';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from './context';

export function Header({ items = headerItems() }: { readonly items?: readonly HeaderItem[] }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const phase = useStore((state) => state.session.phase, app.store);
  const unlocked = phase === 'unlocked';
  return (
    <header className="flex flex-wrap items-center gap-3 border-b border-gray-200 px-4 py-2" data-testid="shell.header">
      <Link className="mr-auto flex min-h-11 items-center font-semibold" to="/" data-testid="shell.brand">
        {t('shell.brand')}
      </Link>
      {unlocked && items.map(({ id, Component }) => <Component key={id} />)}
      {unlocked ? (
        <Link className="flex min-h-11 items-center underline" to="/settings" data-testid="shell.settings">
          {t('shell.settings')}
        </Link>
      ) : (
        <Link className="flex min-h-11 items-center underline" to="/login" data-testid="shell.signin">
          {t('shell.signin')}
        </Link>
      )}
    </header>
  );
}
