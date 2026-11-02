import { expect, type Page } from '@playwright/test';

export const ADMIN_PASSWORD = process.env.VOXA_E2E_ADMIN_PASSWORD ?? 'adminpassword123';

/** Collects console errors and uncaught exceptions for the page. */
export function trackErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console: ${msg.text()}`);
  });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  return errors;
}

/** The app's alert with this text (Next.js also renders an empty role="alert" route announcer). */
export function alertWith(page: Page, text: string) {
  return page.getByRole('alert').filter({ hasText: text });
}

/** Starts a guest session from the landing page and waits until the console is connected. */
export async function startSession(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Start Live Support Session' }).click();
  await page.waitForURL('**/chat');
  await expect(page.locator('header').getByText('Live', { exact: true })).toBeVisible();
}

/** Sends a typed question and returns the text of the reply bubble once it is complete. */
export async function ask(page: Page, question: string): Promise<string> {
  const finished = page.getByRole('button', { name: 'Copy response' });
  const before = await finished.count();
  await page.locator('#chat-input').fill(question);
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(finished).toHaveCount(before + 1);
  const bubbles = page.locator('[role=log] > div').filter({ has: page.getByRole('button', { name: 'Copy response' }) });
  return (await bubbles.last().innerText()).trim();
}
