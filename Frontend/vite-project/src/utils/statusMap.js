// Maps backend RunStatus → frontend phase
export const STATUS_TO_PHASE = {
  ingesting:              'loading',
//   discussing:             'chat',
  discussing:             'loading',
  stage_running:          'pipeline',
  awaiting_intervention:  'pipeline',
  awaiting_more_turns:    'pipeline',
  paused:                 'pipeline',
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

export const AGENT_TO_OUTPUT = {
  'planner': "architect_plan",
  'hint_writer': "test_hint",
  'test_writer': "test_result",
  'implementer': "",
}

// Given a backend run object, returns the derived frontend state
export function deriveRunState(run) {
  console.log("RUN STATUS: ")
  console.log(run.status)
  return {
    phase:            STATUS_TO_PHASE[run.status] ?? 'idle',
    currentAgentIdx:  STAGE_TO_IDX[run.current_stage] ?? -1,
    runId:            run.run_id,
    issueUrl:         run.issue_url,
    status:           run.status,
  };
}