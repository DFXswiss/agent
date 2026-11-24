// Before the Playwright suite: fresh browser coverage cache, the build into src/agent_hub/static/ (the build under
// test is the one the hub serves), the probe page build, the real hub and the probe server, and the community keeper
// (`helpers/community.ts`: the first community member, which hands the key to every new user of the suite).
import { CoverageReport } from 'monocart-coverage-reports';
import { build } from 'vite';
import { buildWeb } from '../../scripts/build.mjs';
import { e2eCoverage } from './coverage-options.ts';
import { compiledRootKey } from '../../build/manifest.ts';
import { startKeeper } from '../helpers/community.ts';
import {
  DIRECTOR_PORT,
  ORIGIN,
  hubPython,
  PORT,
  PROBE_DIR,
  PROBE_ORIGIN,
  PROBE_PORT,
  REPO_DIR,
  STATIC_DIR,
  WEB_DIR,
} from './env.ts';
import { startHub, startHubTestServer } from './server.ts';

export default async function globalSetup() {
  process.chdir(WEB_DIR);
  new CoverageReport(e2eCoverage).cleanCache();
  await buildWeb({ outDir: STATIC_DIR, minify: false });
  await build({ configFile: `${WEB_DIR}e2e/probe/vite.config.ts`, logLevel: 'warn' });
  const rootKey = compiledRootKey();
  const hub = await startHub({
    python: hubPython(),
    script: `${REPO_DIR}scripts/hub_test_server.py`,
    port: PORT,
    rootKey,
    directorPort: DIRECTOR_PORT,
  });
  try {
    const probe = await startHubTestServer({
      port: PROBE_PORT,
      origin: PROBE_ORIGIN,
      staticDir: STATIC_DIR,
      probeDir: PROBE_DIR,
      rootKey,
    });
    const keeper = await startKeeper(ORIGIN).catch(async (error: unknown) => {
      await probe.close();
      throw error;
    });
    return async () => {
      await keeper.stop();
      await probe.close();
      await hub.stop();
    };
  } catch (error) {
    await hub.stop();
    throw error;
  }
}
