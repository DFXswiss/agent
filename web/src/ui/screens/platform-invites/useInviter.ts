// The inviter right of the signed-in user (06-web.md section 7.4), shared by the screen and the header entry; a module
// of its own so the header in the start bundle does not pull the whole screen in (06-web.md section 12.2).
import { isInviter } from '../../../core/platform/invites';
import { useStore } from '../../hooks/useStore';
import { useAppEnv } from '../../shell/context';

/** The verified inviter right of the signed-in user (null while the platform log is not verified). */
export function useInviter(): boolean | null {
  const { app } = useAppEnv();
  const platform = useStore((state) => state.orgs.platform?.state ?? null, app.store);
  const userId = useStore((state) => state.me.userId, app.store);
  return platform === null ? null : isInviter(platform, userId);
}
