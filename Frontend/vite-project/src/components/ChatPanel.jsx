import { useState, useRef, useEffect } from 'react';
import { useApp } from '../store/appStore';
import { RichText } from '../utils/text';
import { ArrowUp, Zap } from 'lucide-react';

function Message({ msg }) {
  const isUser = msg.role === 'user';
  return (
    <div className={`flex gap-2.5 ${isUser ? 'flex-row-reverse' : 'flex-row'} animate-in fade-in slide-in-from-bottom-2 duration-200`}>
      <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold shrink-0 mt-0.5 ${
        isUser
          ? 'bg-gradient-to-br from-primary to-chart-2 text-white'
          : 'bg-accent/20 border border-accent/30 text-accent'
      }`}>
        {isUser ? 'JD' : 'S'}
      </div>
      <div className={[
        'max-w-[72%] px-4 py-2.5 rounded-2xl text-sm leading-relaxed',
        isUser
          ? 'rounded-tr-sm bg-secondary text-secondary-foreground shadow-md'
          : 'rounded-tl-sm bg-card text-card-foreground border border-border shadow-sm',
      ].join(' ')}>
        <RichText text={msg.text} />
      </div>
    </div>
  );
}

export default function ChatPanel() {
  const { state, dispatch } = useApp();
  const { messages, phase, issue } = state;
  const [input, setInput] = useState('');
  const [sessionId, setSessionId] = useState(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const bottomRef = useRef(null);
  const textareaRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Fire opener stream on mount
  useEffect(() => {
    if (!issue) return;

    async function startDiscussion() {
      // Add empty assistant message to stream into
      const msgId = 'init-' + Date.now();
      dispatch({ type: 'ADD_MESSAGE', msg: { id: msgId, role: 'assistant', text: '', ts: Date.now() } });
      setIsStreaming(true);

      try {
        const res = await fetch('http://localhost:8000/discussion/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ issue_text: `${issue.title}\n\n${issue.body}` }),
        });

        

        if (!res.ok) throw new Error('Failed to start discussion');

        // Grab session id from header
        const sid = res.headers.get('X-Session-ID');
        console.log('all headers:', [...res.headers.entries()]);
        console.log('sid:', sid);
        // setSessionId(sid);
        setSessionId(sid);

        // Stream chunks into the message
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let accumulated = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          accumulated += decoder.decode(value, { stream: true });
          dispatch({ type: 'UPDATE_MESSAGE', id: msgId, text: accumulated });
        }
      } catch (e) {
        dispatch({ type: 'UPDATE_MESSAGE', id: msgId, text: 'Failed to load discussion. Please try again.' });
      } finally {
        setIsStreaming(false);
      }
    }

    startDiscussion();
  }, []); // only on mount

  async function send() {
    const text = input.trim();
    if (!text || isStreaming || !sessionId) return;
    
    console.log("send1")
    dispatch({ type: 'ADD_MESSAGE', msg: { id: 'm' + Date.now(), role: 'user', text, ts: Date.now() } });
    setInput('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    const msgId = 'r' + Date.now();
    dispatch({ type: 'ADD_MESSAGE', msg: { id: msgId, role: 'assistant', text: '', ts: Date.now() } });
    setIsStreaming(true);

    try {
      console.log("sending")
      const res = await fetch('http://localhost:8000/discussion/message', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id: sessionId, user_input: text }),
});

      if (!res.ok) throw new Error('Server error');

      // Check if pipeline was triggered (returns JSON not a stream)
      const contentType = res.headers.get('Content-Type') || '';
      if (contentType.includes('application/json')) {
        const data = await res.json();
        if (data.pipeline_triggered) {
          dispatch({ type: 'UPDATE_MESSAGE', id: msgId, text: 'Starting pipeline...' });
          dispatch({ type: 'START_PIPELINE' });
          return;
        }
      }

      // Stream response
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let accumulated = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        accumulated += decoder.decode(value, { stream: true });
        dispatch({ type: 'UPDATE_MESSAGE', id: msgId, text: accumulated });
      }
    } catch (e) {
      dispatch({ type: 'UPDATE_MESSAGE', id: msgId, text: 'Something went wrong. Please try again.' });
    } finally {
      setIsStreaming(false);
    }
  }

  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  }

  function autoResize() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 140) + 'px';
  }

  const showPipelineBtn = phase === 'chat';

  return (
    <div className="flex flex-col flex-1 min-h-0 bg-background">

      {/* Messages */}
      <div className="flex-1 overflow-y-auto py-6">
        <div className="max-w-2xl mx-auto px-4 flex flex-col gap-4">
          {messages.map(msg => <Message key={msg.id} msg={msg} />)}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Start pipeline button */}
      {showPipelineBtn && (
        <div className="max-w-2xl mx-auto w-full px-4 pb-2 animate-in fade-in duration-300">
          <button
            onClick={() => dispatch({ type: 'START_PIPELINE' })}
            disabled={isStreaming}
            className="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl border border-primary/40 bg-primary/8 text-primary text-sm font-medium hover:bg-primary/15 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Zap size={14} />
            Start pipeline — run automated analysis
          </button>
        </div>
      )}

      {/* Input */}
      <div className="max-w-2xl mx-auto w-full px-4 pb-4">
        <div className="flex gap-2 items-end p-3 pl-4 bg-card border border-border rounded-2xl focus-within:border-ring transition-colors shadow-sm">
          <textarea
            ref={textareaRef}
            className="flex-1 bg-transparent text-sm text-foreground placeholder:text-muted-foreground/50 resize-none outline-none leading-relaxed"
            style={{ height: 22, maxHeight: 140, overflow: 'hidden' }}
            placeholder={isStreaming ? 'Waiting for response…' : 'Ask anything about this issue…'}
            value={input}
            onChange={e => { setInput(e.target.value); autoResize(); }}
            onKeyDown={onKey}
            disabled={isStreaming}
            rows={1}
          />
          <button
            onClick={() => { console.log('btn clicked', { isStreaming, input, sessionId }); send(); }}
            disabled={isStreaming || !input.trim()}
            className={`w-8 h-8 rounded-xl flex items-center justify-center transition-all shrink-0 ${
              input.trim() && !isStreaming
                ? 'bg-primary text-primary-foreground hover:opacity-90'
                : 'bg-muted text-muted-foreground/40 cursor-default'
            }`}
          >
            <ArrowUp size={15} />
          </button>
        </div>
        <p className="text-center text-[10px] text-muted-foreground/40 mt-1.5">Shift + Enter for new line</p>
      </div>
    </div>
  );
}