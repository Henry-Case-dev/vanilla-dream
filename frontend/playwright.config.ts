import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.ts',
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4199',
    viewport: { width: 1366, height: 768 },
    permissions: ['clipboard-read', 'clipboard-write'],
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run build && node tests/mock-server.mjs',
    url: 'http://127.0.0.1:4199/api/health',
    reuseExistingServer: false,
    timeout: 240_000,
  },
});
