import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { PageHeader } from '@/components/PageHeader';

interface Result {
  id: string;
  query: string;
  status: string;
  invoked_skill_names: string[] | null;
  published_at: string | null;
}

export default function ResultsPage() {
  const { data } = useQuery({
    queryKey: ['results'],
    queryFn: () => api<Result[]>('/results'),
  });

  return (
    <div>
      <PageHeader
        title="Results"
        description="Published search results — first-class artifacts produced by deep_search_agent + search_orchestrator."
      />
      <div className="space-y-2">
        {(data ?? []).map((r) => (
          <div key={r.id} className="p-4 border border-stone-200 rounded bg-white">
            <div className="flex items-baseline justify-between">
              <div className="font-medium">{r.query}</div>
              <div
                className={`text-xs px-2 py-0.5 rounded ${
                  r.status === 'published'
                    ? 'bg-primary-100 text-primary-700'
                    : 'bg-stone-200 text-stone-600'
                }`}
              >
                {r.status}
              </div>
            </div>
            {r.invoked_skill_names && r.invoked_skill_names.length > 0 && (
              <div className="text-xs text-stone-500 mt-1">
                via {r.invoked_skill_names.join(', ')}
              </div>
            )}
          </div>
        ))}
        {data && data.length === 0 && (
          <div className="text-stone-500 text-sm py-12 text-center border border-dashed border-stone-300 rounded">
            No results yet.
          </div>
        )}
      </div>
    </div>
  );
}
