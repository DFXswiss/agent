// The role of a session (ADR-093 section 7): the name of the rights profile it works with (`profiles.sessionRole`),
// a built-in role in the catalog's words, an own profile by its name; nothing for a session without a profile.
import { ROLES, sessionRole } from '../../core/profiles/profiles';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

export function RoleBadge({ session, task }: { readonly session: string; readonly task: string }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const role = useStore(
    (state) => sessionRole(state.tasks.sessions, session, state.tasks.tasks[task]?.agent_profile),
    app.store,
  );
  if (role === null) return null;
  return (
    <span className="rounded bg-indigo-50 px-2 text-xs text-indigo-900" data-testid="session.role" data-role={role}>
      {(ROLES as readonly string[]).includes(role) ? t(`profiles.role.${role}`) : role}
    </span>
  );
}
