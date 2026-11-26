// How the interface names DFX support (ADR-096 section 5): a support person is "Support <Name>" in a conversation and
// "Support <Name> #XXXX" in the audit, the name the operator set next to the ID mark of the user; who is support comes
// from the verified platform log (`core/support/names.ts`). The support area itself names the members of a released
// workgroup from its `support.info` (`AuthorNames`): it does not hold the organization's profiles.
import { createContext, useContext, useEffect } from 'react';
import { ensureMarks, type SupportName, supportName } from '../../core/support/names';
import { loadOrgSupport } from '../../core/support/people';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

type T = (key: string, params?: Record<string, string>) => string;

/** "Support <Name> #XXXX" (`full`) or "Support <Name>", with the mark where the name is not known. */
export function supportText(t: T, name: SupportName, full = false): string {
  if (name.name !== null && name.mark !== null && full)
    return t('names.support.full', { name: name.name, mark: name.mark });
  if (name.name !== null) return t('names.support.named', { name: name.name });
  return name.mark === null ? t('names.support.plain') : t('names.support.marked', { mark: name.mark });
}

/** The support label of `id` in `org`, null when `id` was never a support person; loads names and marks once. */
export function useSupportLabel(org: string, id: string | null, full = false): string | null {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const label = useStore((state) => {
    const name = id === null ? null : supportName(state, org, id);
    return name === null ? null : supportText(t, name, full);
  }, app.store);
  const known = label !== null;
  useEffect(() => {
    if (!known) return;
    void ensureMarks(app.store, [id!]);
    if (app.store.getState().support.orgs[org] === undefined) void loadOrgSupport(app.tasks, org);
  }, [app, org, id, known]);
  return label;
}

/** A naming of authors that replaces the organization's directory (the support area: names from `support.info`). */
export const AuthorNames = createContext<((id: string) => string | null) | null>(null);

/** The name `AuthorNames` gives `id`, undefined outside a provider or for an author it does not know. */
export function useAuthorOverride(id: string | null): string | undefined {
  const names = useContext(AuthorNames);
  if (names === null || id === null) return undefined;
  return names(id) ?? undefined;
}
