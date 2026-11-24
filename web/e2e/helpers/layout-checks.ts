// Checks of 06-web.md section 12 on the current page, shared by the route sweeps (WEB-13): no horizontal scrolling of
// the document (the terminal container scrolls on its own), touch targets of at least 44 × 44 CSS pixels, no axe-core
// violation of impact "serious" or "critical". Each check returns readable problems instead of failing, so a sweep
// lists every problem of every route at once.
import AxeBuilder from '@axe-core/playwright';
import type { Page } from '@playwright/test';

/** The phone viewport every screen must fit (06-web.md section 1, principle 4). */
export const NARROW = { width: 360, height: 640 } as const;

export const MIN_TARGET = 44;

/** Interactive elements that are touch targets. */
const TARGETS =
  'a[href], button, input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="tab"], ' +
  '[role="link"], [role="switch"], [tabindex]:not([tabindex="-1"])';

/** If the document scrolls horizontally: the innermost elements that stick out on the right (for the message). */
export async function horizontalOverflow(page: Page): Promise<string[] | null> {
  return page.evaluate(() => {
    const root = document.documentElement;
    if (root.scrollWidth <= root.clientWidth) return null;
    // elements inside a container that clips them (its own scroll box) do not widen the page
    const clipped = (element: HTMLElement) => {
      for (
        let parent = element.parentElement;
        parent !== null && parent !== document.body;
        parent = parent.parentElement
      ) {
        if (getComputedStyle(parent).overflowX !== 'visible') return true;
      }
      return false;
    };
    const out = [...document.body.querySelectorAll<HTMLElement>('*')].filter(
      (element) => element.getBoundingClientRect().right > root.clientWidth + 0.5 && !clipped(element),
    );
    return out
      .filter((element) => !out.some((other) => other !== element && element.contains(other)))
      .slice(0, 5)
      .map((element) => element.dataset.testid ?? `${element.tagName.toLowerCase()}.${element.className}`.slice(0, 80));
  });
}

/** Visible touch targets smaller than `MIN_TARGET` (an input counts together with its labels). */
export async function smallTargets(page: Page): Promise<string[]> {
  return page.evaluate(
    ([selector, min]) => {
      const problems: string[] = [];
      for (const element of document.querySelectorAll<HTMLElement>(selector)) {
        // xterm's hidden helper textarea and visually hidden (sr-only) elements are no touch targets; links inside the
        // text of a Markdown block are inline targets (WCAG 2.5.8 exception, decision WEB-13)
        if (element.closest('.xterm-helper-textarea, .xterm-accessibility, [data-testid="markdown"]')) continue;
        if (!element.checkVisibility({ visibilityProperty: true, opacityProperty: true })) continue;
        const own = element.getBoundingClientRect();
        if (own.width <= 1 || own.height <= 1) continue;
        // an input counts together with its labels (a click on a label activates it)
        const boxes = [
          own,
          ...[...((element as HTMLInputElement).labels ?? [])].map((label) => label.getBoundingClientRect()),
        ];
        const width = Math.max(...boxes.map((box) => box.right)) - Math.min(...boxes.map((box) => box.left));
        const height = Math.max(...boxes.map((box) => box.bottom)) - Math.min(...boxes.map((box) => box.top));
        if (width + 0.5 < min || height + 0.5 < min) {
          const name =
            element.dataset.testid ?? `${element.tagName.toLowerCase()}:${element.textContent?.trim().slice(0, 30)}`;
          problems.push(`${name} ${Math.round(width)}×${Math.round(height)}`);
        }
      }
      return problems;
    },
    [TARGETS, MIN_TARGET] as const,
  );
}

/** axe-core violations of impact serious or critical (WCAG 2.2 A/AA and best practices). */
export async function axeViolations(page: Page): Promise<string[]> {
  const result = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'])
    .analyze();
  return result.violations
    .filter((violation) => violation.impact === 'serious' || violation.impact === 'critical')
    .map(
      (violation) =>
        `${violation.id} (${violation.impact}): ${violation.nodes
          .slice(0, 5)
          .map((node) => node.target.join(' '))
          .join(', ')}`,
    );
}

/** Every check of section 12 on the page as it is now: at its own viewport, then at 360 × 640 (restored after). */
export async function layoutProblems(page: Page): Promise<string[]> {
  const problems = [...(await axeViolations(page)), ...(await smallTargets(page)).map((target) => `target ${target}`)];
  const own = page.viewportSize()!;
  if (own.width !== NARROW.width || own.height !== NARROW.height) {
    await page.setViewportSize(NARROW);
    problems.push(...(await smallTargets(page)).map((target) => `target@360 ${target}`));
  }
  const overflow = await horizontalOverflow(page);
  if (overflow !== null) problems.push(`scrolls horizontally at 360 px: ${overflow.join(', ')}`);
  await page.setViewportSize(own);
  return [...new Set(problems)];
}
