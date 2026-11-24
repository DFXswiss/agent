// Chunk groups of the production build (06-web.md sections 10.3 and 12.2; decisions WEB-13, HOT-16 and HOT-18). Few,
// larger chunks for the first load: the framework and the app modules of the first load (`$initial`: imported
// statically from the entry) each go into one chunk, so a phone on a slow network does not wait for a cascade of small
// requests. Screens, terminal, charts and Markdown are lazy chunks; what only they share stays out of the first load.
//
// The bundler hands the groups absolute module ids. Every rule therefore looks at the path relative to web/: a rule
// on the absolute id (`/src/`) also matched every library when the checkout itself lay below a directory named `src`,
// and the same commit gave other chunks there (HOT-18).
import { isAbsolute, relative, sep } from 'node:path';

export interface ChunkGroup {
  readonly name: string;
  readonly test: (id: string) => boolean;
  readonly priority: number;
  readonly tags?: '$initial'[];
}

const FRAMEWORK = /(?:^|\/)node_modules\/(?:react|react-dom|react-router|react-router-dom|@remix-run|scheduler)\//;
const APP = /^src\//;
/** The lazily loaded parts that keep chunks of their own with their libraries (Markdown, chart, terminal). */
const LAZY_PARTS = /^src\/ui\/(?:task-tabs\/chat\/Markdown\.tsx$|charts\/UPlotChart\.tsx$|terminal\/)/;

/** Path of a module relative to `web` with `/` separators; '' for a virtual module or one outside `web`. */
export function fromWebDir(web: string, id: string): string {
  if (!isAbsolute(id)) return '';
  const path = relative(web, id);
  return path.startsWith('..') || isAbsolute(path) ? '' : path.split(sep).join('/');
}

export function chunkGroups(web: string): ChunkGroup[] {
  const at = (rule: RegExp, except?: RegExp) => (id: string) => {
    const path = fromWebDir(web, id);
    return rule.test(path) && !except?.test(path);
  };
  return [
    { name: 'framework', test: at(FRAMEWORK), priority: 30 },
    { name: 'start', test: at(APP), tags: ['$initial'], priority: 20 },
    // every lazily loaded screen in one chunk (the heavy parts – Markdown, charts, terminal – stay apart)
    { name: 'screens', test: at(APP, LAZY_PARTS), priority: 10 },
  ];
}

/** The group with the highest priority whose rule matches `id` (tags aside); for tests. */
export function groupOf(groups: readonly ChunkGroup[], id: string): string | undefined {
  return [...groups].sort((a, b) => b.priority - a.priority).find((group) => group.test(id))?.name;
}
