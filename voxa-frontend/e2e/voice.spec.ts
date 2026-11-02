import path from 'path';
import { expect, test } from '@playwright/test';
import { startSession, trackErrors } from './helpers';

// Chrome's fake capture device plays a recorded WAV ("Where is my order ORD-88412?") as the
// microphone. This exercises recording, Whisper, the answer and playback, but it is not a real
// microphone: that is a manual check (docs/TESTING.md).
const CLIP = path.resolve(__dirname, '../../voxa-backend/tests/fixtures/order_question.wav');

test.use({
  permissions: ['microphone'],
  launchOptions: {
    args: [
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
      `--use-file-for-fake-audio-capture=${CLIP}`,
      '--autoplay-policy=no-user-gesture-required',
    ],
  },
});

test('a recorded question is transcribed, answered and spoken', async ({ page }) => {
  const errors = trackErrors(page);
  // Counts calls to play() on any audio element, to see that the spoken reply starts.
  await page.addInitScript(() => {
    const w = window as unknown as { __plays: number };
    w.__plays = 0;
    const play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
      w.__plays += 1;
      return play.call(this);
    };
  });
  await startSession(page);
  const mic = page.locator('form').getByRole('button', { name: 'Record a voice message' });

  await mic.click();
  await expect(page.locator('form').getByRole('button', { name: 'Stop recording and send' })).toBeVisible();
  await page.waitForTimeout(4500);
  await page.locator('form').getByRole('button', { name: 'Stop recording and send' }).click();

  const log = page.locator('[role=log]');
  await expect(log).toContainText('Transcribing');
  await expect(log).toContainText(/88412/, { timeout: 60_000 });
  await expect(log).toContainText('TRK-99281744US', { timeout: 60_000 });
  await expect(log).not.toContainText('Transcribing');
  await page.waitForFunction(() => (window as unknown as { __plays: number }).__plays > 0, null, { timeout: 60_000 });
  expect(errors).toEqual([]);
});
