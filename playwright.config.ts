import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for airahost E2E.
 *
 * Projects:
 *   - setup                  Runs auth.setup.ts only.  Saves storageState.
 *   - chromium-authenticated Reuses storageState.  Default project for
 *                            dashboard / benchmark editor / blacklist tests.
 *   - chromium-public        Anonymous viewer.  Runs only the share-view spec.
 *
 * Required env (commit-time defaults are not safe to set):
 *   E2E_USER_EMAIL / E2E_USER_PASSWORD  Used by auth.setup.ts.
 *   E2E_BASE_URL (optional)             Skips webServer auto-start.
 *   E2E_LISTING_ID / E2E_SECONDARY_LISTING_ID / E2E_SHARE_ID (optional)
 *
 * Local run:
 *   npm run test:e2e:install   # browsers
 *   E2E_USER_EMAIL=... E2E_USER_PASSWORD=... npm run test:e2e
 */
export default defineConfig({
  testDir: "./tests/e2e",
  // Most specs depend on shared seed data (a single test account / listing),
  // so per-file parallelism is safer than fully-parallel.
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",

  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  // If E2E_BASE_URL is set, assume the user has a server running already
  // and skip the auto-start.  Otherwise spin up Next dev on 127.0.0.1:3000.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        // Use the webpack dev script.  Turbopack panics on OneDrive-synced
        // paths containing non-ASCII characters (Windows os error 1450),
        // which breaks every request.  Webpack is slower to compile but is
        // path-safe on Windows.  Override via E2E_BASE_URL if you'd rather
        // run a different dev server in another terminal.
        command:
          process.platform === "win32"
            ? "npm.cmd run dev:webpack -- --hostname 127.0.0.1 --port 3000"
            : "npm run dev:webpack -- --hostname 127.0.0.1 --port 3000",
        url: "http://127.0.0.1:3000",
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        stdout: "pipe",
        stderr: "pipe",
      },

  projects: [
    {
      name: "setup",
      testMatch: /auth\.setup\.ts/,
    },
    {
      name: "chromium-authenticated",
      use: {
        ...devices["Desktop Chrome"],
        storageState: "tests/e2e/.auth/user.json",
      },
      dependencies: ["setup"],
      // Keep the auth setup file out, plus anything explicitly meant for the
      // public anonymous project.
      testIgnore: [/auth\.setup\.ts/, /public-share-readonly\.spec\.ts/, /daily-agent\.spec\.ts/],
    },
    {
      name: "chromium-public",
      use: {
        ...devices["Desktop Chrome"],
        // Intentionally no storageState — share view must be testable
        // exactly as an anonymous viewer would see it.
      },
      testMatch: /public-share-readonly\.spec\.ts|daily-agent\.spec\.ts/,
    },
  ],
});
