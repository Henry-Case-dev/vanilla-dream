import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:4199';

const LINKS_HEADING = 'Прямые ссылки для интернет-радиоплееров и совместимых приложений';

const HISTORY_FIXTURE = [
  {
    id: 'near-midnight',
    track_id: 't1',
    title: 'Ночной трамвай',
    artist: 'Vanilla Dream',
    album: 'Ночные волны',
    started_at: '2026-11-26T23:30:00Z',
    ended_at: null,
    source_kind: 'library',
    source_ref: 'library',
  },
  {
    id: 'summer',
    track_id: 't2',
    title: 'Тихий сигнал',
    artist: 'Lumen',
    album: 'Северное сияние',
    started_at: '2026-07-15T09:40:00Z',
    ended_at: '2026-07-15T09:44:00Z',
    source_kind: 'library',
    source_ref: 'library',
  },
  {
    id: 'winter',
    track_id: 't3',
    title: 'Полночь на кухне',
    artist: 'Медленные письма',
    album: 'Ночные волны',
    started_at: '2026-01-15T09:40:00Z',
    ended_at: '2026-01-15T09:44:00Z',
    source_kind: 'library',
    source_ref: 'library',
  },
];

function localTime(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone,
  }).format(new Date(iso));
}

for (const timeZone of ['UTC', 'Europe/Moscow', 'Europe/Amsterdam', 'Asia/Almaty', 'Asia/Kamchatka']) {
  test(`public history renders in the visitor timezone ${timeZone}`, async ({ browser, request }) => {
    await request.get(`${BASE}/api/mock/reset`);
    const context = await browser.newContext({ timezoneId: timeZone });
    const page = await context.newPage();
    await page.route('**/api/public/history*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: HISTORY_FIXTURE }),
      }),
    );
    await page.goto(`${BASE}/`);
    await expect(page.locator('.public-history__row')).toHaveCount(HISTORY_FIXTURE.length);
    const rendered = await page.locator('.public-history__time').allTextContents();
    expect(rendered).toEqual(HISTORY_FIXTURE.map((entry) => localTime(entry.started_at, timeZone)));

    // Полночь и сезонные смещения проверяются на одних и тех же абсолютных метках.
    if (timeZone === 'UTC') expect(rendered[0]).toBe('23:30');
    if (timeZone === 'Europe/Moscow') expect(rendered[0]).toBe('02:30');
    if (timeZone === 'Asia/Kamchatka') expect(rendered[0]).toBe('11:30');
    if (timeZone === 'Europe/Amsterdam') {
      expect(rendered[1]).toBe('11:40');
      expect(rendered[2]).toBe('10:40');
    }
    await context.close();
  });
}

