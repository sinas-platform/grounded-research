import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import type { InfoResponse } from '@sinas/sdk';
import { api, client, setUnauthenticatedHandler, tokens } from './api';

export interface Me {
  user_id: string;
  roles: string[];
  is_admin: boolean;
  /** SGR's own auth mode (whether sgr uses real Sinas auth or a single admin key). */
  auth_mode: 'sinas' | 'simplified';
}

interface AuthState {
  status: 'loading' | 'authenticated' | 'unauthenticated';
  me: Me | null;
  /** Sinas-side auth_mode (otp / password / password+otp). Null while loading or on error. */
  info: InfoResponse | null;
  setSession: (access: string, refresh: string) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthState['status']>('loading');
  const [me, setMe] = useState<Me | null>(null);
  const [info, setInfo] = useState<InfoResponse | null>(null);

  const refresh = async () => {
    try {
      const m = await api<Me>('/me');
      setMe(m);
      setStatus('authenticated');
    } catch {
      setMe(null);
      setStatus('unauthenticated');
    }
  };

  useEffect(() => {
    setUnauthenticatedHandler(() => {
      setMe(null);
      setStatus('unauthenticated');
    });
    // /info is unauthenticated and tells us which auth_mode the login form
    // should render. Don't gate auth state on it — render OTP-only as a
    // fallback if it 502s (e.g. upstream Sinas down).
    client.auth.getInfo().then(setInfo).catch(() => setInfo(null));
    void refresh();
  }, []);

  const setSession = async (access: string, refresh_token: string) => {
    tokens.set(access, refresh_token);
    await refresh();
  };

  const signOut = async () => {
    const rt = tokens.refresh;
    tokens.clear();
    setMe(null);
    setStatus('unauthenticated');
    if (rt) {
      try {
        await client.auth.logout(rt);
      } catch {
        // best effort
      }
    }
  };

  return (
    <AuthContext.Provider value={{ status, me, info, setSession, signOut, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
