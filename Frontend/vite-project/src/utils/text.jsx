// Formats agent stream text with newlines and highlighted section headers
export function formatAgentStreamText(text, agentId) {
  if (!text || typeof text !== 'string') return text || '';

  let formatted = text;

  // Unescape concatenated JSON chunks if present e.g. {"chunk":"..."}{"chunk":"..."}
  if (formatted.includes('{"chunk":')) {
    const parsed = [];
    const regex = /\{"chunk":"((?:[^"\\]|\\.)*)"\}/g;
    let match;
    while ((match = regex.exec(formatted)) !== null) {
      try {
        parsed.push(JSON.parse(`"${match[1]}"`));
      } catch {
        parsed.push(match[1]);
      }
    }
    if (parsed.length > 0) {
      formatted = parsed.join('');
    }
  }

  // Planner agent: format FINAL_PLAN / FINAL PLAN
  if (!agentId || agentId === 'planner') {
    formatted = formatted.replace(
      /(?:[\r\n\s]*)(#{1,6}\s*)?(?:\*\*)?(FINAL[_\s]PLAN)(?:\*\*)?(\s*:?)(?:\*\*)?/gi,
      (match, hashes, keyword, colon) => {
        const key = keyword.toUpperCase().replace(/\s+/g, '_');
        return `\n\n\n**${key}:**\n`;
      }
    );
  }

  // Hint Writer agent: format TEST_HINT and IMPL_HINT
  if (!agentId || agentId === 'hint_writer') {
    formatted = formatted.replace(
      /(?:[\r\n\s]*)(#{1,6}\s*)?(?:\*\*)?(TEST[_\s]HINT)(?:\*\*)?(\s*:?)(?:\*\*)?/gi,
      (match, hashes, keyword, colon) => {
        const key = keyword.toUpperCase().replace(/\s+/g, '_');
        return `\n\n\n**${key}:**\n`;
      }
    );

    formatted = formatted.replace(
      /(?:[\r\n\s]*)(#{1,6}\s*)?(?:\*\*)?(IMPL[_\s]HINT|IMPLEMENTATION[_\s]HINT)(?:\*\*)?(\s*:?)(?:\*\*)?/gi,
      (match, hashes, keyword, colon) => {
        const key = keyword.toUpperCase().replace(/\s+/g, '_');
        return `\n\n\n**${key}:**\n`;
      }
    );
  }

  // Clean up any double colons if raw had trailing colon
  formatted = formatted.replace(/\*\*([A-Z_]+):+\*\*/g, '**$1:**');

  // Strip leading newlines if keyword was at the very start of the text
  return formatted.replace(/^\n+/, '');
}

// Renders **bold**, `code`, and ```code blocks``` in message text
export function RichText({ text, className = '' }) {
  if (!text) return <span />;

  // Extract all chunk values from concatenated JSON objects if raw
  let content = text;
  if (typeof text === 'string' && text.includes('{"chunk":')) {
    const parsed = [];
    const regex = /\{"chunk":"((?:[^"\\]|\\.)*)"\}/g;
    let match;
    while ((match = regex.exec(text)) !== null) {
      try {
        parsed.push(JSON.parse(`"${match[1]}"`));
      } catch {
        parsed.push(match[1]);
      }
    }
    if (parsed.length > 0) {
      content = parsed.join('');
    }
  }

  const blockParts = content.split(/(```[\s\S]*?```)/g);
  return (
    <span className={className}>
      {blockParts.map((part, i) => {
        if (part.startsWith('```')) {
          const inner = part.slice(3).replace(/```$/, '');
          const newline = inner.indexOf('\n');
          const lang = newline > -1 ? inner.slice(0, newline) : '';
          const code = newline > -1 ? inner.slice(newline + 1) : inner;
          return (
            <pre
              key={i}
              className="mt-2 mb-2 rounded-lg bg-muted/60 border border-border px-3 py-2 text-xs font-mono text-foreground overflow-x-auto whitespace-pre"
            >
              {lang && <div className="text-muted-foreground text-[10px] mb-1 font-sans">{lang}</div>}
              {code}
            </pre>
          );
        }
        // inline bold + code
        const inlineParts = part.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
        return inlineParts.map((p, j) => {
          if (p.startsWith('**') && p.endsWith('**')) {
            const inner = p.slice(2, -2);
            const isSpecialHeader = /^(FINAL_PLAN|TEST_HINT|IMPL_HINT|IMPLEMENTATION_HINT):?$/i.test(inner.trim());
            if (isSpecialHeader) {
              return (
                <span
                  key={j}
                  className="inline-flex items-center font-semibold text-accent bg-accent/15 border border-accent/30 rounded-md px-2 py-0.5 my-1 text-xs tracking-wider shadow-xs"
                >
                  {inner}
                </span>
              );
            }
            return (
              <strong key={j} className="font-semibold text-foreground">
                {inner}
              </strong>
            );
          }
          if (p.startsWith('`') && p.endsWith('`')) {
            return (
              <code
                key={j}
                className="px-1.5 py-0.5 rounded bg-muted/60 border border-border text-xs font-mono text-accent"
              >
                {p.slice(1, -1)}
              </code>
            );
          }

          // Handle newlines in plain text
          return p.split(/\r?\n|__NEWLINE__/).map((line, k, arr) => (
            <span key={`${j}-${k}`}>
              {line}
              {k < arr.length - 1 && <br />}
            </span>
          ));
        });
      })}
    </span>
  );
}