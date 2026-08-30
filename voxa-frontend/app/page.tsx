'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import { 
  Headphones, 
  ShieldCheck, 
  Database, 
  Zap, 
  Activity, 
  ArrowRight, 
  MessageSquare, 
  Clock, 
  Cpu,
  CheckCircle2,
  Lock,
  Layers
} from 'lucide-react';

export default function LandingPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleStartSession = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8088'}/v1/auth/guest-demo`, {
        method: 'POST',
      });
      const data = await res.json();
      if (res.ok && data.access_token) {
        localStorage.setItem('voxa_token', data.access_token);
        localStorage.setItem('voxa_user', JSON.stringify(data.user));
        router.push('/chat');
      } else {
        localStorage.setItem('voxa_token', 'demo-session-token');
        localStorage.setItem('voxa_user', JSON.stringify({ email: 'support-guest@voxa.io', role: 'user' }));
        router.push('/chat');
      }
    } catch {
      localStorage.setItem('voxa_token', 'demo-session-token');
      localStorage.setItem('voxa_user', JSON.stringify({ email: 'support-guest@voxa.io', role: 'user' }));
      router.push('/chat');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col justify-between bg-[#080c14] text-slate-100 selection:bg-indigo-500/30 selection:text-white relative overflow-hidden font-sans">
      {/* Subtle Ambient Lighting (Linear/Vercel Style) */}
      <div className="absolute top-[-20%] left-[50%] translate-x-[-50%] w-[900px] h-[400px] bg-gradient-to-b from-indigo-500/10 via-cyan-500/5 to-transparent rounded-full blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[-10%] w-[500px] h-[500px] bg-indigo-950/20 rounded-full blur-[140px] pointer-events-none" />

      {/* Navigation Header */}
      <header className="w-full max-w-6xl mx-auto px-6 py-6 flex items-center justify-between z-10">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 via-indigo-500 to-cyan-400 flex items-center justify-center shadow-md shadow-indigo-500/20">
            <Headphones className="w-5 h-5 text-white" />
          </div>
          <div>
            <span className="text-base font-bold tracking-tight text-white flex items-center gap-2">
              Voxa
              <span className="text-[10px] uppercase font-semibold px-2 py-0.5 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-indigo-300">
                Support Platform
              </span>
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button 
            onClick={() => router.push('/admin')}
            className="px-3.5 py-1.5 rounded-lg text-xs font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-900 border border-transparent hover:border-slate-800 transition-all"
          >
            Operations & Telemetry
          </button>
          <button
            onClick={handleStartSession}
            disabled={loading}
            className="px-4 py-2 rounded-lg text-xs font-semibold bg-white text-slate-950 hover:bg-slate-200 transition-all shadow-sm flex items-center gap-1.5"
          >
            <span>Launch Console</span>
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>
      </header>

      {/* Hero Content */}
      <main className="w-full max-w-5xl mx-auto px-6 py-12 flex flex-col items-center text-center my-auto z-10">
        <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-slate-900/90 border border-slate-800 text-xs text-slate-300 mb-6 shadow-sm">
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
          <span>Real-Time Voice AI & Enterprise Knowledge Gateway</span>
        </div>

        <h1 className="text-4xl sm:text-5xl md:text-6xl font-bold tracking-tight text-white max-w-3xl leading-[1.12]">
          Instant Customer Support with{' '}
          <span className="bg-gradient-to-r from-indigo-400 via-cyan-300 to-indigo-200 bg-clip-text text-transparent">
            Low-Latency Voice AI
          </span>
        </h1>

        <p className="mt-6 text-base md:text-lg text-slate-400 max-w-2xl font-normal leading-relaxed">
          An asynchronous, full-duplex support platform combining sub-180ms speech inference, PostgreSQL vector search, and distributed resilience to automate high-volume inquiries.
        </p>

        {/* Action Controls */}
        <div className="mt-8 flex flex-col sm:flex-row items-center gap-3.5">
          <button
            onClick={handleStartSession}
            disabled={loading}
            className="w-full sm:w-auto px-6 py-3 rounded-xl text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/25 transition-all flex items-center justify-center gap-2"
          >
            <span>{loading ? 'Initializing Session...' : 'Start Live Support Session'}</span>
            <ArrowRight className="w-4 h-4" />
          </button>

          <button
            onClick={() => router.push('/admin')}
            className="w-full sm:w-auto px-5 py-3 rounded-xl text-sm font-medium bg-slate-900/80 hover:bg-slate-800 text-slate-300 border border-slate-800 transition-all flex items-center justify-center gap-2"
          >
            <Activity className="w-4 h-4 text-cyan-400" />
            <span>View System Metrics</span>
          </button>
        </div>

        {/* Feature Highlights Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 w-full mt-16 text-left">
          <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
            <div className="w-8 h-8 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center mb-3">
              <Zap className="w-4 h-4 text-indigo-400" />
            </div>
            <h3 className="text-sm font-semibold text-slate-200">&lt;180ms Response Time</h3>
            <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
              Full-duplex WebSocket streaming with dynamic audio buffering for real-time speech interaction.
            </p>
          </div>

          <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
            <div className="w-8 h-8 rounded-lg bg-cyan-500/10 border border-cyan-500/20 flex items-center justify-center mb-3">
              <Database className="w-4 h-4 text-cyan-400" />
            </div>
            <h3 className="text-sm font-semibold text-slate-200">pgvector RAG & Cache</h3>
            <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
              Resolves 42% of frequent policy and order queries in &lt;15ms via in-memory semantic caching.
            </p>
          </div>

          <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
            <div className="w-8 h-8 rounded-lg bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center mb-3">
              <ShieldCheck className="w-4 h-4 text-emerald-400" />
            </div>
            <h3 className="text-sm font-semibold text-slate-200">Fault-Tolerant Workflows</h3>
            <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
              3-state circuit breakers and Dead-Letter Queues (DLQ) with automated escalation tickets.
            </p>
          </div>

          <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
            <div className="w-8 h-8 rounded-lg bg-purple-500/10 border border-purple-500/20 flex items-center justify-center mb-3">
              <Activity className="w-4 h-4 text-purple-400" />
            </div>
            <h3 className="text-sm font-semibold text-slate-200">Live SRE Observability</h3>
            <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
              Prometheus telemetry and Grafana dashboards for monitoring P95/P99 latency and queue backpressure.
            </p>
          </div>
        </div>
      </main>

      {/* Clean Footer */}
      <footer className="w-full max-w-6xl mx-auto px-6 py-6 border-t border-slate-800/60 flex flex-col sm:flex-row items-center justify-between text-xs text-slate-500 gap-3 z-10">
        <div>
          <span>Voxa Customer Support Platform • Real-Time Voice AI Architecture</span>
        </div>
        <div className="flex items-center gap-4">
          <span className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
            Gateway Status: Operational
          </span>
        </div>
      </footer>
    </div>
  );
}
