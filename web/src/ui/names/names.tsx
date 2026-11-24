// How the interface names people and things (ADR-067, rule E-19 of the concept): a name when one is known, else a
// sentence a layperson understands – never a fragment of an ID as the main line. The ID stays available (`title`,
// `data-id`, the member list's "Show ID") to compare it, e.g. with an account code read out on the phone. A person's
// name may be provisional (given by an admin or taken from the invitation) until the person's own name is written; it
// is marked as such wherever a person is named in running text. `tests/core/names-class.test.ts` keeps the class out.
import type { ReactNode } from 'react';
import type { Directory } from '../../core/tasks/directory';

type T = (key: string, params?: Record<string, string>) => string;

/** A person's name as text: own name, "<name> (provisional)", or "Name not known yet". */
export function personName(t: T, directory: Directory, id: string): string {
  const name = directory.members.get(id);
  if (name === undefined) return t('names.person.unknown');
  return directory.sources.get(id)?.source === 'own' ? name : t('names.person.provisional', { name });
}

/** `personName` of a person with a known name, undefined without one (the caller has its own word for "someone"). */
export function knownPersonName(t: T, directory: Directory, id: string | null): string | undefined {
  return id !== null && directory.members.has(id) ? personName(t, directory, id) : undefined;
}

/** Who gave a provisional name (their shown name), or "an admin". */
export function givenBy(t: T, directory: Directory, id: string): string | null {
  const source = directory.sources.get(id);
  if (source === undefined || source.source === 'own') return null;
  const by = source.by === null ? undefined : directory.members.get(source.by);
  const key = source.source === 'admin' ? 'names.person.given' : 'names.person.invited';
  return t(key, { by: by ?? t('names.person.someone') });
}

/**
 * A thing named by a sentence (or by its name): the text is the main line, the ID only on request (tooltip,
 * `data-id`), or – `showId`, in a list where several unnamed things must be told apart – as a small second line.
 */
export function NoName(props: {
  readonly t: T;
  readonly text: string;
  readonly id: string;
  readonly showId?: boolean;
  readonly testId?: string;
}): ReactNode {
  const { t, text, id, showId = false, testId } = props;
  return (
    <span className="inline-flex flex-col" title={t('names.id.value', { id })} data-id={id} data-testid={testId}>
      <span>{text}</span>
      {showId && <span className="break-all text-xs text-gray-600">{t('names.id.value', { id })}</span>}
    </span>
  );
}
