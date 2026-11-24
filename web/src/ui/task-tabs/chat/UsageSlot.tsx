// The usage strip of the chat (06-web.md section 5.2): tokens, cost with basis label and context-window share are
// WEB-10's. This slot renders the strip that package adds at `./usage-strip.tsx` (export `UsageStrip`), discovered
// like every extension point (ADR-027 section 4).
import type { ComponentType } from 'react';
import { isRecord, type ModuleMap } from '../../../core/registry';

export interface UsageStripProps {
  readonly orgId: string;
  readonly taskId: string;
}

const stripModules: ModuleMap = import.meta.glob('./usage-strip.tsx', { eager: true });

/** The discovered strip component, or null while none exists. */
export function usageStrip(modules: ModuleMap = stripModules): ComponentType<UsageStripProps> | null {
  const found = Object.values(modules)[0];
  const strip = isRecord(found) ? found.UsageStrip : undefined;
  return typeof strip === 'function' ? (strip as ComponentType<UsageStripProps>) : null;
}

const STRIP = usageStrip();

export function UsageSlot({ orgId, taskId, strip = STRIP }: UsageStripProps & { readonly strip?: typeof STRIP }) {
  const Strip = strip;
  return (
    <div className="min-h-0" data-testid="chat.usage">
      {Strip !== null && <Strip orgId={orgId} taskId={taskId} />}
    </div>
  );
}
