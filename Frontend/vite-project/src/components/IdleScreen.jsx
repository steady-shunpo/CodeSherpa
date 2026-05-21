import { useState } from 'react';
import { useApp } from '../store/appStore';
import { ArrowRight, Zap } from 'lucide-react';

const EXAMPLES = [
  'github.com/facebook/react/issues/12345',
  'github.com/vercel/next.js/issues/48291',
];

export default function IdleScreen() {
  const { dispatch } = useApp();
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');

  async function handleSubmit() {
    const trimmed = url.trim();
    if (!trimmed) return;
    if (!trimmed.match(/github\.com\/[^/]+\/[^/]+\/issues\/\d+/)) {
      setError('Please paste a valid GitHub issue URL.');
      return;
    }
    setError('');
    dispatch({ type: 'START_LOADING', url: trimmed });

    try {
      const res = await fetch(
        `http://localhost:8000/build-workspace?issue_url=${encodeURIComponent(trimmed)}`
      );
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Server error');
      }
      const data = await res.json();
      dispatch({
        type: 'LOADING_DONE',
        issue: {
          owner: data.owner,
          repo: data.repo,
          number: trimmed.match(/issues\/(\d+)/)[1],
          title: data.title,
          body: data.body,
          labels: [],
          state: 'open',
        },
      });
    } catch (e) {
      dispatch({ type: 'LOADING_ERROR', message: e.message });
    }
  }

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-8 p-10 animate-in fade-in duration-300">
      <div className="flex flex-col items-center gap-3 text-center">
        <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full border border-primary/40 bg-primary/10 text-primary text-xs font-medium">
          <Zap size={11} /> AI-powered debugging
        </span>
        <h1 className="text-4xl font-bold tracking-tight text-foreground leading-tight">
          Drop a GitHub issue.<br />Get a fix.
        </h1>
        <p className="text-muted-foreground text-base max-w-sm leading-relaxed">
          Paste any public GitHub issue link and CodeSherpa will trace, analyze, and patch it — step by step.
        </p>
      </div>

      <div className="w-full max-w-lg flex flex-col gap-2">
        <div className="flex gap-2 p-1.5 pl-4 bg-card border border-border rounded-xl focus-within:border-ring transition-colors shadow-sm">
          <input
            className="flex-1 bg-transparent text-sm font-mono text-foreground placeholder:text-muted-foreground/50 outline-none"
            placeholder="https://github.com/owner/repo/issues/123"
            value={url}
            onChange={e => { setUrl(e.target.value); setError(''); }}
            onKeyDown={e => e.key === 'Enter' && handleSubmit()}
          />
          <button
            onClick={handleSubmit}
            className="flex items-center gap-1.5 px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:opacity-90 transition-opacity"
          >
            Analyze <ArrowRight size={14} />
          </button>
        </div>
        {error && <p className="text-destructive text-xs pl-1">{error}</p>}
      </div>

      <div className="flex flex-col items-center gap-2">
        <p className="text-[10px] uppercase tracking-widest text-muted-foreground/50">Try an example</p>
        <div className="flex gap-2 flex-wrap justify-center">
          {EXAMPLES.map(ex => (
            <button
              key={ex}
              onClick={() => setUrl('https://' + ex)}
              className="px-3 py-1 rounded-full border border-border bg-muted/40 text-xs font-mono text-muted-foreground hover:border-ring hover:text-foreground transition-colors"
            >
              {ex}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}