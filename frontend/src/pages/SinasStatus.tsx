import { useQuery } from '@tanstack/react-query';
import { api, API_BASE, client } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

interface SinasStatus {
  sinas_url: string;
  package_name: string;
  expected_version: string;
  installed: boolean;
  installed_version: string | null;
  drift: boolean;
  note: string | null;
}

async function downloadBundledPackage() {
  const res = await client.fetch(`${API_BASE}/sinas-status/package.yaml`);
  if (!res.ok) {
    alert(`Download failed: ${res.status} ${res.statusText}`);
    return;
  }
  const text = await res.text();
  const blob = new Blob([text], { type: 'application/x-yaml' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'sinas-grounded-research.yaml';
  a.click();
  URL.revokeObjectURL(url);
}

export default function SinasStatusPage() {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['sinas-status'],
    queryFn: () => api<SinasStatus>('/sinas-status'),
  });

  return (
    <div>
      <PageHeader
        title="Sinas integration"
        description="Status of the sinas-grounded-research package on the Sinas instance SGR is talking to."
        actions={
          <div className="flex gap-2">
            <button
              onClick={downloadBundledPackage}
              className="px-3 py-1.5 rounded border border-stone-300 text-sm hover:bg-stone-100"
            >
              Download package YAML
            </button>
            <button
              onClick={() => refetch()}
              className="px-3 py-1.5 rounded border border-stone-300 text-sm hover:bg-stone-100"
            >
              Recheck
            </button>
          </div>
        }
      />
      {isLoading && <div className="text-stone-500">Checking…</div>}
      {data && (
        <div className="space-y-4">
          <Row label="Sinas URL" value={data.sinas_url} mono />
          <Row label="Package" value={data.package_name} mono />
          <Row label="Expected version" value={data.expected_version} mono />
          <Row
            label="Installed"
            value={
              data.installed ? (
                <span className="inline-flex items-center gap-2 px-2 py-0.5 rounded bg-primary-100 text-primary-700 text-xs">
                  installed
                </span>
              ) : (
                <span className="inline-flex items-center gap-2 px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs">
                  not installed
                </span>
              )
            }
          />
          {data.installed && (
            <Row
              label="Installed version"
              value={
                <span className={data.drift ? 'text-amber-700' : 'text-stone-700'}>
                  {data.installed_version}
                  {data.drift && <span className="ml-2 text-xs">(drift)</span>}
                </span>
              }
              mono
            />
          )}
          {data.note && (
            <div className="text-sm text-stone-600 bg-stone-100 border border-stone-200 rounded px-3 py-2">
              {data.note}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-center gap-4 py-1">
      <div className="w-40 text-xs font-medium text-stone-500 uppercase tracking-wider">
        {label}
      </div>
      <div className={`text-sm text-stone-900 ${mono ? 'font-mono' : ''}`}>{value}</div>
    </div>
  );
}
