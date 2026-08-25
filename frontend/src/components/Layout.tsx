import { NavLink, Outlet } from 'react-router-dom';
import { Activity, CheckSquare, FileText, LucideIcon, Network, Play, Sparkles } from 'lucide-react';
import { useAuth } from '@/lib/auth';

// The two orange slices from the Sinas console logo (wordmark dropped —
// the product name is written next to it in text).
function SinasMark({ className }: { className?: string }) {
  return (
    <svg viewBox="75 125 415 325" fill="none" className={className} aria-hidden>
      <path
        d="M121.163 240.205C171.757 189.611 251.576 186.054 306.279 229.533L110.492 425.32C67.0129 370.617 70.5696 290.799 121.163 240.205Z"
        stroke="#E97203"
        strokeWidth="32"
      />
      <path
        d="M444.837 344.179C495.43 293.585 498.988 213.767 455.508 159.064L259.722 354.85C314.425 398.329 394.243 394.773 444.837 344.179Z"
        stroke="#E97203"
        strokeWidth="32"
      />
    </svg>
  );
}

// Daily work first; the two setup surfaces sit below a divider. Schema and
// Discovery define the corpus once (and when a new source arrives) — they are
// not places anyone should be every day.
const links = [
  { to: '/runs', label: 'Runs', icon: Play },
  { to: '/documents', label: 'Documents', icon: FileText },
  { to: '/review/entities', label: 'Entity review', icon: CheckSquare },
  { to: '/activity', label: 'Activity', icon: Activity },
];

const setupLinks = [
  { to: '/schema', label: 'Schema', icon: Network },
  { to: '/discovery', label: 'Discovery', icon: Sparkles },
];

function NavItem({
  to, label, Icon,
}: {
  to: string;
  label: string;
  Icon: LucideIcon;
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors ${
          isActive
            ? 'bg-primary-50 text-primary-700 font-medium'
            : 'text-stone-600 hover:text-stone-900 hover:bg-stone-100'
        }`
      }
    >
      <Icon size={16} strokeWidth={2} />
      {label}
    </NavLink>
  );
}

export function Layout() {
  const { me, signOut } = useAuth();
  return (
    <div className="min-h-screen flex bg-page">
      <aside className="w-60 border-r border-stone-200 bg-white flex flex-col">
        <div className="px-5 pt-6 pb-8">
          <div className="flex items-center gap-2.5">
            <SinasMark className="h-6 w-7 shrink-0" />
            <div className="text-base font-semibold tracking-tight text-stone-900">
              Grounded Research
            </div>
          </div>
          <div className="text-[11px] text-stone-400 uppercase tracking-wider mt-1 pl-[38px]">
            alpha
          </div>
        </div>
        <nav className="flex-1 flex flex-col px-3 gap-0.5">
          {links.map(({ to, label, icon: Icon }) => (
            <NavItem key={to} to={to} label={label} Icon={Icon} />
          ))}
          <div className="mt-6 mb-1 px-3 text-[10.5px] font-semibold text-stone-400 uppercase tracking-wider">
            Corpus setup
          </div>
          {setupLinks.map(({ to, label, icon: Icon }) => (
            <NavItem key={to} to={to} label={label} Icon={Icon} />
          ))}
        </nav>
        {me && (
          <div className="px-5 py-4 border-t border-stone-200 text-xs text-stone-500">
            <div className="text-stone-700 truncate mb-0.5">
              {me.is_admin ? 'admin' : me.roles.length ? me.roles.join(', ') : 'user'}
            </div>
            <div className="flex items-center justify-between">
              <span className="font-mono text-stone-400">{me.auth_mode}</span>
              <button
                onClick={signOut}
                className="text-stone-500 hover:text-stone-900 underline-offset-2 hover:underline"
              >
                Sign out
              </button>
            </div>
          </div>
        )}
      </aside>
      <main className="flex-1 px-10 py-10 overflow-auto">
        <div className="max-w-6xl">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
