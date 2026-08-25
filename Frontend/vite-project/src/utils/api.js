import { useEffect, useRef } from 'react';
import { useApp } from '../store/appStore';
import { deriveRunState } from './statusMap';


export const BASE = 'http://localhost:8000';


async function safeRequest(path, options = {}) {
  var token = localStorage.getItem('token')
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? `Request failed: ${res.status}`);
  }
  return res.json();
}

export function initLogin(provider) {
  window.location.href = `${BASE}/auth/${provider}`;
}

// POST /runs — create a new run, returns the full run object
export function createRun(issueUrl) {
  return safeRequest('/runs', {
    method: 'POST',
    body: JSON.stringify({ issue_url: issueUrl }),
  });
}

// GET /runs/{id} — current status, stage, retry count
export function getRun(runId) {
  return safeRequest(`/runs/${runId}`);
}

// POST /runs/{id}/continue
export function continueRun(runId, extraTurns = 10, feedback = null) {
  return safeRequest(`/runs/${runId}/continue`, {
    method: 'POST',
    body: JSON.stringify({
      extra_turns: extraTurns,
      ...(feedback ? { feedback } : {}),
    }),
  });
}

// GET /runs/{id}/doc — the full assembled doc (all checkpoint outputs merged)
export function getDoc(runId) {
  return safeRequest(`/runs/${runId}/doc`);
}

// POST /runs/{id}/messages — send a chat message during intervention
export function sendMessage(runId, content) {
  return safeRequest(`/runs/${runId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
}

// GET /runs/{id}/messages — fetch chat history, optionally filtered by stage
export function getMessages(runId, stage = null) {
  const qs = stage ? `?stage=${stage}` : '';
  return safeRequest(`/runs/${runId}/messages${qs}`);
}

// POST /runs/{id}/resume — release the pause, optionally rewind to a stage
export function resumeRun(runId, fromStage = null, contextSummary = null, extraTurns = null) {
  return safeRequest(`/runs/${runId}/resume`, {
    method: 'POST',
    body: JSON.stringify({
      ...(fromStage ? { from_stage: fromStage } : {}),
      ...(contextSummary ? { context_summary: contextSummary } : {}),
      ...(extraTurns ? { extra_turns: extraTurns } : {}),
    }),
  });
}

export function listRuns() {
  return safeRequest(`/runs/`);  // trailing slash since endpoint is just /
}

export function getRunMessages(runId) {
  return safeRequest(`/runs/${runId}/messages`);
}

export function getRunDoc(runId) {
  return safeRequest(`/runs/${runId}/doc`);
}

export function useSSEStream(runId, agentIdx, turnsUsed) {
  const { dispatch } = useApp();

  useEffect(() => {
    if (!runId) return;

    const es = new EventSource(`${BASE}/runs/${runId}/stream`);

    
    es.addEventListener('token', (e) => {
      dispatch({ type: 'AGENT_STREAM_CHUNK', chunk: e.data });
    });
    
    es.addEventListener('done', () => {
      const run = getRun(runId);
      const derived = deriveRunState(run)
      dispatch({ type: 'SYNC_RUN',  run, derived});
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
  }, [runId, agentIdx, turnsUsed]);
}


export function useChatStream(runId, messageId, onChunk, onDone) {
  const { dispatch } = useApp();

  useEffect(() => {
    if (!runId || !messageId) return;

    dispatch({ type: 'CHAT_STREAM_START' });
    const es = new EventSource(`${BASE}/runs/${runId}/chat/stream`);

    es.addEventListener('token', (e) => onChunk(e.data));

    es.addEventListener('done', () => {
      dispatch({ type: 'CHAT_STREAM_DONE' });
      es.close();
      if (onDone) onDone();
    });

    es.onerror = (err) => {
      console.error('Chat stream error:', err);
      dispatch({ type: 'CHAT_STREAM_DONE' });
      es.close();
      if (onDone) onDone();
    };

    es.addEventListener('timeout', () => {
      dispatch({ type: 'CHAT_STREAM_DONE' });
      es.close();
      if (onDone) onDone();
    });

    return () => {
      es.close();
    };
  }, [runId, messageId]);
}


// POST /runs/{id}/cancel
export function cancelRun(runId) {
  return safeRequest(`/runs/${runId}/cancel`, { method: 'POST' });
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));