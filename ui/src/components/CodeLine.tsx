/** One command or snippet with a Copy button beside it. */
export function CodeLine({ text, onCopy }: { text: string; onCopy: () => void }) {
  return (
    <div className="code-block">
      <pre className="code">{text}</pre>
      <button type="button" onClick={onCopy}>
        Copy
      </button>
    </div>
  );
}
