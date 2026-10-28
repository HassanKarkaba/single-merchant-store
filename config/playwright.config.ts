import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '../tests/browser',
  testMatch: 'storefront.e2e.spec.ts',
  fullyParallel: false, workers: 1, retries: 0, timeout: 120000,
  forbidOnly: !!process.env.CI,
  reporter: [['line'], ['json', {outputFile: process.env.SME_BROWSER_REPORT || 'test-results/portfolio.json'}]],
  use: {
    baseURL: process.env.SME_TEST_BASE_URL,
    ignoreHTTPSErrors: false, trace: 'off', screenshot: 'off', video: 'off',
    launchOptions: {args: ['--host-resolver-rules=MAP store.test 127.0.0.1']}
  }
});
