// U-WEB-ui-chat-links: bare URLs are links and GFM tables are tables wherever the web client shows text of sessions or
// persons (06-web.md section 2.1): Markdown of outputs and whiteboard entries, prompts, the plain-text fallbacks and the
// texts of operation cards. Every link passes `safeUrl` and opens in a new tab with `LINK_REL`.
import { act, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { LINK_REL } from '../../src/core/chat/links';
import { createI18n } from '../../src/core/i18n';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import Markdown from '../../src/ui/task-tabs/chat/Markdown';
import { LinkedText } from '../../src/ui/task-tabs/chat/LinkedText';
import { I18nProvider } from '../../src/ui/i18n';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

const PR = 'https://github.com/example-org/example-repo/pull/6371';

function markdown(text: string) {
  render(
    <I18nProvider value={createI18n('en')}>
      <Markdown text={text} />
    </I18nProvider>,
  );
  return screen.getByTestId('markdown');
}

async function chat() {
  const t = await taskWorld();
  t.client.on(`GET /v1/tasks/${TASK}`, () => ({
    task: taskRow(t, {}),
    sessions: [{ session: sessionRow(t, { state: 'working' }), transitions: [] }],
  }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}`);
  await screen.findByTestId('task.screen');
  act(() => setActiveOrg(t.store, t.org));
  return t;
}

const put = (t: TaskWorld, ...entries: EventEntry[]) => act(() => putEvents(t.store, t.wg, entries));

describe('Markdown (GFM)', () => {
  it('makes a bare URL of an output a link with rel and target', () => {
    const root = markdown(`SEC-020: ${PR}, mail to someone@example.com or see www.example.com.`);
    const links = within(root).getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      PR,
      'mailto:someone@example.com',
      'http://www.example.com',
    ]);
    for (const link of links) {
      expect(link).toHaveAttribute('rel', LINK_REL);
      expect(link).toHaveAttribute('target', '_blank');
    }
    expect(links[0]).toHaveTextContent(PR);
  });

  it('keeps a javascript: or data: URL as text', () => {
    const root = markdown('Do not click javascript:alert(1) or data:text/html,x nor <javascript:alert(2)>.');
    expect(within(root).queryAllByRole('link')).toHaveLength(0);
    expect(root.querySelector('a')).toBeNull();
    expect(root).toHaveTextContent('javascript:alert(1)');
    expect(within(root).getByTestId('markdown.link-removed')).toHaveTextContent('javascript:alert(2)');
  });

  it('renders a table in a scroll box of its own that the keyboard reaches', () => {
    const root = markdown(`| ID | PR |\n| --- | :-: |\n| SEC-020 | ${PR} |\n| SEC-021 | [b](javascript:x) |`);
    const table = root.querySelector('table');
    expect(table).not.toBeNull();
    const box = within(root).getByTestId('markdown.table');
    expect(box).toContainElement(table);
    expect(box).toHaveAttribute('tabindex', '0');
    expect(box.className).toContain('overflow-x-auto');
    expect([...table!.querySelectorAll('th')].map((cell) => cell.textContent)).toEqual(['ID', 'PR']);
    expect([...table!.querySelectorAll('tbody tr')].map((row) => row.querySelectorAll('td').length)).toEqual([2, 2]);
    expect(within(table!).getByRole('link')).toHaveAttribute('href', PR);
    expect(within(table!).getByTestId('markdown.link-removed')).toHaveTextContent('b');
    expect(root).not.toHaveTextContent('---');
  });

  it('still drops raw HTML and shows images only as links', () => {
    const root = markdown('<b>raw</b> ~~gone~~ ![logo](https://img.example/i.png)');
    expect(root.querySelector('b, img, script')).toBeNull();
    expect(root.querySelector('del')).toHaveTextContent('gone');
    expect(within(root).getByRole('link')).toHaveAttribute('href', 'https://img.example/i.png');
  });
});

describe('plain text with links', () => {
  it('links http, https and mailto URLs and keeps every other scheme as text', () => {
    render(<LinkedText text={`See ${PR}. Ask mailto:a@b.example or javascript:alert(1) / data:text/html,x`} />);
    const links = screen.getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual([PR, 'mailto:a@b.example']);
    for (const link of links) {
      expect(link).toHaveAttribute('rel', LINK_REL);
      expect(link).toHaveAttribute('target', '_blank');
    }
    expect(screen.getByText(/javascript:alert\(1\)/)).not.toHaveAttribute('href');
  });

  it('links only https URLs for texts from GitHub', () => {
    render(<LinkedText text="http://a.example https://b.example mailto:c@d.example" schemes={['https:']} />);
    expect(screen.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual(['https://b.example']);
  });
});

describe('chat', () => {
  it('makes a URL in a prompt of a person a link and keeps the prompt plain text', async () => {
    const t = await chat();
    put(t, verified(t, 'prompt', { text: `Look at ${PR} and **this**` }, { ts: '2026-11-21T12:00:01.000Z' }));
    const prompt = screen.getByTestId('chat.prompt.text');
    const link = within(prompt).getByRole('link');
    expect(link).toHaveAttribute('href', PR);
    expect(link).toHaveAttribute('rel', LINK_REL);
    expect(link).toHaveAttribute('target', '_blank');
    expect(prompt).toHaveTextContent(`Look at ${PR} and **this**`);
    expect(prompt.querySelector('strong')).toBeNull();
  });

  it('makes a bare URL of a session output a link and renders its table', async () => {
    const t = await chat();
    put(
      t,
      verified(t, 'session.output', { text: `SEC-020: ${PR}\n\n| a | b |\n|---|---|\n| 1 | 2 |` }, { session: MAIN }),
    );
    const output = screen.getByTestId('chat.output');
    // the plain-text fallback links as well, then the Markdown
    expect(within(output).getByRole('link')).toHaveAttribute('href', PR);
    await within(output).findByTestId('markdown');
    expect(within(output).getByRole('link')).toHaveAttribute('href', PR);
    expect(output.querySelector('table')).not.toBeNull();
  });
});
