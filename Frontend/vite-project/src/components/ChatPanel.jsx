import { useState, useRef, useEffect } from 'react';
import { useApp } from '../store/appStore';
import { RichText } from '../utils/text';
import { ArrowUp, Zap, AlertCircle } from 'lucide-react';
import { sendMessage, getMessages } from '../utils/api';

function Message({ msg }) {
  const isUser = msg.role === 'user';
  return (
    <div className={`flex gap-2.5 ${isUser ? 'flex-row-reverse' : 'flex-row'} animate-in fade-in slide-in-from-bottom-2 duration-200`}>
      <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold shrink-0 mt-0.5 ${
        isUser
          ? 'bg-linear-to-br from-primary to-chart-2 text-white'
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
        <RichText text={msg.content} />
      </div>
    </div>
  );
}

export default function ChatPanel() {
  const { state, dispatch } = useApp();
  const { messages, phase, runId } = state;
  const [input, setInput] = useState('');
  const [isSending, setIsSending] = useState(false);
  const bottomRef = useRef(null);
  const textareaRef = useRef(null);

  const isAwaiting = phase === 'awaiting_intervention';

  // Fetch messages whenever runId changes
  useEffect(() => {
    if (!runId) return;
    getMessages(runId)
      .then(data => dispatch({ type: 'SET_MESSAGES', messages: data.messages ?? [] }))
      .catch(() => {});
  }, [runId]);

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  async function send() {
    const text = input.trim();
    if (!text || isSending || !runId) return;

    dispatch({ type: 'ADD_MESSAGE', msg: { id: 'm' + Date.now(), role: 'user', content: text, ts: Date.now() } });
    setInput('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
    setIsSending(true);

    try {
      await sendMessage(runId, text);
      // Response will come via poller / SSE — no need to handle here
    } catch (e) {
      dispatch({ type: 'ADD_MESSAGE', msg: { id: 'err' + Date.now(), role: 'assistant', content: 'Failed to send message.', ts: Date.now() } });
    } finally {
      setIsSending(false);
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

      {/* Awaiting intervention banner */}
      {isAwaiting && (
        <div className="max-w-2xl mx-auto w-full px-4 pb-2 animate-in fade-in duration-300">
          <div className="flex items-start gap-3 p-3.5 rounded-xl border border-accent/40 bg-accent/8">
            <AlertCircle size={15} className="text-accent shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-accent">Pipeline paused — your input needed</p>
              <p className="text-xs text-muted-foreground mt-0.5">Review the agent output in the pipeline panel, then continue or give feedback.</p>
            </div>
          </div>
        </div>
      )}

      {/* Start pipeline button */}
      {showPipelineBtn && (
        <div className="max-w-2xl mx-auto w-full px-4 pb-2 animate-in fade-in duration-300">
          <button
            onClick={() => dispatch({ type: 'START_PIPELINE' })}
            disabled={isSending}
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
            placeholder={isSending ? 'Sending…' : 'Ask anything about this issue…'}
            value={input}
            onChange={e => { setInput(e.target.value); autoResize(); }}
            onKeyDown={onKey}
            disabled={isSending}
            rows={1}
          />
          <button
            onClick={send}
            disabled={isSending || !input.trim()}
            className={`w-8 h-8 rounded-xl flex items-center justify-center transition-all shrink-0 ${
              input.trim() && !isSending
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