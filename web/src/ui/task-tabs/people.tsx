// Names of the people of a shared task (06-web.md sections 5.2-5.4, ADR-083): a person is named by the display name
// of their verified `member.profile` (`core/tasks/directory.ts`), oneself as "you", an unknown person neutrally - never
// by an ID. "Triggered by <name>" marks an operation or a session that followed from that person's instruction
// (`triggered_by`; nothing when the hub sends none or null). The consumption per person (`people` of the billing and
// charges answers) lists each person with the amounts of the other usage rows, "Not attributed" for consumption
// without a person; an older hub sends no `people` and nothing is shown.
import { formatUsd, type PersonConsumption } from '../../core/billing/billing';
import { selectDirectory } from '../../core/tasks/directory';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

export interface People {
  /** The signed-in user. */
  readonly me: string | null;
  /** The display name of a member of the org (a neutral word for an unknown one). */
  readonly name: (id: string) => string;
}

export function usePeople(org: string): People {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const members = useStore(selectDirectory(org), app.store).members;
  const me = useStore((state) => state.me.userId, app.store);
  return { me, name: (id) => members.get(id) ?? t('task.chat.author.unknown') };
}

export function TriggeredBy({
  org,
  user,
  testId,
}: {
  readonly org: string;
  readonly user: string | null | undefined;
  readonly testId: string;
}) {
  const { t } = useI18n();
  const { me, name } = usePeople(org);
  if (typeof user !== 'string') return null;
  return (
    <span className="text-xs text-gray-600" data-testid={testId} data-user={user}>
      {user === me ? t('task.triggered-by.you') : t('task.triggered-by', { name: name(user) })}
    </span>
  );
}

export function PeopleUsage({
  org,
  rows,
  testId,
}: {
  readonly org: string;
  readonly rows: readonly PersonConsumption[];
  readonly testId: string;
}) {
  const { t, locale } = useI18n();
  const { me, name } = usePeople(org);
  const label = (user: string | null) => {
    if (user === null) return t('billing.people.none');
    return user === me ? t('task.chat.author.you') : name(user);
  };
  if (rows.length === 0) return <p data-testid={`${testId}.empty`}>{t('billing.usage.empty')}</p>;
  return (
    <ul data-testid={testId}>
      {rows.map((row) => (
        <li key={row.userId ?? ''} data-testid={`${testId}.item`} data-user={row.userId ?? ''}>
          {t('billing.usage.row', {
            label: label(row.userId),
            total: formatUsd(row.tokensMicroUsd + row.runtimeMicroUsd, locale),
            tokens: formatUsd(row.tokensMicroUsd, locale),
            runtime: formatUsd(row.runtimeMicroUsd, locale),
          })}
        </li>
      ))}
    </ul>
  );
}
