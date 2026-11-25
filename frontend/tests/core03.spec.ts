import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const BASE = 'http://127.0.0.1:4199';

async function state(page: Page): Promise<Record<string, unknown>> {
  const response = await page.request.get(`${BASE}/api/admin/station/state`);
  return (await response.json()) as Record<string, unknown>;
}

async function playlists(page: Page): Promise<Array<Record<string, unknown>>> {
  const response = await page.request.get(`${BASE}/api/admin/playlists`);
  const body = (await response.json()) as { items: Array<Record<string, unknown>> };
  return body.items;
}

async function reset(page: Page): Promise<void> {
  await page.request.get(`${BASE}/api/mock/program?playlist=p1&mode=playlist`);
  await page.request.get(`${BASE}/api/mock/station?desired=playing`);
  await page.request.get(`${BASE}/api/mock/sync-pending?on=0`);
  await page.request.get(`${BASE}/api/mock/delay?ms=0`);
  await page.request.get(`${BASE}/api/mock/rotation-repeat?on=0`);
}

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ context, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
});

test('overview source control, real override, queue editing and one-shot shuffle', async ({ page }) => {
  await reset(page);
  await page.goto('/admin');
  await expect(page.locator('.source-control')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Обновить', exact: true })).toHaveCount(0);
  await expect(page.getByText('Движок сообщает текущий трек')).toHaveCount(0);

  await page.locator('.segmented').getByRole('button', { name: 'Автоматически' }).click();
  await expect(page.locator('.source-control__effective')).toContainText('Ротация');
  expect((await state(page)).mode).toBe('auto');

  await page.locator('.source-control select').selectOption('p2');
  await expect.poll(async () => (await state(page)).mode).toBe('playlist');
  await expect.poll(async () => (await state(page)).selected_playlist_id).toBe('p2');

  await page.getByRole('button', { name: 'Временное переключение' }).click();
  const temp = page.getByRole('dialog', { name: 'Временное переключение' });
  await expect(temp).toBeVisible();
  await temp.getByLabel('Плейлист').selectOption('p2');
  await temp.getByRole('button', { name: 'Включить' }).click();
  const banner = page.locator('.banner--accent');
  await expect(banner).toContainText('Временное переключение');
  await expect(banner.getByRole('button', { name: 'Вернуться к плейлисту' })).toBeVisible();
  await banner.getByRole('button', { name: 'Вернуться к плейлисту' }).click();
  await expect(banner).toBeHidden();
  expect((await state(page)).override).toBeNull();

  await expect(page.getByRole('heading', { name: 'Следующие треки эфира' })).toBeVisible();
  await expect(page.locator('.queue-row--current')).toContainText('Сейчас');
  await expect(page.getByText('подготовлен', { exact: false })).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Перемешать' }),
  ).toHaveAttribute('title', 'Перемешать ближайшие автоматические треки один раз');

  const future = page.locator('.queue-row:not(.queue-row--current)');
  const secondTitle = await future.nth(1).locator('.queue-row__title').textContent();
  await future.nth(1).getByRole('button', { name: 'Поставить следующим' }).click();
  await expect.poll(async () => {
    const response = await page.request.get(`${BASE}/api/admin/queue`);
    const body = (await response.json()) as { items: Array<{ track: { title: string } }> };
    return body.items[0]?.track.title;
  }).toBe(secondTitle ?? '');
});

