export function parseGithubUrl(url) {
  const m = url.trim().match(/github\.com\/([^/]+)\/([^/]+)\/issues\/(\d+)/);
  if (!m) return null;
  return { owner: m[1], repo: m[2], number: m[3] };
}

export async function fetchIssue(url) {
  const parsed = parseGithubUrl(url);
  if (!parsed) throw new Error('Invalid GitHub issue URL');
  // Real: fetch(`https://api.github.com/repos/${parsed.owner}/${parsed.repo}/issues/${parsed.number}`)
  await sleep(2400);
  return {
    ...parsed,
    title: `NullPointerException in ${parsed.repo} #${parsed.number}`,
    body: 'a missing null check on session tokens when AuthFilter is bypassed in test environments',
    labels: ['bug', 'high-priority'],
    state: 'open',
  };
}

const AGENT_SCRIPTS = {
  reader: [
    'Fetching issue metadata and comments...',
    'Identified issue type: **NullPointerException**',
    'Affected component: `UserController.java:148`',
    'Trigger condition: session token is `null` when AuthFilter is bypassed',
    'Linked PRs: none. Related issues: #441, #389',
    'Extracted **3 relevant files** from issue body and comments.',
  ],
  scanner: [
    'Cloning repo index...',
    'Tracing call chain: `AuthFilter → SessionManager → UserService → UserController`',
    'Found unguarded token access at `SessionManager.java:92`',
    'Found secondary risk at `UserService.java:204` — same pattern',
    'Root cause: AuthFilter guarantee not enforced in test profile',
    'Affected paths: **3 files**, 2 critical, 1 low-risk',
  ],
  patcher: [
    'Generating patch for `UserController.java:148`...',
    '```java\n- User user = session.getUser();\n+ if (session == null) throw new UnauthorizedException();\n+ User user = session.getUser();```',
    'Applying same guard to `SessionManager.java:92`',
    'Patch is minimal — **6 lines changed** across 2 files.',
    'No logic changes outside null-guard additions.',
  ],
  reviewer: [
    'Running patch analysis...',
    'No regressions detected in happy path.',
    'Edge case: concurrent session expiry — patch handles correctly.',
    'Suggested test: `testNullSessionTokenReturns401()`',
    '**Confidence: high.** Patch is ready to commit.',
  ],
};

export async function* streamAgent(agentId) {
  const lines = AGENT_SCRIPTS[agentId] || ['Processing...', 'Done.'];
  for (const line of lines) {
    await sleep(500 + Math.random() * 500);
    yield line;
  }
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));