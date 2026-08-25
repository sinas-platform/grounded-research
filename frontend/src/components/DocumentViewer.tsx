import { useEffect, useMemo, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '@/lib/api';

/** A cited passage, addressed the way the corpus addresses passages: by line. */
export interface EvidenceSpan {
  line_from?: number | null;
  line_to?: number | null;
  note?: string | null;
}

interface DocumentMeta {
  id: string;
  filename: string;
  summary: string | null;
}

interface DocumentVersion {
  version: number;
}

interface DocumentContent {
  content: string;
  line_from: number | null;
  line_to: number | null;
  total_lines: number;
  is_truncated: boolean;
  extracted: boolean;
  note?: string;
}

// The read endpoint returns at most 1000 lines per call, so a long document is
// read through a moving window. Around a citation the window is centred on it;
// the context either side is generous enough to read the passage in place.
const MAX_WINDOW = 1000;
const SPAN_CONTEXT = 120;
const PAGE_STEP = 800;

function Markdown({ children }: { children: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}

interface FrontMatterEntry {
  key: string;
  value: string;
}

const unquote = (v: string) => v.replace(/^['"]/, '').replace(/['"]$/, '').trim();

/**
 * Reads the `key: value` block nearly every document in the corpus opens with.
 *
 * Handed to a markdown renderer this block is worse than useless: a text line
 * followed by `---` is a setext heading, so the whole header collapsed into one
 * bold run-on line. Parsed here instead, and rendered as the document's header.
 *
 * Deliberately not a full YAML parser — these blocks are flat `key: value`
 * pairs whose long values fold across indented continuation lines. Anything it
 * cannot attribute to a key is dropped rather than guessed at.
 */
function parseFrontMatter(lines: string[]): FrontMatterEntry[] {
  const out: FrontMatterEntry[] = [];
  for (const raw of lines) {
    const m = /^([A-Za-z_][\w.-]*):\s*(.*)$/.exec(raw);
    if (m && !/^\s/.test(raw)) {
      out.push({ key: m[1], value: unquote(m[2]) });
    } else if (out.length && raw.trim()) {
      const last = out[out.length - 1];
      last.value = `${last.value} ${raw.trim()}`.trim();
    }
  }
  return out.filter((e) => e.value !== '');
}

function FrontMatterHeader({ entries }: { entries: FrontMatterEntry[] }) {
  const title = entries.find((e) => e.key.toLowerCase() === 'title');
  const rest = entries.filter((e) => e !== title);
  return (
    <div className="mb-4 pb-3 border-b border-stone-200">
      {title && (
        <div className="text-sm font-semibold text-stone-900 leading-snug mb-2">
          {title.value}
        </div>
      )}
      {rest.length > 0 && (
        <dl className="flex flex-wrap gap-x-4 gap-y-1">
          {rest.map((e) => (
            <div key={e.key} className="flex items-baseline gap-1.5 min-w-0">
              <dt className="text-[10px] uppercase tracking-wider text-stone-400 shrink-0">
                {e.key.replace(/_/g, ' ')}
              </dt>
              <dd className="text-[11.5px] text-stone-700 font-mono break-words min-w-0">
                {e.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

/**
 * Renders a source document as markdown, highlighting a cited line range and
 * scrolling to it.
 *
 * The rendered text is split at the span's line boundaries and each part
 * rendered separately, so the highlight covers exactly the lines the evidence
 * records — no text matching, no guessing. A split can land inside a block (a
 * long table, say), which renders that block as two; the alternative is
 * hunting for the quoted text in the rendered output, which is far more likely
 * to highlight the wrong thing.
 */
export function DocumentViewer({
  docId,
  span,
}: {
  docId: string;
  span?: EvidenceSpan | null;
}) {
  const spanFrom = span?.line_from ?? null;
  const spanTo = span?.line_to ?? null;

  // Where the window starts. Centred on the citation when there is one.
  const [from, setFrom] = useState(() =>
    spanFrom != null ? Math.max(1, spanFrom - SPAN_CONTEXT) : 1,
  );

  const versions = useQuery({
    queryKey: ['document-versions', docId],
    queryFn: () => api<DocumentVersion[]>(`/documents/${docId}/versions`),
    enabled: !!docId,
  });
  const latest = versions.data?.length ? versions.data[versions.data.length - 1] : null;

  const meta = useQuery({
    queryKey: ['document-meta', docId],
    queryFn: () => api<DocumentMeta>(`/documents/${docId}`),
    enabled: !!docId,
  });

  const to = from + MAX_WINDOW - 1;
  const content = useQuery({
    queryKey: ['document-window', docId, latest?.version, from],
    queryFn: () =>
      api<DocumentContent>(
        `/documents/${docId}/versions/${latest!.version}/content` +
          `?line_from=${from}&line_to=${to}&max_lines=${MAX_WINDOW}`,
      ),
    enabled: !!latest,
  });

  const spanRef = useRef<HTMLDivElement>(null);
  const data = content.data;

  const parts = useMemo(() => {
    if (!data?.content) return null;
    const lines = data.content.split('\n');
    const windowFrom = data.line_from ?? 1;
    // The span's position inside this window, 0-based.
    const startIdx = spanFrom != null ? spanFrom - windowFrom : -1;
    const endIdx = spanTo != null ? spanTo - windowFrom : -1;
    const spanVisible = startIdx >= 0 && endIdx >= startIdx && startIdx < lines.length;

    // The header block, when this window holds the top of the document. If a
    // citation reaches into it, leave it in the body so the marked lines stay
    // where the evidence says they are.
    let front: FrontMatterEntry[] | null = null;
    let bodyStart = 0;
    if (windowFrom === 1 && lines[0]?.trim() === '---') {
      const close = lines.findIndex((l, i) => i > 0 && l.trim() === '---');
      if (close > 0 && (!spanVisible || startIdx > close)) {
        front = parseFrontMatter(lines.slice(1, close));
        bodyStart = close + 1;
      }
    }

    const body = lines.slice(bodyStart);
    const s = startIdx - bodyStart;
    const e = endIdx - bodyStart;
    const showSpan = spanVisible && s >= 0 && e >= s && s < body.length;
    return {
      front,
      before: showSpan ? body.slice(0, s).join('\n') : body.join('\n'),
      highlight: showSpan ? body.slice(s, e + 1).join('\n') : '',
      after: showSpan ? body.slice(e + 1).join('\n') : '',
    };
  }, [data, spanFrom, spanTo]);

  // Bring the cited passage into view once it has rendered.
  useEffect(() => {
    if (!parts?.highlight) return;
    const id = window.setTimeout(
      () => spanRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }),
      100,
    );
    return () => window.clearTimeout(id);
  }, [parts?.highlight, docId]);

  if (versions.isLoading || content.isLoading) {
    return <div className="text-stone-400 text-sm">Loading document…</div>;
  }
  if (versions.data && !latest) {
    return <div className="text-stone-400 text-sm italic">This document has no stored version.</div>;
  }
  if (content.error) {
    return <div className="text-stone-400 text-sm italic">Could not read this document's text.</div>;
  }
  if (data && !data.extracted) {
    return (
      <div className="text-stone-500 text-sm italic">
        {data.note ?? 'No extracted text for this version.'}
      </div>
    );
  }

  const shownFrom = data?.line_from ?? 1;
  const shownTo = data?.line_to ?? 0;
  const total = data?.total_lines ?? 0;
  const hasEarlier = shownFrom > 1;
  const hasLater = shownTo < total;

  return (
    <>
      {parts?.front && parts.front.length > 0 && <FrontMatterHeader entries={parts.front} />}
      {meta.data?.summary && (
        <p className="text-xs text-stone-600 italic border-l-2 border-primary-100 pl-3 mb-4">
          {meta.data.summary}
        </p>
      )}
      {(hasEarlier || hasLater) && (
        <div className="flex items-center gap-2 text-[11px] text-stone-500 bg-stone-50 border border-stone-200 rounded px-2.5 py-1.5 mb-3">
          <span>
            Lines {shownFrom}–{shownTo} of {total}
          </span>
          <span className="ml-auto flex gap-2">
            {hasEarlier && (
              <button
                onClick={() => setFrom(Math.max(1, shownFrom - PAGE_STEP))}
                className="text-primary-700 hover:underline font-medium"
              >
                ↑ earlier
              </button>
            )}
            {hasLater && (
              <button
                onClick={() => setFrom(shownFrom + PAGE_STEP)}
                className="text-primary-700 hover:underline font-medium"
              >
                ↓ later
              </button>
            )}
          </span>
        </div>
      )}
      {parts && (
        <>
          {parts.before && <Markdown>{parts.before}</Markdown>}
          {parts.highlight && (
            <div
              ref={spanRef}
              className="my-2 rounded-r border-l-[3px] border-amber-400 bg-amber-50/70 pl-3 pr-2 py-1"
            >
              <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-700 mb-1">
                cited passage · lines {spanFrom}–{spanTo}
              </div>
              <Markdown>{parts.highlight}</Markdown>
            </div>
          )}
          {parts.after && <Markdown>{parts.after}</Markdown>}
        </>
      )}
      {!parts && <div className="text-stone-400 text-sm italic">(empty)</div>}
    </>
  );
}

/** The viewer in a dialog, with the document's identity in the header. */
export function DocumentModal({
  docId,
  span,
  onClose,
}: {
  docId: string;
  span?: EvidenceSpan | null;
  onClose: () => void;
}) {
  const meta = useQuery({
    queryKey: ['document-meta', docId],
    queryFn: () => api<DocumentMeta>(`/documents/${docId}`),
    enabled: !!docId,
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 bg-stone-900/40 flex items-center justify-center p-8"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl max-w-3xl w-full max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-baseline gap-3 px-5 py-3.5 border-b border-stone-200 shrink-0">
          <div className="font-mono text-xs text-primary-600 truncate">
            {meta.data?.filename ?? '…'}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="ml-auto text-stone-400 hover:text-stone-900 shrink-0 self-center"
          >
            <X size={16} />
          </button>
        </div>
        <div className="overflow-y-auto px-5 py-4">
          <DocumentViewer docId={docId} span={span} />
        </div>
      </div>
    </div>
  );
}
