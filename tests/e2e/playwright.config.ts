import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
    testDir: '.',
    testMatch: '*.spec.ts',
    timeout: 180_000,
    expect: { timeout: 30_000 },
    workers: 1,
    fullyParallel: false,
    retries: 0,
    reporter: 'list',
    // Default outputDir (test-results/) stays inside the container: failure artifacts contain the
    // prompt canary and must not land in the scanned evidence directory.
    use: {
        baseURL: process.env.BASE_URL ?? 'http://127.0.0.1:5173',
        actionTimeout: 15_000,
        navigationTimeout: 30_000,
    },
    projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
