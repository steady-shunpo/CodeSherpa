import { createContext, useContext, useReducer } from 'react';

export const AGENTS = [
  {
    id: 'planner',
    name: 'Planner',
    desc: 'Reads the issue, builds a plan, and identifies the files and components involved.',
  },
  {
    id: 'hint_writer',
    name: 'Hint Writer',
    desc: 'Reviews the plan and injects targeted hints to guide the implementation.',
  },
  {
    id: 'test_writer',
    name: 'Test Writer',
    desc: 'Writes failing tests that capture the expected behaviour after the fix.',
  },
  {
    id: 'implementer',
    name: 'Implementer',
    desc: 'Produces the patch — minimal code changes that make the tests pass.',
  },
  {
    id: 'verifier',
    name: 'Verifier',
    desc: 'Runs the tests, validates the patch, and decides whether to accept or retry.',
  },
];

const initialState = {
  runId: null,
  sidebarOpen: true,
  chatHistory: [
    { id: 'h1', title: 'NullPointer in UserController', ts: '2h ago' },
    { id: 'h2', title: 'Race condition on login', ts: 'Yesterday' },
    { id: 'h3', title: 'Memory leak in EventBus', ts: '3d ago' },
    { id: 'h4', title: 'Timeout on large payloads', ts: '5d ago' },
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

    case 'UPDATE_MESSAGE':
      return {
        ...state,
        messages: state.messages.map(m =>
          m.id === action.id ? { ...m, content: action.text } : m
        ),
      };

    case 'LOADING_DONE':
      return {
        ...state,
        phase: 'chat',
        issue: action.issue,
        messages: [{
          id: 'init',
          role: 'assistant',
          content: '',   // ← empty, stream fills it
          ts: Date.now(),
        }],
      };

    case 'ADD_MESSAGE':
      return { ...state, messages: [...state.messages, action.msg] };

    case 'SET_MESSAGES':
      return { ...state, messages: action.messages };

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
      const idx = state.currentAgentIdx; // always correct
      const agents = state.agents.map((a, i) =>
        i === idx ? { ...a, stream: [...a.stream, action.chunk] } : a
      );
      return { ...state, agents };
    }

    case 'AGENT_AWAITING': {
      const idx = state.currentAgentIdx;
      const agents = state.agents.map((a, i) =>
        i === idx ? { ...a, status: 'awaiting' } : a
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
          ? [...state.messages, { id: 'fin' + Date.now(), role: 'assistant', content: 'Pipeline complete...', ts: Date.now() }]
          : state.messages,
      };
    }

    // NEW — grant turns without advancing
    case 'AGENT_GRANT_TURNS': {
      const agents = state.agents.map((a, i) =>
        i === state.currentAgentIdx ? { ...a, status: 'running', expanded: true } : a
      );
      return { ...state, agents };
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
          content: `Pipeline paused at **${state.agents[state.currentAgentIdx]?.name}**. I have full context of everything found so far — ask me anything or dive into the results above.`,
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

    case 'SET_RUN_ID':
      return { ...state, runId: action.runId };

    case 'SYNC_RUN': {
      const { run, derived } = action;
      const agentIdx = derived.currentAgentIdx;

      const agents = state.agents.map((a, i) => {
        // Already marked done — don't overwrite
        if (state.agents[i].status === 'done') return a;

        if (i < agentIdx) return { ...a, status: 'done', expanded: false };
        if (i === agentIdx) {
          // running vs awaiting comes from backend status
          const status =
            run.status === 'awaiting_intervention' ||
              run.status === 'awaiting_more_turns'        // ← new
              ? 'awaiting'
              : 'running';
          return { ...a, status, expanded: true };
        }
        return { ...a, status: 'waiting' };
      });

      return {
        ...state,
        phase: derived.phase,
        currentAgentIdx: agentIdx,
        issueUrl: derived.issueUrl ?? state.issueUrl,
        runStatus: run.status,
        agents,
      };
    }



    case 'POLL_ERROR':
      // Don't crash the UI — just surface it if you want
      return { ...state, pollError: action.message };

    default: return state;
  }


}

const Ctx = createContext(null);
export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return <Ctx.Provider value={{ state, dispatch }}>{children}</Ctx.Provider>;
}
export const useApp = () => useContext(Ctx);