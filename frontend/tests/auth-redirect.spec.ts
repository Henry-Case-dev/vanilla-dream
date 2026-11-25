import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:4199';

test.beforeEach(async ({ request }) => {
  await request.get(`${BASE}/api/mock/reset`);
});

test('successful login navigates from /admin/login to the overview', async ({ page, context }) => {
  await context.clearCookies();
  await page.goto('/admin/login');
  await expect(page.getByRole('heading', { name: 'Панель управления' })).toBeVisible();
  await page.getByLabel('Пароль', { exact: true }).fill('e2e-owner-password');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.getByRole('heading', { name: 'Радиостанция' })).toBeVisible();
  await expect(page.locator('.np-hero__title')).toBeVisible();
});

test('login returns to the protected route stored in location.state.from', async ({ page, context }) => {
  await context.clearCookies();
  await page.goto('/admin/library');
  await expect(page).toHaveURL(/\/admin\/login$/);
  await page.getByLabel('Пароль', { exact: true }).fill('e2e-owner-password');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).toHaveURL(/\/admin\/library$/);
  await expect(page.getByRole('heading', { name: 'Медиатека' })).toBeVisible();
});

test('visiting /admin/login with a valid session redirects immediately', async ({ page, context }) => {
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
  await page.goto('/admin/login');
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.getByRole('heading', { name: 'Радиостанция' })).toBeVisible();
});

test('logout returns to login and the next /admin visit is guarded', async ({ page, context }) => {
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
  await page.goto('/admin/settings');
  await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
  await page.getByRole('button', { name: 'Выйти из панели' }).click();
  await expect(page).toHaveURL(/\/admin\/login$/);
  await expect(page.getByRole('heading', { name: 'Панель управления' })).toBeVisible();
  await page.goto('/admin');
  await expect(page).toHaveURL(/\/admin\/login$/);
});

test('upload panel is keyboard reachable and the file input has an accessible name', async ({
  page,
  context,
}) => {
  await context.clearCookies();
  await context.addCookies([{ name: 'mock_session', value: '1', url: BASE }]);
  await page.goto('/admin/library');
  const uploadButton = page.getByRole('button', { name: 'Загрузить' });
  await uploadButton.focus();
  await expect(uploadButton).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByText('Загрузка музыки')).toBeVisible();
  const input = page.getByLabel('Выбрать аудиофайлы для загрузки');
  await expect(input).toHaveAttribute('type', 'file');
  await input.focus();
  await expect(input).toBeFocused();
});
