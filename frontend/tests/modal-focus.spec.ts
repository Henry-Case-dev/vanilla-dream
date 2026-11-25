import { test, expect } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

type TranscriptRow = {
  field: string;
  key: string;
  activeTag: string;
  activeId: string | null;
  activePlaceholder: string | null;
};

const transcript: TranscriptRow[] = [];

async function activeInfo(page: Page): Promise<Omit<TranscriptRow, 'field' | 'key'>> {
  return page.evaluate(() => {
    const active = document.activeElement as HTMLElement | null;
    return {
      activeTag: active?.tagName ?? 'NONE',
      activeId: active?.id ?? null,
      activePlaceholder:
        active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement
          ? active.placeholder
          : null,
    };
  });
}

async function assertFocused(page: Page, target: Locator, field: string, key: string): Promise<void> {
  await expect(target).toBeFocused();
  const info = await activeInfo(page);
  transcript.push({ field, key, ...info });
}

async function typePerCharacter(
  page: Page,
  target: Locator,
  text: string,
  field: string,
): Promise<void> {
  await assertFocused(page, target, field, 'start');
  for (const char of Array.from(text)) {
    await target.pressSequentially(char);
    await assertFocused(page, target, field, char);
  }
}

async function clearField(page: Page, target: Locator, field: string): Promise<void> {
  await target.click();
  await page.keyboard.press('Control+A');
  await page.keyboard.press('Backspace');
  await expect(target).toHaveValue('');
  await assertFocused(page, target, field, 'cleared');
}

test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ context, baseURL }) => {
  await context.addCookies([
    { name: 'mock_session', value: '1', url: baseURL ?? 'http://127.0.0.1:4199' },
  ]);
});

