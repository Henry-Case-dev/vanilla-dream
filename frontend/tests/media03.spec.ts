import { test, expect } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

const BASE = 'http://127.0.0.1:4199';
const SHOT_DIR = 'C:/Users/main/AppData/Local/Temp/opencode/media03-shots';

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ context, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
});

function audioFile(name: string, content: string) {
  return { name, mimeType: 'audio/mpeg', buffer: Buffer.from(content) };
}

async function startUpload(
  page: Page,
  files: Array<{ name: string; mimeType: string; buffer: Buffer }>,
): Promise<Locator> {
  await page.goto('/admin/library');
  await page.getByRole('button', { name: 'Загрузить', exact: true }).click();
  const upload = page.getByRole('dialog', { name: 'Загрузка музыки' });
  await expect(upload).toBeVisible();
  await upload.getByLabel('Выбрать аудиофайлы для загрузки').setInputFiles(files);
  return upload;
}

async function openDecisions(
  page: Page,
  upload: Locator,
  expectedPending?: number,
): Promise<Locator> {
  if (expectedPending !== undefined) {
    await expect(upload.getByText(`Требуют решения: ${expectedPending}`)).toBeVisible({
      timeout: 20_000,
    });
  }
  const trigger = upload.getByRole('button', { name: /Просмотреть совпадения/ }).first();
  await expect(trigger).toBeVisible({ timeout: 20_000 });
  await trigger.click();
  const decisions = page.getByRole('dialog', { name: 'Обнаружены совпадения' });
  await expect(decisions).toBeVisible();
  return decisions;
}

async function closeOverlays(page: Page): Promise<void> {
  const decisions = page.getByRole('dialog', { name: 'Обнаружены совпадения' });
  if (await decisions.isVisible()) {
    await decisions.getByRole('button', { name: 'Назад к загрузке' }).first().click();
  }
  const upload = page.getByRole('dialog', { name: 'Загрузка музыки' });
  await expect(upload).toBeVisible();
  await upload.getByRole('button', { name: 'Закрыть' }).click();
  await expect(upload).toBeHidden();
}

async function trackByQuery(request: import('@playwright/test').APIRequestContext, q: string) {
  const response = await request.get(
    `${BASE}/api/admin/tracks?q=${encodeURIComponent(q)}&limit=5`,
    { headers: { cookie: 'mock_session=1' } },
  );
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { items: Array<{ id: string; duration: number | null }> };
  return body.items;
}

