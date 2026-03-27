import { createContext, useContext, useReducer } from 'react';

export const AGENTS = [
  { id: 'reader',   name: 'Issue Reader',     desc: 'Parses the issue, extracts context and reproduction steps.' },
  { id: 'scanner',  name: 'Codebase Scanner', desc: 'Traces call chains and locates relevant files.' },
  { id: 'patcher',  name: 'Patch Writer',      desc: 'Generates a minimal diff that fixes the root cause.' },
  { id: 'reviewer', name: 'Reviewer',          desc: 'Validates the patch and suggests regression tests.' },
];

const initialState = {
  sidebarOpen: true,
  chatHistory: [
    { id: 'h1', title: 'NullPointer in UserController', ts: '2h ago' },
    { id: 'h2', title: 'Race condition on login',       ts: 'Yesterday' },
    { id: 'h3', title: 'Memory leak in EventBus',       ts: '3d ago' },
    { id: 'h4', title: 'Timeout on large payloads',     ts: '5d ago' },
  ],
  // 'idle' | 'loading' | 'chat' | 'pipeline'
  phase: 'idle',
  issueUrl: '',
  issue: null,
  messages: [],
  agents: AGENTS.map(a => ({ ...a, status: 'waiting', stream: [], expanded: false })),
  currentAgentIdx: -1,
};

function reducer(state, action) {
  switch (action.type) {

    case 'TOGGLE_SIDEBAR':
      return { ...state, sidebarOpen: !state.sidebarOpen };

    case 'START_LOADING':
      return { ...state, phase: 'loading', issueUrl: action.url };

    case 'LOADING_DONE':
      return {
        ...state,
        phase: 'chat',
        issue: action.issue,
        messages: [{
          id: 'init',
          role: 'assistant',
          text: `I've loaded **${action.issue.title}**. I can see this involves ${action.issue.body}. Ask me anything, or hit **Start Pipeline** to run the full automated analysis.`,
          ts: Date.now(),
        }],
      };

    case 'ADD_MESSAGE':
      return { ...state, messages: [...state.messages, action.msg] };

    case 'START_PIPELINE':
      return {
        ...state,
        phase: 'pipeline',
        currentAgentIdx: 0,
        agents: state.agents.map((a, i) => ({
          ...a, status: i === 0 ? 'running' : 'waiting', stream: [], expanded: i === 0,
        })),
      };

    case 'AGENT_STREAM_CHUNK': {
      const agents = state.agents.map((a, i) =>
        i === action.idx ? { ...a, stream: [...a.stream, action.chunk] } : a
      );
      return { ...state, agents };
    }

    case 'AGENT_AWAITING': {
      const agents = state.agents.map((a, i) =>
        i === action.idx ? { ...a, status: 'awaiting' } : a
      );
      return { ...state, agents };
    }

    case 'AGENT_CONTINUE': {
      const next = state.currentAgentIdx + 1;
      const finished = next >= state.agents.length;
      const agents = state.agents.map((a, i) => {
        if (i === state.currentAgentIdx) return { ...a, status: 'done', expanded: false };
        if (!finished && i === next) return { ...a, status: 'running', expanded: true };
        return a;
      });
      return {
        ...state, agents,
        currentAgentIdx: finished ? state.currentAgentIdx : next,
        phase: finished ? 'chat' : 'pipeline',
        messages: finished
          ? [...state.messages, { id: 'fin' + Date.now(), role: 'assistant', text: 'Pipeline complete. All agents have finished. Review the results above, or ask me to explain any step.', ts: Date.now() }]
          : state.messages,
      };
    }

    case 'AGENT_RERUN': {
      const agents = state.agents.map((a, i) =>
        i === state.currentAgentIdx ? { ...a, status: 'running', stream: [], expanded: true } : a
      );
      return { ...state, agents };
    }

    case 'STOP_PIPELINE': {
      const agents = state.agents.map((a, i) =>
        i === state.currentAgentIdx ? { ...a, status: 'stopped' } : a
      );
      return {
        ...state, agents, phase: 'chat',
        messages: [...state.messages, {
          id: 'stop' + Date.now(), role: 'assistant',
          text: `Pipeline paused at **${state.agents[state.currentAgentIdx]?.name}**. I have full context of everything found so far — ask me anything or dive into the results above.`,
          ts: Date.now(),
        }],
      };
    }

    case 'TOGGLE_AGENT_EXPAND': {
      const agents = state.agents.map((a, i) =>
        i === action.idx ? { ...a, expanded: !a.expanded } : a
      );
      return { ...state, agents };
    }

    default: return state;
  }
}

const Ctx = createContext(null);
export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return <Ctx.Provider value={{ state, dispatch }}>{children}</Ctx.Provider>;
}
export const useApp = () => useContext(Ctx);