test('modal keeps focus per keystroke, traps Tab, restores opener and saves typed values', async ({
  page,
}) => {
  await page.goto('/admin/playlists/p1');
  const coverButton = page.locator('.playlist-head__cover-button');
  await expect(coverButton).toBeVisible();
  await coverButton.click();

  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  const nameInput = dialog.getByLabel('Название');
  const description = dialog.getByLabel('Описание');

  // The close X must never win the initial focus.
  await expect(nameInput).toBeFocused();
  await assertFocused(page, nameInput, 'playlist-name', 'initial');

  await clearField(page, nameInput, 'playlist-name');
  await typePerCharacter(page, nameInput, 'Сиреневый рассвет', 'playlist-name');

  // Cursor movement and editing in the middle of the value.
  await nameInput.press('Home');
  await nameInput.press('ArrowRight');
  await nameInput.press('ArrowRight');
  await assertFocused(page, nameInput, 'playlist-name', 'cursor-middle');
  await nameInput.press('Backspace');
  await assertFocused(page, nameInput, 'playlist-name', 'backspace');
  await nameInput.press('Delete');
  await assertFocused(page, nameInput, 'playlist-name', 'delete');
  await nameInput.press('End');
  await nameInput.press('ArrowLeft');
  await assertFocused(page, nameInput, 'playlist-name', 'end-left');
  await nameInput.pressSequentially('!');
  await assertFocused(page, nameInput, 'playlist-name', 'suffix');

  // Shift+Tab must stay inside the dialog and not land on the close X.
  await nameInput.press('Shift+Tab');
  const shiftTab = await page.evaluate(() => {
    const dialog = document.querySelector('.modal__panel');
    const active = document.activeElement as HTMLElement | null;
    return {
      tag: active?.tagName ?? 'NONE',
      label: active?.getAttribute('aria-label'),
      inside: Boolean(dialog && active && dialog.contains(active)),
    };
  });
  expect(shiftTab.inside).toBe(true);
  expect(shiftTab.tag).toBe('BUTTON');
  expect(shiftTab.label).not.toBe('Закрыть');
  transcript.push({
    field: 'playlist-name',
    key: 'shift-tab',
    activeTag: shiftTab.tag,
    activeId: null,
    activePlaceholder: shiftTab.label,
  });
  await page.keyboard.press('Tab');
  await expect(nameInput).toBeFocused();

  // Tab must move to the next field, not to the header close button.
  await nameInput.press('Tab');
  await expect(description).toBeFocused();
  await assertFocused(page, description, 'playlist-description', 'tab-forward');

  await clearField(page, description, 'playlist-description');
  await typePerCharacter(page, description, 'Первая строка', 'playlist-description');
  await description.press('Enter');
  await assertFocused(page, description, 'playlist-description', 'enter');
  await typePerCharacter(page, description, 'Вторая строка', 'playlist-description');
  await description.press('Home');
  await description.press('ArrowRight');
  await description.press('Backspace');
  await assertFocused(page, description, 'playlist-description', 'multiline-edit');

  // Select all + paste with Cyrillic via the real clipboard.
  await page.evaluate(() => navigator.clipboard.writeText('Вставка кириллицей'));
  await description.press('Control+A');
  await description.press('Control+V');
  await expect(description).toHaveValue('Вставка кириллицей');
  await assertFocused(page, description, 'playlist-description', 'paste');

  // Background SSE/live updates must not steal focus while typing.
  await page.waitForTimeout(1500);
  await assertFocused(page, description, 'playlist-description', 'during-live-update');
  await description.pressSequentially(' 2');
  await assertFocused(page, description, 'playlist-description', 'after-live-update');

  const finalName = await nameInput.inputValue();
  const finalDescription = await description.inputValue();
  await dialog.getByRole('button', { name: 'Сохранить' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator('.playlist-head__name')).toHaveText(finalName);
  await expect(page.locator('.playlist-head__desc')).toContainText(finalDescription);

  // Reopen: focus is initialised again and Escape restores the opener.
  await coverButton.click();
  await expect(dialog).toBeVisible();
  await expect(nameInput).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(coverButton).toBeFocused();
});

test('add-tracks, metadata, password and confirm dialogs use the same focus contract', async ({
  page,
}) => {
  await page.goto('/admin/playlists/p1');
  await page.getByRole('button', { name: 'Добавить треки' }).click();
  const addDialog = page.getByRole('dialog');
  await expect(addDialog).toBeVisible();
  const search = addDialog.getByLabel('Поиск треков');
  await expect(search).toBeFocused();
  await typePerCharacter(page, search, 'сирень', 'add-tracks-search');
  await search.press('Tab');
  await expect(addDialog.getByRole('checkbox').first()).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(addDialog).toBeHidden();

  await page.goto('/admin/library');
  await page.locator('.track-table tbody tr').first().locator('.menu .icon-btn').click();
  await page.getByRole('menuitem', { name: 'Изменить метаданные' }).click();
  const metadataDialog = page.getByRole('dialog');
  await expect(metadataDialog).toBeVisible();
  const title = metadataDialog.getByLabel('Название');
  await expect(title).toBeFocused();
  await clearField(page, title, 'metadata-title');
  await typePerCharacter(page, title, 'Новое название', 'metadata-title');
  await page.keyboard.press('Escape');
  await expect(metadataDialog).toBeHidden();

  await page.goto('/admin/settings');
  await page.getByRole('button', { name: 'Сменить пароль' }).click();
  const passwordDialog = page.getByRole('dialog');
  const current = passwordDialog.getByLabel('Текущий пароль', { exact: true });
  const next = passwordDialog.getByLabel('Новый пароль', { exact: true });
  await expect(current).toBeFocused();
  await typePerCharacter(page, current, 'Пароль', 'password-current');
  await current.press('Tab');
  await expect(next).toBeFocused();
  await typePerCharacter(page, next, 'Пароль2', 'password-new');
  await page.keyboard.press('Escape');
  await expect(passwordDialog).toBeHidden();

  await page.goto('/admin/playlists/p1');
  await page.getByRole('button', { name: 'Другие действия с плейлистом' }).click();
  await page.getByRole('menuitem', { name: 'Удалить плейлист' }).click();
  const confirmDialog = page.getByRole('dialog');
  await expect(confirmDialog).toBeVisible();
  await expect(confirmDialog.getByRole('button', { name: 'Отмена' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(confirmDialog).toBeHidden();
});

test.afterAll(() => {
  console.log('FOCUS_TRANSCRIPT ' + JSON.stringify(transcript));
});
