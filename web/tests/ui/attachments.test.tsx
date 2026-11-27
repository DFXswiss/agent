// U-WEB-ui-attachments (ADR-102): files and images in the composers (button, paste, drop, limits, removal, upload
// before the prompt, no second upload for a retry to the same workgroup) and in the chat (pictures shown from the
// opened bytes, other files saved on click, failures) in jsdom against the core task world.
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MAX_ATTACHMENTS, MAX_BYTES, sealAttachment } from '../../src/core/crypto/attachment';
import { b64uEncode } from '../../src/core/encoding/b64u';
import { epochKey, WG } from '../../src/core/log/state';
import { HubError } from '../../src/core/net/signedFetch';
import { putEvents } from '../../src/core/store/slices/events';
import { orgEntry } from '../../src/core/store/slices/orgs';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { type TaskRow } from '../../src/core/store/slices/tasks';
import { PromptAttachments, dataUrl } from '../../src/ui/attachments/PromptAttachments';
import { sizeText } from '../../src/ui/composer/Attachments';
import { Composer } from '../../src/ui/composer/Composer';
import { ChatComposer } from '../../src/ui/task-tabs/chat/ChatComposer';
import { taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { mount, verified } from './chat-world';

afterEach(() => vi.restoreAllMocks());

const OBJECT = '0190f000-0000-7000-8000-00000000f001';

function file(name: string, bytes: number[] | Uint8Array, type = 'text/plain'): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

/** Answers every chunk upload and commit of the world's workgroup; the uploaded objects. */
function hub(t: TaskWorld): string[] {
  const objects: string[] = [];
  const original = t.client.request.bind(t.client);
  t.client.request = async (method, target, options = {}) => {
    const match = /^\/v1\/workgroups\/[^/]+\/attachments\/([^/]+)\/(chunks\/\d+|commit)$/.exec(target);
    if (match === null) return original(method, target, options);
    t.client.calls.push({ method, target, body: options.body });
    if (match[2] === 'commit') {
      objects.push(match[1]!);
      return { completed_at: '2026-11-27T12:00:00.000Z', replayed: false };
    }
    return { replayed: false };
  };
  return objects;
}

function created(t: TaskWorld, body: unknown) {
  const id = (body as { task_id: string }).task_id;
  return {
    task: taskRow(t, { task_id: id }),
    session: { session_id: 's1', state: 'requested', device_id: 'd', estimate_ms: 3000 },
    events: [],
  };
}

async function composer(t: TaskWorld) {
  t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [] }));
  t.client.on(`GET /v1/workgroups/${t.wg}/device-candidates`, () => ({ candidates: [] }));
  return mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
}

const choose = (testId: string, files: File[]) =>
  act(async () => {
    fireEvent.change(screen.getByTestId(testId), { target: { files } });
  });

