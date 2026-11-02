import { defineConfig } from '@playwright/test';

// Browser tests against a running stack (`docker compose up`), never a shared deployment.
// VOXA_APP_URL points at the console; the gateway URL is whatever the frontend was built with.
// Tests run one at a time: guest sign-ups and logins are rate-limited per client IP.
export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  workers: 1,
  retries: 0,
  reporter: [['list'], ['junit', { outputFile: 'test-results/junit.xml' }]],
  use: {
    baseURL: process.env.VOXA_APP_URL ?? 'http://localhost:3000',
    // The installed Chrome, so no browser download is needed; PLAYWRIGHT_CHANNEL=chromium uses Playwright's own.
    channel: process.env.PLAYWRIGHT_CHANNEL ?? 'chrome',
    trace: 'retain-on-failure',
  },
});
