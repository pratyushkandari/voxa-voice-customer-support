'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  Activity,
  Cpu,
  Database,
  Zap,
  ArrowLeft,
  AlertTriangle,
  Headphones,
  Server,
  RefreshCw,
  Lock,
  LogOut
} from 'lucide-react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8088';
// Kept separate from the chat's guest token so signing in here does not change the
// identity of an open support session.
const ADMIN_TOKEN_KEY = 'voxa_admin_token';
const REFRESH_INTERVAL_MS = 10_000;
const TTFT_TARGET_MS = 180;

interface AdminMetrics {
  active_connections: number;
  queue_depth: number;
  cache_hit_ratio: number;
  avg_ttft_ms: number;
  p95_latency_ms: number;
  active_workers: number;
  total_conversations: number;
  total_messages: number;
}

interface Ticket {
  id: string;
  issue_summary: string;
  priority: string;
  status: string;
  created_at: string;
}

type LoadState = 'loading' | 'ready' | 'auth' | 'error';

const priorityStyle = (priority: string) =>
  priority === 'urgent' || priority === 'high'
    ? 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
    : priority === 'medium'
      ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
      : 'bg-slate-800 text-slate-400 border border-slate-700';

const statusStyle = (status: string) =>
  status === 'in_progress'
    ? 'bg-amber-500/10 text-amber-300 border border-amber-500/20'
    : status === 'open'
      ? 'bg-blue-500/10 text-blue-300 border border-blue-500/20'
      : 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/20';

const WORKER_FLEET = [
  { name: 'Faster-Whisper STT', detail: 'INT8 CTranslate2 • stream:audio:transcribe', group: 'stt_group' },
  { name: 'LLM / Tool-Routing Worker', detail: 'Tools + pgvector RAG • stream:llm:inference', group: 'llm_group' },
  { name: 'edge-tts Synthesis', detail: 'Neural voice • stream:tts:synthesize', group: 'tts_group' },
  { name: 'PostgreSQL + pgvector', detail: '384-dim HNSW cosine index', group: 'storage' }
];

