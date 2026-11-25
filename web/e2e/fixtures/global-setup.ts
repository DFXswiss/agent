// Before the Playwright suite: fresh browser coverage cache, the build into src/agent_hub/static/ (the build under
// test is the one the hub serves), the probe page build, the real hub and the probe server, and the community keeper
// (`helpers/community.ts`: the first community member, which hands the key to every new user of the suite). Every
// further lane of the run (`env.ts`, decisions/HOT-21.md) gets a hub and a keeper of its own, started side by side;
// the lanes of the volume gate get their data set from the cache instead of a keeper.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { CoverageReport } from 'monocart-coverage-reports';
import { build } from 'vite';
import { buildWeb } from '../../scripts/build.mjs';
import { e2eCoverage } from './coverage-options.ts';
import { compiledRootKey } from '../../build/manifest.ts';
import { startKeeper } from '../helpers/community.ts';
import { laneError } from '../volume/completeness.ts';
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
 * volume gate has no keeper (the volume org's founder holds the community key) and takes its data set from the cache;
 * it may wait there for the other volume lane's seed, so the run does not wait for it (`startVolumeLane`).
 */
async function startLane(lane: string, rootKey: string, stops: Stop[]): Promise<void> {
  const volume = VOLUME_LANES.includes(lane);
  // a data set shrunk by VOLUME_COUNTS is seeded on request and never cached (such a run always ends red)
  const shrunk = Object.keys(JSON.parse(process.env.VOLUME_COUNTS ?? '{}') as object).length > 0;
  await startHub({
    python: hubPython(),
    script: `${REPO_DIR}scripts/hub_test_server.py`,
    port: lanePort(lane),
    rootKey,
    directorPort: laneDirectorPort(lane),
    ...(volume && !shrunk && { volumeCache: volumeCache(), readyTimeoutMs: VOLUME_READY_MS }),
    onSpawn: (stop) => stops.push(stop),
  });
  if (volume) return;
  const keeper = await startKeeper(laneOrigin(lane));
  stops.push(() => keeper.stop());
}

/** How long a volume lane may take to answer: waiting for the other lane's seed and restoring its entry. */
const VOLUME_READY_MS = 25 * 60_000;

/** Starts a volume lane in the background: its worker waits for it (`volume/data.ts`), a failure is written to
 *  `laneError` for that worker to report. */
function startVolumeLane(lane: string, rootKey: string, stops: Stop[]): Promise<void> {
  return startLane(lane, rootKey, stops).catch((error: unknown) => {
    const file = laneError(WEB_DIR, lane);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, error instanceof Error ? error.message : String(error));
  });
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
  const selected = lanesOf(process.argv);
  const background = selected
    .filter((lane) => VOLUME_LANES.includes(lane))
    .map((lane) => startVolumeLane(lane, rootKey, stops));
  const lanes = selected.filter((lane) => !VOLUME_LANES.includes(lane)).map((lane) => startLane(lane, rootKey, stops));
  const finish = async () => {
    // a lane still starting is ended through the stop it registered when it was spawned
    await stopAll(stops);
    await Promise.all(background);
  };
  const failed = (await Promise.allSettled([probe, ...lanes])).find((result) => result.status === 'rejected');
  if (failed) {
    await finish();
    throw failed.reason;
  }
  return finish;
}
