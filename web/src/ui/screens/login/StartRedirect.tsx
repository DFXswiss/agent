// `/`: unauthenticated → `/login`; unlocked → the last org used on this browser (`/o/:org`), or the settings when the
// user has no org yet.
import { Navigate } from 'react-router-dom';
import { useStore } from '../../hooks/useStore';
import { useAppEnv } from '../../shell/context';

export function StartRedirect() {
  const { app } = useAppEnv();
  const unlocked = useStore((state) => state.session.phase === 'unlocked', app.store);
  if (!unlocked) return <Navigate to="/login" replace />;
  const org = app.landingOrg();
  return <Navigate to={org === null ? '/settings' : `/o/${org}`} replace />;
}
