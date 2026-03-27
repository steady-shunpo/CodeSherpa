import { useApp } from '../store/appStore';
import { useTheme } from '../hooks/useTheme';
import { PanelLeft, GitBranch, Sun, Moon } from 'lucide-react';

export default function Navbar() {
  const { state, dispatch } = useApp();
  const { issue, phase } = state;
  const { theme, toggle } = useTheme();
  const showIssue = issue && phase !== 'idle' && phase !== 'loading';

  return (
    <header className="h-13 flex items-center gap-3 px-4 border-b border-border bg-card shrink-0 z-10">
      <button
        onClick={() => dispatch({ type: 'TOGGLE_SIDEBAR' })}
        className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
      >
        <PanelLeft size={18} />
      </button>

      {/* Logo */}
      <div className="flex items-center gap-2">
        <div className="w-7 h-7 rounded-lg bg-primary flex items-center justify-center text-primary-foreground text-xs font-bold tracking-tight">
          CS
        </div>
        <span className="font-semibold text-sm tracking-tight text-foreground">CodeSherpa</span>
      </div>

      <div className="flex-1" />

      {/* Active issue chip */}
      {showIssue && (
        <div className="flex items-center gap-2 px-3 py-1 rounded-full border border-border bg-muted/40 text-xs font-mono text-muted-foreground">
          <GitBranch size={11} className="text-accent" />
          {issue.owner}/{issue.repo}#{issue.number}
          <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse" />
        </div>
      )}

      {/* Theme toggle */}
      <button
        onClick={toggle}
        title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors"
      >
        {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
      </button>

      {/* Avatar */}
      <button className="w-8 h-8 rounded-full bg-gradient-to-br from-primary to-chart-2 flex items-center justify-center text-white text-xs font-semibold hover:opacity-90 transition-opacity">
        JD
      </button>
    </header>
  );
}