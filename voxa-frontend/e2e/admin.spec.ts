import { expect, test, type Page } from '@playwright/test';
import { ADMIN_PASSWORD, alertWith, trackErrors } from './helpers';

async function signIn(page: Page, email: string, password: string) {
  await page.goto('/admin');
  await page.locator('#admin-email').fill(email);
  await page.locator('#admin-password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

test('a wrong password is refused with a clear message', async ({ page }) => {
  await signIn(page, 'admin@voxa.ai', 'wrong-password');
  await expect(alertWith(page, 'Invalid email or password.')).toBeVisible();
});

test('an account without the admin role is refused', async ({ page }) => {
  await signIn(page, 'demo@voxa.ai', 'demopassword123'); // seeded demo user
  await expect(alertWith(page, 'This account does not have admin access.')).toBeVisible();
});

test('the sign-in form requires an email and a password', async ({ page }) => {
  await page.goto('/admin');
  await page.locator('#admin-email').fill('');
  await page.getByRole('button', { name: 'Sign in' }).click();
  expect(await page.locator('#admin-email').evaluate((el: HTMLInputElement) => el.validity.valueMissing)).toBe(true);
  expect(await page.locator('#admin-password').evaluate((el: HTMLInputElement) => el.validity.valueMissing)).toBe(true);
});

test('the admin signs in, sees metrics and tickets, refreshes and signs out', async ({ page }) => {
  const errors = trackErrors(page);
  await signIn(page, 'admin@voxa.ai', ADMIN_PASSWORD);

  await expect(page.getByText('Average TTFT')).toBeVisible();
  await expect(page.getByText('Cache Hit Ratio')).toBeVisible();
  await expect(page.getByText('Support Escalation Queue')).toBeVisible();
  await expect(page.getByText(/Workers Reporting|No Worker Heartbeats/)).toBeVisible();

  await page.getByRole('button', { name: 'Refresh metrics' }).click();
  await expect(page.getByText(/^Updated /)).toBeVisible();

  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByText('Admin sign-in required')).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('voxa_admin_token'))).toBeNull();
  expect(errors).toEqual([]);
});