test('playlist page distinguishes active, pending, selected and in-flight without false success', async ({ page }) => {
  await reset(page);
  await page.request.post(`${BASE}/api/admin/playlists/p2/tracks`, {
    data: { track_ids: ['t6', 't7'] },
  });
  await page.goto('/admin/playlists/p1');
  await expect(page.locator('.playlist-live-marker')).toContainText('Сейчас в эфире');

  await page.goto('/admin/playlists/p2');
  await expect(page.locator('.playlist-live-marker')).toHaveCount(0);
  await page.getByRole('button', { name: 'Варианты запуска плейлиста' }).click();
  await page.getByRole('menuitem', { name: 'После текущего трека' }).click();
  await expect(page.locator('.playlist-switch-note')).toContainText('После текущего трека');
  expect((await state(page)).pending_source).not.toBeNull();
  await page.request.get(`${BASE}/api/mock/advance`);
  await expect(page.locator('.playlist-live-marker')).toContainText('Сейчас в эфире');
  await expect(page.locator('.playlist-switch-note')).toHaveCount(0);

  // Нижние настройки перешли на подтверждённый новый источник.
  await expect(
    page.locator('.player-bar .icon-btn[aria-label*="Утренний свет"]').first(),
  ).toBeVisible();

  // Ручная вставка не перехватывает управление.
  await page.request.post(`${BASE}/api/admin/tracks/t9/queue`);
  await expect(
    page.locator('.player-bar .icon-btn[aria-label*="Утренний свет"]').first(),
  ).toBeVisible();
  await page.request.post(`${BASE}/api/admin/tracks/t9/play-next`);
  await page.request.get(`${BASE}/api/mock/advance`);
  const live = (await (await page.request.get(`${BASE}/api/public/now-playing`)).json()) as {
    source: { kind: string; playlist_id: string | null };
  };
  expect(live.source.kind).toBe('manual');
  expect(live.source.playlist_id).toBeNull();
  await expect(
    page.locator('.player-bar .icon-btn[aria-label*="Утренний свет"]').first(),
  ).toBeVisible();

  // Остановленная станция: выбранный плейлист не зелёный.
  await page.request.get(`${BASE}/api/mock/station?desired=stopped`);
  await expect(page.locator('.playlist-live-marker')).toHaveCount(0);
  await expect(page.locator('.playlist-selected-marker')).toContainText('Выбран');
  await page.locator('.playlist-head__actions').getByRole('button', { name: 'В эфир' }).click();
  await expect(page.locator('.playlist-switch-note')).toContainText('Переключаем на');
  await expect(page.locator('.playlist-live-marker')).toContainText('Сейчас в эфире', {
    timeout: 10_000,
  });
});

test('bottom shuffle/repeat share stored settings with cards/pages, survive rapid toggles and respect rotation', async ({
  page,
  context,
}) => {
  await reset(page);
  const other = await context.newPage();
  await page.request.patch(`${BASE}/api/admin/playlists/p1`, {
    data: { shuffle: true, repeat_mode: 'all' },
  });
  await page.request.get(`${BASE}/api/mock/delay?ms=0`);
  await page.goto('/admin/playlists/p1');
  await other.goto('/admin/playlists');

  const bottom = page.locator('.player-bar');
  const shuffleButton = bottom.locator('.icon-btn[aria-label*="перемешивание"]');
  const repeatButton = bottom.locator('.pb-controls .icon-btn').nth(1);

  const initial = (await playlists(page)).find((item) => item.id === 'p1');
  expect(initial?.shuffle).toBe(true);
  await expect(shuffleButton).toHaveAttribute('aria-label', /Выключить перемешивание «Ночной сиреневый»/);
  await shuffleButton.click();
  await expect(shuffleButton).toHaveAttribute('aria-label', /Включить перемешивание «Ночной сиреневый»/);
  await expect
    .poll(async () => (await playlists(other)).find((item) => item.id === 'p1')?.shuffle)
    .toBe(false);
  await expect(
    other.locator('.playlist-card').filter({ hasText: 'Ночной сиреневый' }),
  ).not.toContainText('Перемешивание');

  // Repeat all → one → none.
  await expect(repeatButton).toHaveAttribute('aria-pressed', 'true');
  await repeatButton.click();
  await expect(repeatButton).toHaveAttribute('aria-label', /Повтор трека/);
  await expect
    .poll(async () => (await playlists(other)).find((item) => item.id === 'p1')?.repeat_mode)
    .toBe('one');
  await expect(
    other.locator('.playlist-card').filter({ hasText: 'Ночной сиреневый' }),
  ).toContainText('Повтор трека');
  await repeatButton.click();
  await expect(repeatButton).toHaveAttribute('aria-label', /Без повтора/);
  await expect
    .poll(async () => (await playlists(other)).find((item) => item.id === 'p1')?.repeat_mode)
    .toBe('none');

  // Delayed HTTP + rapid toggles: последний клик побеждает.
  await page.request.get(`${BASE}/api/mock/delay?ms=600`);
  await shuffleButton.click();
  await shuffleButton.click();
  await page.waitForTimeout(1600);
  expect((await playlists(page)).find((item) => item.id === 'p1')?.shuffle).toBe(false);
  await expect(shuffleButton).toHaveAttribute('aria-label', /Включить перемешивание/);

  await repeatButton.click();
  await repeatButton.click();
  await page.waitForTimeout(1600);
  expect((await playlists(page)).find((item) => item.id === 'p1')?.repeat_mode).toBe('one');
  await expect(repeatButton).toHaveAttribute('aria-label', /Повтор трека/);
  await page.request.get(`${BASE}/api/mock/delay?ms=0`);

  // Shuffle и repeat не перетирают друг друга.
  await shuffleButton.click();
  await repeatButton.click();
  await page.waitForTimeout(800);
  const both = (await playlists(page)).find((item) => item.id === 'p1');
  expect(both?.shuffle).toBe(true);
  expect(both?.repeat_mode).toBe('none');

  // Rotация управляет повтором: нижняя кнопка disabled, страница редактирует.
  await page.request.get(`${BASE}/api/mock/program?playlist=p3&mode=playlist`);
  await expect(repeatButton).toBeDisabled();
  await expect(repeatButton).toHaveAttribute('title', 'Повтор определяется ротацией');
  await page.goto('/admin/playlists/p3');
  const pageRepeat = page
    .locator('.playlist-head__actions')
    .getByRole('button', { name: /Повтор/ });
  await expect(pageRepeat).toBeEnabled();

  // Repeat One: разовое перемешивание очереди недоступно и не выключает Repeat One.
  await page.request.patch(`${BASE}/api/admin/playlists/p1`, { data: { repeat_mode: 'one' } });
  await page.request.get(`${BASE}/api/mock/program?playlist=p1&mode=playlist`);
  await page.goto('/admin');
  const shuffleQueue = page.getByRole('button', { name: 'Перемешать' });
  await expect(shuffleQueue).toBeDisabled();
  await expect(shuffleQueue).toHaveAttribute(
    'title',
    'При повторе трека разовое перемешивание недоступно',
  );
  expect((await playlists(page)).find((item) => item.id === 'p1')?.repeat_mode).toBe('one');
  await other.close();
});