test('three decisions per match, replace explanation only when selected, target picker and identical state', async ({
  page,
  request,
}) => {
  const upload = await startUpload(page, [
    audioFile('copy night.mp3', 'media03-one'),
    audioFile('same track.mp3', 'media03-two'),
    audioFile('possible copy.mp3', 'media03-three'),
  ]);
  const decisions = await openDecisions(page, upload, 3);
  const rows = decisions.locator('.decision');
  await expect(rows).toHaveCount(3);

  const tagsRow = rows.filter({ hasText: 'copy night.mp3' });
  const identicalRow = rows.filter({ hasText: 'same track.mp3' });
  const singleRow = rows.filter({ hasText: 'possible copy.mp3' });
  const apply = decisions.getByRole('button', { name: 'Применить решения' });

  for (const row of [tagsRow, identicalRow, singleRow]) {
    await expect(row.getByRole('button', { name: 'Пропустить' })).toBeVisible();
    await expect(row.getByRole('button', { name: 'Заменить трек' })).toBeVisible();
    await expect(row.getByRole('button', { name: 'Сохранить копию' })).toBeVisible();
  }

  // Идентичный файл: замена отключена, копия доступна.
  await expect(identicalRow.getByText('Файлы идентичны').first()).toBeVisible();
  await expect(identicalRow.getByRole('button', { name: 'Заменить трек' })).toBeDisabled();
  await expect(identicalRow.getByRole('button', { name: 'Сохранить копию' })).toBeEnabled();

  // Пояснение появляется только после выбора замены.
  await expect(tagsRow.getByText('Аудиофайл будет заменён')).toHaveCount(0);
  await tagsRow.getByRole('button', { name: 'Заменить трек' }).click();
  await expect(
    tagsRow.getByText(
      'Аудиофайл будет заменён. Плейлисты и пользовательские изменения сохранятся.',
    ),
  ).toBeVisible();
  await expect(tagsRow.getByRole('radiogroup', { name: 'Трек для замены' })).toBeVisible();
  await expect(tagsRow.getByRole('radio')).toHaveCount(3);
  await expect(tagsRow.getByText('Дубль').first()).toBeVisible();
  await expect(apply).toBeDisabled();

  // Цель не выбирается автоматически при нескольких совпадениях.
  await tagsRow.getByRole('radio', { name: /Тихий сигнал/ }).click();
  await expect(tagsRow.getByRole('radio', { name: /Тихий сигнал/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await expect(decisions.locator('.decisions-footer__counts')).toContainText('Заменить: 1');

  // Переключение решения снимает подсветку и пояснение.
  await tagsRow.getByRole('button', { name: 'Пропустить' }).click();
  await expect(tagsRow.getByRole('button', { name: 'Пропустить' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(tagsRow.getByRole('button', { name: 'Заменить трек' })).toHaveAttribute(
    'aria-pressed',
    'false',
  );
  await expect(tagsRow.getByText('Аудиофайл будет заменён')).toHaveCount(0);

  // Одна цель: замена выбирается сразу и показывает трек и подсказку об эфире.
  await singleRow.getByRole('button', { name: 'Заменить трек' }).click();
  await expect(singleRow.getByRole('radiogroup')).toHaveCount(0);
  await expect(singleRow.getByText('Будет заменён: Ночной трамвай')).toBeVisible();
  await expect(singleRow.getByText('Текущий трек доиграет до конца')).toBeVisible();

  const counts = decisions.locator('.decisions-footer__counts');
  await expect(counts).toContainText('Осталось: 1');
  await expect(counts).toContainText('Заменить: 1');
  await expect(counts).toContainText('Сохранить: 0');
  await expect(counts).toContainText('Пропустить: 1');

  // Escape закрывает диалог без применения решений.
  await page.keyboard.press('Escape');
  await expect(decisions).toBeHidden();
  const log = (await (await request.get(`${BASE}/api/mock/requests`)).json()) as {
    requests: unknown[];
  };
  expect(log.requests).toHaveLength(0);
});

test('batch replace selects only unambiguous targets, reports conflicts and applies exactly once', async ({
  page,
  request,
}) => {
  const upload = await startUpload(page, [
    audioFile('copy batch.mp3', 'batch-one'),
    audioFile('possible copy one.mp3', 'batch-two'),
    audioFile('possible copy two.mp3', 'batch-three'),
  ]);
  const decisions = await openDecisions(page, upload, 3);
  const rows = decisions.locator('.decision');
  await expect(rows).toHaveCount(3);

  await decisions.getByRole('button', { name: 'Заменить оставшиеся' }).click();
  await expect(decisions.locator('.decisions-bulk__notice')).toContainText('Заменено будет: 1');
  await expect(decisions.locator('.decisions-bulk__notice')).toContainText('нужен выбор цели: 1');
  await expect(decisions.locator('.decisions-bulk__notice')).toContainText('конфликт цели: 1');

  const copyRow = rows.filter({ hasText: 'copy batch.mp3' });
  const oneRow = rows.filter({ hasText: 'possible copy one.mp3' });
  const twoRow = rows.filter({ hasText: 'possible copy two.mp3' });
  await expect(oneRow.getByRole('button', { name: 'Заменить трек' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(twoRow.locator('.decision__outcome--conflict')).toContainText(
    'Несколько файлов претендуют на один трек',
  );
  await expect(copyRow.getByRole('button', { name: 'Заменить трек' })).toHaveAttribute(
    'aria-pressed',
    'false',
  );

  const counts = decisions.locator('.decisions-footer__counts');
  await expect(counts).toContainText('Заменить: 1');
  await expect(counts).toContainText('Осталось: 2');

  // Оставшиеся решаем вручную: копию для конфликтного файла, пропуск для многовариантного.
  await twoRow.getByRole('button', { name: 'Сохранить копию' }).click();
  await copyRow.getByRole('button', { name: 'Пропустить' }).click();
  await expect(counts).toContainText('Осталось: 0');

  const apply = decisions.getByRole('button', { name: 'Применить решения' });
  await expect(apply).toBeEnabled();
  // Двойной клик не должен отправить решения дважды.
  await page.evaluate(() => {
    const button = Array.from(document.querySelectorAll('button')).find((entry) =>
      entry.textContent?.includes('Применить решения'),
    ) as HTMLButtonElement | undefined;
    button?.click();
    button?.click();
  });
  const result = decisions.locator('.decisions-result');
  await expect(result).toContainText('Добавлено: 1');
  await expect(result).toContainText('Заменено: 1');
  await expect(result).toContainText('Пропущено: 1');
  await expect(decisions).toBeVisible();
  await expect(apply).toBeDisabled();

  const log = (await (await request.get(`${BASE}/api/mock/requests`)).json()) as {
    requests: Array<{ action: string; pending_id: string; track_id: string | null }>;
  };
  expect(log.requests).toHaveLength(3);
  const replacements = log.requests.filter((entry) => entry.action === 'replace');
  expect(replacements).toHaveLength(1);
  expect(replacements[0]?.track_id).toBe('t1');
  expect(log.requests.filter((entry) => entry.action === 'save')).toHaveLength(1);
  expect(log.requests.filter((entry) => entry.action === 'skip')).toHaveLength(1);
});

test('stale match keeps the old track, requires re-confirmation and refreshes library, playlist and queue without reload', async ({
  page,
  request,
}) => {
  await request.get(`${BASE}/api/mock/replace-stale?on=1`);
  const upload = await startUpload(page, [audioFile('possible copy stale.mp3', 'stale-one')]);
  const decisions = await openDecisions(page, upload, 1);
  const row = decisions.locator('.decision').filter({ hasText: 'possible copy stale.mp3' });

  await row.getByRole('button', { name: 'Заменить трек' }).click();
  await expect(row.getByText('Текущий трек доиграет до конца')).toBeVisible();
  await decisions.getByRole('button', { name: 'Применить решения' }).click();
  await expect(row.locator('.decision__outcome--stale')).toContainText('Совпадение изменилось');
  await expect(decisions.locator('.decisions-result__errors')).toContainText('possible copy stale.mp3');
  await expect(decisions).toBeVisible();

  // Старый трек не изменён, решение снято для повторного подтверждения.
  let tracks = await trackByQuery(request, 'Ночной трамвай');
  expect(tracks[0]?.duration).toBe(180);
  await expect(row.getByRole('button', { name: 'Заменить трек' })).toHaveAttribute(
    'aria-pressed',
    'false',
  );

  await row.getByRole('button', { name: 'Заменить трек' }).click();
  await decisions.getByRole('button', { name: 'Применить решения' }).click();
  await expect(decisions.locator('.decisions-result')).toContainText('Заменено: 1');
  await expect(decisions.locator('.decisions-result__errors')).toHaveCount(0);

  const log = (await (await request.get(`${BASE}/api/mock/requests`)).json()) as {
    requests: Array<{ action: string; track_id: string | null }>;
  };
  expect(log.requests).toHaveLength(2);
  expect(log.requests.every((entry) => entry.action === 'replace' && entry.track_id === 't1')).toBe(
    true,
  );

  await closeOverlays(page);
  await page.getByLabel('Поиск по медиатеке').fill('Ночной трамвай');
  await expect(page.locator('.track-table tbody tr')).toHaveCount(1);
  const libraryRow = page.locator('.track-table tbody tr').first();
  await expect(libraryRow).toContainText('3:21');
  tracks = await trackByQuery(request, 'Ночной трамвай');
  expect(tracks).toHaveLength(1);
  expect(tracks[0]?.duration).toBe(201);

  // Плейлист и очередь обновились через клиентскую навигацию, без перезагрузки страницы.
  await page.getByRole('link', { name: 'Ночной сиреневый' }).first().click();
  const playlistRow = page
    .locator('.playlist-row')
    .filter({ hasText: 'Ночной трамвай' })
    .first();
  await expect(playlistRow.locator('.playlist-row__duration')).toHaveText('3:21');

  await page.getByRole('link', { name: 'Обзор', exact: true }).click();
  await expect(page.locator('.np-hero__title')).toHaveText('Ночной трамвай');
  // Текущий экземпляр сохраняет длительность, с которой начался; строка трека — новая.
  await expect(page.locator('.np-hero__times')).toContainText('3:00');
  await expect(page.locator('.np-hero__times')).not.toContainText('3:21');
  await expect(page.locator('.np-hero__cover img').first()).toHaveAttribute('src', /\?v=/);
  expect(await page.evaluate(() => performance.getEntriesByType('navigation').length)).toBe(1);

  const queue = (await (
    await request.get(`${BASE}/api/admin/queue`, { headers: { cookie: 'mock_session=1' } })
  ).json()) as {
    current: { track: { id: string; duration: number | null } };
    items: Array<{ track: { id: string; duration: number | null } }>;
  };
  const entries = [queue.current, ...queue.items].filter((entry) => entry.track.id === 't1');
  expect(entries).toHaveLength(1);
  expect(entries[0]?.track.duration).toBe(201);
});

test('failed replace keeps the row and allows a retry with the same target', async ({
  page,
  request,
}) => {
  await request.get(`${BASE}/api/mock/replace-fail?on=1`);
  const upload = await startUpload(page, [audioFile('possible copy retry.mp3', 'retry-one')]);
  const decisions = await openDecisions(page, upload, 1);
  const row = decisions.locator('.decision').filter({ hasText: 'possible copy retry.mp3' });

  await row.getByRole('button', { name: 'Заменить трек' }).click();
  await decisions.getByRole('button', { name: 'Применить решения' }).click();
  await expect(row.locator('.decision__outcome--failed')).toContainText(
    'Не удалось заменить файл',
  );
  expect((await trackByQuery(request, 'Ночной трамвай'))[0]?.duration).toBe(180);
  await expect(decisions.locator('.decisions-result__errors')).toContainText(
    'possible copy retry.mp3',
  );

  await row.getByRole('button', { name: 'Повторить' }).click();
  await expect(decisions.locator('.decisions-result')).toContainText('Заменено: 1');
  await expect(decisions.locator('.decisions-result__errors')).toHaveCount(0);
  expect((await trackByQuery(request, 'Ночной трамвай'))[0]?.duration).toBe(201);

  const log = (await (await request.get(`${BASE}/api/mock/requests`)).json()) as {
    requests: Array<{ action: string; track_id: string | null }>;
  };
  expect(log.requests).toHaveLength(2);
  expect(log.requests.every((entry) => entry.track_id === 't1')).toBe(true);
});

test('mixed apply keeps the successful replacement and retries only the failed file', async ({
  page,
  request,
}) => {
  await request.get(`${BASE}/api/mock/replace-fail?attempt=2`);
  const upload = await startUpload(page, [
    audioFile('possible copy first.mp3', 'mixed-one'),
    audioFile('possible copy second.mp3', 'mixed-two'),
  ]);
  const decisions = await openDecisions(page, upload, 2);
  const rows = decisions.locator('.decision');
  const firstRow = rows.filter({ hasText: 'possible copy first.mp3' });
  const secondRow = rows.filter({ hasText: 'possible copy second.mp3' });

  await firstRow.getByRole('button', { name: 'Заменить трек' }).click();
  await secondRow.getByRole('button', { name: 'Заменить трек' }).click();
  await decisions.getByRole('button', { name: 'Применить решения' }).click();

  await expect(decisions.locator('.decisions-result')).toContainText('Заменено: 1');
  await expect(decisions.locator('.decisions-result__errors')).toContainText(
    'possible copy second.mp3',
  );
  expect((await trackByQuery(request, 'Ночной трамвай'))[0]?.duration).toBe(201);
  expect((await trackByQuery(request, 'Полночь на кухне'))[0]?.duration).toBe(214);

  await secondRow.getByRole('button', { name: 'Повторить' }).click();
  await expect(decisions.locator('.decisions-result')).toContainText('Заменено: 2');
  await expect(decisions.locator('.decisions-result__errors')).toHaveCount(0);
  expect((await trackByQuery(request, 'Полночь на кухне'))[0]?.duration).toBe(201);

  const log = (await (await request.get(`${BASE}/api/mock/requests`)).json()) as {
    requests: Array<{ action: string; track_id: string | null }>;
  };
  expect(log.requests.map((entry) => entry.track_id)).toEqual(['t1', 't3', 't3']);
});

test('replace uses the explicitly chosen non-first candidate, follows it in the comparison and exposes the replaced filter', async ({
  page,
  request,
}) => {
  const upload = await startUpload(page, [audioFile('copy multi target.mp3', 'multi-one')]);
  const decisions = await openDecisions(page, upload, 1);
  const row = decisions.locator('.decision').filter({ hasText: 'copy multi target.mp3' });

  await row.getByRole('button', { name: 'Заменить трек' }).click();
  await expect(row.getByRole('radio')).toHaveCount(3);
  await row.getByRole('radio', { name: /Полночь на кухне/ }).click();
  await expect(row.getByRole('radio', { name: /Полночь на кухне/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await expect(row.locator('.compare__col').first()).toContainText('Полночь на кухне');

  await decisions.getByRole('button', { name: 'Применить решения' }).click();
  await expect(decisions.locator('.decisions-result')).toContainText('Заменено: 1');

  const log = (await (await request.get(`${BASE}/api/mock/requests`)).json()) as {
    requests: Array<{ action: string; track_id: string | null }>;
  };
  expect(log.requests).toHaveLength(1);
  expect(log.requests[0]?.action).toBe('replace');
  expect(log.requests[0]?.track_id).toBe('t3');

  expect((await trackByQuery(request, 'Ночной трамвай'))[0]?.duration).toBe(180);
  expect((await trackByQuery(request, 'Тихий сигнал'))[0]?.duration).toBe(197);
  expect((await trackByQuery(request, 'Полночь на кухне'))[0]?.duration).toBe(201);
  const all = (await (
    await request.get(`${BASE}/api/admin/tracks?limit=1`, {
      headers: { cookie: 'mock_session=1' },
    })
  ).json()) as { total: number };
  expect(all.total).toBe(120);

  await decisions.getByRole('button', { name: 'Назад к загрузке' }).first().click();
  const uploadDialog = page.getByRole('dialog', { name: 'Загрузка музыки' });
  await expect(uploadDialog.getByText('Заменено: 1')).toBeVisible();
  await uploadDialog.getByRole('tab', { name: /Заменённые/ }).click();
  await expect(uploadDialog.locator('.upload-item--replaced')).toHaveCount(1);
  await expect(uploadDialog.getByText('Заменён').first()).toBeVisible();
  await uploadDialog.getByRole('tab', { name: 'Добавленные' }).click();
  await expect(uploadDialog.locator('.upload-item--replaced')).toHaveCount(0);
  await expect(uploadDialog.getByText('В этом фильтре пока нет файлов.')).toBeVisible();
});

test('mock rejects a replace target outside the candidate list and keeps the match intact', async ({
  page,
  request,
}) => {
  const upload = await startUpload(page, [audioFile('possible copy invalid.mp3', 'invalid-one')]);
  await expect(upload.getByText('Требуют решения: 1')).toBeVisible({ timeout: 20_000 });
  const batchId = await page.evaluate(() => localStorage.getItem('vd.upload.batch'));
  expect(batchId).toBeTruthy();
  const batch = (await (
    await request.get(`${BASE}/api/admin/uploads/${batchId}`, {
      headers: { cookie: 'mock_session=1' },
    })
  ).json()) as {
    batch: {
      items: Array<{
        id: string;
        state: string;
        decision: string | null;
        replace_candidates: Array<{ id: string }>;
      }>;
    };
  };
  const pending = batch.batch.items.find((entry) => entry.state === 'pending');
  expect(pending?.replace_candidates.map((entry) => entry.id)).toEqual(['t1']);

  const rejected = await request.post(
    `${BASE}/api/admin/uploads/${pending?.id}/decide`,
    {
      headers: { cookie: 'mock_session=1' },
      data: { action: 'replace', track_id: 't999' },
    },
  );
  expect(rejected.status()).toBe(422);
  const rejectedBody = (await rejected.json()) as { detail: { code: string } };
  expect(rejectedBody.detail.code).toBe('invalid_target');
  expect((await trackByQuery(request, 'Ночной трамвай'))[0]?.duration).toBe(180);

  const afterReject = (await (
    await request.get(`${BASE}/api/admin/uploads/${batchId}`, {
      headers: { cookie: 'mock_session=1' },
    })
  ).json()) as { batch: { items: Array<{ id: string; state: string; decision: string | null }> } };
  const stillPending = afterReject.batch.items.find((entry) => entry.id === pending?.id);
  expect(stillPending?.state).toBe('pending');
  expect(stillPending?.decision).toBeNull();

  const accepted = await request.post(
    `${BASE}/api/admin/uploads/${pending?.id}/decide`,
    {
      headers: { cookie: 'mock_session=1' },
      data: { action: 'replace', track_id: 't1' },
    },
  );
  expect(accepted.ok()).toBeTruthy();
  expect((await trackByQuery(request, 'Ночной трамвай'))[0]?.duration).toBe(201);
});

test('replace without an available target shows a visible reason, not only a tooltip', async ({
  page,
  request,
}) => {
  await request.get(`${BASE}/api/mock/no-target?on=1`);
  const upload = await startUpload(page, [audioFile('copy orphan.mp3', 'orphan-one')]);
  const decisions = await openDecisions(page, upload, 1);
  const row = decisions.locator('.decision').filter({ hasText: 'copy orphan.mp3' });

  await expect(row.getByText('Замена недоступна: нет подходящего трека')).toBeVisible();
  await expect(row.getByRole('button', { name: 'Заменить трек' })).toBeDisabled();
  await expect(row.getByRole('button', { name: 'Сохранить копию' })).toBeEnabled();
});

test('replace versions the library cover from track timestamps and the browser loads the new image', async ({
  page,
  request,
}) => {
  const coverRequests: string[] = [];
  page.on('request', (entry) => {
    if (entry.url().includes('/mock-cover.svg')) coverRequests.push(entry.url());
  });
  const upload = await startUpload(page, [audioFile('possible copy cover.mp3', 'cover-one')]);
  const decisions = await openDecisions(page, upload, 1);
  const row = decisions.locator('.decision').filter({ hasText: 'possible copy cover.mp3' });
  const libraryRow = page
    .locator('.track-table tbody tr')
    .filter({ hasText: 'Ночной трамвай' })
    .first();
  const image = libraryRow.locator('img').first();
  await expect(image).toHaveAttribute('src', /\?v=\d+/);
  const before = await image.getAttribute('src');
  expect(before).toBeTruthy();
  await page.waitForTimeout(1_800);
  await expect(image).toHaveAttribute('src', before!);

  await row.getByRole('button', { name: 'Заменить трек' }).click();
  await decisions.getByRole('button', { name: 'Применить решения' }).click();
  await expect(decisions.locator('.decisions-result')).toContainText('Заменено: 1');
  await closeOverlays(page);
  await page.getByLabel('Поиск по медиатеке').fill('Ночной трамвай');
  await expect(page.locator('.track-table tbody tr')).toHaveCount(1);
  const updatedRow = page.locator('.track-table tbody tr').first();
  const updatedImage = updatedRow.locator('img').first();
  const after = await updatedImage.getAttribute('src');
  expect(after).not.toBe(before);
  expect(after).toMatch(/\?v=\d+/);
  const requestedVersion = new URL(after!, BASE).search;
  await expect
    .poll(() => coverRequests.some((url) => url.includes(requestedVersion)))
    .toBe(true);
  expect((await trackByQuery(request, 'Ночной трамвай'))[0]?.duration).toBe(201);
});

test('background refresh keeps the modal, search focus, typed text and list scroll', async ({
  page,
}) => {
  const files = Array.from({ length: 6 }, (_, index) =>
    audioFile(`copy focus ${index}.mp3`, `focus-${index}`),
  );
  const upload = await startUpload(page, files);
  const decisions = await openDecisions(page, upload, 6);
  await expect(decisions.locator('.decision')).toHaveCount(6);

  const search = decisions.getByLabel('Поиск совпадений');
  await search.click();
  await search.pressSequentially('copy');
  const list = decisions.locator('.decisions-list');
  await list.evaluate((element) => {
    element.scrollTop = 80;
  });
  const scrollBefore = await list.evaluate((element) => element.scrollTop);
  expect(scrollBefore).toBeGreaterThan(0);

  // Пока решения не приняты, пакет опрашивается каждые 1,5 с.
  await page.waitForTimeout(1_800);
  await expect(decisions).toBeVisible();
  await expect(search).toBeFocused();
  await expect(search).toHaveValue('copy');
  expect(await list.evaluate((element) => element.scrollTop)).toBe(scrollBefore);
  await expect(decisions.locator('.decision')).toHaveCount(6);
});

test.describe('narrow layout', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('three decision buttons wrap and the target picker fits the dialog', async ({ page }) => {
    const upload = await startUpload(page, [audioFile('copy narrow.mp3', 'narrow-one')]);
    const decisions = await openDecisions(page, upload, 1);
    const row = decisions.locator('.decision').first();
    await row.getByRole('button', { name: 'Заменить трек' }).click();
    await expect(row.getByRole('radiogroup', { name: 'Трек для замены' })).toBeVisible();

    const skip = await row.getByRole('button', { name: 'Пропустить' }).boundingBox();
    const save = await row.getByRole('button', { name: 'Сохранить копию' }).boundingBox();
    expect(skip).not.toBeNull();
    expect(save).not.toBeNull();
    expect(save!.y).toBeGreaterThan(skip!.y + 4);

    const option = await row.getByRole('radio').first().boundingBox();
    expect(option).not.toBeNull();
    expect(option!.x + option!.width).toBeLessThanOrEqual(390);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
    ).toBe(true);
    await page.screenshot({ path: `${SHOT_DIR}/decisions-390x844.png` });

    const collapse = row.getByRole('button', { name: 'Свернуть сравнение' });
    if (await collapse.isVisible()) await collapse.click();
    await decisions.locator('.modal__body').evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    await expect(row.getByRole('button', { name: 'Сохранить копию' })).toBeVisible();
    await page.screenshot({ path: `${SHOT_DIR}/decisions-390x844-actions.png` });
  });
});

test('visual snapshots at 1920x1080, 1366x768 and 390x844', async ({ page }) => {
  const upload = await startUpload(page, [
    audioFile('copy visual-a.mp3', 'visual-one'),
    audioFile('same visual.mp3', 'visual-two'),
    audioFile('possible copy visual-b.mp3', 'visual-three'),
  ]);
  const decisions = await openDecisions(page, upload, 3);
  const rows = decisions.locator('.decision');
  const tagsRow = rows.filter({ hasText: 'copy visual-a.mp3' });
  const identicalRow = rows.filter({ hasText: 'same visual.mp3' });
  const collapse = tagsRow.getByRole('button', { name: 'Свернуть сравнение' });
  if (await collapse.isVisible()) await collapse.click();
  await tagsRow.getByRole('button', { name: 'Заменить трек' }).click();
  await tagsRow.getByRole('radio').first().click();

  for (const [width, height, name] of [
    [1920, 1080, 'decisions-1920x1080'],
    [1366, 768, 'decisions-1366x768'],
    [390, 844, 'decisions-390x844-visual'],
  ] as const) {
    await page.setViewportSize({ width, height });
    await expect(decisions).toBeVisible();
    await decisions.locator('.decisions-list').evaluate((element) => {
      element.scrollTop = 0;
    });
    await page.screenshot({ path: `${SHOT_DIR}/${name}.png` });
    if (width === 390) {
      await decisions.locator('.modal__body').evaluate((element) => {
        element.scrollTop = element.scrollHeight;
      });
      await page.screenshot({ path: `${SHOT_DIR}/decisions-390x844-actions.png` });
    }
  }

  // Состояние «Файлы идентичны» и итоговые счётчики после применения.
  await page.setViewportSize({ width: 1366, height: 768 });
  await identicalRow.scrollIntoViewIfNeeded();
  await expect(identicalRow.getByRole('button', { name: 'Заменить трек' })).toBeDisabled();
  await page.screenshot({ path: `${SHOT_DIR}/identical-1366x768.png` });
  await decisions.locator('.decisions-list').evaluate((element) => {
    element.scrollTop = 0;
  });
  await decisions.getByRole('button', { name: 'Пропустить оставшиеся' }).click();
  await decisions.getByRole('button', { name: 'Применить решения' }).click();
  await expect(decisions.locator('.decisions-result')).toContainText('Заменено: 1');
  await page.screenshot({ path: `${SHOT_DIR}/result-1366x768.png` });
});




