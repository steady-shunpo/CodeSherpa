import { createContext, useContext, useReducer } from 'react';
import { AGENT_TO_OUTPUT } from '../utils/statusMap';

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
  runs: [],
  // 'idle' | 'loading' | 'chat' | 'pipeline'
  phase: 'idle',
  issueUrl: '',
  issue: null,
  chatMessages: [],
  agents: AGENTS.map(a => ({ ...a, status: 'waiting', stream: [], expanded: false })),
  currentAgentIdx: -1,
};

function reducer(state, action) {
  switch (action.type) {

    case 'TOGGLE_SIDEBAR':
      return { ...state, sidebarOpen: !state.sidebarOpen };

    case 'RUNS_LOADING':
      return { ...state, runsLoading: true };

    case 'RUNS_LOADED':
      return { ...state, runs: action.runs, runsLoading: false };

    case 'RUN_CREATED':
      return {
        ...state,
        runs: [action.run, ...state.runs],
        // don't touch phase — let the poller drive it
      };

    case 'RUNS_ERROR':
      return { ...state, runsLoading: false };

    case 'START_LOADING':
      return { ...state, phase: 'loading', issueUrl: action.url };

    case 'UPDATE_MESSAGE':
      return {
        ...state,
        chatMessages: state.chatMessages.map(m =>
          m.id === action.id ? { ...m, content: action.text } : m
        ),
      };

    case 'LOAD_RUN':
      // console.log(AGENT_TO_OUTPUT['planner'])
      // console.log("DOC", action.payload.doc)
      return {
        ...state,
        phase: 'loading',
        chatMessages: [],
        agents: AGENTS.map(a => ({ ...a, status: 'waiting', 
          stream: action.payload.doc[AGENT_TO_OUTPUT[a.id]] ? [action.payload.doc[AGENT_TO_OUTPUT[a.id]]] : [],  
          expanded: true })),
        currentAgentIdx: -1,
      };

    case 'LOADING_DONE':
      return {
        ...state,
        phase: 'chat',
        issue: action.issue,
        chatMessages: [{
          id: 'init_' + Date.now(),
          role: 'assistant',
          content: '',   // ← empty, stream fills it
          ts: Date.now(),
        }],
      };

    case 'ADD_MESSAGE':
      return { ...state, chatMessages: [...state.chatMessages, action.msg] };

    case 'SET_MESSAGES':
      return { ...state, chatMessages: action.messages };

    case 'START_PIPELINE':
      return {
        ...state,
        phase: 'pipeline',
        currentAgentIdx: 0,
        agents: state.agents.map((a, i) => ({
          ...a, status: i === 0 ? 'running' : 'waiting', stream: [], expanded: i === 0,
        })),
      };

    case 'CHAT_STREAM_START': {
      // Finalize any existing streaming message first to prevent multi-bubble updates
      const finalizedMessages = state.chatMessages.map(m =>
        m.id === 'streaming'
          ? { ...m, id: 'msg_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7), streaming: false }
          : m
      );
      return {
        ...state,
        chatMessages: [
          ...finalizedMessages,
          { id: 'streaming', role: 'assistant', content: '', streaming: true }
        ]
      };
    }

    case 'CHAT_STREAM_CHUNK':
      return {
        ...state,
        chatMessages: state.chatMessages.map(m =>
          m.id === 'streaming'
            ? { ...m, content: m.content + action.chunk }
            : m
        )
      };

    case 'CHAT_STREAM_DONE':
      return {
        ...state,
        chatMessages: state.chatMessages.map(m =>
          m.id === 'streaming'
            ? { ...m, id: 'msg_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7), streaming: false }
            : m
        )
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
        chatMessages: finished
          ? [...state.chatMessages, { id: 'fin' + Date.now(), role: 'assistant', content: 'Pipeline complete...', ts: Date.now() }]
          : state.chatMessages,
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
        chatMessages: [...state.chatMessages, {
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

    case 'RESET': {
      return {
        ...initialState,
        sidebarOpen: state.sidebarOpen, // preserve sidebar state
        runs: state.runs,               // preserve loaded runs list
      };
    }

    case 'SET_RUN_ID':
      return { ...state, runId: action.runId };



    case 'SYNC_RUN': {
      const { run, derived } = action;
      const agentIdx = derived.currentAgentIdx;

      const agents = state.agents.map((a, i) => {
        if (i < agentIdx) return { ...a, status: 'done', expanded: false };
        if (i === agentIdx) {
          const status =
            run.status === 'awaiting_intervention' ||
              run.status === 'awaiting_more_turns'
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
        turnsUsed: run.turns_used,
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