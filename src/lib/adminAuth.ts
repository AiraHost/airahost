import { createHash, createHmac, timingSafeEqual } from "crypto";

/** Session cookie holding a signed admin token. */
export const ADMIN_COOKIE_NAME = "airahost_admin";

/** How long an admin session stays valid. */
export const ADMIN_SESSION_MAX_AGE_SECONDS = 12 * 60 * 60;

function adminPassword(): string {
  const password = process.env.ADMIN_PASSWORD ?? "";
  if (!password) {
    throw new Error("Missing ADMIN_PASSWORD. The /admin dashboard requires it to be set.");
  }
  return password;
}

/** False when ADMIN_PASSWORD is missing — every admin check would throw. */
export function isAdminConfigured(): boolean {
  return !!process.env.ADMIN_PASSWORD;
}

/** Constant-time password check — hashes first so length never leaks. */
export function isAdminPasswordValid(candidate: string): boolean {
  const given = createHash("sha256").update(candidate).digest();
  const expected = createHash("sha256").update(adminPassword()).digest();
  return timingSafeEqual(given, expected);
}

function sign(expiresAt: number): string {
  return createHmac("sha256", adminPassword()).update(`admin:${expiresAt}`).digest("hex");
}

/** Token is `<expiryMs>.<hmac>` — unforgeable without ADMIN_PASSWORD. */
export function createAdminSessionToken(): string {
  const expiresAt = Date.now() + ADMIN_SESSION_MAX_AGE_SECONDS * 1000;
  return `${expiresAt}.${sign(expiresAt)}`;
}

export function verifyAdminSessionToken(token: string | undefined): boolean {
  if (!token || !isAdminConfigured()) return false;
  const [rawExpiry, signature] = token.split(".");
  const expiresAt = Number(rawExpiry);
  if (!Number.isFinite(expiresAt) || !signature) return false;
  if (expiresAt <= Date.now()) return false;

  const expected = Buffer.from(sign(expiresAt), "hex");
  const given = Buffer.from(signature, "hex");
  if (given.length !== expected.length) return false;
  return timingSafeEqual(given, expected);
}
