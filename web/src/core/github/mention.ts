// The composer's GitHub hint (06-web.md section 4.1, KOR-25 part three): a deterministic check whether a prompt
// obviously refers to GitHub, so that the composer offers "Connect GitHub" before the task starts while no connection
// exists (the task is sent either way). It looks for
//
// - a GitHub address (`github.com`, any sub-domain such as `gist.github.com`);
// - a repository `owner/name` as a word of its own (03-api.md section 9; owner and name each with a letter, not a
//   path with more slashes, not a URL, not a common pair such as "and/or");
// - the words "GitHub", "Issue(s)", "Pull Request(s)" (also hyphenated or written together), "PR(s)" (upper case),
//   "Repo(s)", "Repository", "Repositories", "Repositorys" – German and English use the same words.
//
// No model and no network: the same text always gives the same answer.

const ADDRESS_RE = /(^|[^A-Za-z0-9.-])([a-z0-9-]+\.)*github\.com\b/i;
const WORD_RE =
  /(^|[^\p{L}\p{N}_])(github|issues?|pull[ -]?requests?|repos?|repository|repositories|repositorys)(?=$|[^\p{L}\p{N}_])/iu;
const PR_RE = /(^|[^\p{L}\p{N}_])PRs?(?=$|[^\p{L}\p{N}_])/u;
/** `owner/name` delimited by white space, brackets, quotes or a final punctuation mark. */
const REPO_RE =
  /(?<=^|[\s(["'`„“‚‘<])([A-Za-z0-9-]{1,39})\/([A-Za-z0-9._-]{1,100}?)(?=[.,;:!?]*(?:$|[\s)\]"'`“”‘’>]))/gu;
const LETTER_RE = /[A-Za-z]/;
/** Pairs that look like `owner/name` but are words (lower case). */
const COMMON_PAIRS: ReadonlySet<string> = new Set([
  'and/or',
  'und/oder',
  'or/and',
  'oder/und',
  'yes/no',
  'ja/nein',
  'on/off',
  'an/aus',
  'in/out',
  'input/output',
  'read/write',
  'true/false',
  'client/server',
  'he/she',
  'er/sie',
  'km/h',
  'n/a',
  'w/o',
]);

/** Whether `text` names a repository `owner/name` (module comment). */
function namesRepository(text: string): boolean {
  for (const match of text.matchAll(REPO_RE)) {
    const owner = match[1]!;
    const name = match[2]!;
    if (!LETTER_RE.test(owner) || !LETTER_RE.test(name)) continue;
    if (COMMON_PAIRS.has(`${owner}/${name}`.toLowerCase())) continue;
    return true;
  }
  return false;
}

/** Whether the prompt obviously refers to GitHub (module comment). */
export function mentionsGithub(text: string): boolean {
  return ADDRESS_RE.test(text) || WORD_RE.test(text) || PR_RE.test(text) || namesRepository(text);
}
