import { useApp } from '../store/appStore';

export default function LoadingScreen() {
  const { state } = useApp();
  const short = state.issueUrl.replace(/^https?:\/\//, '');

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-6 animate-in fade-in duration-300">
      <div className="relative w-14 h-14">
        <div className="absolute inset-0 rounded-full border-2 border-border border-t-primary animate-spin" />
        <div className="absolute inset-0 flex items-center justify-center text-xl">🔍</div>
      </div>

      <div className="flex flex-col items-center gap-2 text-center">
        <p className="text-base font-medium text-foreground">Gathering context...</p>
        <code className="px-3 py-1 rounded-full border border-border bg-muted/40 text-xs font-mono text-muted-foreground max-w-sm truncate">
          {short}
        </code>
        <p className="text-xs text-muted-foreground mt-1">This may take a minute while we clone and index the repo.</p>
      </div>
    </div>
  );
}