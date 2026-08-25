import { useEffect } from 'react';
import { useApp } from '../store/appStore';
// import { streamAgent } from '../utils/api';
import AgentCard from './AgentCard';
import { cancelRun, useSSEStream } from '../utils/api';
import { useParams } from 'react-router-dom';



export default function PipelinePanel() {
  const { state, dispatch } = useApp();
  const { agents, currentAgentIdx, turnUsed } = state;
  const { runId } = useParams()

  useSSEStream(runId, currentAgentIdx, turnUsed);

  function handleStopRun(){
    cancelRun(runId);
  }

  // const doneCount = agents.filter(a => a.status === 'done').length;
  // const progress = (doneCount / agents.length) * 100;

  // Drive the current agent's stream
  // useEffect(() => {
  //   const agent = agents[currentAgentIdx];
  //   if (!agent || agent.status !== 'running') return;

  //   let cancelled = false;
  //   async function run() {
  //     for await (const chunk of streamAgent(agent.id)) {
  //       if (cancelled) return;
  //       dispatch({ type: 'AGENT_STREAM_CHUNK', idx: currentAgentIdx, chunk });
  //     }
  //     if (!cancelled) {
  //       dispatch({ type: 'AGENT_AWAITING', idx: currentAgentIdx });
  //     }
  //   }
  //   // run();
  //   return () => { cancelled = true; };
  // }, [currentAgentIdx, agents[currentAgentIdx]?.status]);

  // Progress bar
  const doneCount = agents.filter(a => a.status === 'done').length;
  const progress = (doneCount / agents.length) * 100;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-border shrink-0">
  <div className="flex items-center justify-between mb-2">
    <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground/60">
      Pipeline
    </p>

    <div className="flex items-center gap-2">
      <span className="text-xs text-muted-foreground">
        {doneCount}/{agents.length} done
      </span>

      <button
        onClick={handleStopRun}
        className="flex items-center gap-1.5 px-3 py-1.5 bg-red-500 text-white text-xs font-medium rounded-lg hover:opacity-90 transition-opacity"
      >
        Stop Run
      </button>
    </div>
  </div>

  {/* Progress */}
  <div className="h-1 rounded-full bg-muted overflow-hidden">
    <div
      className="h-full rounded-full bg-primary transition-all duration-500"
      style={{ width: `${progress}%` }}
    />
  </div>
</div>

      {/* Agent cards */}
      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-2.5">
        {agents.map((agent, idx) => (
          <AgentCard key={agent.id} agent={agent} idx={idx} />
        ))}
      </div>
    </div>
  );
}