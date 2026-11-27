// Files and images attached to a prompt in the composers (ADR-102, 06-web.md section 4.3): a button "Attach file"
// (a hidden file input, several files at once), dropping files onto the composer and pasting an image into the prompt
// field. The chosen files are listed with name and size and can be removed before sending; the limits (10 files,
// 1 byte to 32 MB each) are checked at once (`checkFiles`). The files are read into memory when chosen and uploaded
// only when the prompt is sent (`upload`): sealed under the workgroup's key, chunk by chunk, committed; the prompt is
// sealed afterwards with their references. A file uploaded for a send that then failed is not uploaded again for the
// retry to the same workgroup (the same reference); for another workgroup it is uploaded anew.
import { type ClipboardEvent, type DragEvent, useId, useRef, useState } from 'react';
import {
  type AttachmentRef,
  checkFiles,
  type FilesProblem,
  type PendingFile,
  pendingFile,
  uploadAttachment,
} from '../../core/attachments/transfer';
import { uuid7 } from '../../core/encoding/ids';
import type { TasksContext } from '../../core/tasks/pull';
import { useI18n } from '../i18n';

/** A chosen file and, once uploaded, its reference with the workgroup it was uploaded to. */
export interface Attached {
  readonly file: PendingFile;
  readonly uploaded: { readonly wg: string; readonly ref: AttachmentRef } | null;
}

export type AttachProblem = FilesProblem | 'read';

export interface Attachments {
  readonly items: readonly Attached[];
  readonly problem: AttachProblem | null;
  /** Adds files (checked against the limits; read into memory). */
  readonly add: (files: readonly File[]) => Promise<void>;
  readonly remove: (id: string) => void;
  /** Uploads every file not yet uploaded to `wg`; the references in the order of the list. */
  readonly upload: (context: TasksContext, org: string, wg: string) => Promise<AttachmentRef[]>;
  readonly clear: () => void;
}

async function read(file: File): Promise<PendingFile> {
  return pendingFile(uuid7(), file.name, file.type, new Uint8Array(await file.arrayBuffer()));
}

export function useAttachments(): Attachments {
  const [items, setItems] = useState<readonly Attached[]>([]);
  const [problem, setProblem] = useState<AttachProblem | null>(null);

  const add = async (files: readonly File[]) => {
    const found = checkFiles([...items.map((item) => ({ size: item.file.data.length })), ...files]);
    if (found !== null) {
      setProblem(found);
      return;
    }
    try {
      const pending = await Promise.all(files.map(read));
      setItems((known) => [...known, ...pending.map((file) => ({ file, uploaded: null }))]);
      setProblem(null);
    } catch {
      setProblem('read');
    }
  };

  const remove = (id: string) => setItems((known) => known.filter((item) => item.file.id !== id));

  const upload = async (context: TasksContext, org: string, wg: string) => {
    const refs: AttachmentRef[] = [];
    for (const item of items) {
      if (item.uploaded !== null && item.uploaded.wg === wg) {
        refs.push(item.uploaded.ref);
        continue;
      }
      const ref = await uploadAttachment(context, org, wg, item.file);
      refs.push(ref);
      setItems((known) =>
        known.map((entry) => (entry.file.id === item.file.id ? { ...entry, uploaded: { wg, ref } } : entry)),
      );
    }
    return refs;
  };

  const clear = () => {
    setItems([]);
    setProblem(null);
  };

  return { items, problem, add, remove, upload, clear };
}

/** The files of a paste or a drop (none for text). */
export function filesOf(transfer: DataTransfer | null): File[] {
  return transfer === null ? [] : [...transfer.files];
}

/** Paste and drop handlers that attach files and leave text alone. */
export function attachHandlers(attachments: Attachments, disabled: boolean) {
  const take = (event: ClipboardEvent | DragEvent, transfer: DataTransfer | null) => {
    const files = filesOf(transfer);
    if (files.length === 0 || disabled) return;
    event.preventDefault();
    void attachments.add(files);
  };
  return {
    onPaste: (event: ClipboardEvent) => take(event, event.clipboardData),
    onDrop: (event: DragEvent) => take(event, event.dataTransfer),
    onDragOver: (event: DragEvent) => {
      if (!disabled && [...event.dataTransfer.types].includes('Files')) event.preventDefault();
    },
  };
}

/** A file size for people (1 KB = 1024 bytes). */
export function sizeText(size: number, locale: string): string {
  const format = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${format.format(size / 1024)} KB`;
  return `${format.format(size / (1024 * 1024))} MB`;
}

export function AttachButton({
  attachments,
  disabled,
  testId,
}: {
  readonly attachments: Attachments;
  readonly disabled: boolean;
  readonly testId: string;
}) {
  const { t } = useI18n();
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  return (
    <>
      <label htmlFor={id} className="sr-only">
        {t('attach.input.label')}
      </label>
      <input
        id={id}
        ref={input}
        type="file"
        multiple
        className="hidden"
        disabled={disabled}
        onChange={(event) => {
          const files = [...event.target.files!];
          event.target.value = '';
          void attachments.add(files);
        }}
        data-testid={`${testId}.input`}
      />
      <button
        type="button"
        className="min-h-11 rounded border border-gray-300 px-3 disabled:opacity-50"
        disabled={disabled}
        onClick={() => input.current!.click()}
        data-testid={`${testId}.button`}
      >
        {t('attach.button')}
      </button>
    </>
  );
}

export function AttachmentList({
  attachments,
  disabled,
  testId,
}: {
  readonly attachments: Attachments;
  readonly disabled: boolean;
  readonly testId: string;
}) {
  const { t, locale } = useI18n();
  const { items, problem } = attachments;
  return (
    <>
      {items.length > 0 && (
        <ul className="flex flex-wrap gap-2" aria-label={t('attach.list.label')} data-testid={`${testId}.list`}>
          {items.map((item) => (
            <li
              key={item.file.id}
              className="flex min-h-11 items-center gap-2 rounded border border-gray-300 px-2 text-sm"
              data-testid={`${testId}.item`}
              data-uploaded={item.uploaded !== null}
            >
              <span className="max-w-48 truncate">{item.file.name}</span>
              <span className="text-xs text-gray-600">{sizeText(item.file.data.length, locale)}</span>
              <button
                type="button"
                className="min-h-11 min-w-11 rounded px-2 text-gray-700 disabled:opacity-50"
                aria-label={t('attach.remove', { name: item.file.name })}
                disabled={disabled}
                onClick={() => attachments.remove(item.file.id)}
                data-testid={`${testId}.remove`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {problem !== null && (
        <p className="text-sm text-red-800" role="alert" data-testid={`${testId}.problem`} data-problem={problem}>
          {t(`attach.error.${problem}`)}
        </p>
      )}
    </>
  );
}
