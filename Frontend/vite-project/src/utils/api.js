import { useEffect, useRef } from 'react';
import { useApp } from '../store/appStore';


export const BASE = 'http://localhost:8000';


async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Request failed: ${res.status}`);
  }
  return res.json();
}

// POST /runs — create a new run, returns the full run object
export function createRun(issueUrl) {
  return request('/runs', {
    method: 'POST',
    body: JSON.stringify({ issue_url: issueUrl }),
  });
}

// GET /runs/{id} — current status, stage, retry count
export function getRun(runId) {
  return request(`/runs/${runId}`);
}

// POST /runs/{id}/continue
export function continueRun(runId, extraTurns = 10, feedback = null) {
  return request(`/runs/${runId}/continue`, {
    method: 'POST',
    body: JSON.stringify({
      extra_turns: extraTurns,
      ...(feedback ? { feedback } : {}),
    }),
  });
}

// GET /runs/{id}/doc — the full assembled doc (all checkpoint outputs merged)
export function getDoc(runId) {
  return request(`/runs/${runId}/doc`);
}

// POST /runs/{id}/messages — send a chat message during intervention
export function sendMessage(runId, content) {
  return request(`/runs/${runId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
}

// GET /runs/{id}/messages — fetch chat history, optionally filtered by stage
export function getMessages(runId, stage = null) {
  const qs = stage ? `?stage=${stage}` : '';
  return request(`/runs/${runId}/messages${qs}`);
}

// POST /runs/{id}/resume — release the pause, optionally rewind to a stage
export function resumeRun(runId, fromStage = null, contextSummary = null, extraTurns = null) {
  return request(`/runs/${runId}/resume`, {
    method: 'POST',
    body: JSON.stringify({
      ...(fromStage ? { from_stage: fromStage } : {}),
      ...(contextSummary ? { context_summary: contextSummary } : {}),
      ...(extraTurns ? { extra_turns: extraTurns } : {}),
    }),
  });
}


export function useSSEStream(runId, agentIdx) {
  const { dispatch } = useApp();

  useEffect(() => {
    if (!runId) return;

    const es = new EventSource(`${BASE}/runs/${runId}/stream`);

    
    es.addEventListener('token', (e) => {
      dispatch({ type: 'AGENT_STREAM_CHUNK', chunk: e.data });
    });
    
    es.addEventListener('done', () => {
      dispatch({ type: 'AGENT_AWAITING',  });
      es.close();
    });
    
    es.onmessage = (e) => console.log('message:', e.data);
    es.addEventListener('token', (e) => console.log('token:', e.data));
    es.addEventListener('done', (e) => console.log('DONE FIRED:', e.data));
    
    es.addEventListener('timeout', () => {
      es.close();
    });

    return () => {
      es.close();
    };
  }, [runId, agentIdx]);
}


// POST /runs/{id}/cancel
export function cancelRun(runId) {
  return request(`/runs/${runId}/cancel`, { method: 'POST' });
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));