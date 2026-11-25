import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const BASE = 'http://127.0.0.1:4199';

async function playlistInfo(page: Page, id: string): Promise<{ track_count: number }> {
  const response = await page.request.get(`${BASE}/api/admin/playlists/${id}`);
  const body = (await response.json()) as { playlist: { track_count: number } };
  return body.playlist;
}

async function addTrackFromDialog(page: Page, titles: string[]): Promise<void> {
  await page.getByRole('button', { name: 'Добавить треки' }).first().click();
  const dialog = page.getByRole('dialog', { name: 'Добавить треки в плейлист' });
  await expect(dialog).toBeVisible();
  for (const title of titles) {
    await dialog.getByLabel('Поиск треков').fill(title);
    const row = dialog.locator('.pick-row').filter({ hasText: title }).first();
    await expect(row).toBeVisible();
    await expect(row).toBeEnabled();
    await row.click();
  }
  await dialog.getByRole('button', { name: /^Добавить/ }).click();
  await expect(dialog).toBeHidden();
}

async function reset(page: Page): Promise<void> {
  await page.request.get(`${BASE}/api/mock/program?playlist=p1&mode=playlist`);
  await page.request.get(`${BASE}/api/mock/station?desired=playing`);
  await page.request.get(`${BASE}/api/mock/delay?ms=0`);
  await page.request.get(`${BASE}/api/mock/sync-pending?on=0`);
}

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ context, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
});

test('added tracks update active playlist, card, queue and report sync honestly', async ({ page }) => {
  await reset(page);
  await page.goto('/admin/playlists/p1');
  const before = await playlistInfo(page, 'p1');
  await page.evaluate(() => {
    (window as unknown as Record<string, unknown>).__vdAudio = document.querySelector(
      '[data-local-audio]',
    );
  });

  await addTrackFromDialog(page, ['Стеклянный берег']);
  await expect(page.getByText('Добавлено треков: 1')).toBeVisible();
  await expect(page.getByText('Новые треки включены в текущий цикл')).toBeVisible({
    timeout: 10_000,
  });
  await expect.poll(async () => (await playlistInfo(page, 'p1')).track_count).toBe(
    before.track_count + 1,
  );
  await expect(page.locator('.playlist-head__info')).toContainText(
    `${before.track_count + 1}`,
  );
  expect(
    await page.evaluate(
      () =>
        (window as unknown as Record<string, unknown>).__vdAudio ===
        document.querySelector('[data-local-audio]'),
    ),
  ).toBe(true);

  // Карточка обновляется без выхода из раздела и без F5.
  await page.goto('/admin/playlists');
  await expect(
    page.locator('.playlist-card').filter({ hasText: 'Ночной сиреневый' }),
  ).toContainText(String(before.track_count + 1));

  // Новая позиция появилась в ближайшей очереди.
  await page.goto('/admin');
  await expect(page.getByText('Стеклянный берег').first()).toBeVisible();

  // Неактивный плейлист: только сохранение, без обещаний эфира.
  await page.waitForTimeout(4200);
  await page.goto('/admin/playlists/p2');
  await addTrackFromDialog(page, ['Тихий трек 009']);
  await expect(page.getByText('Добавлено треков: 1')).toBeVisible();
  await page.waitForTimeout(800);
  await expect(page.getByText('Новые треки включены в текущий цикл')).toHaveCount(0);
});

test('re-adding existing tracks is honest and does not reset the queue view', async ({ page }) => {
  await reset(page);
  await page.goto('/admin/library');
  await page.getByLabel('Поиск по медиатеке').fill('Тихий сигнал');
  await expect(page.locator('.track-table tbody tr')).toHaveCount(1);
  await page.locator('.track-table tbody tr').first().locator('input[type="checkbox"]').check();
  await page.locator('.bulk-bar').getByRole('button', { name: 'В плейлист' }).click();
  const dialog = page.getByRole('dialog', { name: 'Добавить в плейлист' });
  await dialog.getByLabel('Плейлист').selectOption('p1');
  await dialog.getByRole('button', { name: 'Добавить' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText('Все выбранные треки уже в плейлисте')).toBeVisible();
});

test('pending engine sync is shown and resolved automatically', async ({ page }) => {
  await reset(page);
  await page.request.get(`${BASE}/api/mock/sync-pending?on=1`);
  await page.goto('/admin/playlists/p1');
  await addTrackFromDialog(page, ['Тихий трек 008']);
  await expect(page.getByText('Добавлено треков: 1')).toBeVisible();
  await expect(page.locator('.playlist-sync-note')).toContainText('Очередь обновится');
  await page.request.get(`${BASE}/api/mock/sync-complete`);
  await expect(page.getByText('Новые треки включены в текущий цикл')).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.locator('.playlist-sync-note')).toBeHidden();
});

test('an unconfirmed engine sync keeps the honest pending note and no success toast', async ({
  page,
}) => {
  await reset(page);
  await page.request.get(`${BASE}/api/mock/sync-pending?on=1`);
  await page.goto('/admin/playlists/p1');
  await addTrackFromDialog(page, ['Сиреневый дождь']);
  await expect(page.getByText('Добавлено треков: 1')).toBeVisible();
  await expect(page.locator('.playlist-sync-note')).toContainText('Очередь обновится');
  await page.waitForTimeout(3500);
  await expect(page.locator('.playlist-sync-note')).toContainText('Очередь обновится');
  await expect(page.getByText('Новые треки включены в текущий цикл')).toHaveCount(0);
});

test('a small batch keeps the player instance and updates the composition', async ({ page }) => {
  await reset(page);
  await page.goto('/admin/playlists/p1');
  await page.evaluate(() => {
    (window as unknown as Record<string, unknown>).__vdAudio2 = document.querySelector(
      '[data-local-audio]',
    );
  });
  const before = await playlistInfo(page, 'p1');
  await addTrackFromDialog(page, ['Сирень в апреле', 'Сиреневый дождь']);
  await expect(page.getByText(/Добавлено треков: [12]/)).toBeVisible();
  await expect.poll(async () => (await playlistInfo(page, 'p1')).track_count).toBeGreaterThan(
    before.track_count,
  );
  expect(
    await page.evaluate(
      () =>
        (window as unknown as Record<string, unknown>).__vdAudio2 ===
        document.querySelector('[data-local-audio]'),
    ),
  ).toBe(true);
  await expect(page.locator('.player-bar')).toBeVisible();
});
