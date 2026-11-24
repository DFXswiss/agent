// Chat timeline items discovered at `src/ui/chat-items/<kind>.tsx` (export `chatItem`, ADR-027 §4): one renderer
// per item kind (e.g. `prompt`, `session.output`); the file name is the kind.
import type { ComponentType } from 'react';
import { baseName, collect, hasShape, type ModuleMap } from '../../core/registry';

export interface ChatItemProps {
  /** The verified, decrypted timeline item; each renderer narrows it to its kind. */
  readonly item: unknown;
}

export interface ChatItem {
  readonly kind: string;
  readonly Component: ComponentType<ChatItemProps>;
}

const itemModules: ModuleMap = import.meta.glob('../chat-items/*.tsx', { eager: true });

const isItem = (value: unknown): value is ChatItem => hasShape(value, { kind: 'string', Component: 'component' });

export function chatItems(modules: ModuleMap = itemModules): ReadonlyMap<string, ChatItem> {
  const items = new Map<string, ChatItem>();
  for (const { path, value } of collect(modules, 'chatItem', isItem)) {
    if (value.kind !== baseName(path, '.tsx')) throw new Error(`${path}: kind "${value.kind}" must match the file`);
    items.set(value.kind, value);
  }
  return items;
}
