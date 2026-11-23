// The org and workgroup whiteboards (06-web.md section 6.1): a level bar with the organization and every workgroup the
// user reads (decrypted names, the default workgroup first), then the board. A board the user cannot read is "not
// found" (existence is not revealed, 03-api.md section 1); the task boards are tabs of the task page.
import { useMemo } from 'react';
import { NavLink, useParams } from 'react-router-dom';
import { WG } from '../../../core/log/state';
import { type BoardLocation, boardPath, canReadBoard, orgBoard, wgBoard } from '../../../core/whiteboards/location';
import { useI18n } from '../../i18n';
import { Board } from '../../whiteboard/Board';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { Unlocking } from '../../unlock/Unlocking';

export function WhiteboardScreen() {
  const { org, wg } = useParams() as { readonly org: string; readonly wg?: string };
  const { t } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const location: BoardLocation = useMemo(() => (wg === undefined ? orgBoard(org) : wgBoard(org, wg)), [org, wg]);
  if (state === null) {
    return (
      <p className="p-4" data-testid="whiteboard.loading">
        {t('whiteboard.loading')}
      </p>
    );
  }
  if (!canReadBoard(state, userId, location)) {
    return (
      <p className="p-4" data-testid="whiteboard.not-found">
        {t('whiteboard.not-found')}
      </p>
    );
  }
  // every member reads the default workgroup; it comes first, the others by ID
  const others = [...state.workgroups.keys()].filter((id) => id !== state.defaultWg && state.canRead(userId!, WG, id));
  const workgroups = [state.defaultWg, ...others.sort()];
  const wgName = (id: string) => directory.workgroups.get(id) ?? t('whiteboard.wg.unnamed');
  const title =
    location.level === 'org' ? t('whiteboard.title.org') : t('whiteboard.title.workgroup', { wg: wgName(wg!) });
  const levels = [
    { id: 'org', to: boardPath(orgBoard(org)), label: t('whiteboard.level.org') },
    ...workgroups.map((id) => ({ id, to: boardPath(wgBoard(org, id)), label: wgName(id) })),
  ];
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="whiteboard.screen">
      <h1 className="text-xl font-semibold" data-testid="whiteboard.title">
        {title}
      </h1>
      <nav aria-label={t('whiteboard.levels')} className="flex gap-1 overflow-x-auto" data-testid="whiteboard.levels">
        {levels.map((level) => (
          <NavLink
            key={level.id}
            to={level.to}
            end
            className="flex min-h-11 shrink-0 items-center rounded px-3 aria-[current=page]:bg-blue-100 aria-[current=page]:font-medium"
            data-testid="whiteboard.level"
            data-level={level.id}
          >
            {level.label}
          </NavLink>
        ))}
      </nav>
      {wg === undefined ? (
        <Board location={location} />
      ) : (
        <Unlocking org={org} wg={wg}>
          <Board location={location} />
        </Unlocking>
      )}
    </main>
  );
}