describe('composer', () => {
  it('attaches chosen files, lists and removes them and checks the limits', async () => {
    const t = await taskWorld();
    await composer(t);
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => undefined);
    fireEvent.click(screen.getByTestId('composer.attach.button'));
    expect(click).toHaveBeenCalled();
    await choose('composer.attach.input', [file('a.txt', [1, 2, 3]), file('b/c.txt', new Uint8Array(2048))]);
    const items = await screen.findAllByTestId('composer.attachments.item');
    expect(items.map((item) => item.textContent)).toEqual(['a.txt3 B×', 'b_c.txt2 KB×']);
    expect(screen.getByTestId('composer.attachments.list')).toHaveAccessibleName('Attached files');
    fireEvent.click(within(items[0]!).getByRole('button', { name: 'Remove a.txt' }));
    expect(screen.getAllByTestId('composer.attachments.item')).toHaveLength(1);
    await choose('composer.attach.input', [file('empty.txt', [])]);
    expect(screen.getByTestId('composer.attachments.problem')).toHaveAttribute('data-problem', 'empty');
    const many = Array.from({ length: MAX_ATTACHMENTS }, (_, index) => file(`${index}.txt`, [1]));
    await choose('composer.attach.input', many);
    expect(screen.getByTestId('composer.attachments.problem')).toHaveTextContent('At most 10 files per message.');
    const large = file('large.bin', [1]);
    Object.defineProperty(large, 'size', { value: MAX_BYTES + 1 });
    await choose('composer.attach.input', [large]);
    expect(screen.getByTestId('composer.attachments.problem')).toHaveAttribute('data-problem', 'too_large');
    const broken = file('broken.txt', [1]);
    broken.arrayBuffer = () => Promise.reject(new Error('gone'));
    await choose('composer.attach.input', [broken]);
    expect(screen.getByTestId('composer.attachments.problem')).toHaveAttribute('data-problem', 'read');
    await choose('composer.attach.input', [file('d.txt', [4])]);
    expect(screen.queryByTestId('composer.attachments.problem')).toBeNull();
    expect(screen.getAllByTestId('composer.attachments.item')).toHaveLength(2);
  });

  it('takes files from a paste and a drop, leaves text alone', async () => {
    const t = await taskWorld();
    await composer(t);
    const prompt = screen.getByTestId('composer.prompt');
    await act(async () => {
      fireEvent.paste(prompt, { clipboardData: { files: [file('shot.png', [1], 'image/png')], types: ['Files'] } });
    });
    expect(await screen.findAllByTestId('composer.attachments.item')).toHaveLength(1);
    await act(async () => {
      fireEvent.paste(prompt, { clipboardData: { files: [], types: ['text/plain'] } });
    });
    const form = screen.getByTestId('composer');
    const over = (types: string[]) => fireEvent.dragOver(form, { dataTransfer: { files: [], types } });
    expect(over(['Files'])).toBe(false);
    expect(over(['text/plain'])).toBe(true);
    await act(async () => {
      fireEvent.drop(form, { dataTransfer: { files: [file('log.txt', [2])], types: ['Files'] } });
    });
    expect(screen.getAllByTestId('composer.attachments.item')).toHaveLength(2);
  });

  it('uploads the files before the prompt, names them in it and uploads none again for the retry', async () => {
    const t = await taskWorld();
    const objects = hub(t);
    const router = await composer(t);
    let posts = 0;
    t.client.on('POST /v1/tasks', (body) => {
      posts += 1;
      if (posts === 1) throw new HubError('unavailable', 503);
      return created(t, body);
    });
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Look' } });
    await choose('composer.attach.input', [file('a.txt', [1, 2, 3])]);
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(screen.getByTestId('composer.error')).toHaveAttribute('data-code', 'unavailable'));
    expect(objects).toHaveLength(1);
    const methods = t.client.calls.filter((call) => call.method !== 'GET').map((call) => call.method);
    expect(methods).toEqual(['PUT', 'POST', 'POST']);
    expect(screen.getByTestId('composer.attachments.item')).toHaveAttribute('data-uploaded', 'true');
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(router.router.state.location.pathname).toMatch(/\/t\//));
    expect(objects).toHaveLength(1);
    const [first, second] = t.client.of('POST /v1/tasks');
    expect(JSON.stringify(first!.body)).toBe(JSON.stringify(second!.body));
  });

  it('ignores files while the prompt is being sent', async () => {
    const t = await taskWorld();
    await composer(t);
    t.client.on('POST /v1/tasks', () => new Promise(() => undefined));
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Wait' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(screen.getByTestId('composer.attach.button')).toBeDisabled());
    const form = screen.getByTestId('composer');
    expect(fireEvent.dragOver(form, { dataTransfer: { files: [], types: ['Files'] } })).toBe(true);
    await act(async () => {
      fireEvent.drop(form, { dataTransfer: { files: [file('late.txt', [1])], types: ['Files'] } });
    });
    expect(screen.queryByTestId('composer.attachments.item')).toBeNull();
  });
});

function Switch({ first, second }: { readonly first: TaskRow; readonly second: TaskRow }) {
  const [task, setTask] = useState(first);
  return (
    <>
      <button type="button" onClick={() => setTask(second)} data-testid="switch">
        switch
      </button>
      <ChatComposer task={task} main={null} />
    </>
  );
}

describe('chat composer', () => {
  it('sends a follow-up with its files, clears them, and uploads anew for another workgroup', async () => {
    const t = await taskWorld();
    const objects = hub(t);
    const task = taskRow(t) as TaskRow;
    const other = { ...task, wg_id: '0190f000-0000-7000-8000-00000000aaaa' };
    let fail = true;
    t.client.on(`POST /v1/tasks/${task.task_id}/prompts`, () => {
      if (fail) throw new HubError('unavailable', 503);
      return { scope_seq: 4, session_id: 's4' };
    });
    mount(t, [{ path: '/', element: <Switch first={task} second={other} /> }], '/');
    fireEvent.change(screen.getByTestId('chat.composer.prompt'), { target: { value: 'And this' } });
    await choose('chat.composer.attach.input', [file('a.txt', [1])]);
    fireEvent.click(screen.getByTestId('chat.composer.send'));
    await waitFor(() => expect(screen.getByTestId('chat.composer.error')).toBeInTheDocument());
    expect(objects).toHaveLength(1);
    // the retry names the same object without a second upload
    fail = false;
    fireEvent.click(screen.getByTestId('chat.composer.send'));
    await waitFor(() => expect(screen.queryByTestId('chat.composer.attachments.item')).toBeNull());
    expect(objects).toHaveLength(1);
    expect(screen.getByTestId('chat.composer.prompt')).toHaveValue('');
    // a file uploaded to one workgroup is uploaded anew for another (here without its key: the send fails)
    fail = true;
    fireEvent.change(screen.getByTestId('chat.composer.prompt'), { target: { value: 'Next' } });
    await choose('chat.composer.attach.input', [file('b.txt', [2])]);
    fireEvent.click(screen.getByTestId('chat.composer.send'));
    await waitFor(() => expect(objects).toHaveLength(2));
    fireEvent.click(screen.getByTestId('switch'));
    fireEvent.click(screen.getByTestId('chat.composer.send'));
    // the other workgroup is not in this org's log: sealing for it fails before anything is uploaded or sent
    await waitFor(() => expect(t.client.of(`POST /v1/tasks/${task.task_id}/prompts`)).toHaveLength(3));
    await waitFor(() => expect(screen.getByTestId('chat.composer.error')).toBeInTheDocument());
    expect(objects).toHaveLength(2);
    expect(t.client.calls.some((call) => call.target.startsWith(`/v1/workgroups/${other.wg_id}/`))).toBe(false);
  });
});