export default function AdminDashboard() {
  const router = useRouter();
  const [metrics, setMetrics] = useState<AdminMetrics | null>(null);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [loadState, setLoadState] = useState<LoadState>('loading');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const [email, setEmail] = useState('admin@voxa.ai');
  const [password, setPassword] = useState('');
  const [signingIn, setSigningIn] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);

  const loadDashboard = useCallback(async () => {
    const token = localStorage.getItem(ADMIN_TOKEN_KEY);
    if (!token) {
      setLoadState('auth');
      return;
    }
    setIsRefreshing(true);
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const [metricsRes, ticketsRes] = await Promise.all([
        fetch(`${API_URL}/v1/admin/metrics`, { headers }),
        fetch(`${API_URL}/v1/admin/tickets`, { headers })
      ]);
      if ([metricsRes.status, ticketsRes.status].some((s) => s === 401 || s === 403)) {
        localStorage.removeItem(ADMIN_TOKEN_KEY);
        setAuthError('Your admin session expired or lacks admin rights. Please sign in again.');
        setLoadState('auth');
        return;
      }
      if (!metricsRes.ok || !ticketsRes.ok) {
        throw new Error(`Gateway returned HTTP ${metricsRes.ok ? ticketsRes.status : metricsRes.status}`);
      }
      setMetrics(await metricsRes.json());
      setTickets(await ticketsRes.json());
      setLastUpdated(new Date());
      setErrorMessage(null);
      setLoadState('ready');
    } catch (err: any) {
      setErrorMessage(err?.message?.startsWith('Gateway') ? err.message : `Cannot reach the gateway at ${API_URL}.`);
      setLoadState((prev) => (prev === 'ready' ? 'ready' : 'error')); // keep last good data visible
    } finally {
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadDashboard();
  }, [loadDashboard]);

  // Live refresh while the dashboard is visible and authenticated.
  useEffect(() => {
    if (loadState !== 'ready') return;
    const timer = setInterval(loadDashboard, REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [loadState, loadDashboard]);

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setSigningIn(true);
    setAuthError(null);
    try {
      const res = await fetch(`${API_URL}/v1/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.access_token) {
        setAuthError(res.status === 401 ? 'Invalid email or password.' : data.detail || `Sign-in failed (HTTP ${res.status}).`);
        return;
      }
      if (data.user?.role !== 'admin') {
        setAuthError('This account does not have admin access.');
        return;
      }
      localStorage.setItem(ADMIN_TOKEN_KEY, data.access_token);
      setPassword('');
      setLoadState('loading');
      await loadDashboard();
    } catch {
      setAuthError(`Cannot reach the gateway at ${API_URL}.`);
    } finally {
      setSigningIn(false);
    }
  };

  const handleSignOut = () => {
    localStorage.removeItem(ADMIN_TOKEN_KEY);
    setMetrics(null);
    setTickets([]);
    setLoadState('auth');
  };

  const ttftMet = metrics && metrics.avg_ttft_ms > 0 ? metrics.avg_ttft_ms <= TTFT_TARGET_MS : null;

  return (
    <div className="min-h-screen bg-[#080c14] text-slate-100 p-4 sm:p-6 md:p-10 font-sans selection:bg-indigo-500/30 selection:text-white">
      {/* Top Header */}
      <div className="max-w-6xl mx-auto flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-8 border-b border-slate-800/80">
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={() => router.push('/chat')}
            className="p-2.5 rounded-xl bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-400 hover:text-white transition-colors"
            aria-label="Return to support console"
            title="Return to Support Console"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <h1 className="text-lg sm:text-xl font-bold tracking-tight text-white">Operations & Telemetry Console</h1>
            <p className="text-xs text-slate-400 mt-0.5">Live operational metrics, queue backlog, and escalation tickets</p>
          </div>
        </div>

        {loadState === 'ready' && metrics && (
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className={`px-3 py-1.5 rounded-full text-xs font-mono border flex items-center gap-1.5 shadow-sm ${
                metrics.active_workers > 0
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                  : 'bg-amber-500/10 text-amber-300 border-amber-500/20'
              }`}
            >
              <span className={`w-2 h-2 rounded-full ${metrics.active_workers > 0 ? 'bg-emerald-400 animate-pulse' : 'bg-amber-400'}`} />
              {metrics.active_workers > 0 ? 'Workers Reporting' : 'No Worker Heartbeats'}
            </span>
            <button
              type="button"
              onClick={loadDashboard}
              className="p-2 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-400 hover:text-white transition-colors"
              aria-label="Refresh metrics"
              title="Refresh"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
            </button>
            <button
              type="button"
              onClick={handleSignOut}
              className="p-2 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-400 hover:text-white transition-colors"
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>

      {loadState === 'loading' && (
        <p className="max-w-6xl mx-auto mt-10 text-sm text-slate-400" role="status">Loading operational metrics…</p>
      )}

      {loadState === 'error' && (
        <div role="alert" className="max-w-6xl mx-auto mt-8 p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm flex items-center justify-between gap-3">
          <span className="flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-rose-400" aria-hidden="true" /> {errorMessage}
          </span>
          <button type="button" onClick={loadDashboard} className="text-xs font-semibold text-rose-100 hover:text-white">
            Retry
          </button>
        </div>
      )}

      {loadState === 'auth' && (
        <form onSubmit={handleSignIn} className="max-w-sm mx-auto mt-12 p-6 rounded-2xl bg-slate-900/40 border border-slate-800/80 space-y-4">
          <div className="flex items-center gap-2 text-slate-200">
            <Lock className="w-4 h-4 text-indigo-400" aria-hidden="true" />
            <h2 className="text-sm font-semibold">Admin sign-in required</h2>
          </div>
          <p className="text-xs text-slate-400">
            The operations console reads <code className="text-cyan-300">/v1/admin/*</code>, which requires an account with the admin role.
          </p>
          <div className="space-y-1">
            <label htmlFor="admin-email" className="text-xs text-slate-400">Email</label>
            <input
              id="admin-email"
              type="email"
              required
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="admin-password" className="text-xs text-slate-400">Password</label>
            <input
              id="admin-password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          {authError && <p role="alert" className="text-xs text-rose-300">{authError}</p>}
          <button
            type="submit"
            disabled={signingIn}
            className="w-full py-2.5 rounded-lg text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-colors disabled:opacity-60 disabled:cursor-wait"
          >
            {signingIn ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      )}

      {loadState === 'ready' && metrics && (
        <>
          {errorMessage && (
            <p role="status" className="max-w-6xl mx-auto mt-4 text-xs text-amber-300">
              Showing last successful snapshot — {errorMessage}
            </p>
          )}

          {/* Metrics Grid */}
          <div className="max-w-6xl mx-auto grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-8">
            <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
              <div className="flex items-center justify-between text-slate-400 mb-2">
                <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Average TTFT</span>
                <Zap className="w-4 h-4 text-indigo-400" aria-hidden="true" />
              </div>
              <div className="text-2xl font-bold text-slate-100">{metrics.avg_ttft_ms} ms</div>
              <span className={`text-[11px] font-mono ${ttftMet === null ? 'text-slate-500' : ttftMet ? 'text-emerald-400' : 'text-amber-300'}`}>
                {ttftMet === null ? 'No responses recorded yet' : `Target <${TTFT_TARGET_MS}ms ${ttftMet ? 'met' : 'missed'}`}
              </span>
            </div>

            <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
              <div className="flex items-center justify-between text-slate-400 mb-2">
                <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Cache Hit Ratio</span>
                <Database className="w-4 h-4 text-cyan-400" aria-hidden="true" />
              </div>
              <div className="text-2xl font-bold text-slate-100">{(metrics.cache_hit_ratio * 100).toFixed(1)}%</div>
              <span className="text-[11px] text-cyan-400 font-mono">Redis semantic cache (cosine &ge; 0.92)</span>
            </div>

            <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
              <div className="flex items-center justify-between text-slate-400 mb-2">
                <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Active Workers</span>
                <Cpu className="w-4 h-4 text-emerald-400" aria-hidden="true" />
              </div>
              <div className="text-2xl font-bold text-slate-100">{metrics.active_workers}</div>
              <span className="text-[11px] text-slate-400 font-mono">Heartbeats seen in the last 10s</span>
            </div>

            <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
              <div className="flex items-center justify-between text-slate-400 mb-2">
                <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Queue Depth</span>
                <Activity className="w-4 h-4 text-pink-400" aria-hidden="true" />
              </div>
              <div className="text-2xl font-bold text-slate-100">{metrics.queue_depth} Tasks</div>
              <span className={`text-[11px] font-mono ${metrics.queue_depth === 0 ? 'text-emerald-400' : 'text-amber-300'}`}>
                {metrics.queue_depth === 0 ? 'No backlog' : 'Pending + undelivered STT/LLM tasks'}
              </span>
            </div>
          </div>

          {/* Secondary stats */}
          <dl className="max-w-6xl mx-auto mt-4 grid grid-cols-2 lg:grid-cols-4 gap-3 text-xs font-mono">
            {[
              ['P95 latency', `${metrics.p95_latency_ms} ms`],
              ['Open WebSockets', metrics.active_connections],
              ['Conversations', metrics.total_conversations],
              ['Messages', metrics.total_messages]
            ].map(([label, value]) => (
              <div key={label as string} className="px-4 py-3 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-center justify-between">
                <dt className="text-slate-500">{label}</dt>
                <dd className="text-slate-200 font-semibold">{value}</dd>
              </div>
            ))}
          </dl>

          {/* Cluster Components & Support Escalation Queue */}
          <div className="max-w-6xl mx-auto grid grid-cols-1 lg:grid-cols-3 gap-6 mt-8">
            <div className="p-6 rounded-2xl bg-slate-900/40 border border-slate-800/80 lg:col-span-1">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-4 flex items-center gap-2">
                <Server className="w-4 h-4 text-indigo-400" aria-hidden="true" /> Pipeline Components
              </h3>
              <div className="space-y-3 font-mono text-xs">
                {WORKER_FLEET.map((component) => (
                  <div key={component.name} className="p-3.5 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <div className="font-semibold text-slate-200">{component.name}</div>
                      <div className="text-[10px] text-slate-500 truncate">{component.detail}</div>
                    </div>
                    <span className="text-slate-400 text-[10px] font-semibold flex-shrink-0">{component.group}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="p-6 rounded-2xl bg-slate-900/40 border border-slate-800/80 lg:col-span-2">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
                  <Headphones className="w-4 h-4 text-purple-400" aria-hidden="true" /> Support Escalation Queue
                </h3>
                <span className="text-[11px] text-slate-500 font-mono">
                  {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : ''}
                </span>
              </div>

              {tickets.length === 0 ? (
                <p className="text-xs text-slate-400">No escalation tickets yet. Ask the assistant for a human agent to create one.</p>
              ) : (
                <ul className="space-y-3">
                  {tickets.map((ticket) => (
                    <li
                      key={ticket.id}
                      className="p-4 rounded-xl bg-slate-950/60 border border-slate-800/80 hover:border-slate-700/80 transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs"
                    >
                      <div className="space-y-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-mono font-semibold text-indigo-300" title={ticket.id}>#{ticket.id.slice(0, 8)}</span>
                          <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${priorityStyle(ticket.priority)}`}>
                            {ticket.priority}
                          </span>
                          <span className="text-[10px] text-slate-500 font-mono">{new Date(ticket.created_at).toLocaleString()}</span>
                        </div>
                        <p className="text-slate-300 text-xs leading-relaxed break-words">{ticket.issue_summary}</p>
                      </div>

                      <span className={`self-start sm:self-center flex-shrink-0 px-2.5 py-1 rounded-full text-[10px] font-medium uppercase font-mono ${statusStyle(ticket.status)}`}>
                        {ticket.status.replace('_', ' ')}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
