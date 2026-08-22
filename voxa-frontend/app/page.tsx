'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Sparkles, Shield, Cpu, Zap, Activity, ArrowRight, Mic, Lock, Mail } from 'lucide-react';

export default function LandingPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleGuestLogin = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/v1/auth/guest-demo`, {
        method: 'POST',
      });
      const data = await res.json();
      if (res.ok) {
        localStorage.setItem('voxa_token', data.access_token);
        localStorage.setItem('voxa_user', JSON.stringify(data.user));
        router.push('/chat');
      } else {
        // Fallback demo token for offline preview
        localStorage.setItem('voxa_token', 'demo-guest-token');
        localStorage.setItem('voxa_user', JSON.stringify({ email: 'guest@voxa.demo', role: 'user' }));
        router.push('/chat');
      }
    } catch (e) {
      // Fallback demo for preview without server
      localStorage.setItem('voxa_token', 'demo-guest-token');
      localStorage.setItem('voxa_user', JSON.stringify({ email: 'guest@voxa.demo', role: 'user' }));
      router.push('/chat');
    } finally {
      setLoading(false);
    }
  };

  const handleAuth = async (isSignup: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const endpoint = isSignup ? '/v1/auth/signup' : '/v1/auth/login';
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password })
      });
      const data = await res.json();
      if (res.ok) {
        localStorage.setItem('voxa_token', data.access_token);
        localStorage.setItem('voxa_user', JSON.stringify(data.user));
        router.push(data.user.role === 'admin' ? '/admin' : '/chat');
      } else {
        setError(data.detail || 'Authentication failed');
      }
    } catch (e) {
      setError('Could not connect to backend gateway. Try Guest Demo Mode!');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-between p-6 relative overflow-hidden bg-radial from-slate-900 to-[#090d16]">
      {/* Ambient background glow */}
      <div className="absolute top-[-10%] left-[20%] w-[500px] h-[500px] bg-indigo-600/20 rounded-full blur-[140px] pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[20%] w-[500px] h-[500px] bg-cyan-600/20 rounded-full blur-[140px] pointer-events-none" />

      {/* Top Navigation */}
      <header className="w-full max-w-6xl flex justify-between items-center py-4 z-10">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-600 to-cyan-400 flex items-center justify-center shadow-lg shadow-indigo-500/30">
            <Mic className="w-5 h-5 text-white" />
          </div>
          <span className="text-xl font-bold tracking-tight bg-gradient-to-r from-white via-slate-200 to-slate-400 bg-clip-text text-transparent">
            VOXA 2.0
          </span>
        </div>

        <div className="flex items-center gap-4">
          <button 
            onClick={() => router.push('/admin')}
            className="text-xs font-mono text-slate-400 hover:text-cyan-300 transition-colors"
          >
            SRE / ADMIN
          </button>
          <button
            onClick={handleGuestLogin}
            disabled={loading}
            className="px-4 py-2 rounded-xl text-xs font-semibold bg-indigo-600/20 border border-indigo-500/40 text-indigo-300 hover:bg-indigo-600/30 transition-all hover:scale-[1.02]"
          >
            1-Click Recruiter Demo
          </button>
        </div>
      </header>

      {/* Hero Body */}
      <main className="w-full max-w-4xl flex flex-col items-center text-center my-auto py-12 z-10">
        <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-slate-800/80 border border-slate-700 text-xs text-indigo-300 mb-6 shadow-inner">
          <Sparkles className="w-3.5 h-3.5 text-cyan-400" />
          <span>Production-Grade Multimodal AI Voice & Inference Gateway</span>
        </div>

        <h1 className="text-4xl md:text-6xl font-extrabold tracking-tight text-white max-w-3xl leading-[1.15]">
          Sub-200ms Voice AI with{' '}
          <span className="bg-gradient-to-r from-indigo-400 via-cyan-300 to-pink-400 bg-clip-text text-transparent">
            Dynamic Micro-Batching
          </span>
        </h1>

        <p className="mt-6 text-base md:text-lg text-slate-400 max-w-2xl">
          An asynchronous, fault-tolerant voice-and-text AI platform built with FastAPI, Redis Streams, PostgreSQL (pgvector), and Next.js under 500+ concurrent connections.
        </p>

        {/* Feature Badges */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 w-full max-w-3xl mt-10">
          <div className="glass-panel p-4 rounded-2xl flex flex-col items-center">
            <Zap className="w-6 h-6 text-yellow-400 mb-2" />
            <span className="text-xs font-bold text-slate-200">&lt;200ms TTFT</span>
            <span className="text-[11px] text-slate-400">Streamed Tokens</span>
          </div>

          <div className="glass-panel p-4 rounded-2xl flex flex-col items-center">
            <Cpu className="w-6 h-6 text-cyan-400 mb-2" />
            <span className="text-xs font-bold text-slate-200">Micro-Batching</span>
            <span className="text-[11px] text-slate-400">15ms Window / 8x</span>
          </div>

          <div className="glass-panel p-4 rounded-2xl flex flex-col items-center">
            <Shield className="w-6 h-6 text-emerald-400 mb-2" />
            <span className="text-xs font-bold text-slate-200">Circuit Breakers</span>
            <span className="text-[11px] text-slate-400">DLQ Auto-Recovery</span>
          </div>

          <div className="glass-panel p-4 rounded-2xl flex flex-col items-center">
            <Activity className="w-6 h-6 text-pink-400 mb-2" />
            <span className="text-xs font-bold text-slate-200">pgvector RAG</span>
            <span className="text-[11px] text-slate-400">Semantic Cache &ge;0.92</span>
          </div>
        </div>

        {/* 1-Click Launch Button */}
        <div className="mt-10 flex flex-col sm:flex-row items-center gap-4">
          <button
            onClick={handleGuestLogin}
            disabled={loading}
            className="px-8 py-4 rounded-2xl font-bold text-sm bg-gradient-to-r from-indigo-500 to-cyan-500 hover:from-indigo-600 hover:to-cyan-600 text-white shadow-xl shadow-indigo-500/25 flex items-center gap-3 transition-transform hover:scale-105 active:scale-95"
          >
            <span>Launch Live Voice Demo</span>
            <ArrowRight className="w-4 h-4" />
          </button>
        </div>

        {error && (
          <div className="mt-4 text-xs text-rose-400 bg-rose-950/40 px-4 py-2 rounded-xl border border-rose-800/60">
            {error}
          </div>
        )}
      </main>

      {/* Footer */}
      <footer className="w-full text-center py-6 text-xs text-slate-600 border-t border-slate-800/40 z-10">
        Voxa 2.0 — Engineered for Tier-1 Screening & MAANG 2026 Standards • Faster-Whisper + Qwen2.5-0.5B + edge-tts
      </footer>
    </div>
  );
}
