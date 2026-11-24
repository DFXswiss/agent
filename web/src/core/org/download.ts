// The runner package of an own Mac in the hub's download area (ADR-074 sections 2 and 7; 06-web.md section 7.3).
// The devices page offers it next to a device code only for a release whose `latest.json` says the package is
// notarised and has not expired: for a downloaded package Apple's Gatekeeper is the check of its origin, and an
// unsigned package would need System Settings → "Open Anyway". The page never signs or changes anything; the agent
// way (`agent-platform device add`) verifies the release itself.

export interface MacDownload {
  readonly href: string;
  readonly version: string;
}

export const FEED_PATH = '/downloads/runner/latest.json';
const PACKAGE = /^DFX-Agent-Runner-(\d{4}\.\d{2}\.\d{2}\.\d{4})\.pkg$/;

/** The download of a feed document, or null when the feed offers none (unsigned, expired, malformed). */
export function macDownload(feed: unknown, now: number): MacDownload | null {
  if (typeof feed !== 'object' || feed === null) return null;
  const { package: name, version, notarized, expires } = feed as Record<string, unknown>;
  if (notarized !== true || typeof name !== 'string' || typeof expires !== 'string') return null;
  const found = PACKAGE.exec(name)?.[1];
  if (found === undefined || found !== version || !(Date.parse(expires) > now)) return null;
  return { href: `/downloads/runner/${name}`, version: found };
}

/** Reads the feed of the hub that serves this page; null on any failure (the page then offers no download). */
export async function loadMacDownload(fetcher: typeof fetch, now: number): Promise<MacDownload | null> {
  try {
    const answer = await fetcher(FEED_PATH, { cache: 'no-cache', credentials: 'omit' });
    return answer.ok ? macDownload(await answer.json(), now) : null;
  } catch {
    return null;
  }
}
