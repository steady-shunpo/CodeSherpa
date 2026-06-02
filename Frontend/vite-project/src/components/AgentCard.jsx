import { useState } from 'react';
import { useApp } from '../store/appStore';
import { RichText } from '../utils/text';
import { ChevronDown, CheckCircle2, Circle, Loader2, XCircle, Clock } from 'lucide-react';
import { resumeRun, continueRun, cancelRun } from '../utils/api';

const STATUS_META = {
  waiting: { icon: Circle, iconClass: 'text-muted-foreground/40', label: 'Waiting', labelClass: 'text-muted-foreground/50' },
  running: { icon: Loader2, iconClass: 'text-primary animate-spin', label: 'Running...', labelClass: 'text-primary' },
  awaiting: { icon: Clock, iconClass: 'text-accent', label: 'Awaiting review', labelClass: 'text-accent' },
  done: { icon: CheckCircle2, iconClass: 'text-chart-4', label: 'Done', labelClass: 'text-chart-4' },
  stopped: { icon: XCircle, iconClass: 'text-destructive', label: 'Stopped', labelClass: 'text-destructive' },
};

export default function AgentCard({ agent, idx }) {
  const { state, dispatch } = useApp();
  const { runId, runStatus } = state;
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackText, setFeedbackText] = useState('');
  const [isActing, setIsActing] = useState(false);
  const [extraTurns, setExtraTurns] = useState(10);

  const meta = STATUS_META[agent.status] || STATUS_META.waiting;
  const Icon = meta.icon;
  const canExpand = agent.stream.length > 0;
  const isAwaiting = agent.status === 'awaiting';
  const isDimmed = agent.status === 'waiting';

  function toggle() {
    if (!canExpand) return;
    dispatch({ type: 'TOGGLE_AGENT_EXPAND', idx });
  }

  async function handleContinue() {
    if (!runId || isActing) return;
    setIsActing(true);
    try {
      if (runStatus === 'awaiting_more_turns') {
        await continueRun(runId, extraTurns, null);
        dispatch({ type: 'AGENT_GRANT_TURNS' }); // ← not AGENT_CONTINUE
      } else {
        await resumeRun(runId);
        dispatch({ type: 'AGENT_CONTINUE' });
      }
    } catch (e) {
      console.error('Continue failed:', e.message);
    } finally {
      setIsActing(false);
    }
  }

  async function submitFeedback() {
    if (!feedbackText.trim() || !runId || isActing) return;
    setIsActing(true);
    try {
      if (runStatus === 'awaiting_more_turns') {
        // grant turns with feedback
        await continueRun(runId, extraTurns, feedbackText.trim());
        dispatch({ type: 'AGENT_GRANT_TURNS' });
      } else {
        // intervention: resume with feedback + extra turns
        await resumeRun(runId, null, feedbackText.trim(), extraTurns);
        dispatch({ type: 'AGENT_RERUN' });
      }
      setFeedbackText('');
      setFeedbackOpen(false);
    } catch (e) {
      console.error('Feedback failed:', e.message);
    } finally {
      setIsActing(false);
    }
  }

  async function handleStop() {
    if (!runId || isActing) return;
    setIsActing(true);
    try {
      await cancelRun(runId);
      dispatch({ type: 'STOP_PIPELINE' });
    } catch (e) {
      console.error('Cancel failed:', e.message);
    } finally {
      setIsActing(false);
    }
  }



  return (
    <div
      className={[
        'rounded-xl border bg-card transition-all duration-200',
        isAwaiting ? 'border-accent/50 shadow-[0_0_0_1px_rgba(0,255,204,0.12)]' : 'border-border',
        isDimmed ? 'opacity-40' : '',
      ].join(' ')}
    >
      {/* Header */}
      <div
        className={[
          'flex items-center gap-3 px-4 py-3 rounded-xl',
          canExpand ? 'cursor-pointer hover:bg-muted/30 transition-colors' : '',
          agent.expanded ? 'rounded-b-none border-b border-border' : '',
        ].join(' ')}
        onClick={toggle}
      >
        <Icon size={15} className={meta.iconClass} />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium text-card-foreground">{agent.name}</p>
          <p className="text-xs text-muted-foreground truncate">{agent.desc}</p>
        </div>
        <span className={`text-xs font-medium ${meta.labelClass}`}>{meta.label}</span>
        {canExpand && (
          <ChevronDown
            size={14}
            className={`text-muted-foreground transition-transform duration-200 ${agent.expanded ? 'rotate-180' : ''}`}
          />
        )}
      </div>

      {/* Stream */}
      {agent.expanded && agent.stream.length > 0 && (
        <div className="px-4 py-3 border-b border-border">
          <RichText
            text={agent.stream.join('')}
            className="text-xs text-muted-foreground leading-relaxed"
          />
          {agent.status === 'running' && (
            <div className="flex items-center gap-1.5 mt-2">
              {[0, 1, 2].map(i => (
                <span
                  key={i}
                  className="w-1 h-1 rounded-full bg-muted-foreground/40 animate-pulse"
                  style={{ animationDelay: `${i * 180}ms` }}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Running indicator (no stream yet) */}
      {agent.status === 'running' && agent.stream.length === 0 && (
        <div className="px-4 py-3 border-b border-border flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 size={12} className="animate-spin text-primary" />
          Starting agent...
        </div>
      )}

      {isAwaiting && (
        <div className="px-4 py-3">
          {runStatus === 'awaiting_more_turns' ? (
            /* ── Turn grant UI ────────────────────────────────── */
            !feedbackOpen ? (
              <div className="flex flex-wrap gap-2 items-center">
                <div className="flex items-center gap-1.5 mr-1">
                  <span className="text-xs text-muted-foreground">Turns:</span>
                  <button
                    onClick={() => setExtraTurns(t => Math.max(1, t - 5))}
                    className="w-6 h-6 flex items-center justify-center rounded border border-border text-xs hover:bg-muted/40"
                  >−</button>
                  <span className="text-xs font-mono w-6 text-center">{extraTurns}</span>
                  <button
                    onClick={() => setExtraTurns(t => Math.min(100, t + 5))}
                    className="w-6 h-6 flex items-center justify-center rounded border border-border text-xs hover:bg-muted/40"
                  >+</button>
                </div>
                <button
                  onClick={handleContinue}
                  disabled={isActing}
                  className="px-4 py-1.5 bg-primary text-primary-foreground text-xs font-medium rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                >
                  {isActing ? 'Working…' : 'Grant turns →'}
                </button>
                <button
                  onClick={() => setFeedbackOpen(true)}
                  disabled={isActing}
                  className="px-3 py-1.5 border border-border text-xs text-muted-foreground rounded-lg hover:bg-muted/40 hover:text-foreground transition-colors disabled:opacity-50"
                >
                  Give feedback
                </button>
                <button
                  onClick={handleStop}
                  disabled={isActing}
                  className="px-3 py-1.5 border border-destructive/50 text-destructive bg-destructive/5 text-xs rounded-lg hover:bg-destructive/10 transition-colors disabled:opacity-50"
                >
                  Stop & chat
                </button>
              </div>
            ) : (
              /* feedback form for awaiting_more_turns — no turns stepper, already visible above */
              <div className="flex flex-col gap-2">
                <textarea
                  className="w-full bg-muted/40 border border-border rounded-lg px-3 py-2 text-xs text-foreground placeholder:text-muted-foreground/50 resize-none outline-none focus:border-ring transition-colors min-h-[72px] font-sans"
                  placeholder="Tell the agent what to change or reconsider..."
                  value={feedbackText}
                  onChange={e => setFeedbackText(e.target.value)}
                  autoFocus
                />
                <div className="flex gap-2">
                  <button
                    onClick={submitFeedback}
                    disabled={isActing}
                    className="px-3 py-1.5 bg-primary text-primary-foreground text-xs font-medium rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                  >
                    {isActing ? 'Sending…' : 'Grant turns →'}
                  </button>
                  <button
                    onClick={() => { setFeedbackOpen(false); setFeedbackText(''); }}
                    disabled={isActing}
                    className="px-3 py-1.5 border border-border text-xs text-muted-foreground rounded-lg hover:bg-muted/40 transition-colors disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )
          ) : (
            /* ── Normal intervention UI ───────────────────────── */
            !feedbackOpen ? (
              <div className="flex flex-wrap gap-2 items-center">
                <button
                  onClick={handleContinue}
                  disabled={isActing}
                  className="px-4 py-1.5 bg-primary text-primary-foreground text-xs font-medium rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                >
                  {isActing ? 'Working…' : 'Continue →'}
                </button>
                <button
                  onClick={() => setFeedbackOpen(true)}
                  disabled={isActing}
                  className="px-3 py-1.5 border border-border text-xs text-muted-foreground rounded-lg hover:bg-muted/40 hover:text-foreground transition-colors disabled:opacity-50"
                >
                  Give feedback
                </button>
                <button
                  onClick={handleStop}
                  disabled={isActing}
                  className="px-3 py-1.5 border border-destructive/50 text-destructive bg-destructive/5 text-xs rounded-lg hover:bg-destructive/10 transition-colors disabled:opacity-50"
                >
                  Stop & chat
                </button>
              </div>
            ) : (
              /* feedback form for awaiting_intervention — includes turns stepper */
              <div className="flex flex-col gap-2">
                <textarea
                  className="w-full bg-muted/40 border border-border rounded-lg px-3 py-2 text-xs text-foreground placeholder:text-muted-foreground/50 resize-none outline-none focus:border-ring transition-colors min-h-[72px] font-sans"
                  placeholder="Tell the agent what to change or reconsider..."
                  value={feedbackText}
                  onChange={e => setFeedbackText(e.target.value)}
                  autoFocus
                />
                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-muted-foreground">Extra turns:</span>
                  <button
                    onClick={() => setExtraTurns(t => Math.max(1, t - 5))}
                    className="w-6 h-6 flex items-center justify-center rounded border border-border text-xs hover:bg-muted/40"
                  >−</button>
                  <span className="text-xs font-mono w-6 text-center">{extraTurns}</span>
                  <button
                    onClick={() => setExtraTurns(t => Math.min(100, t + 5))}
                    className="w-6 h-6 flex items-center justify-center rounded border border-border text-xs hover:bg-muted/40"
                  >+</button>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={submitFeedback}
                    disabled={isActing}
                    className="px-3 py-1.5 bg-primary text-primary-foreground text-xs font-medium rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
                  >
                    {isActing ? 'Sending…' : 'Send feedback'}
                  </button>
                  <button
                    onClick={() => { setFeedbackOpen(false); setFeedbackText(''); }}
                    disabled={isActing}
                    className="px-3 py-1.5 border border-border text-xs text-muted-foreground rounded-lg hover:bg-muted/40 transition-colors disabled:opacity-50"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}