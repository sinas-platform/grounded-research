import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  GitMerge,
  PlayCircle,
  Sparkles,
} from 'lucide-react';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

interface IngestionRun {
  id: string;
  status: string;
  parts: string[];
  total_units: number;
  done_units: number;
  failed_units: number;
  created_at: string;
}

interface DiscoveryRun {
  id: string;
  kind: string;
  status: string;
  total_docs: number;
  scanned_docs: number;
  proposal_count: number;
  candidate_count: number;
  created_at: string;
}

interface Proposal {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  status: string;
  created_at: string;
}

interface RelationshipProposal {
  id: string;
  proposing_agent: string | null;
  reasoning: string | null;
  status: string;
}

interface SinasStatus {
  installed: boolean;
  package_name: string;
  installed_version: string | null;
  expected_version: string;
  drift: boolean;
  legacy_record: boolean;
  note: string | null;
}

export default function ActivityPage() {
  const ingestionRuns = useQuery({
    queryKey: ['ingestion-runs'],
    queryFn: () => api<IngestionRun[]>('/ingestion/runs?limit=10'),
    refetchInterval: 6000,
  });
  const discoveryRuns = useQuery({
    queryKey: ['discovery-runs'],
    queryFn: () => api<DiscoveryRun[]>('/discovery/runs?limit=10'),
    refetchInterval: 6000,
  });
  const configProposals = useQuery({
    queryKey: ['proposals', 'all-pending'],
    queryFn: () => api<Proposal[]>('/discovery/proposals?status_filter=pending&limit=200'),
    refetchInterval: 8000,
  });
  const relationshipProposals = useQuery({
    queryKey: ['relationship-proposals'],
    queryFn: () => api<RelationshipProposal[]>('/relationships/proposals?status_filter=pending'),
    refetchInterval: 8000,
  });
  const sinas = useQuery({
    queryKey: ['sinas-status'],
    queryFn: () => api<SinasStatus>('/sinas-status'),
  });

  const activeIngestion = (ingestionRuns.data ?? []).filter((r) =>
    ['pending', 'running'].includes(r.status),
  );
  const activeDiscovery = (discoveryRuns.data ?? []).filter((r) =>
    ['pending', 'scanning', 'consolidating'].includes(r.status),
  );

  return (
    <div>
      <PageHeader
        title="Activity"
        description="What's running, what's waiting on you, and the state of the Sinas integration."
      />

      {/* Top row: Sinas status banner */}
      {sinas.data && (
        <SinasBanner status={sinas.data} />
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <DashCard
          icon={<PlayCircle size={18} />}
          title="Ingestion runs"
          subtitle={
            activeIngestion.length > 0
              ? `${activeIngestion.length} active`
              : 'no active runs'
          }
          to="/ingestion/runs"
        >
          <RunsMini runs={ingestionRuns.data ?? []} />
        </DashCard>

        <DashCard
          icon={<Sparkles size={18} />}
          title="Discovery runs"
          subtitle={
            activeDiscovery.length > 0
              ? `${activeDiscovery.length} active`
              : 'no active runs'
          }
          to="/discovery"
        >
          <DiscoveryMini runs={discoveryRuns.data ?? []} />
        </DashCard>

        <DashCard
          icon={<GitMerge size={18} />}
          title="Config proposals"
          subtitle={`${(configProposals.data ?? []).length} awaiting review`}
          to="/discovery"
        >
          <ProposalsMini proposals={configProposals.data ?? []} />
        </DashCard>

        <DashCard
          icon={<GitMerge size={18} />}
          title="Relationship proposals"
          subtitle={`${(relationshipProposals.data ?? []).length} awaiting review`}
          to="/review/entities#relationships"
        >
          {(relationshipProposals.data ?? []).length === 0 ? (
            <Empty>No pending relationship proposals.</Empty>
          ) : (
            <ul className="space-y-1">
              {(relationshipProposals.data ?? []).slice(0, 5).map((p) => (
                <li key={p.id} className="text-sm text-stone-600 truncate">
                  {p.reasoning ?? <span className="text-stone-400">no reasoning</span>}
                </li>
              ))}
            </ul>
          )}
        </DashCard>
      </div>
    </div>
  );
}

// ─────────────── presentational ───────────────
function DashCard({
  icon,
  title,
  subtitle,
  to,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  to: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      to={to}
      className="block p-5 rounded-lg border border-stone-200 bg-white hover:border-primary-500 hover:shadow-sm transition"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-stone-700">
          <span className="text-primary-600">{icon}</span>
          <span className="font-semibold">{title}</span>
        </div>
        <span className="text-xs text-stone-500">{subtitle}</span>
      </div>
      {children}
    </Link>
  );
}

function SinasBanner({ status }: { status: SinasStatus }) {
  if (status.installed && !status.drift) {
    return (
      <div className="mb-6 flex items-start gap-2 px-4 py-2 rounded-md border border-primary-100 bg-primary-50 text-sm text-primary-800">
        <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
        <span>
          Sinas integration healthy — <span className="font-mono">{status.package_name}</span>{' '}
          <span className="font-mono">{status.installed_version}</span> installed.
          {status.note && <span className="block text-xs text-primary-700/80 mt-0.5">{status.note}.</span>}
        </span>
      </div>
    );
  }
  return (
    <Link
      to="/sinas-status"
      className="mb-6 flex items-center gap-2 px-4 py-2 rounded-md border border-amber-200 bg-amber-50 text-sm text-amber-900 hover:bg-amber-100 transition"
    >
      <AlertTriangle size={16} />
      <span>
        {!status.installed
          ? 'Sinas package not installed — SGR will not function until you install it.'
          : `Sinas package version drift — installed ${status.installed_version}, expected ${status.expected_version}.`}
      </span>
    </Link>
  );
}

function RunsMini({ runs }: { runs: IngestionRun[] }) {
  if (runs.length === 0) return <Empty>No ingestion runs yet.</Empty>;
  return (
    <ul className="space-y-2">
      {runs.slice(0, 5).map((r) => (
        <li key={r.id} className="text-sm">
          <div className="flex items-center justify-between">
            <span className="text-stone-700">
              <StatusDot status={r.status} />{' '}
              <span className="font-mono text-xs text-stone-500">{(r.parts ?? []).join(', ')}</span>
            </span>
            <span className="text-xs text-stone-500">
              {r.done_units}/{r.total_units}
              {r.failed_units > 0 && (
                <span className="text-amber-700"> · {r.failed_units} failed</span>
              )}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function DiscoveryMini({ runs }: { runs: DiscoveryRun[] }) {
  if (runs.length === 0) return <Empty>No discovery runs yet.</Empty>;
  return (
    <ul className="space-y-2">
      {runs.slice(0, 5).map((r) => (
        <li key={r.id} className="text-sm">
          <div className="flex items-center justify-between">
            <span className="text-stone-700">
              <StatusDot status={r.status} />{' '}
              <span className="font-mono text-xs text-stone-500">{r.kind}</span>
            </span>
            <span className="text-xs text-stone-500">
              {r.candidate_count} → {r.proposal_count}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}

function ProposalsMini({ proposals }: { proposals: Proposal[] }) {
  if (proposals.length === 0) return <Empty>No pending proposals.</Empty>;
  // Group by kind for a quick summary
  const byKind = new Map<string, number>();
  for (const p of proposals) byKind.set(p.kind, (byKind.get(p.kind) ?? 0) + 1);
  return (
    <ul className="space-y-1">
      {Array.from(byKind.entries()).map(([kind, count]) => (
        <li key={kind} className="text-sm text-stone-700 flex justify-between">
          <span className="font-mono text-xs text-stone-500">{kind}</span>
          <span className="text-xs">{count}</span>
        </li>
      ))}
    </ul>
  );
}

function StatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    pending: 'bg-stone-300',
    running: 'bg-blue-400',
    scanning: 'bg-blue-400',
    consolidating: 'bg-purple-400',
    completed: 'bg-primary-500',
    failed: 'bg-red-500',
    cancelled: 'bg-stone-400',
  };
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${colors[status] ?? 'bg-stone-300'}`}
      title={status}
    />
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-xs text-stone-400 italic flex items-center gap-2">
      <Clock size={12} />
      {children}
    </div>
  );
}
