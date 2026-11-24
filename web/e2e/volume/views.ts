// Opening and measuring one view of the volume org: a full page load with the passkey unlock, then `measure`.
import type { Locator, Page, TestInfo } from '@playwright/test';
import { type Measurement, measure } from './budget.ts';
import type { VolumeUser } from './fixtures.ts';
import { knownGap } from './gaps.ts';

export interface ViewSpec {
  /** The measured id (the first of the spec's `COVERS`). */
  readonly id: string;
  /** Path of the view (built from the data set). */
  readonly path: string;
  /** The primary control and what shows that the data is shown. */
  readonly primary: (page: Page) => Locator;
  readonly ready?: (page: Page) => Locator;
  readonly scroll?: (page: Page) => Locator | null;
  readonly also?: readonly string[];
  readonly live?: boolean;
  /** A page without sign-in (static): no passkey unlock. */
  readonly anonymous?: boolean;
}

/** Opens the view with a fresh page load and measures it (a known gap of the project is marked first). */
export async function openView(volume: VolumeUser, info: TestInfo, view: ViewSpec): Promise<Measurement> {
  knownGap(info, view.id);
  const since = await volume.open(view.path, !view.anonymous);
  const page = volume.page;
  return measure(page, info, view.id, {
    primary: view.primary(page),
    since,
    ...(view.ready && { ready: view.ready(page) }),
    ...(view.scroll && { scroll: view.scroll(page) }),
    ...(view.also && { also: view.also }),
    ...(view.live && { live: true }),
  });
}

/** The same within the open page after a user action at `since` (a tab, a drawer, a section). */
export async function measureHere(
  volume: VolumeUser,
  info: TestInfo,
  since: number,
  view: Omit<ViewSpec, 'path'>,
): Promise<Measurement> {
  const page = volume.page;
  return measure(page, info, view.id, {
    primary: view.primary(page),
    since,
    ...(view.ready && { ready: view.ready(page) }),
    ...(view.scroll && { scroll: view.scroll(page) }),
    ...(view.also && { also: view.also }),
    ...(view.live && { live: true }),
  });
}