test('history updates on a real track start without reloading the page', async ({ browser, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  const context = await browser.newContext({ timezoneId: 'UTC' });
  const page = await context.newPage();
  await page.goto(`${BASE}/`);
  await expect(page.locator('.public-history__row').first()).toContainText('Ночной трамвай');
  await page.evaluate(() => {
    (window as unknown as { __vdPublicNoReload?: boolean }).__vdPublicNoReload = true;
  });

  await request.get(`${BASE}/api/mock/advance`);
  const first = page.locator('.public-history__row').first();
  await expect(first.locator('.public-history__title')).toHaveText('Тихий сигнал', { timeout: 8000 });
  const nowPlaying = (await (await request.get(`${BASE}/api/public/now-playing`)).json()) as {
    track: { started_at: string };
  };
  await expect(first.locator('.public-history__time')).toHaveText(localTime(nowPlaying.track.started_at, 'UTC'));

  const stillSameDocument = await page.evaluate(
    () => (window as unknown as { __vdPublicNoReload?: boolean }).__vdPublicNoReload === true,
  );
  expect(stillSameDocument).toBe(true);
  await context.close();
});

test('public page drops local-playback wording and the top admin link', async ({ page, context, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  await context.clearCookies();
  await page.goto(`${BASE}/`);
  await expect(page.locator('.public-hero__title')).toBeVisible();
  await expect(page.locator('.public-hero__controls-title')).toHaveText('Слушать эфир');
  await expect(page.locator('.public__top')).not.toContainText('Вход в панель');
  await expect(page.locator('.public__top')).toContainText('В эфире');

  await expect(page.getByText('Звук только в вашем браузере')).toHaveCount(0);
  await expect(page.getByText('Играет локально')).toHaveCount(0);
  await expect(page.locator('.public-hero__cover')).not.toContainText('локально');
  await expect(page.getByText('Публичный адрес станции')).toHaveCount(0);
  await expect(page.getByText('Последние треки и время выхода')).toHaveCount(0);

  const heading = page.locator('#links-title');
  await expect(heading).toHaveCount(1);
  await expect(heading).toHaveText(LINKS_HEADING);
  await expect(page.locator('.link-row')).toHaveCount(3);
  await expect(page.locator('.link-row--primary .link-row__hint')).toHaveCount(0);
  await expect(page.locator('.link-row__hint')).toHaveCount(2);
  await expect(page.getByRole('heading', { name: 'Недавно в эфире' })).toBeVisible();

  const footer = page.locator('.public__footer');
  await expect(footer.locator('span').first()).toHaveText('Vanilla Dream Radio');
  await expect(footer).not.toContainText('обновляется автоматически');
  await expect(footer).not.toContainText('обновление опросом');
  const adminLink = page.getByRole('link', { name: 'Панель управления' });
  await expect(adminLink).toBeVisible();
  await adminLink.focus();
  await expect(adminLink).toBeFocused();
});

test('footer admin entry leads to the existing login', async ({ page, context, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  await context.clearCookies();
  await page.goto(`${BASE}/`);
  await page.getByRole('link', { name: 'Панель управления' }).click();
  await page.waitForURL('**/admin/login');
  await expect(page.getByRole('heading', { name: /панель/i })).toBeVisible();
});

test('no top admin link is rendered while the stream state is loading or unavailable', async ({ browser, request }) => {
  await request.get(`${BASE}/api/mock/reset`);

  const loadingContext = await browser.newContext();
  const loadingPage = await loadingContext.newPage();
  await loadingPage.route('**/api/public/now-playing', () => new Promise(() => undefined));
  await loadingPage.route('**/api/public/events', (route) => route.abort());
  await loadingPage.goto(`${BASE}/`);
  await expect(loadingPage.locator('.public__top')).toBeVisible();
  await expect(loadingPage.locator('.public__top')).not.toContainText('Вход в панель');
  await expect(loadingPage.getByText('Подключаемся к эфиру…')).toBeVisible();
  await loadingContext.close();

  const errorContext = await browser.newContext();
  const errorPage = await errorContext.newPage();
  await errorPage.route('**/api/public/now-playing', (route) => route.abort());
  await errorPage.route('**/api/public/events', (route) => route.abort());
  await errorPage.goto(`${BASE}/`);
  await expect(errorPage.getByText('Эфир временно недоступен')).toBeVisible();
  await expect(errorPage.locator('.public__top')).not.toContainText('Вход в панель');
  await errorContext.close();
});

test('public player shows the listening label and a stop tooltip while playing', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(HTMLMediaElement.prototype, 'paused', { configurable: true, get: () => false });
    Object.defineProperty(HTMLMediaElement.prototype, 'readyState', { configurable: true, get: () => 4 });
    HTMLMediaElement.prototype.play = () => Promise.resolve();
    HTMLMediaElement.prototype.pause = () => undefined;
  });
  await page.goto(`${BASE}/`);
  const fab = page.locator('.play-fab');
  await fab.click();
  await expect(page.locator('.public-hero__controls-title')).toHaveText('Слушаете эфир');
  await expect(fab).toHaveAttribute('aria-label', 'Остановить прослушивание');
  await expect(fab).toHaveAttribute('title', 'Остановить прослушивание');
  await expect(page.locator('.public__footer-icon')).toHaveAttribute('title', 'Остановить прослушивание');
  await fab.click();
  await expect(page.locator('.public-hero__controls-title')).toHaveText('Слушать эфир');
  await expect(fab).toHaveAttribute('title', 'Слушать эфир');
});

test('admin factual timestamps are local while the schedule keeps the station timezone', async ({
  browser,
  request,
}) => {
  await request.get(`${BASE}/api/mock/reset`);
  const context = await browser.newContext({ timezoneId: 'UTC' });
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
  const page = await context.newPage();

  await page.goto(`${BASE}/admin`);
  const historyTime = page.locator('.history-row:not(.history-row--head) .history-row__time').first();
  await expect(historyTime).toHaveText('04:00');
  await expect(page.locator('.meta-item').filter({ hasText: 'Начало:' })).toContainText('04:00');

  await page.goto(`${BASE}/admin/schedule`);
  await expect(page.locator('.schedule-subtitle')).toContainText('Время станции:');
  await expect(page.locator('.schedule-subtitle')).toContainText('Asia/Magadan');
  await context.close();
});
