import { useState } from 'react';
import type { AuthMode } from '@sinas/sdk';
import { client } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import {
  ErrorBanner,
  Field,
  PrimaryButton,
  SecondaryButton,
  inputClasses,
} from '@/components/Form';

type Step = 'credentials' | 'otp';

export default function LoginPage() {
  const { setSession, info } = useAuth();
  // Default to OTP-only if /info is still loading or unreachable — matches the
  // pre-refactor behavior so the screen still works against older Sinas builds.
  const authMode: AuthMode = info?.auth_mode ?? 'otp';
  const requiresPassword = authMode === 'password' || authMode === 'password+otp';
  const requiresOtp = authMode === 'otp' || authMode === 'password+otp';

  const [step, setStep] = useState<Step>('credentials');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [otp, setOtp] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submitCredentials = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await client.auth.login({
        email: email.trim(),
        password: requiresPassword ? password : undefined,
      });
      if (res.access_token && res.refresh_token) {
        // password-only mode — tokens are already issued
        await setSession(res.access_token, res.refresh_token);
      } else if (res.session_id) {
        setSessionId(res.session_id);
        setStep('otp');
      } else {
        setError('Unexpected response from /auth/login');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'login failed');
    } finally {
      setSubmitting(false);
    }
  };

  const verify = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!sessionId) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await client.auth.verifyOTP({
        session_id: sessionId,
        otp_code: otp.trim(),
      });
      await setSession(res.access_token, res.refresh_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'invalid code');
    } finally {
      setSubmitting(false);
    }
  };

  const credentialsCta = (() => {
    if (submitting) return 'Working…';
    if (authMode === 'password') return 'Sign in';
    if (authMode === 'password+otp') return 'Continue';
    return 'Send code';
  })();

  const credentialsHelp = (() => {
    if (authMode === 'password') return 'Sign in with your Sinas account.';
    if (authMode === 'password+otp') return 'Sign in — a verification code follows.';
    return 'Sign in with your Sinas account.';
  })();

  return (
    <div className="min-h-screen flex items-center justify-center bg-stone-50 px-4">
      <div className="w-full max-w-md p-8 bg-white border border-stone-200 rounded">
        <div className="text-xl font-semibold text-primary-700 mb-1">Sinas Grounded Research</div>
        <div className="text-sm text-stone-500 mb-6">
          {step === 'credentials' ? credentialsHelp : `We sent a code to ${email}.`}
        </div>

        {step === 'credentials' && (
          <form onSubmit={submitCredentials}>
            <Field label="Email">
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoFocus
                required
                placeholder="you@example.com"
                className={inputClasses}
              />
            </Field>
            {requiresPassword && (
              <Field label="Password">
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  placeholder="••••••••"
                  autoComplete="current-password"
                  className={inputClasses}
                />
              </Field>
            )}
            <ErrorBanner message={error} />
            <div className="mt-4">
              <PrimaryButton
                type="submit"
                disabled={submitting || !email.trim() || (requiresPassword && !password)}
              >
                {credentialsCta}
              </PrimaryButton>
            </div>
          </form>
        )}

        {step === 'otp' && requiresOtp && (
          <form onSubmit={verify}>
            <Field label="Verification code">
              <input
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={otp}
                onChange={(e) => setOtp(e.target.value)}
                autoFocus
                required
                placeholder="123456"
                className={`${inputClasses} font-mono tracking-widest text-lg`}
              />
            </Field>
            <ErrorBanner message={error} />
            <div className="mt-4 flex gap-2">
              <PrimaryButton type="submit" disabled={submitting || !otp.trim()}>
                {submitting ? 'Verifying…' : 'Verify'}
              </PrimaryButton>
              <SecondaryButton
                onClick={() => {
                  setStep('credentials');
                  setOtp('');
                  setError(null);
                }}
              >
                Back
              </SecondaryButton>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
