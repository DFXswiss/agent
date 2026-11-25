// Chat item `browser.state` (06-web.md section 5.4b, ADR-085 section 7): the card of a browser session – state,
// countdown while starting, page, actions, VM start and "Watch live" – is a lazy chunk (`browser/StateCard.tsx`), so
// the platform browser adds next to nothing to the start of the app (06-web.md section 12.2).
import { lazy, Suspense } from 'react';
import type { ChatItem, ChatItemProps } from '../registries/chat-items';
import { useI18n } from '../i18n';
import { ChunkBoundary } from '../lazy/ChunkBoundary';

const StateCard = lazy(() => import('../browser/StateCard'));

function State({ item }: ChatItemProps) {
  const { t } = useI18n();
  return (
    <ChunkBoundary
      fallback={
        <p className="text-sm text-red-800" role="alert" data-testid="browser.chunk-unavailable">
          {t('app.screen-unavailable.title')}
        </p>
      }
    >
      <Suspense fallback={null}>
        <StateCard item={item} />
      </Suspense>
    </ChunkBoundary>
  );
}

export const chatItem: ChatItem = { kind: 'browser.state', Component: State };
