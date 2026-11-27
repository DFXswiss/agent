// The fields of a captured task as a form (E-41, ADR-102 section 7), shared by "Capture task" and the task page's
// "Edit": title (required), description, person (members and agent accounts), topic (choose one, or create one inline:
// a `topic.profile` of the org), GitHub organisation and repository (suggestions, free input allowed; a repository
// `owner/name` fills an empty organisation with its owner) and urgency (preset normal).
import { useId, useState } from 'react';
import { sendErrorCode } from '../../../core/tasks/send';
import { createTopic, type Topic, type Urgency, URGENCIES } from '../../../core/tasks/tickets';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useGithubSuggestions, usePeople, useTopics } from './tickets';

/** What the form edits. */
export interface TicketValues {
  readonly title: string;
  readonly description: string;
  /** A user or agent ID, "" for nobody. */
  readonly assignee: string;
  /** A topic ID, "" for none. */
  readonly topic: string;
  readonly githubOrg: string;
  readonly githubRepo: string;
  readonly urgency: Urgency;
}

export const EMPTY_VALUES: TicketValues = {
  title: '',
  description: '',
  assignee: '',
  topic: '',
  githubOrg: '',
  githubRepo: '',
  urgency: 'normal',
};

export const INPUT = 'min-h-11 w-full rounded border border-gray-300 px-2';
const NEW_TOPIC = '#new';

/** The fields (module comment); `prefix` makes the test IDs (`<prefix>.title`, …). */
export function TicketForm({
  org,
  wgs,
  values,
  onChange,
  disabled,
  prefix,
}: {
  readonly org: string;
  /** The workgroups whose repository lists are suggested. */
  readonly wgs: readonly string[];
  readonly values: TicketValues;
  readonly onChange: (values: TicketValues) => void;
  readonly disabled: boolean;
  readonly prefix: string;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const id = useId();
  const people = usePeople(org);
  const known = useTopics(org);
  const suggestions = useGithubSuggestions(org, wgs);
  const [creating, setCreating] = useState(false);
  const [topicName, setTopicName] = useState('');
  const [made, setMade] = useState<Topic | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  // a topic created here is a choice before its sealed event is read back
  const topics = made === null || known.some((topic) => topic.id === made.id) ? known : [...known, made];
  const set = (patch: Partial<TicketValues>) => onChange({ ...values, ...patch });
  const create = async () => {
    setBusy(true);
    setCode(null);
    try {
      const name = topicName.trim();
      const topic = await createTopic(app.tasks, org, name);
      setMade({ id: topic, name });
      set({ topic });
      setCreating(false);
      setTopicName('');
    } catch (failure) {
      setCode(sendErrorCode(failure));
    } finally {
      setBusy(false);
    }
  };
  const repoChanged = (repo: string) => {
    const owner = repo.includes('/') ? repo.split('/')[0]!.trim() : '';
    set({ githubRepo: repo, ...(values.githubOrg.trim() === '' && owner !== '' && { githubOrg: owner }) });
  };
  const off = disabled || busy;
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div className="flex flex-col gap-1 sm:col-span-2">
        <label htmlFor={`${id}-title`} className="text-sm font-medium">
          {t('tickets.field.title')}
        </label>
        <input
          id={`${id}-title`}
          className={INPUT}
          value={values.title}
          maxLength={120}
          required
          disabled={off}
          onChange={(event) => set({ title: event.target.value })}
          data-testid={`${prefix}.title`}
        />
      </div>
      <div className="flex flex-col gap-1 sm:col-span-2">
        <label htmlFor={`${id}-description`} className="text-sm font-medium">
          {t('tickets.field.description')}
        </label>
        <textarea
          id={`${id}-description`}
          className="min-h-24 w-full rounded border border-gray-300 p-2"
          value={values.description}
          maxLength={4096}
          disabled={off}
          onChange={(event) => set({ description: event.target.value })}
          data-testid={`${prefix}.description`}
        />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-person`} className="text-sm font-medium">
          {t('tickets.field.person')}
        </label>
        <select
          id={`${id}-person`}
          className={INPUT}
          value={values.assignee}
          disabled={off}
          onChange={(event) => set({ assignee: event.target.value })}
          data-testid={`${prefix}.person`}
        >
          <option value="">{t('tickets.person.none')}</option>
          {people.map((person) => (
            <option key={person.id} value={person.id}>
              {person.name}
            </option>
          ))}
        </select>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-topic`} className="text-sm font-medium">
          {t('tickets.field.topic')}
        </label>
        <select
          id={`${id}-topic`}
          className={INPUT}
          value={creating ? NEW_TOPIC : values.topic}
          disabled={off}
          onChange={(event) => {
            const value = event.target.value;
            setCreating(value === NEW_TOPIC);
            if (value !== NEW_TOPIC) set({ topic: value });
          }}
          data-testid={`${prefix}.topic`}
        >
          <option value="">{t('tickets.topic.none')}</option>
          {topics.map((topic) => (
            <option key={topic.id} value={topic.id}>
              {topic.name}
            </option>
          ))}
          <option value={NEW_TOPIC}>{t('tickets.topic.new')}</option>
        </select>
        {creating && (
          <div className="flex flex-wrap items-end gap-2">
            <label htmlFor={`${id}-topic-name`} className="sr-only">
              {t('tickets.topic.name')}
            </label>
            <input
              id={`${id}-topic-name`}
              className="min-h-11 min-w-0 flex-1 rounded border border-gray-300 px-2"
              value={topicName}
              maxLength={100}
              placeholder={t('tickets.topic.name')}
              disabled={off}
              onChange={(event) => setTopicName(event.target.value)}
              data-testid={`${prefix}.topic.name`}
            />
            <button
              type="button"
              className="min-h-11 rounded border border-gray-300 px-3 text-sm disabled:opacity-50"
              disabled={off || topicName.trim() === ''}
              onClick={() => void create()}
              data-testid={`${prefix}.topic.create`}
            >
              {t('tickets.topic.create')}
            </button>
          </div>
        )}
        {code !== null && (
          <p className="text-sm text-red-800" role="alert" data-testid={`${prefix}.topic.error`} data-code={code}>
            {t('tickets.topic.failed', { reason: error(code).title })}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-org`} className="text-sm font-medium">
          {t('tickets.field.github-org')}
        </label>
        <input
          id={`${id}-org`}
          className={INPUT}
          value={values.githubOrg}
          list={`${id}-orgs`}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          disabled={off}
          onChange={(event) => set({ githubOrg: event.target.value })}
          data-testid={`${prefix}.github-org`}
        />
        <datalist id={`${id}-orgs`}>
          {suggestions.orgs.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-repo`} className="text-sm font-medium">
          {t('tickets.field.github-repo')}
        </label>
        <input
          id={`${id}-repo`}
          className={INPUT}
          value={values.githubRepo}
          list={`${id}-repos`}
          placeholder={t('tickets.field.repo.placeholder')}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          disabled={off}
          onChange={(event) => repoChanged(event.target.value)}
          data-testid={`${prefix}.github-repo`}
        />
        <datalist id={`${id}-repos`}>
          {suggestions.repos.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-urgency`} className="text-sm font-medium">
          {t('tickets.field.urgency')}
        </label>
        <select
          id={`${id}-urgency`}
          className={INPUT}
          value={values.urgency}
          disabled={off}
          onChange={(event) => set({ urgency: event.target.value as Urgency })}
          data-testid={`${prefix}.urgency`}
        >
          {URGENCIES.map((urgency) => (
            <option key={urgency} value={urgency}>
              {t(`tickets.urgency.${urgency}`)}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
