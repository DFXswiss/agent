// Before the Playwright suite: fresh browser coverage cache, the build into src/agent_hub/static/ (the build under
// test is the one the hub serves), the probe page build, the real hub and the probe server, and the community keeper
// (`helpers/community.ts`: the first community member, which hands the key to every new user of the suite). Every
// further lane of the run (`env.ts`, decisions/HOT-21.md) gets a hub and a keeper of its own, started side by side;
// the lanes of the volume gate get their data set from the cache instead of a keeper.
import { CoverageReport } from 'monocart-coverage-reports';
import { build } from 'vite';
import { buildWeb } from '../../scripts/build.mjs';
import { e2eCoverage } from './coverage-options.ts';
import { compiledRootKey } from '../../build/manifest.ts';
import { startKeeper } from '../helpers/community.ts';
import {
  hubPython,
  laneDirectorPort,
  laneOrigin,
  lanePort,
  lanesOf,
  PROBE_DIR,
  PROBE_ORIGIN,
  PROBE_PORT,
  REPO_DIR,
  STATIC_DIR,
  VOLUME_LANES,
  volumeCache,
  WEB_DIR,
} from './env.ts';
import { startHub, startHubTestServer } from './server.ts';

type Stop = () => Promise<void>;

/** Stops in reverse order of the starts (every stop is tried). */
async function stopAll(stops: Stop[]): Promise<void> {
  for (const stop of stops.reverse()) await stop().catch(() => undefined);
}

/**
 * The hub (with device double) and the community keeper of one lane; `stops` collects how to end them. A lane of the
 * volume gate has no keeper (the volume org's founder holds the community key) and takes its data set from the cache.
 */
async function startLane(lane: string, rootKey: string, stops: Stop[]): Promise<void> {
  const volume = VOLUME_LANES.includes(lane);
  const hub = await startHub({
    python: hubPython(),
    script: `${REPO_DIR}scripts/hub_test_server.py`,
    port: lanePort(lane),
    rootKey,
    directorPort: laneDirectorPort(lane),
    ...(volume && { volumeCache: volumeCache() }),
  });
  stops.push(() => hub.stop());
  if (volume) return;
  const keeper = await startKeeper(laneOrigin(lane));
  stops.push(() => keeper.stop());
}

export default async function globalSetup() {
  process.chdir(WEB_DIR);
  new CoverageReport(e2eCoverage).cleanCache();
  await buildWeb({ outDir: STATIC_DIR, minify: false });
  await build({ configFile: `${WEB_DIR}e2e/probe/vite.config.ts`, logLevel: 'warn' });
  const rootKey = compiledRootKey();
  const stops: Stop[] = [];
  const probe = startHubTestServer({
    port: PROBE_PORT,
    origin: PROBE_ORIGIN,
    staticDir: STATIC_DIR,
    probeDir: PROBE_DIR,
    rootKey,
  }).then((server) => {
    stops.push(() => server.close());
  });
  const lanes = lanesOf(process.argv).map((lane) => startLane(lane, rootKey, stops));
  // every start has ended before anything is stopped (a lane still starting would outlive the stop)
  const failed = (await Promise.allSettled([probe, ...lanes])).find((result) => result.status === 'rejected');
  if (failed) {
    await stopAll(stops);
    throw failed.reason;
  }
  return () => stopAll(stops);
}
