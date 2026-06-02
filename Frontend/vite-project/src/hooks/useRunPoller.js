import { useEffect, useRef } from 'react';
import { useApp } from '../store/appStore';
import { getRun } from '../utils/api';
import { deriveRunState } from '../utils/statusMap';

const POLL_INTERVAL = 6000; // ms

const TERMINAL_STATUSES = new Set([
  'succeeded', 'failed', 'cancelled',
]);

export function useRunPoller() {
  const { state, dispatch } = useApp();
  const { runId } = state;
  const timerRef = useRef(null);

  useEffect(() => {
    // Don't poll if there's no active run
    if (!runId) return;

    async function poll() {
      try {
        const run = await getRun(runId);
        console.log('POLL:', run.status, run.current_stage);
        const derived = deriveRunState(run);
        console.log('DERIVED:', derived);
        console.log('RUN OBJECT:', JSON.stringify(run));


        dispatch({ type: 'SYNC_RUN', run, derived });

        // Stop polling once we hit a terminal state
        if (TERMINAL_STATUSES.has(run.status)) return;

        timerRef.current = setTimeout(poll, POLL_INTERVAL);
      } catch (err) {
        dispatch({ type: 'POLL_ERROR', message: err.message });
        // Retry even on error — backend might just be momentarily busy
        timerRef.current = setTimeout(poll, POLL_INTERVAL * 2);
      }
    }

    poll(); // kick off immediately

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [runId]);
}