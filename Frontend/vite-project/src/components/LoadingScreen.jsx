import { useApp } from '../store/appStore';

const STEPS = ['Fetching issue metadata', 'Cloning repo index', 'Building context window'];

export default function LoadingScreen() {
  const { state } = useApp();
  const short = state.issueUrl.replace(/^https?:\/\//, '');

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-8 animate-in fade-in duration-300">
      {/* Spinner */}
      <div className="relative w-14 h-14">
        <div className="absolute inset-0 rounded-full border-2 border-border border-t-primary animate-spin" />
        <div className="absolute inset-0 flex items-center justify-center text-xl">🔍</div>
      </div>

      <div className="flex flex-col items-center gap-2 text-center">
        <p className="text-base font-medium text-foreground">Reading repository...</p>
        <code className="px-3 py-1 rounded-full border border-border bg-muted/40 text-xs font-mono text-muted-foreground max-w-sm truncate">
          {short}
        </code>
      </div>

      {/* Step shimmer list */}
      <div className="flex flex-col gap-2.5 w-64">
        {STEPS.map((step, i) => (
          <div
            key={step}
            className="flex items-center gap-3 animate-in fade-in slide-in-from-bottom-2"
            style={{ animationDelay: `${i * 400}ms`, animationFillMode: 'both' }}
          >
            <div
              className="h-7 flex-1 rounded-lg flex items-center px-3"
              style={{
                background: 'linear-gradient(90deg, var(--muted) 25%, var(--secondary) 50%, var(--muted) 75%)',
                backgroundSize: '200% 100%',
                animation: `shimmer 1.6s ease infinite ${i * 300}ms`,
              }}
            >
              <span className="text-xs text-muted-foreground">{step}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}