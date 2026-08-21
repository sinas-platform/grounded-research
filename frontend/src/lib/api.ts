import { SinasClient } from '@sinas/sdk';

export const API_BASE = import.meta.env.VITE_SGR_API ?? '/api/v1';

const ACCESS_KEY = 'sgr_access_token';
const REFRESH_KEY = 'sgr_refresh_token';

export const tokens = {
  get access(): string | null {
    return localStorage.getItem(ACCESS_KEY);
  },
  get refresh(): string | null {
    return localStorage.getItem(REFRESH_KEY);
  },
  set(access: string, refresh: string): void {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },
  setAccess(access: string): void {
    localStorage.setItem(ACCESS_KEY, access);
  },
  clear(): void {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

let unauthenticatedHandler: (() => void) | null = null;

export function setUnauthenticatedHandler(fn: () => void): void {
  unauthenticatedHandler = fn;
}

/**
 * SinasClient configured against sgr's /api/v1. Used for:
 *   - auth (client.auth.login / verifyOTP / refresh / logout / getInfo)
 *   - all sgr-side data calls (via the `api<T>` helper below)
 *
 * Tokens are read fresh on every request so the client itself never
 * goes stale across re-renders or refresh-token rotation.
 */
export const client = new SinasClient({
  baseUrl: API_BASE,
  getAccessToken: () => tokens.access,
  getRefreshToken: () => tokens.refresh,
  onTokenRefresh: (access) => tokens.setAccess(access),
  onUnauthenticated: () => {
    tokens.clear();
    unauthenticatedHandler?.();
  },
});

/**
 * Thin helper for sgr's own /api/v1/* endpoints. Reuses the SinasClient
 * for headers + refresh-on-401, so 401s automatically trigger one refresh
 * attempt before the request fails.
 */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const url = `${API_BASE}${path}`;
  const res = await client.fetch(url, init);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
