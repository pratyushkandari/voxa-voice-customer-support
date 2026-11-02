import { expect, test } from '@playwright/test';
import { alertWith, ask, startSession, trackErrors } from './helpers';

test('a typed question is answered with the order facts and measured latency', async ({ page }) => {
  const errors = trackErrors(page);
  await startSession(page);

  const reply = await ask(page, 'Where is my order ORD-88412?');

  expect(reply).toContain('TRK-99281744US');
  expect(reply).toMatch(/TTFT: \d+ms/);
  expect(errors).toEqual([]);
});

test('the quick actions answer order, refund, policy and escalation questions', async ({ page }) => {
  await startSession(page);
  const sidebar = page.locator('aside');
  const finished = page.getByRole('button', { name: 'Copy response' });

  const labels = ['Track Order: ORD-88412', 'Check Refund: ORD-10294', 'Warranty & Return Terms', 'Escalate to Specialist'];
  for (let i = 0; i < labels.length; i++) {
    await sidebar.getByRole('button', { name: labels[i] }).click();
    await expect(finished).toHaveCount(i + 1);
  }

  const log = page.locator('[role=log]');
  await expect(log).toContainText('TRK-99281744US');
  await expect(log).toContainText('ORD-10294');
  await expect(log).toContainText(/#[0-9a-f]{8}/);
});

test('the message box enforces its rules', async ({ page }) => {
  await startSession(page);
  const input = page.locator('#chat-input');
  const send = page.getByRole('button', { name: 'Send message' });

  await expect(send).toBeDisabled();
  await input.fill('   ');
  await expect(send).toBeDisabled();
  await expect(input).toHaveAttribute('maxlength', '2000');
  await input.fill('');
  await input.pressSequentially('x'.repeat(5));
  await expect(send).toBeEnabled();
});

test('markup in a message is shown as text and never runs', async ({ page }) => {
  const dialogs: string[] = [];
  page.on('dialog', (dialog) => {
    dialogs.push(dialog.message());
    void dialog.dismiss();
  });
  await startSession(page);

  await ask(page, '<img src=x onerror=alert("xss")>');

  await expect(page.locator('[role=log]')).toContainText('<img src=x onerror=alert("xss")>');
  expect(dialogs).toEqual([]);
});

test('the telemetry panel shows the measurements of the last answer', async ({ page }) => {
  await startSession(page);
  await ask(page, 'What is your return policy?');

  await page.getByTitle('View latency telemetry for the latest response').click();
  const panel = page.locator('#telemetry-popover');
  await expect(panel).toContainText(/Time to First Token:\s*\d+ ms/);
  await expect(panel).toContainText('Correlation Trace ID:');
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
});

test('the console shows a banner when the connection to the gateway drops', async ({ page }) => {
  // The gateway closes every WebSocket at once, as when it restarts.
  await page.routeWebSocket(/\/v1\/ws\/stream/, (ws) => ws.close());
  await page.goto('/chat');

  await expect(page.getByText('Connection to the support gateway was lost. Reconnecting automatically')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send message' })).toBeDisabled();
});

test('a blocked microphone shows how to fix it', async ({ page }) => {
  // No microphone permission is granted to this page.
  await startSession(page);

  await page.locator('form').getByRole('button', { name: 'Record a voice message' }).click();

  await expect(alertWith(page, 'Microphone access is blocked')).toBeVisible();
});

test.describe('on a phone-sized screen', () => {
  test.use({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true });

  test('nothing scrolls sideways and the chat still works', async ({ page }) => {
    await startSession(page);

    const chips = page.getByRole('group', { name: 'Frequent support inquiries' }).getByRole('button');
    await expect(chips).toHaveCount(4);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(await ask(page, 'Where is ORD-88412?')).toContain('TRK-99281744US');

    for (const path of ['/', '/admin']) {
      await page.goto(path);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });
});
