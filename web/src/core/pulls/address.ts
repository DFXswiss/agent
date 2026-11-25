// Pull-request addresses (KOR-48, ADR-084 section 1): what a person pastes to link a pull request to a task –
// `https://github.com/<owner>/<repo>/pull/<n>` (also with `www.`, a sub-page such as `/files`, a query or a fragment)
// or the short form `<owner>/<repo>#<n>` – and the forms derived from a pull request: its key (the repository in
// lower case, as the tag is computed, ADR-072 section 8) and its address on GitHub (https only).
/** A pull request: the repository as GitHub spells it and the number. */
export interface PullRef {
  readonly repo: string;
  readonly number: number;
}

const OWNER = '[A-Za-z0-9-]{1,39}';
const NAME = '[A-Za-z0-9._-]{1,100}';
const NUMBER = '([1-9][0-9]{0,9})';
const URL_FORM = new RegExp(`^https://(?:www\\.)?github\\.com/(${OWNER})/(${NAME})/pull/${NUMBER}(?:[/?#]\\S*)?$`, 'i');
const SHORT_FORM = new RegExp(`^(${OWNER})/(${NAME})#${NUMBER}$`);
/** The largest number a tag accepts (`requireInt` of the link text: a safe positive integer). */
export const NUMBER_MAX = 2 ** 31 - 1;

/**
 * The pull request of a pasted address, or null when the text is none of the two forms (module comment); the parts
 * follow `owner/name` of 03-api.md section 9 (`PULL_REPO_RE` of `checks.ts`).
 */
export function parsePullAddress(text: string): PullRef | null {
  const trimmed = text.trim();
  const match = URL_FORM.exec(trimmed) ?? SHORT_FORM.exec(trimmed);
  if (match === null) return null;
  const repo = `${match[1]}/${match[2]}`;
  const number = Number(match[3]);
  if (number > NUMBER_MAX || match[2] === '.' || match[2] === '..') return null;
  return { repo, number };
}

/** The key of a pull request: the repository in lower case and the number (`owner/name#n`). */
export const pullKey = (ref: PullRef): string => `${ref.repo.toLowerCase()}#${ref.number}`;

/** The same pull request (the repository compared in lower case). */
export const samePull = (a: PullRef, b: PullRef): boolean => pullKey(a) === pullKey(b);

/** The short form `owner/name#n` as GitHub spells the repository. */
export const pullLabel = (ref: PullRef): string => `${ref.repo}#${ref.number}`;

/** The pull request's page on GitHub (the device's `url` is preferred when it was read). */
export const pullUrl = (ref: PullRef): string => `https://github.com/${ref.repo}/pull/${ref.number}`;
