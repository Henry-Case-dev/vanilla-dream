import { test, expect } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

const BASE = 'http://127.0.0.1:4199';
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64',
);

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ context, request }) => {
  await request.get(`${BASE}/api/mock/reset`);
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
});

async function typePerCharacter(page: Page, target: Locator, text: string): Promise<void> {
  for (const char of Array.from(text)) {
    await target.pressSequentially(char);
    await expect(target).toBeFocused();
  }
}

async function openUploadDialog(page: Page): Promise<Locator> {
  await page.getByRole('button', { name: 'Загрузить', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Загрузка музыки' });
  await expect(dialog).toBeVisible();
  return dialog;
}

test('upload dialog shows per-file states, filters, decisions apply only to undecided and the indicator reopens the batch', async ({
  page,
}) => {
  await page.goto('/admin/library');
  const upload = await openUploadDialog(page);

  await upload.getByLabel('Выбрать аудиофайлы для загрузки').setInputFiles([
    { name: 'first new.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('audio-one') },
    { name: 'copy night.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('audio-two') },
    { name: 'possible copy.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('audio-three') },
    { name: 'same track.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('audio-four') },
    { name: 'bad file.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from('audio-five') },
  ]);

  await expect(upload.getByText('Добавлено: 1')).toBeVisible({ timeout: 20_000 });
  await expect(upload.getByText('Требуют решения: 3')).toBeVisible();
  await expect(upload.getByText('Ошибки: 1')).toBeVisible();
  await expect(upload.locator('.upload-item--pending')).toHaveCount(3);
  await expect(upload.locator('.upload-item--added')).toHaveCount(1);
  await expect(upload.getByText('Файл повреждён или не читается').first()).toBeVisible();

  // Фильтры.
  await upload.getByRole('tab', { name: /Совпадения/ }).click();
  await expect(upload.locator('.upload-item--pending')).toHaveCount(3);
  await upload.getByRole('tab', { name: 'Добавленные' }).click();
  await expect(upload.locator('.upload-item--added')).toHaveCount(1);
  await upload.getByRole('tab', { name: 'Ошибки' }).click();
  await expect(upload.locator('.upload-item--error')).toHaveCount(1);
  await upload.getByRole('tab', { name: 'Все' }).click();

  const matchesTrigger = upload
    .getByRole('button', { name: /Просмотреть совпадения/ })
    .first();
  await matchesTrigger.click();
  const decisions = page.getByRole('dialog', { name: 'Обнаружены совпадения' });
  await expect(decisions).toBeVisible();
  const search = decisions.getByLabel('Поиск совпадений');
  await expect(search).toBeFocused();

  // Сравнение: обе стороны и причина.
  await decisions.getByRole('button', { name: 'Сравнить файлы' }).first().click();
  await expect(decisions.getByText('В медиатеке').first()).toBeVisible();
  await expect(decisions.getByText('Новый файл').first()).toBeVisible();
  await expect(decisions.getByText('Совпадают теги').first()).toBeVisible();
  await expect(decisions.locator('.compare__row.is-diff').first()).toBeVisible();
  await expect(decisions.getByRole('button', { name: 'Применить решения' })).toBeDisabled();

  // Escape закрывает только верхний диалог, загрузка остаётся открытой.
  await page.keyboard.press('Escape');
  await expect(decisions).toBeHidden();
  await expect(upload).toBeVisible();
  await expect(matchesTrigger).toBeFocused();

  // Повторное открытие и решения.
  await matchesTrigger.click();
  await expect(decisions).toBeVisible();
  const rows = decisions.locator('.decision');
  await expect(rows).toHaveCount(3);
  const firstRow = rows.first();
  await firstRow.getByRole('button', { name: 'Сохранить копию' }).click();
  await expect(firstRow.getByRole('button', { name: 'Сохранить копию' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await decisions.getByRole('button', { name: 'Пропустить оставшиеся' }).click();

  // Решение по первому файлу не перезаписано массовым действием.
  await expect(firstRow.getByRole('button', { name: 'Сохранить копию' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(decisions.locator('.decisions-footer__counts')).toContainText('Осталось: 0');
  await expect(decisions.locator('.decisions-footer__counts')).toContainText('Сохранить: 1');
  await expect(decisions.locator('.decisions-footer__counts')).toContainText('Пропустить: 2');
  const apply = decisions.getByRole('button', { name: 'Применить решения' });
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(decisions).toBeHidden();
  await expect(upload).toBeVisible();

  // Закрытие диалога не теряет операцию: индикатор возвращает тот же пакет.
  await upload.getByRole('button', { name: 'Закрыть' }).click();
  await expect(upload).toBeHidden();
  const indicator = page.getByRole('button', { name: 'Открыть загрузку музыки' });
  await expect(indicator).toBeVisible();

  // Операция переживает перезагрузку страницы: batch id сохранён, состояние
  // перечитывается через GET /api/admin/uploads/{batch_id}.
  await page.reload();
  await expect(indicator).toBeVisible();
  await indicator.click();
  await expect(upload).toBeVisible();
  await expect(upload.getByText('Сохранён как дубликат').first()).toBeVisible();
  await expect(upload.getByText('Пропущен').first()).toBeVisible();

  await upload.getByRole('button', { name: 'Скрыть' }).click();
  await expect(upload).toBeHidden();
  await expect(page.getByRole('button', { name: 'Открыть загрузку музыки' })).toBeHidden();
});

test('track editor keeps focus and typed values, previews covers without saving and applies changes', async ({
  page,
}) => {
  await page.goto('/admin/library');
  await page.getByLabel('Поиск по медиатеке').fill('Ночной трамвай');
  await expect(page.locator('.track-table tbody tr')).toHaveCount(1);
  const firstRow = page.locator('.track-table tbody tr').first();
  await firstRow.locator('.menu .icon-btn').click();
  await page.getByRole('menuitem', { name: 'Изменить трек' }).click();

  const editor = page.getByRole('dialog', { name: 'Изменить трек' });
  await expect(editor).toBeVisible();
  const title = editor.getByLabel('Название', { exact: true });
  await expect(title).toBeFocused();
  await expect(editor.getByLabel('Исполнитель', { exact: true })).toBeVisible();
  await expect(editor.getByLabel('Альбом', { exact: true })).toBeVisible();

  await title.click();
  await page.keyboard.press('Control+A');
  await page.keyboard.press('Backspace');
  await typePerCharacter(page, title, 'Ночной черновик');
  await title.press('Home');
  await title.press('ArrowRight');
  await title.press('ArrowRight');
  await title.press('Backspace');
  await expect(title).toBeFocused();

  // SSE-обновления не отбирают фокус.
  await page.waitForTimeout(1600);
  await expect(title).toBeFocused();
  await title.pressSequentially('!');
  await expect(title).toHaveValue('Н!чной черновик');

  await editor.getByLabel('Выбрать обложку трека').setInputFiles({
    name: 'cover.png',
    mimeType: 'image/png',
    buffer: PNG,
  });
  await expect(editor.locator('.track-editor__preview img')).toHaveAttribute('src', /^blob:/);
  await expect(editor.getByText('Новая обложка сохранится вместе с формой')).toBeVisible();

  // Отмена не сохраняет предпросмотр.
  await page.keyboard.press('Escape');
  await expect(editor).toBeHidden();
  await firstRow.locator('.menu .icon-btn').click();
  await page.getByRole('menuitem', { name: 'Изменить трек' }).click();
  await expect(editor).toBeVisible();
  await expect(editor.locator('.track-editor__preview img')).toHaveAttribute('src', /mock-cover/);

  // Ошибка сохраняет форму и значения.
  const titleAgain = editor.getByLabel('Название', { exact: true });
  await titleAgain.click();
  await page.keyboard.press('Control+A');
  await page.keyboard.press('Backspace');
  await editor.getByRole('button', { name: 'Сохранить' }).click();
  await expect(editor.getByText('Название трека не может быть пустым')).toBeVisible();
  await expect(editor).toBeVisible();
  await expect(titleAgain).toHaveValue('');

  await titleAgain.fill('Ночной трамвай — правка');
  await editor.getByLabel('Выбрать обложку трека').setInputFiles({
    name: 'cover.png',
    mimeType: 'image/png',
    buffer: PNG,
  });
  await editor.getByRole('button', { name: 'Сохранить' }).click();
  await expect(editor).toBeHidden();
  await expect(page.locator('.track-table tbody tr').first()).toContainText('Ночной трамвай — правка');
});

test('library and playlist downloads open the export dialog, archives poll to ready and duplicate badges are visible', async ({
  page,
}) => {
  await page.goto('/admin/library');
  await expect(page.locator('.dup-badge').first()).toBeVisible();
  await expect(page.locator('.dup-badge').first()).toHaveAttribute('title', /Совпадают теги/);

  const headerDownload = page.locator('.page-header__actions').getByRole('button', { name: 'Скачать' });
  await headerDownload.click();
  await expect(page.getByRole('menuitem', { name: 'Скачать выбранные треки' })).toBeDisabled();
  await page.getByRole('menuitem', { name: 'Скачать всю медиатеку' }).click();

  const exportDialog = page.getByRole('dialog', { name: 'Экспорт медиатеки' });
  await expect(exportDialog).toBeVisible();
  await expect(exportDialog.getByRole('radio', { name: /ZIP-архив/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await expect(exportDialog.getByRole('radio', { name: /С текущими тегами/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await expect(exportDialog.locator('.export-composition')).toContainText(/трек/);

  await exportDialog.getByRole('button', { name: 'Подготовить архив' }).click();
  await expect(exportDialog.getByText(/Подготавливаем архив:/)).toBeVisible();
  const archiveLink = exportDialog.getByRole('link', { name: 'Скачать архив' });
  await expect(archiveLink).toBeVisible({ timeout: 20_000 });
  await expect(archiveLink).toHaveAttribute('href', /\/api\/admin\/exports\/[^/]+\/archive/);
  await expect(exportDialog.getByText('Vanilla Dream Radio.zip')).toBeVisible();

  await exportDialog.locator('.modal__footer').getByRole('button', { name: 'Закрыть' }).click();
  const chip = page.getByRole('button', { name: /Архив готов/ });
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page.getByRole('dialog', { name: 'Экспорт' })).toContainText('Vanilla Dream Radio.zip');
  await page.getByRole('dialog', { name: 'Экспорт' }).getByRole('button', { name: 'Скрыть' }).click();

  // Отдельные файлы.
  await headerDownload.click();
  await page.getByRole('menuitem', { name: 'Скачать всю медиатеку' }).click();
  const filesDialog = page.getByRole('dialog', { name: 'Экспорт медиатеки' });
  await filesDialog.getByRole('radio', { name: /Отдельными файлами/ }).click();
  await expect(filesDialog.getByRole('radio', { name: /Отдельными файлами/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await filesDialog.getByRole('button', { name: 'Скачать файлы' }).click();
  await expect(filesDialog.getByRole('button', { name: 'Скачать все файлы' })).toBeVisible();
  await expect(filesDialog.locator('.export-files__list li').first()).toBeVisible();
  await expect(filesDialog.getByText(/разрешение на несколько загрузок/)).toBeVisible();
  await filesDialog.locator('.modal__footer').getByRole('button', { name: 'Закрыть' }).click();

  // Выделение: скачивание работает по выбранным id.
  const checkboxes = page.locator('.track-table tbody input[type="checkbox"]');
  await checkboxes.nth(0).check();
  await checkboxes.nth(1).check();
  await page.locator('.bulk-bar').getByRole('button', { name: 'Скачать' }).click();
  const selectionDialog = page.getByRole('dialog', { name: 'Экспорт выбранных треков' });
  await expect(selectionDialog.locator('.export-composition')).toContainText('2 трека');
  await selectionDialog.getByRole('button', { name: 'Отмена' }).click();

  // Плейлист: пустой — кнопка недоступна, непустой — открывает экспорт.
  await page.goto('/admin/playlists/p2');
  const emptyDownload = page
    .locator('.playlist-head__actions')
    .getByRole('button', { name: 'Скачать' });
  await expect(emptyDownload).toBeDisabled();
  await expect(emptyDownload).toHaveAttribute('title', 'В плейлисте пока нет треков');

  await page.goto('/admin/playlists/p1');
  await expect(page.locator('.playlist-row .dup-badge').first()).toBeVisible();
  await page
    .locator('.playlist-head__actions')
    .getByRole('button', { name: 'Скачать' })
    .click();
  const playlistDialog = page.getByRole('dialog', { name: 'Экспорт плейлиста' });
  await expect(playlistDialog).toBeVisible();
  await expect(playlistDialog.locator('.export-composition')).toContainText('Ночной сиреневый');
  await playlistDialog.getByRole('button', { name: 'Отмена' }).click();
});
