// Maps backend RunStatus → frontend phase
export const STATUS_TO_PHASE = {
  ingesting:              'loading',
//   discussing:             'chat',
  discussing:             'loading',
  stage_running:          'pipeline',
  awaiting_intervention:  'pipeline',
  awaiting_more_turns:    'pipeline',
  blocked_on_human:       'blocked',
  succeeded:              'done',
  failed:                 'error',
  cancelled:              'idle',
};

// Maps backend stage id → index in AGENTS array
// Must stay in sync with AGENTS in appStore.jsx
export const STAGE_TO_IDX = {
  planner:         0,
  hint_writer:     1,
  test_writer:     2,
  implementer:     3,
  verifier:        4,
};

// Given a backend run object, returns the derived frontend state
export function deriveRunState(run) {
  return {
    phase:            STATUS_TO_PHASE[run.status] ?? 'idle',
    currentAgentIdx:  STAGE_TO_IDX[run.current_stage] ?? -1,
    runId:            run.run_id,
    issueUrl:         run.issue_url,
    status:           run.status,
  };
}