describe('chat', () => {
  async function shown(t: TaskWorld, body: unknown, store = true) {
    setActiveOrg(t.store, t.org);
    const entry = verified(t, 'prompt', body);
    if (store) putEvents(t.store, t.wg, [entry]);
    mount(t, [{ path: '/', element: <PromptAttachments seq={entry.scopeSeq} body={body} /> }], '/');
  }

  async function sealedRef(t: TaskWorld, data: Uint8Array, name: string, type: string) {
    const key = orgEntry(t.store.getState().orgs, t.org).keys.get(epochKey(WG, t.wg, 1))!;
    const { ref, chunks } = await sealAttachment(key, { objectId: OBJECT, name, type, epoch: 1, data });
    return { ref, ct: b64uEncode(chunks[0]!) };
  }

  it('shows a picture from the opened bytes and toggles its size', async () => {
    const t = await taskWorld();
    const { ref, ct } = await sealedRef(t, new Uint8Array([137, 80, 78, 71]), 'shot.png', 'image/png');
    t.client.on(`GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`, () => ({ ct }));
    await shown(t, { text: 'Look', attachments: [ref] });
    expect(screen.getByTestId('chat.attachment.state')).toHaveAttribute('data-state', 'loading');
    const button = await screen.findByTestId('chat.attachment.image');
    const image = within(button).getByRole('img', { name: 'Image shot.png' });
    expect(image).toHaveAttribute('src', 'data:image/png;base64,iVBORw==');
    expect(image).toHaveClass('max-h-48');
    fireEvent.click(button);
    expect(button).toHaveAttribute('aria-pressed', 'true');
    expect(image).not.toHaveClass('max-h-48');
    expect(screen.getByTestId('chat.attachments')).toHaveAccessibleName('Attachments');
  });

  it('says so when a picture cannot be opened and stops caring after unmount', async () => {
    const t = await taskWorld();
    const { ref } = await sealedRef(t, new Uint8Array([1]), 'shot.png', 'image/png');
    t.client.on(`GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`, () => {
      throw new HubError('not_found', 404);
    });
    await shown(t, { text: 'Look', attachments: [ref] });
    await waitFor(() => expect(screen.getByTestId('chat.attachment.state')).toHaveAttribute('data-state', 'failed'));
    cleanup();
    // an answer after the chat item is gone changes nothing (opened or not)
    const { ct } = await sealedRef(t, new Uint8Array([1]), 'shot.png', 'image/png');
    for (const settle of ['resolve', 'reject'] as const) {
      let release: () => void = () => undefined;
      t.client.on(
        `GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`,
        () =>
          new Promise((resolve, reject) => {
            release = () => (settle === 'resolve' ? resolve({ ct }) : reject(new HubError('unavailable', 503)));
          }),
      );
      await shown(t, { text: 'Look', attachments: [ref] });
      await waitFor(() =>
        expect(t.client.of(`GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`)).not.toHaveLength(0),
      );
      cleanup();
      await act(async () => release());
    }
  });

  it('saves another file when clicked and reports a failure', async () => {
    const t = await taskWorld();
    const { ref, ct } = await sealedRef(t, new Uint8Array(2048), 'report.pdf', 'application/pdf');
    let answer: unknown = { ct };
    t.client.on(`GET /v1/workgroups/${t.wg}/attachments/${OBJECT}/chunks/0`, () => answer);
    const create = vi.fn(() => 'blob:x');
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    await shown(t, { text: 'Read', attachments: [ref] });
    const entry = screen.getByTestId('chat.attachment.file');
    expect(entry).toHaveAccessibleName('Download report.pdf');
    expect(entry).toHaveTextContent('report.pdf 2 KB');
    fireEvent.click(entry);
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(create).toHaveBeenCalled();
    answer = null;
    fireEvent.click(entry);
    expect(await screen.findByTestId('chat.attachment.failed')).toHaveTextContent('The attachment could not be opened');
  });

  it('shows nothing for a prompt of text only or one the store does not hold', async () => {
    const t = await taskWorld();
    await shown(t, { text: 'Only text', attachments: [] });
    expect(screen.queryByTestId('chat.attachments')).toBeNull();
    const { ref } = await sealedRef(t, new Uint8Array([1]), 'a.txt', 'text/plain');
    await shown(t, { text: 'Elsewhere', attachments: [ref] }, false);
    expect(screen.queryByTestId('chat.attachments')).toBeNull();
  });

  it('formats sizes and picture addresses', () => {
    expect(sizeText(10, 'en')).toBe('10 B');
    expect(sizeText(5 * 1024 * 1024, 'de')).toBe('5 MB');
    expect(dataUrl('image/gif', new Uint8Array([71, 73, 70]))).toBe('data:image/gif;base64,R0lG');
  });
});
