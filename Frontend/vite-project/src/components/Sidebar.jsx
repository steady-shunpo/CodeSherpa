import { useApp } from '../store/appStore';
import { Plus, MessageSquare, Settings } from 'lucide-react';

export default function Sidebar() {
  const { state } = useApp();
  const { sidebarOpen, chatHistory } = state;

  return (
    <aside
      className="shrink-0 flex flex-col bg-sidebar border-r border-sidebar-border overflow-hidden transition-all duration-250 ease-in-out"
      style={{ width: sidebarOpen ? 252 : 0, minWidth: sidebarOpen ? 252 : 0 }}
    >
      {/* Fixed-width inner so content doesn't squish during animation */}
      <div className="w-[252px] flex flex-col h-full py-3">

        <button className="mx-3 mb-4 flex items-center gap-2 px-3 py-2 rounded-lg border border-primary/40 bg-primary/10 text-primary text-sm font-medium hover:bg-primary/20 transition-colors">
          <Plus size={15} />
          New session
        </button>

        <p className="px-4 mb-2 text-[10px] font-semibold tracking-widest uppercase text-muted-foreground/60">
          Recent
        </p>

        <div className="flex-1 overflow-y-auto flex flex-col gap-0.5 px-2">
          {chatHistory.map(item => (
            <button
              key={item.id}
              className="group flex items-start gap-2.5 px-3 py-2.5 rounded-lg text-left hover:bg-sidebar-accent/10 transition-colors"
            >
              <MessageSquare size={13} className="mt-0.5 text-muted-foreground/50 shrink-0" />
              <div className="min-w-0">
                <p className="text-sm text-sidebar-foreground truncate">{item.title}</p>
                <p className="text-[11px] text-muted-foreground mt-0.5">{item.ts}</p>
              </div>
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="mx-3 pt-3 mt-2 border-t border-sidebar-border">
          <div className="flex items-center gap-2.5 px-1">
            <div className="w-7 h-7 rounded-full bg-gradient-to-br from-primary to-chart-2 flex items-center justify-center text-white text-[11px] font-semibold shrink-0">
              JD
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-sidebar-foreground truncate">John Dev</p>
              <p className="text-[11px] text-muted-foreground">Free plan</p>
            </div>
            <button className="p-1 rounded text-muted-foreground hover:text-sidebar-foreground transition-colors">
              <Settings size={13} />
            </button>
          </div>
        </div>
      </div>
    </aside>
  );
}