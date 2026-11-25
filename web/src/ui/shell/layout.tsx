// The app shell (registration point `src/ui/shell/layout.tsx`, export `Layout`, see `ui/router.tsx`): skip link,
// header, navigation, the banners (connection, main orchestrator on another computer, session renewal, recovery
// reminder), the screen and the footer.
// Responsive layout of 06-web.md section 12.1: below `md` a bottom navigation (hidden while the on-screen keyboard is
// open; the page is then padded by the keyboard's height so a composer can stay above it), from `md` a navigation row,
// from `lg` a left sidebar. Routes that need an unlocked identity send a locked or anonymous browser to
// `/login?next=…`; inside `/o/:org` the org becomes the active one (its live channel runs), outside an org path the
// landing org does when none is active yet (`AppSession.landingOrg`, KOR-68); the user's own name is
// written into it once its log is verified if the user has none there yet (ADR-073, `ensureOwnNameOnce`), and an org
// in the blocking error state shows that state instead of the screen.
import { useEffect } from 'react';
import { matchPath, Navigate, Outlet, useLocation } from 'react-router-dom';
import { watchSessionExpiry } from '../../core/account/expiry';
import { ensureOwnNameOnce } from '../../core/account/own-name';
import { meOrgs } from '../../core/account/session';
import { orgEntry } from '../../core/store/slices/orgs';
import { setOnline } from '../../core/store/slices/connection';
import { CONTENT_ID, SkipLink } from '../a11y/SkipLink';
import { useStore } from '../hooks/useStore';
import { BottomNav } from '../responsive/BottomNav';
import { Footer } from '../responsive/Footer';
import { useKeyboardInset } from '../responsive/keyboard';
import { BlockedOrg } from './BlockedOrg';
import { ConnectionBanner } from './ConnectionBanner';
import { useAppEnv } from './context';
import { loginTarget, needsUnlock } from './guard';
import { Header } from './Header';
import { Nav } from './Nav';
import { RemoteAgentBanner } from './RemoteAgentBanner';
import { RenewalNotice } from './RenewalNotice';

export function Layout() {
  const { app } = useAppEnv();
  const location = useLocation();
  const keyboard = useKeyboardInset();
  const phase = useStore((state) => state.session.phase, app.store);
  const org = matchPath('/o/:org/*', location.pathname)?.params.org ?? null;
  const block = useStore((state) => (org === null ? null : orgEntry(state.orgs, org).blocked), app.store);
  const unlocked = phase === 'unlocked';
  const member = useStore(
    (state) =>
      org !== null && state.me.userId !== null && orgEntry(state.orgs, org).log?.state.member(state.me.userId) != null,
    app.store,
  );
  const profile = useStore((state) => state.me.profile, app.store);
  useEffect(() => {
    if (!unlocked) return;
    if (org !== null) {
      app.activate(org);
      return;
    }
    // a page outside an org (settings, community, …) opened directly: the landing org becomes the active one, so
    // the sections of the active org have one without a further step (KOR-68)
    const landing = app.store.getState().session.activeOrg === null ? app.landingOrg() : null;
    if (landing !== null) app.activate(landing);
  }, [app, org, unlocked, profile]);
  useEffect(() => {
    if (org === null || !unlocked || !member) return;
    const orgs = meOrgs(app.store.getState().me.profile).map((item) => item.org_id);
    void ensureOwnNameOnce(app.tasks, org, orgs);
  }, [app, org, unlocked, member]);
  useEffect(() => watchSessionExpiry(app.store), [app]);
  useEffect(() => {
    const update = () => setOnline(app.store, navigator.onLine);
    update();
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    return () => {
      window.removeEventListener('online', update);
      window.removeEventListener('offline', update);
    };
  }, [app]);
  if (!unlocked && needsUnlock(location.pathname)) {
    return <Navigate to={loginTarget(location.pathname, location.search)} replace />;
  }
  const docked = unlocked && keyboard === 0;
  return (
    <div
      className={`flex min-h-screen flex-col ${docked ? 'pb-14 md:pb-0' : ''}`}
      style={keyboard > 0 ? { paddingBottom: keyboard } : undefined}
      data-testid="shell.layout"
      data-keyboard={keyboard}
    >
      <SkipLink />
      <Header />
      {unlocked && <ConnectionBanner />}
      {unlocked && <RemoteAgentBanner />}
      <RenewalNotice />
      <div className="flex flex-1 flex-col lg:flex-row">
        <Nav />
        <div id={CONTENT_ID} tabIndex={-1} className="min-w-0 flex-1 focus:outline-none">
          {block === null ? <Outlet /> : <BlockedOrg block={block} />}
        </div>
      </div>
      <Footer />
      {docked && <BottomNav />}
    </div>
  );
}
