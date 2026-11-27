// The files and images of a prompt in the chat (ADR-102, 06-web.md section 5.2): below the prompt's bubble, one entry
// per attachment the verified prompt names. An image (PNG, JPEG, GIF, WebP) is fetched, checked and opened at once and
// shown as a picture (a `data:` URL of the opened bytes: the hub's CSP allows `img-src 'self' data:`); a click shows it
// in full size and again small. Every other file is a named entry with its size that fetches, opens and saves the file
// when clicked. The workgroup comes from the verified event (`locateEvent`); nothing while the store does not hold it.
import { useEffect, useState } from 'react';
import { type AttachmentRef, downloadAttachment, isImage } from '../../core/attachments/transfer';
import { locateEvent } from '../../core/logs/reader';
import { promptAttachments } from '../../core/tasks/send';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { saveBlob } from '../logs/LogView';
import { useAppEnv } from '../shell/context';
import { sizeText } from '../composer/Attachments';

/** The `data:` URL of the bytes of a picture (base64 in pieces). */
export function dataUrl(type: string, bytes: Uint8Array): string {
  let binary = '';
  for (let at = 0; at < bytes.length; at += 0x8000) binary += String.fromCharCode(...bytes.subarray(at, at + 0x8000));
  return `data:${type};base64,${btoa(binary)}`;
}

type Loaded =
  { readonly state: 'loading' } | { readonly state: 'ready'; readonly url: string } | { readonly state: 'failed' };

function Picture({
  org,
  wg,
  attached,
}: {
  readonly org: string;
  readonly wg: string;
  readonly attached: AttachmentRef;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const [loaded, setLoaded] = useState<Loaded>({ state: 'loading' });
  const [large, setLarge] = useState(false);
  useEffect(() => {
    let live = true;
    downloadAttachment(app.tasks, org, wg, attached).then(
      (bytes) => live && setLoaded({ state: 'ready', url: dataUrl(attached.type, bytes) }),
      () => live && setLoaded({ state: 'failed' }),
    );
    return () => {
      live = false;
    };
  }, [app, org, wg, attached]);
  if (loaded.state !== 'ready') {
    return (
      <span className="text-xs text-gray-600" data-testid="chat.attachment.state" data-state={loaded.state}>
        {loaded.state === 'loading' ? t('attach.chat.loading') : t('attach.chat.failed')} · {attached.name}
      </span>
    );
  }
  return (
    <button
      type="button"
      className="rounded border border-gray-200"
      aria-label={t('attach.chat.open', { name: attached.name })}
      aria-pressed={large}
      onClick={() => setLarge(!large)}
      data-testid="chat.attachment.image"
    >
      <img
        src={loaded.url}
        alt={t('attach.chat.image', { name: attached.name })}
        className={large ? 'max-w-full' : 'max-h-48 max-w-full'}
      />
    </button>
  );
}

function FileEntry({
  org,
  wg,
  attached,
}: {
  readonly org: string;
  readonly wg: string;
  readonly attached: AttachmentRef;
}) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const [failed, setFailed] = useState(false);
  const save = async () => {
    setFailed(false);
    try {
      const bytes = await downloadAttachment(app.tasks, org, wg, attached);
      saveBlob(attached.name, new Blob([bytes], { type: attached.type }));
    } catch {
      setFailed(true);
    }
  };
  return (
    <span className="flex flex-col items-end">
      <button
        type="button"
        className="min-h-11 rounded border border-gray-300 px-3 text-sm"
        aria-label={t('attach.chat.download', { name: attached.name })}
        onClick={() => void save()}
        data-testid="chat.attachment.file"
      >
        {attached.name} <span className="text-xs text-gray-600">{sizeText(attached.bytes, locale)}</span>
      </button>
      {failed && (
        <span className="text-xs text-red-800" role="alert" data-testid="chat.attachment.failed">
          {t('attach.chat.failed')}
        </span>
      )}
    </span>
  );
}

/** The attachments of the verified prompt at `seq` with this `body`; nothing for a prompt of text only. */
export function PromptAttachments({ seq, body }: { readonly seq: number; readonly body: unknown }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const org = useStore((state) => state.session.activeOrg ?? '', app.store);
  const wg = useStore((state) => locateEvent(state, seq, body)?.wg ?? '', app.store);
  const refs = promptAttachments(body);
  if (refs.length === 0 || wg === '') return null;
  return (
    <ul className="flex flex-col items-end gap-2" aria-label={t('attach.chat.label')} data-testid="chat.attachments">
      {refs.map((attached) => (
        <li key={attached.object}>
          {isImage(attached) ? (
            <Picture org={org} wg={wg} attached={attached} />
          ) : (
            <FileEntry org={org} wg={wg} attached={attached} />
          )}
        </li>
      ))}
    </ul>
  );
}
