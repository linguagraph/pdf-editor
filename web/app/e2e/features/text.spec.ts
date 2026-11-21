// W5.4: text selection, Copy and Select All over the page text (W1.4 section 10, the desktop's
// model). A drag across two lines and Ctrl+C copies both lines, and Ctrl+A then Ctrl+C copies the
// whole page. The flows only read the document, so nothing is saved and no host check is recorded.
// text_multipage's page 1 has a heading line, then body lines; the expected text is read from the
// page's own text layer, so the test checks what the view shows is what Copy takes.
import { expect, type Locator, type Page, test } from '@playwright/test';
import { STAGED, fixture, openApp, openPdf, watchPage } from './support';

interface CopyWindow {
  __copied: string[];
}

// Records what Copy writes. The clipboard permission is not granted in every browser the suite
// runs on, so writeText is replaced before the app loads: Copy still runs its own path, and the text
// it hands to the clipboard is kept for the test to read.
async function captureClipboard(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const copied: string[] = [];
    Object.defineProperty(window, '__copied', { value: copied });
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: (text: string) => {
          copied.push(text);
          return Promise.resolve();
        },
        // A large copy writes a ClipboardItem whose data is a promise (largeCopy.ts): read it back.
        write: async (items: ClipboardItems) => {
          const blob = await items[0]?.getType('text/plain');
          if (blob !== undefined) copied.push(await blob.text());
        },
      },
    });
  });
}

// The text lines of one page, as the view draws them (the text layer's spans, in reading order).
function pageLines(page: Page, index: number): Locator {
  return page.locator(`[data-page="${index}"] .page-text-line`);
}

// The text of each line, skipping empty spans.
async function lineTexts(lines: Locator): Promise<string[]> {
  const texts = await lines.allTextContents();
  return texts.filter((text) => text.trim() !== '');
}

async function copied(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as CopyWindow).__copied);
}

test.describe('engine staged', () => {
  test.skip(!STAGED, 'engine assets are not staged (scripts/stage_engine_assets.mjs); not run');

  test('a selection across two lines: Copy writes both lines, joined by a line break', async ({
    page,
  }) => {
    const problems = watchPage(page);
    await captureClipboard(page);
    await openApp(page);
    await openPdf(page, 'text_multipage.pdf', fixture('text_multipage.pdf'));

    // Drag from the start of the second line to the end of the third. The drag stays on the page's
    // text, so the selection covers the lines from the first's start to the third's end.
    const lines = pageLines(page, 0);
    const first = await lines.nth(1).boundingBox();
    const last = await lines.nth(2).boundingBox();
    if (first === null || last === null) throw new Error('page 1 has no text lines to select');
    const all = await lines.allTextContents();
    expect(all[1]?.trim(), 'second line has text').not.toBe('');
    expect(all[2]?.trim(), 'third line has text').not.toBe('');
    await page.mouse.move(first.x + 1, first.y + first.height / 2);
    await page.mouse.down();
    await page.mouse.move(last.x + last.width + 4, last.y + last.height / 2, { steps: 10 });
    await page.mouse.up();

    await page.keyboard.press('Control+C');
    await expect(page.getByText('Copied to clipboard.', { exact: true })).toBeVisible();
    const written = await copied(page);
    expect(written).toHaveLength(1);
    expect(written[0]).toBe(`${all[1]}\n${all[2]}`);

    expect(problems, 'CSP or page errors').toEqual([]);
  });

  test('Select All then Copy: the text of every page is copied in reading order', async ({
    page,
  }) => {
    const problems = watchPage(page);
    await captureClipboard(page);
    await openApp(page);
    await openPdf(page, 'text_multipage.pdf', fixture('text_multipage.pdf'));

    // Ctrl+A selects the text of the whole document (W5.4), and Ctrl+C copies it in slices. The
    // pages are not all mounted, so the check is on the order of their headings, not on every line.
    expect(await lineTexts(pageLines(page, 0))).toContain('Page 1 heading');
    await page.locator('[data-page="0"]').click({ position: { x: 5, y: 5 } });
    await page.keyboard.press('Control+A');
    // Select All reads the last page's text before it reports, so Copy waits for that toast.
    await expect(
      page.getByText('Selected the text of all 5 pages.', { exact: true }),
    ).toBeVisible();
    await page.keyboard.press('Control+C');

    await expect(page.getByText('Copied to clipboard.', { exact: true })).toBeVisible();
    const written = await copied(page);
    expect(written).toHaveLength(1);
    let at = -1;
    for (let index = 1; index <= 5; index += 1) {
      const next = written[0]?.indexOf(`Page ${index} heading`, at + 1) ?? -1;
      expect(next, `Page ${index} heading after the previous page`).toBeGreaterThan(at);
      at = next;
    }

    expect(problems, 'CSP or page errors').toEqual([]);
  });
});
