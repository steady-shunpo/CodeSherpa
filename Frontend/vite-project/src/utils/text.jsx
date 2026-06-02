// Renders **bold**, `code`, and ```code blocks``` in message text
export function RichText({ text, className = '' }) {
  // Split on code blocks first
  if (!text) return <span />

  const blockParts = text.split(/(```[\s\S]*?```)/g);
  return (
    <span className={className}>
      {blockParts.map((part, i) => {
        if (part.startsWith('```')) {
          const inner = part.slice(3).replace(/```$/, '');
          const newline = inner.indexOf('\n');
          const lang = newline > -1 ? inner.slice(0, newline) : '';
          const code = newline > -1 ? inner.slice(newline + 1) : inner;
          return (
            <pre key={i} className="mt-2 mb-1 rounded-md bg-muted/60 border border-border px-3 py-2 text-xs font-mono text-foreground overflow-x-auto whitespace-pre">
              {lang && <div className="text-muted-foreground text-[10px] mb-1">{lang}</div>}
              {code}
            </pre>
          );
        }
        // inline bold + code
        const inlineParts = part.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
        return inlineParts.map((p, j) => {
          if (p.startsWith('**') && p.endsWith('**'))
            return <strong key={j} className="font-semibold text-foreground">{p.slice(2, -2)}</strong>;
          if (p.startsWith('`') && p.endsWith('`'))
            return <code key={j} className="px-1.5 py-0.5 rounded bg-muted/60 border border-border text-xs font-mono text-accent">{p.slice(1, -1)}</code>;
          return <span key={j}>{p}</span>;
        });
      })}
    </span>
  );
}