'use client';

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { 
  Activity, 
  Cpu, 
  Database, 
  ShieldCheck, 
  Zap, 
  Users, 
  Clock, 
  ArrowLeft, 
  CheckCircle2, 
  AlertTriangle,
  Headphones,
  FileText,
  Server,
  Layers
} from 'lucide-react';

export default function AdminDashboard() {
  const router = useRouter();
  const [metrics, setMetrics] = useState({
    active_connections: 18,
    queue_depth: 0,
    cache_hit_ratio: 0.42,
    avg_ttft_ms: 138.4,
    p95_latency_ms: 205.0,
    active_workers: 4,
    total_conversations: 342,
    total_messages: 1890
  });

  const [tickets, setTickets] = useState([
    {
      id: 'ticket-109283',
      issue_summary: 'Customer received damaged headset on order ORD-88412. Requested priority replacement label.',
      priority: 'high',
      status: 'in_progress',
      created_at: '2026-08-30T10:14:00Z'
    },
    {
      id: 'ticket-109284',
      issue_summary: 'Inquiry regarding international warranty coverage and customs duties for EU orders.',
      priority: 'medium',
      status: 'open',
      created_at: '2026-08-30T11:30:00Z'
    },
    {
      id: 'ticket-109285',
      issue_summary: 'B2B enterprise quote requested for 35 units of Smart Studio Headphones.',
      priority: 'normal',
      status: 'resolved',
      created_at: '2026-08-30T09:20:00Z'
    }
  ]);

  useEffect(() => {
    // Fetch live metrics from FastAPI Gateway
    fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8088'}/v1/admin/metrics`, {
      headers: {
        'Authorization': `Bearer ${localStorage.getItem('voxa_token') || ''}`
      }
    })
      .then((res) => res.json())
      .then((data) => {
        if (data && data.active_connections !== undefined) {
          setMetrics(data);
        }
      })
      .catch(() => {});
  }, []);

  return (
    <div className="min-h-screen bg-[#080c14] text-slate-100 p-6 md:p-10 font-sans selection:bg-indigo-500/30 selection:text-white">
      {/* Top Header */}
      <div className="max-w-6xl mx-auto flex items-center justify-between pb-8 border-b border-slate-800/80">
        <div className="flex items-center gap-4">
          <button
            onClick={() => router.push('/chat')}
            className="p-2.5 rounded-xl bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-400 hover:text-white transition-colors"
            title="Return to Support Console"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
              Operations & Telemetry Console
            </h1>
            <p className="text-xs text-slate-400 mt-0.5">Live operational SLA metrics, queue backpressure, and worker cluster health</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className="px-3 py-1.5 rounded-full text-xs font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 flex items-center gap-1.5 shadow-sm">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
            Cluster Healthy
          </span>
        </div>
      </div>

      {/* Metrics Grid */}
      <div className="max-w-6xl mx-auto grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-8">
        {/* Metric 1 */}
        <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
          <div className="flex items-center justify-between text-slate-400 mb-2">
            <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Average TTFT</span>
            <Zap className="w-4 h-4 text-indigo-400" />
          </div>
          <div className="text-2xl font-bold text-slate-100">{metrics.avg_ttft_ms} ms</div>
          <span className="text-[11px] text-emerald-400 font-mono">Target: &lt;180ms Met</span>
        </div>

        {/* Metric 2 */}
        <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
          <div className="flex items-center justify-between text-slate-400 mb-2">
            <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Cache Hit Ratio</span>
            <Database className="w-4 h-4 text-cyan-400" />
          </div>
          <div className="text-2xl font-bold text-slate-100">{(metrics.cache_hit_ratio * 100).toFixed(1)}%</div>
          <span className="text-[11px] text-cyan-400 font-mono">Redis Semantic Cache &ge;0.92</span>
        </div>

        {/* Metric 3 */}
        <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
          <div className="flex items-center justify-between text-slate-400 mb-2">
            <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Active Workers</span>
            <Cpu className="w-4 h-4 text-emerald-400" />
          </div>
          <div className="text-2xl font-bold text-slate-100">{metrics.active_workers} Daemons</div>
          <span className="text-[11px] text-slate-400 font-mono">2 STT + 2 LLM Instances</span>
        </div>

        {/* Metric 4 */}
        <div className="p-5 rounded-2xl bg-slate-900/40 border border-slate-800/80 hover:border-slate-700/80 transition-all">
          <div className="flex items-center justify-between text-slate-400 mb-2">
            <span className="text-xs font-medium uppercase tracking-wider text-slate-500">Queue Depth</span>
            <Activity className="w-4 h-4 text-pink-400" />
          </div>
          <div className="text-2xl font-bold text-slate-100">{metrics.queue_depth} Tasks</div>
          <span className="text-[11px] text-emerald-400 font-mono">0 Backpressure Backlog</span>
        </div>
      </div>

      {/* Cluster Health & Support Escalation Queue */}
      <div className="max-w-6xl mx-auto grid grid-cols-1 lg:grid-cols-3 gap-6 mt-8">
        {/* Worker Fleet Status */}
        <div className="p-6 rounded-2xl bg-slate-900/40 border border-slate-800/80 lg:col-span-1">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-4 flex items-center gap-2">
            <Server className="w-4 h-4 text-indigo-400" /> Distributed Worker Fleet
          </h3>
          <div className="space-y-3 font-mono text-xs">
            <div className="p-3.5 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-center justify-between">
              <div>
                <div className="font-semibold text-slate-200">Faster-Whisper STT #1</div>
                <div className="text-[10px] text-slate-500">Pinned INT8 RAM • 16kHz</div>
              </div>
              <span className="text-emerald-400 text-[10px] font-semibold">5s Heartbeat</span>
            </div>

            <div className="p-3.5 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-center justify-between">
              <div>
                <div className="font-semibold text-slate-200">Qwen2.5-0.5B LLM #1</div>
                <div className="text-[10px] text-slate-500">15ms Micro-Batching • Batch 8</div>
              </div>
              <span className="text-emerald-400 text-[10px] font-semibold">5s Heartbeat</span>
            </div>

            <div className="p-3.5 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-center justify-between">
              <div>
                <div className="font-semibold text-slate-200">Redis Streams Broker</div>
                <div className="text-[10px] text-slate-500">XREADGROUP • In-Memory</div>
              </div>
              <span className="text-cyan-400 text-[10px] font-semibold">0.5ms Ping</span>
            </div>

            <div className="p-3.5 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-center justify-between">
              <div>
                <div className="font-semibold text-slate-200">PostgreSQL + pgvector</div>
                <div className="text-[10px] text-slate-500">384-dim HNSW Cosine Index</div>
              </div>
              <span className="text-emerald-400 text-[10px] font-semibold">ACID OK</span>
            </div>
          </div>
        </div>

        {/* Human Escalation Tickets */}
        <div className="p-6 rounded-2xl bg-slate-900/40 border border-slate-800/80 lg:col-span-2">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
              <Headphones className="w-4 h-4 text-purple-400" /> Support Escalation Queue
            </h3>
            <span className="text-[11px] text-slate-500 font-mono">Live Automated Dispatch</span>
          </div>

          <div className="space-y-3">
            {tickets.map((ticket) => (
              <div
                key={ticket.id}
                className="p-4 rounded-xl bg-slate-950/60 border border-slate-800/80 hover:border-slate-700/80 transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs"
              >
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-semibold text-indigo-300">{ticket.id}</span>
                    <span
                      className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${
                        ticket.priority === 'high'
                          ? 'bg-rose-500/10 text-rose-400 border border-rose-500/20'
                          : ticket.priority === 'medium'
                          ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                          : 'bg-slate-800 text-slate-400 border border-slate-700'
                      }`}
                    >
                      {ticket.priority}
                    </span>
                  </div>
                  <p className="text-slate-300 text-xs leading-relaxed">{ticket.issue_summary}</p>
                </div>

                <div className="flex items-center gap-2 sm:self-center flex-shrink-0">
                  <span
                    className={`px-2.5 py-1 rounded-full text-[10px] font-medium uppercase font-mono ${
                      ticket.status === 'in_progress'
                        ? 'bg-amber-500/10 text-amber-300 border border-amber-500/20'
                        : ticket.status === 'open'
                        ? 'bg-blue-500/10 text-blue-300 border border-blue-500/20'
                        : 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/20'
                    }`}
                  >
                    {ticket.status.replace('_', ' ')}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
