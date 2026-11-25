// The "Pull requests" view of a workgroup (KOR-48, ADR-084 sections 1-3, 06-web.md section 5.6): a level bar with every
// workgroup the user reads (decrypted names, the default workgroup first), the workgroup's linked pull requests newest
// first (`PullRow`: state, conflict state, link to GitHub, the task, "Fix conflicts" for writers), "Load more" while
// the hub has further pages, and for users who may write in the workgroup "Paste a pull-request address" with the
// choice of the task. The states are read by one device request for the rows on the screen. A workgroup the user
// cannot read is "not found" (existence is not revealed, 03-api.md section 1).
import { Link, useParams } from 'react-router-dom';
import { WG } from '../../../core/log/state';
import { levelCurrent } from '../../../core/pulls/view';
import { useI18n } from '../../i18n';
import { LinkForm } from '../../pulls/LinkForm';
import { LinkList } from '../../pulls/LinkList';
import { Unlocking } from '../../unlock/Unlocking';
import { useDirectory, useOrgView } from '../tasks/hooks';

export function PullsScreen() {
  const { org, wg: param } = useParams() as { readonly org: string; readonly wg?: string };
  const { t } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  if (state === null || userId === null) {
    return (
      <p className="p-4" data-testid="pulls.screen.loading">
        {t('pulls.screen.loading')}
      </p>
    );
  }
  const wg = param ?? state.defaultWg;
  if (!state.canRead(userId, WG, wg)) {
    return (
      <p className="p-4" data-testid="pulls.not-found">
        {t('pulls.not-found')}
      </p>
    );
  }
  // every member reads the default workgroup; it comes first, the others by ID
  const others = state
    .readableScopes(userId)
    .filter(([scope, id]) => scope === WG && id !== state.defaultWg)
    .map(([, id]) => id);
  const workgroups = [state.defaultWg, ...others.sort()];
  const wgName = (id: string) => directory.workgroups.get(id) ?? t('pulls.wg.unnamed');
  const writer = state.canWriteWg(userId, wg);
  const base = `/o/${encodeURIComponent(org)}`;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="pulls.screen" data-wg={wg}>
      <h1 className="text-xl font-semibold" data-testid="pulls.title">
        {t('pulls.title', { wg: wgName(wg) })}
      </h1>
      <p className="text-sm text-gray-700">{t('pulls.intro')}</p>
      {workgroups.length > 1 && (
        <nav aria-label={t('pulls.levels')} className="flex gap-1 overflow-x-auto" data-testid="pulls.levels">
          {workgroups.map((id) => (
            <Link
              key={id}
              to={`${base}/w/${encodeURIComponent(id)}/pulls`}
              aria-current={levelCurrent(id, wg)}
              className="flex min-h-11 shrink-0 items-center rounded px-3 aria-[current=page]:bg-blue-100 aria-[current=page]:font-medium"
              data-testid="pulls.level"
              data-wg={id}
            >
              {wgName(id)}
            </Link>
          ))}
        </nav>
      )}
      <Unlocking org={org} wg={wg}>
        <div className="flex flex-col gap-4">
          {writer && <LinkForm org={org} wg={wg} />}
          <LinkList org={org} wg={wg} task={null} writer={writer} />
        </div>
      </Unlocking>
    </main>
  );
}
