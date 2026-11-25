import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:4199';

test.beforeEach(async ({ context, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
});

test('stale HTTP now-playing never overwrites a newer SSE revision', async ({ page }) => {
  await page.request.get(`${BASE}/api/mock/program?playlist=p1&mode=playlist`);
  await page.request.get(`${BASE}/api/mock/station?desired=playing`);
  const fresh = (await (await page.request.get(`${BASE}/api/public/now-playing`)).json()) as Record<
    string,
    unknown
  >;
  const old = JSON.parse(JSON.stringify(fresh)) as {
    station: Record<string, unknown>;
    track: Record<string, unknown> | null;
  };
  old.station.state_revision = 1;
  old.track = { ...(old.track ?? {}), id: 'old', title: 'СТАРЫЙ HTTP', artist: 'Old' };

  await page.route('**/api/public/now-playing', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(old) }),
  );
  await page.goto('/admin');
  await expect(page.locator('.np-hero__title')).toHaveText('Ночной трамвай');
  await page.evaluate(() => window.dispatchEvent(new Event('vd:data-changed')));
  await page.waitForTimeout(800);
  await expect(page.locator('.np-hero__title')).toHaveText('Ночной трамвай');
  await expect(page.locator('.np-hero__title')).not.toHaveText('СТАРЫЙ HTTP');
});

test('SSE down keeps polling as a fallback', async ({ page }) => {
  await page.route('**/api/public/events', (route) => route.abort());
  await page.goto('/');
  await expect(page.locator('.public-hero__title')).toBeVisible();
  await expect(page.locator('.public__footer')).toContainText('обновление опросом');
});

test('local player shows connecting/reconnecting, can cancel and never changes the global station', async ({
  page,
}) => {
  await page.request.get(`${BASE}/api/mock/station?desired=playing`);
  await page.goto('/admin');
  const local = page.locator('.pb-local .icon-btn').first();
  await expect(local).toHaveAttribute('title', /Слушать эфир локально/);
  await local.click();
  await expect(page.locator('.pb-local__label')).toContainText(/Подключение|Переподключение/, {
    timeout: 5000,
  });
  await expect(local).toHaveAttribute('title', 'Отменить подключение');
  await local.click();
  await expect(page.locator('.pb-local__label')).toHaveText('Локально');
  await expect(page.locator('.pb-air__text')).toHaveText('В эфире');
});

test('public player recovers on the same stream and offers cancel/reconnect', async ({ page }) => {
  await page.request.get(`${BASE}/api/mock/station?desired=playing`);
  await page.goto('/');
  const fab = page.locator('.play-fab');
  await fab.click();
  await expect(page.locator('.public-hero__controls-title')).toContainText(
    /Подключение|Переподключение|Буферизация/,
    { timeout: 5000 },
  );
  await fab.click();
  await expect(page.locator('.public-hero__controls-title')).toHaveText('Слушать эфир');
  await expect(page.locator('.public-hero__cover')).not.toContainText('локально');
});

test('queue revision refetches the nearest queue on a real track start', async ({ page }) => {
  await page.request.get(`${BASE}/api/mock/program?playlist=p1&mode=playlist`);
  await page.request.get(`${BASE}/api/mock/station?desired=playing`);
  await page.request.get(`${BASE}/api/mock/queue?ids=t2,t3,t4,t5`);
  await page.goto('/admin');
  await expect(page.locator('.queue-row--current')).toContainText('Ночной трамвай');

  // Реальный track-start: queue_revision меняется, без ожидания 30-секундного polling.
  await page.request.get(`${BASE}/api/mock/advance`);
  await expect(page.locator('.queue-row--current')).toContainText('Тихий сигнал', {
    timeout: 8000,
  });
  const snapshot = (await (await page.request.get(`${BASE}/api/admin/queue`)).json()) as {
    state_revision: number;
  };
  expect(snapshot.state_revision).toBeGreaterThan(0);
});

test('settings change in another tab does not steal focus or typing', async ({ page, context }) => {
  await page.request.get(`${BASE}/api/mock/program?playlist=p1&mode=playlist`);
  const other = await context.newPage();
  await page.goto('/admin/playlists/p1');
  await other.goto('/admin/playlists/p1');
  await page.locator('.playlist-head__cover-button').click();
  const dialog = page.getByRole('dialog', { name: 'Изменить плейлист' });
  const name = dialog.getByLabel('Название');
  await name.click();
  await page.keyboard.press('Control+A');
  await name.pressSequentially('Черновик без потери фокуса');
  await expect(name).toBeFocused();

  const shuffle = other.locator('.player-bar .icon-btn[aria-label*="перемешивание"]');
  await shuffle.click();
  await page.waitForTimeout(1500);
  await expect(name).toBeFocused();
  await expect(name).toHaveValue('Черновик без потери фокуса');
  await other.close();
});
