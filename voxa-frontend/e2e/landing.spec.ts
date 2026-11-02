import { expect, test } from '@playwright/test';
import { alertWith, trackErrors } from './helpers';

test('every page loads without console errors', async ({ page }) => {
  const errors = trackErrors(page);
  for (const [path, heading] of [
    ['/', 'Customer Support by'],
    ['/chat', 'Support Session Console'],
    ['/admin', 'Operations & Telemetry Console'],
  ]) {
    await page.goto(path);
    await expect(page.getByText(heading).first()).toBeVisible();
    await page.waitForLoadState('networkidle');
  }
  expect(errors).toEqual([]);
});

test('the landing page shows the gateway as operational and starts a guest session', async ({ page }) => {
  const errors = trackErrors(page);
  await page.goto('/');
  await expect(page.getByText('Gateway Status: Operational')).toBeVisible();

  await page.getByRole('button', { name: 'Start Live Support Session' }).click();
  await page.waitForURL('**/chat');
  await expect(page.locator('header').getByText('Live', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('voxa_token'))).toBeTruthy();
  expect(errors).toEqual([]);
});

test('the landing page explains when the gateway cannot be reached', async ({ page }) => {
  // Requests to the gateway are dropped in the browser, as when it is down or the URL is wrong.
  await page.route('**/health', (route) => route.abort());
  await page.route('**/v1/auth/guest-demo', (route) => route.abort());
  await page.goto('/');

  await expect(page.getByText('Gateway Status: Unreachable')).toBeVisible();
  await page.getByRole('button', { name: 'Start Live Support Session' }).click();
  await expect(alertWith(page, 'Cannot reach the support gateway at')).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
});