test('pending playlist setting is shown as saved/applying, not as applied', async ({ page }) => {
  await reset(page);
  await page.request.get(`${BASE}/api/mock/sync-pending?on=1`);
  await page.goto('/admin/playlists/p1');
  const repeat = page.locator('.player-bar .pb-controls .icon-btn').nth(1);
  await repeat.click();
  await expect(page.locator('.playlist-sync-note')).toContainText('Настройка сохранена, применяется');
  await page.request.get(`${BASE}/api/mock/sync-complete`);
  await expect(page.locator('.playlist-sync-note')).toBeHidden({ timeout: 10_000 });
  await expect(repeat).toHaveAttribute('aria-label', /Повтор трека/);
});

test('shared track menu exists everywhere, row play works and delete offers after/now', async ({
  page,
}) => {
  await reset(page);
  await page.request.get(`${BASE}/api/mock/program?playlist=p1&mode=playlist`);
  await page.goto('/admin/library');
  await page.getByLabel('Поиск по медиатеке').fill('Ночной трамвай');
  await expect(page.locator('.track-table tbody tr')).toHaveCount(1);
  await page.locator('.track-table tbody tr').first().locator('.menu .icon-btn').click();
  for (const label of [
    'Воспроизвести сейчас',
    'Воспроизвести следующим',
    'Добавить в очередь',
    'Добавить в плейлист',
    'Изменить трек',
    'Скачать трек',
    'Скачать оригинал',
    'Удалить из медиатеки',
  ]) {
    await expect(page.getByRole('menuitem', { name: label })).toBeVisible();
  }
  await page.keyboard.press('Escape');

  await page.goto('/admin/playlists/library');
  await page.locator('.playlist-row:not(.playlist-row--head)').first().locator('.menu .icon-btn').click();
  await expect(page.getByRole('menuitem', { name: 'Убрать из плейлиста' })).toHaveCount(0);
  await expect(page.getByRole('menuitem', { name: 'Удалить из медиатеки' })).toBeVisible();
  await page.keyboard.press('Escape');

  await page.goto('/admin/playlists/p1');
  const row = page.locator('.playlist-row:not(.playlist-row--head)').first();
  await row.hover();
  const rowPlay = row.getByRole('button', { name: /Воспроизвести в эфире/ });
  await expect(rowPlay).toBeVisible();
  await rowPlay.click();
  await expect.poll(async () => {
    const response = await page.request.get(`${BASE}/api/admin/queue`);
    const body = (await response.json()) as { current: { track: { id: string } } };
    return body.current.track.id;
  }).toBe('t1');

  // Удаление: текущий трек, режим «Сразу».
  await page.goto('/admin/library');
  await page.getByLabel('Поиск по медиатеке').fill('Ночной трамвай');
  await page.locator('.track-table tbody tr').first().locator('.menu .icon-btn').click();
  await page.getByRole('menuitem', { name: 'Удалить из медиатеки' }).click();
  const dialog = page.getByRole('dialog', { name: 'Удалить трек из медиатеки?' });
  await expect(dialog).toContainText('Ночной трамвай');
  await expect(dialog).toContainText('Сейчас в эфире');
  await expect(dialog).toContainText('Входит в 1 плейлист');
  await expect(dialog.getByRole('radio', { name: /После завершения трека/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await dialog.getByRole('radio', { name: /Сразу/ }).click();
  await dialog.getByRole('button', { name: 'Удалить' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator('.track-table tbody tr')).toHaveCount(0);
});
