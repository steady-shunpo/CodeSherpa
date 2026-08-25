import { useNavigate, useParams } from 'react-router-dom';
import { useApp } from '../store/appStore';
import { useRunsList } from '../hooks/useRunList';
import { Plus, MessageSquare, Settings, LogOut } from 'lucide-react';
import { useState, useRef, useEffect } from 'react';
import { getCurrentUser, logout } from '../utils/auth';

const RESUMABLE = new Set(['paused', 'awaiting_intervention', 'awaiting_more_turns']);

function timeAgo(dateStr) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days === 1) return 'Yesterday';
  return `${days}d ago`;
}

function statusBadge(status) {
  if (RESUMABLE.has(status)) return 'text-yellow-500';
  if (status === 'succeeded') return 'text-green-500';
  if (status === 'failed') return 'text-red-500';
  return 'text-muted-foreground';
}

function initials(name, email) {
  if (name) {
    const parts = name.trim().split(/\s+/);
    return parts.length > 1
      ? (parts[0][0] + parts[1][0]).toUpperCase()
      : parts[0].slice(0, 2).toUpperCase();
  }
  return email ? email.slice(0, 2).toUpperCase() : '?';
}

export default function Sidebar() {
  const { state } = useApp();
  const { sidebarOpen, runs, runsLoading } = state;
  const { runId: activeRunId } = useParams();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);

  useRunsList();

  const user = getCurrentUser();

  useEffect(() => {
    function handleClickOutside(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleClick = (run) => {
    navigate(`/runs/${run.id}`)
  }

  const handleLogout = () => {
    setMenuOpen(false);
    logout(navigate);
  };

  return (
    <aside
      className="shrink-0 flex flex-col bg-sidebar border-r border-sidebar-border overflow-hidden transition-all duration-250 ease-in-out"
      style={{ width: sidebarOpen ? 252 : 0, minWidth: sidebarOpen ? 252 : 0 }}
    >
      <div className="w-[252px] flex flex-col h-full py-3">

        <button
          onClick={() => navigate('/')}
          className="mx-3 mb-4 flex items-center gap-2 px-3 py-2 rounded-lg border border-primary/40 bg-primary/10 text-primary text-sm font-medium hover:bg-primary/20 transition-colors"
        >
          <Plus size={15} />
          New session
        </button>

        <p className="px-4 mb-2 text-[10px] font-semibold tracking-widest uppercase text-muted-foreground/60">
          Recent
        </p>

        <div className="flex-1 overflow-y-auto flex flex-col gap-0.5 px-2">
          {runsLoading && (
            <p className="px-3 py-2 text-xs text-muted-foreground">Loading...</p>
          )}
          {!runsLoading && runs.length === 0 && (
            <p className="px-3 py-2 text-xs text-muted-foreground">No runs yet</p>
          )}
          {runs.map(run => (
            <button
              key={run.id}
              onClick={() => handleClick(run)}
              className={`group flex items-start gap-2.5 px-3 py-2.5 rounded-lg text-left transition-colors
                ${run.id === activeRunId
                  ? 'bg-sidebar-accent/20'
                  : 'hover:bg-sidebar-accent/10'
                }`}
            >
              <MessageSquare size={13} className={`mt-0.5 shrink-0 ${statusBadge(run.status)}`} />
              <div className="min-w-0">
                <p className="text-sm text-sidebar-foreground truncate">
                  {run.issue_url.replace('https://https://github.com/', '')}
                </p>
                <p className="text-[11px] text-muted-foreground mt-0.5">
                  {timeAgo(run.created_at)}
                </p>
              </div>
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="mx-3 pt-3 mt-2 border-t border-sidebar-border relative" ref={menuRef}>
          <div className="flex items-center gap-2.5 px-1">
            {user?.picture ? (
              <img
                src={user.picture}
                alt={user.name ?? user.email}
                className="w-7 h-7 rounded-full shrink-0 object-cover"
              />
            ) : (
              <div className="w-7 h-7 rounded-full bg-gradient-to-br from-primary to-chart-2 flex items-center justify-center text-white text-[11px] font-semibold shrink-0">
                {initials(user?.name, user?.email)}
              </div>
            )}
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-sidebar-foreground truncate">
                {user?.name ?? user?.email ?? 'Unknown user'}
              </p>
              <p className="text-[11px] text-muted-foreground truncate">
                {user?.email ?? 'Free plan'}
              </p>
            </div>
            <button
              onClick={() => setMenuOpen(o => !o)}
              className="p-1 rounded text-muted-foreground hover:text-sidebar-foreground transition-colors"
            >
              <Settings size={13} />
            </button>
          </div>

          {menuOpen && (
            <div className="absolute bottom-full right-0 mb-1 w-40 bg-card border border-border rounded-lg shadow-lg py-1 z-10">
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left text-destructive hover:bg-muted/50 transition-colors"
              >
                <LogOut size={13} />
                Log out
              </button>
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}