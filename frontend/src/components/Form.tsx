import type { ReactNode } from 'react';

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <div className="text-xs font-medium text-stone-700 mb-1">
        {label}
        {hint && <span className="ml-2 text-stone-400 font-normal">{hint}</span>}
      </div>
      {children}
    </label>
  );
}

export const inputClasses =
  'w-full border border-stone-300 rounded px-2 py-1 text-sm';
export const textareaClasses =
  'w-full border border-stone-300 rounded px-2 py-1 text-sm font-mono';

export function PrimaryButton({
  children,
  disabled,
  onClick,
  type,
}: {
  children: ReactNode;
  disabled?: boolean;
  onClick?: () => void;
  type?: 'button' | 'submit';
}) {
  return (
    <button
      type={type ?? 'button'}
      onClick={onClick}
      disabled={disabled}
      className="px-3 py-1.5 rounded bg-primary-600 text-white text-sm hover:bg-primary-700 disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {children}
    </button>
  );
}

export function SecondaryButton({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-3 py-1.5 rounded border border-stone-300 text-sm hover:bg-stone-100"
    >
      {children}
    </button>
  );
}

export function DangerButton({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="px-2 py-1 rounded text-xs border border-red-300 text-red-700 hover:bg-red-50"
    >
      {children}
    </button>
  );
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
      {message}
    </div>
  );
}
