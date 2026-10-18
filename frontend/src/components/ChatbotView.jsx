import React, { useState, useRef, useEffect } from 'react';
import { api } from '@/lib/api';
import { invalidateDischarge } from '@/lib/queries';
import { Bot, Send, User, Sparkles, RefreshCw, ShieldCheck, X, Check } from 'lucide-react';


const QUICK_COMMANDS = [
  'List patients',
  'My tasks',
  'Analytics',
  'Insurance claims',
  'Help',
];

const formatResponse = (text) => {
  // Convert **bold** markdown to JSX spans
  const parts = text.split(/(\*\*[^*]+\*\*|•)/g);
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={i} className="text-zinc-100">{part.slice(2, -2)}</strong>;
    }
    if (part === '•') return <span key={i} className="text-blue-400 mr-1">•</span>;
    return <span key={i}>{part}</span>;
  });
};

const ChatbotView = () => {
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content: "Hello! I'm DischargeFlow AI, your clinical operations assistant. I can look up patients, tasks, analytics and insurance claims, and prepare tasks, discharges and claims for you to confirm. Nothing changes until you confirm it.",
      timestamp: new Date(),
    }
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [resolving, setResolving] = useState(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async (text) => {
    const userMessage = text || input.trim();
    if (!userMessage) return;
    setInput('');
    // Recent turns give follow-up questions context; the server de-identifies them before the model sees them.
    const history = messages.slice(1).slice(-10).map(m => ({ role: m.role, content: m.content.slice(0, 4000) }));
    setMessages(prev => [...prev, { role: 'user', content: userMessage, timestamp: new Date() }]);
    setLoading(true);
    try {
      const res = await api.post(`/chatbot/message`, { message: userMessage, history });
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: res.data.response || (res.data.pending_actions?.length ? 'Please review the proposed change below.' : ''),
        data: res.data.data,
        actions: (res.data.pending_actions || []).map(a => ({ ...a, state: 'pending' })),
        timestamp: new Date(),
      }]);
    } catch (err) {
      if (err.response && err.response.status === 401) {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: "Your session has expired or you are not authenticated. Please refresh the page to log in again.",
          timestamp: new Date(),
        }]);
      } else {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: "I'm having trouble connecting to the backend. Please check your connection and try again.",
          timestamp: new Date(),
        }]);
      }
    } finally {
      setLoading(false);
    }
  };

  const setActionState = (actionId, state) => {
    setMessages(prev => prev.map(m => m.actions
      ? { ...m, actions: m.actions.map(a => (a.action_id === actionId ? { ...a, state } : a)) }
      : m));
  };

  const resolveAction = async (action, decision) => {
    setResolving(action.action_id);
    try {
      const res = await api.post(`/chatbot/actions/${action.action_id}/${decision}`, {});
      setActionState(action.action_id, decision === 'confirm' ? 'confirmed' : 'cancelled');
      if (decision === 'confirm') {
        invalidateDischarge();
        setMessages(prev => [...prev, { role: 'assistant', content: `Done: ${res.data.response}`, timestamp: new Date() }]);
      }
    } catch (err) {
      const detail = err.response?.data?.detail || 'The action could not be completed.';
      setActionState(action.action_id, 'failed');
      setMessages(prev => [...prev, { role: 'assistant', content: detail, timestamp: new Date() }]);
    } finally {
      setResolving(null);
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="flex flex-col h-full max-h-[calc(100vh-160px)]">
      {/* Header */}
      <div className="flex items-center gap-3 mb-4">
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center">
          <Bot className="w-5 h-5 text-white" />
        </div>
        <div>
          <h2 className="text-xl font-bold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
            🤖 AI Assistant
          </h2>
          <p className="text-zinc-400 text-sm">Clinical command interface — all actions are HIPAA-audited</p>
        </div>
        <div className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/20">
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          <span className="text-emerald-400 text-xs font-medium">Online</span>
        </div>
      </div>

      {/* Quick command chips */}
      <div className="flex flex-wrap gap-2 mb-4">
        {QUICK_COMMANDS.map(cmd => (
          <button
            key={cmd}
            onClick={() => sendMessage(cmd)}
            disabled={loading}
            className="flex items-center gap-1 px-3 py-1.5 rounded-full bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs hover:bg-zinc-700 hover:text-zinc-100 transition-colors disabled:opacity-50"
          >
            <Sparkles className="w-3 h-3 text-blue-400" />
            {cmd}
          </button>
        ))}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto space-y-4 pr-1 pb-2">
        {messages.map((msg, i) => (
          <div key={i} className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}>
            {/* Avatar */}
            <div className={`w-8 h-8 rounded-lg flex-shrink-0 flex items-center justify-center ${
              msg.role === 'assistant'
                ? 'bg-gradient-to-br from-blue-600 to-violet-600'
                : 'bg-zinc-700'
            }`}>
              {msg.role === 'assistant'
                ? <Bot className="w-4 h-4 text-white" />
                : <User className="w-4 h-4 text-zinc-300" />
              }
            </div>

            {/* Bubble */}
            <div className={`max-w-[75%] ${msg.role === 'user' ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
              <div className={`px-4 py-3 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap ${
                msg.role === 'user'
                  ? 'bg-blue-600 text-white rounded-tr-sm'
                  : 'bg-zinc-800 text-zinc-200 border border-zinc-700 rounded-tl-sm'
              }`}>
                {msg.role === 'assistant' ? formatResponse(msg.content) : msg.content}
              </div>

              {/* Data preview */}
              {msg.data?.patients && msg.data.patients.length > 0 && (
                <div className="bg-zinc-900 border border-zinc-700 rounded-xl p-3 text-xs space-y-1 w-full">
                  <p className="text-zinc-500 mb-2">Showing first {msg.data.patients.length} patients:</p>
                  {msg.data.patients.map(p => (
                    <div key={p.patient_id} className="flex justify-between items-center text-zinc-300">
                      <span>{p.name}</span>
                      <span className="text-zinc-500">{p.room_number} — {p.status}</span>
                    </div>
                  ))}
                </div>
              )}
              {msg.actions && msg.actions.map(action => (
                <div key={action.action_id} data-testid="assistant-action-card"
                  className="bg-zinc-900 border border-amber-500/30 rounded-xl p-3 text-xs w-full">
                  <p className="flex items-center gap-1.5 text-amber-400 font-medium mb-1">
                    <ShieldCheck className="w-3.5 h-3.5" /> Needs your confirmation
                  </p>
                  <p className="text-zinc-200 text-sm mb-3">{action.summary}</p>
                  {action.state === 'pending' ? (
                    <div className="flex gap-2">
                      <button
                        data-testid="confirm-action-btn"
                        onClick={() => resolveAction(action, 'confirm')}
                        disabled={resolving === action.action_id}
                        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white disabled:opacity-50"
                      >
                        <Check className="w-3.5 h-3.5" /> Confirm
                      </button>
                      <button
                        data-testid="cancel-action-btn"
                        onClick={() => resolveAction(action, 'cancel')}
                        disabled={resolving === action.action_id}
                        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-300 hover:bg-zinc-700 disabled:opacity-50"
                      >
                        <X className="w-3.5 h-3.5" /> Cancel
                      </button>
                    </div>
                  ) : (
                    <p className="text-zinc-500 capitalize">{action.state}</p>
                  )}
                </div>
              ))}
              {msg.data?.tasks && msg.data.tasks.length > 0 && (
                <div className="bg-zinc-900 border border-zinc-700 rounded-xl p-3 text-xs space-y-1 w-full">
                  <p className="text-zinc-500 mb-2">Your tasks:</p>
                  {msg.data.tasks.map(t => (
                    <div key={t.task_id} className="flex justify-between items-center text-zinc-300">
                      <span>{t.title}</span>
                      <span className={`px-2 py-0.5 rounded text-xs ${t.status === 'completed' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>{t.status}</span>
                    </div>
                  ))}
                </div>
              )}

              <span className="text-zinc-600 text-xs">
                {msg.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              </span>
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-600 to-violet-600 flex items-center justify-center flex-shrink-0">
              <Bot className="w-4 h-4 text-white" />
            </div>
            <div className="bg-zinc-800 border border-zinc-700 rounded-2xl rounded-tl-sm px-4 py-3">
              <RefreshCw className="w-4 h-4 text-zinc-500 animate-spin" />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="mt-4 flex gap-3 items-end">
        <div className="flex-1 bg-zinc-800 border border-zinc-700 rounded-xl focus-within:border-blue-500 transition-colors">
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask me anything: 'List patients', 'My tasks', 'Create a task for…'"
            rows={1}
            className="w-full bg-transparent px-4 py-3 text-zinc-100 text-sm focus:outline-none resize-none placeholder-zinc-500"
          />
        </div>
        <button
          onClick={() => sendMessage()}
          disabled={loading || !input.trim()}
          className="w-10 h-10 flex items-center justify-center rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors flex-shrink-0"
        >
          <Send className="w-4 h-4" />
        </button>
      </div>

      <p className="text-zinc-600 text-xs mt-2 text-center">Patient identifiers are removed before the AI sees them. Every lookup and change is audited, and changes need your confirmation.</p>
    </div>
  );
};

export default ChatbotView;
