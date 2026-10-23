'use client';

import React, { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import {
  Mic,
  MicOff,
  Send,
  RotateCcw,
  Package,
  ShieldCheck,
  AlertCircle,
  Copy,
  Check,
  Headphones,
  ArrowLeft,
  Activity,
  User,
  Zap,
  WifiOff
} from 'lucide-react';
import { VoiceOrb } from '@/components/VoiceOrb';
import { AudioWaveform } from '@/components/AudioWaveform';
import { TelemetryHUD } from '@/components/TelemetryHUD';
import { RichText } from '@/components/RichText';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8088';
const WS_BASE_URL = process.env.NEXT_PUBLIC_WS_URL || API_URL.replace(/^http/, 'ws');
const MAX_MESSAGE_CHARS = 2000; // mirrors the gateway's WS_MAX_TEXT_QUERY_CHARS

type ConnectionState = 'connecting' | 'open' | 'closed';

interface ChatMessage {
  id: string;
  sender: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  ttftMs?: number;
  totalLatencyMs?: number;
  traceId?: string;
  cacheHit?: boolean;
  streaming?: boolean; // true until the final token_chunk for this trace arrives
  isError?: boolean;
}

interface TelemetryState {
  ttftMs: number | null;
  tokensPerSec: number | null;
  totalLatencyMs: number | null;
  traceId: string | null;
  cacheHit: boolean;
  circuitBreakerState: string | null;
}

const clockTime = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

// crypto.randomUUID only exists in secure contexts (https / localhost).
const newTraceId = () =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `trace-${Date.now()}-${Math.random().toString(16).slice(2)}`;

const QUICK_ACTIONS = [
  { label: 'Track Order: ORD-88412', query: 'Check status for order ORD-88412', icon: Package, color: 'text-indigo-400', hover: 'hover:border-indigo-500/40' },
  { label: 'Check Refund: ORD-10294', query: 'Is order ORD-10294 eligible for a refund?', icon: RotateCcw, color: 'text-cyan-400', hover: 'hover:border-cyan-500/40' },
  { label: 'Warranty & Return Terms', query: 'What is your warranty and return policy?', icon: ShieldCheck, color: 'text-emerald-400', hover: 'hover:border-emerald-500/40' },
  { label: 'Escalate to Specialist', query: 'I need to speak with a human support agent', icon: User, color: 'text-purple-400', hover: 'hover:border-purple-500/40' },
];

export default function ChatPage() {
  const router = useRouter();
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      sender: 'assistant',
      content: 'Hello! I am your **Voxa Support Assistant**. I can look up live order statuses, process return & refund inquiries, verify warranty coverage, or connect you directly with a specialist. How can I assist you today?'
    }
  ]);
  const [inputMessage, setInputMessage] = useState('');
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting');
  const [isRecording, setIsRecording] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [interimTranscript, setInterimTranscript] = useState('');
  const [micError, setMicError] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  // Telemetry shows only values measured from real server responses (null until then).
  const [telemetry, setTelemetry] = useState<TelemetryState>({
    ttftMs: null,
    tokensPerSec: null,
    totalLatencyMs: null,
    traceId: null,
    cacheHit: false,
    circuitBreakerState: null
  });

  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const recognitionRef = useRef<any>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const animFrameRef = useRef<number | null>(null);
  const lastLevelRef = useRef(0);
  const transcriptAccumulatorRef = useRef<string>('');
  // Per-trace stream statistics used to derive throughput: first chunk time + chunk count.
  const streamStatsRef = useRef<Record<string, { firstAt: number; chunks: number }>>({});

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, interimTranscript]);

  const stopAudioAnalyser = () => {
    if (animFrameRef.current) {
      cancelAnimationFrame(animFrameRef.current);
      animFrameRef.current = null;
    }
    analyserRef.current = null;
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((track) => track.stop());
      micStreamRef.current = null;
    }
    if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
      audioContextRef.current.close().catch(() => {});
    }
    audioContextRef.current = null;
    lastLevelRef.current = 0;
    setAudioLevel(0);
  };

  // Connect to the gateway WebSocket (authenticated with the session token so the
  // conversation is persisted), reconnecting with capped exponential backoff.
  useEffect(() => {
    let socket: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempts = 0;
    let disposed = false;

    const handleServerEvent = (raw: string) => {
      let data: any;
      try {
        data = JSON.parse(raw);
      } catch {
        return;
      }

      if (data.type === 'token_chunk') {
        const traceId: string = data.trace_id || 'untraced';
        const now = performance.now();
        const stats = (streamStatsRef.current[traceId] ??= { firstAt: now, chunks: 0 });
        if (data.token) stats.chunks += 1;
        setIsSpeaking(!data.is_final);

        setMessages((prev) => {
          // Append to the in-flight bubble for this trace; otherwise start a new one.
          const idx = prev.findIndex((m) => m.streaming && m.traceId === traceId);
          if (idx === -1) {
            return [
              ...prev,
              {
                id: `assistant-${traceId}-${prev.length}`,
                sender: 'assistant',
                content: data.token || '',
                timestamp: clockTime(),
                ttftMs: data.ttft_ms,
                totalLatencyMs: data.total_latency_ms,
                traceId,
                cacheHit: data.cache_hit,
                streaming: !data.is_final
              }
            ];
          }
          const current = prev[idx];
          const updated: ChatMessage = {
            ...current,
            content: current.content + (data.token || ''),
            ttftMs: data.ttft_ms ?? current.ttftMs,
            totalLatencyMs: data.total_latency_ms ?? current.totalLatencyMs,
            cacheHit: data.cache_hit ?? current.cacheHit,
            streaming: !data.is_final
          };
          return [...prev.slice(0, idx), updated, ...prev.slice(idx + 1)];
        });

        if (data.is_final) {
          const elapsedSec = (now - stats.firstAt) / 1000;
          setTelemetry({
            ttftMs: data.ttft_ms ?? null,
            totalLatencyMs: data.total_latency_ms ?? null,
            tokensPerSec: elapsedSec > 0 && stats.chunks > 1 ? stats.chunks / elapsedSec : null,
            traceId,
            cacheHit: !!data.cache_hit,
            circuitBreakerState: data.circuit_breaker || 'CLOSED'
          });
          delete streamStatsRef.current[traceId];
        }
      } else if (data.type === 'error') {
        setIsSpeaking(false);
        setMessages((prev) => [
          ...prev,
          {
            id: `error-${Date.now()}-${prev.length}`,
            sender: 'assistant',
            content: data.message || 'Something went wrong.',
            timestamp: clockTime(),
            traceId: data.trace_id,
            isError: true
          }
        ]);
      }
    };

    const connect = () => {
      const token = localStorage.getItem('voxa_token');
      const url = `${WS_BASE_URL}/v1/ws/stream${token ? `?token=${encodeURIComponent(token)}` : ''}`;
      setConnectionState('connecting');
      const ws = new WebSocket(url);
      socket = ws;
      wsRef.current = ws;

      ws.onopen = () => {
        attempts = 0;
        setConnectionState('open');
      };
      ws.onmessage = (event) => handleServerEvent(event.data);
      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (disposed) return;
        setIsSpeaking(false);
        setConnectionState('closed');
        const delay = Math.min(10000, 1000 * 2 ** attempts);
        attempts += 1;
        retryTimer = setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      socket?.close();
      try {
        recognitionRef.current?.abort();
      } catch {}
      stopAudioAnalyser();
    };
  }, []);

  // Web Audio level meter that drives the VoiceOrb while listening.
  const startAudioAnalyser = (stream: MediaStream) => {
    try {
      const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
      const ctx = new AudioCtx();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      ctx.createMediaStreamSource(stream).connect(analyser);

      audioContextRef.current = ctx;
      analyserRef.current = analyser;
      const dataArray = new Uint8Array(analyser.frequencyBinCount);

      const updateLevel = () => {
        if (!analyserRef.current) return;
        analyserRef.current.getByteFrequencyData(dataArray);
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) sum += dataArray[i];
        // Quantise to 5% steps and only update state on change: setting state on every
        // animation frame re-rendered the whole chat (all messages) ~60 times a second.
        const level = Math.round(Math.min(1, sum / dataArray.length / 100) * 20) / 20;
        if (level !== lastLevelRef.current) {
          lastLevelRef.current = level;
          setAudioLevel(level);
        }
        animFrameRef.current = requestAnimationFrame(updateLevel);
      };
      updateLevel();
    } catch (err) {
      console.warn('Audio analyser unavailable:', err);
    }
  };

  const handleSendMessage = (textToSend?: string) => {
    const text = (textToSend ?? inputMessage).trim().slice(0, MAX_MESSAGE_CHARS);
    if (!text) return;

    const socket = wsRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      // Keep the typed text so nothing is lost; the banner explains the reconnect.
      setInputMessage(text);
      return;
    }

    const traceId = newTraceId();
    socket.send(JSON.stringify({ type: 'text_query', text, trace_id: traceId }));
    setMessages((prev) => [
      ...prev,
      { id: `user-${traceId}`, sender: 'user', content: text, timestamp: clockTime(), traceId }
    ]);
    setInputMessage('');
    setInterimTranscript('');
    transcriptAccumulatorRef.current = '';
  };

  // Voice input via the browser's Web Speech API (Chrome / Edge / Safari).
  const handleToggleVoice = async () => {
    if (isRecording) {
      if (recognitionRef.current) {
        // onend submits the transcript exactly once.
        try {
          recognitionRef.current.stop();
        } catch {}
      } else {
        stopAudioAnalyser();
        setIsRecording(false);
      }
      return;
    }

    setMicError(null);
    setInterimTranscript('');
    transcriptAccumulatorRef.current = '';

    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setMicError('Voice input is not supported in this browser. Please use Chrome, Edge or Safari, or type your question.');
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      micStreamRef.current = stream;
      startAudioAnalyser(stream);
    } catch (err) {
      console.warn('Microphone permission error:', err);
      setMicError('Microphone access is blocked. Allow microphone access in your browser settings to speak.');
      return;
    }

    try {
      const recognition = new SpeechRecognition();
      recognition.continuous = false; // single utterance, auto-commits on silence
      recognition.interimResults = true;
      recognition.lang = 'en-US';
      recognition.maxAlternatives = 1;

      recognition.onresult = (event: any) => {
        let transcript = '';
        for (let i = 0; i < event.results.length; i++) {
          transcript += event.results[i][0].transcript;
        }
        if (transcript) {
          setInterimTranscript(transcript);
          transcriptAccumulatorRef.current = transcript;
          setInputMessage(transcript);
        }
      };

      recognition.onerror = (event: any) => {
        if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
          setMicError('Microphone permission was denied in your browser settings.');
        } else if (event.error === 'no-speech') {
          setMicError('No speech was detected. Please try again.');
        } else if (event.error !== 'aborted') {
          setMicError(`Voice input error: ${event.error}`);
        }
      };

      // Fires after stop(), silence, or an error: the single place that submits.
      recognition.onend = () => {
        recognitionRef.current = null;
        setIsRecording(false);
        stopAudioAnalyser();
        const transcript = transcriptAccumulatorRef.current.trim();
        transcriptAccumulatorRef.current = '';
        setInterimTranscript('');
        if (transcript) handleSendMessage(transcript);
      };

      recognitionRef.current = recognition;
      recognition.start();
      setIsRecording(true);
    } catch (err) {
      console.warn('Speech recognition init error:', err);
      recognitionRef.current = null;
      stopAudioAnalyser();
      setMicError('Could not start voice input. Please type your question instead.');
    }
  };

  const copyToClipboard = async (text: string, id: string) => {
    try {
      // navigator.clipboard is undefined on plain-http origins (e.g. a public IP).
      await navigator.clipboard.writeText(text.replace(/\*\*|`/g, ''));
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 2000);
    } catch {
      console.warn('Clipboard is unavailable in this context.');
    }
  };

  const isConnected = connectionState === 'open';
  const statusLabel = isRecording
    ? 'Listening (speak now)...'
    : isSpeaking
      ? 'Responding...'
      : isConnected
        ? 'Voice assistant ready'
        : 'Connecting to gateway...';

  return (
    <div className="flex h-screen h-dvh bg-[#080c14] text-slate-100 overflow-hidden font-sans relative">
      {/* Left Assistant Panel */}
      <aside className="w-80 border-r border-slate-800/80 bg-slate-950/60 p-6 flex-col justify-between hidden lg:flex relative z-10 backdrop-blur-md">
        <div>
          {/* Brand Header */}
          <div className="flex items-center gap-3 pb-6 border-b border-slate-800/80">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 to-cyan-400 flex items-center justify-center shadow-md shadow-indigo-500/20">
              <Headphones className="w-5 h-5 text-white" aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-sm font-bold tracking-tight text-white">Voxa Support</h1>
              <p className="text-[11px] text-slate-400">Live Voice & Knowledge Console</p>
            </div>
          </div>

          {/* Voice Orb Area */}
          <div className="mt-8 flex flex-col items-center text-center">
            <button
              type="button"
              onClick={handleToggleVoice}
              aria-pressed={isRecording}
              aria-label={isRecording ? 'Stop listening' : 'Start voice input'}
              className="group relative p-3 rounded-full hover:bg-slate-900/60 transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
            >
              <VoiceOrb isSpeaking={isSpeaking} isListening={isRecording} audioLevel={audioLevel} />
              <span className="absolute inset-0 rounded-full border border-indigo-500/20 group-hover:border-indigo-500/50 transition-colors pointer-events-none" />
            </button>

            <div className="mt-4 flex items-center gap-2" aria-live="polite">
              <span className={`w-2 h-2 rounded-full ${isRecording ? 'bg-pink-500 animate-pulse' : isSpeaking ? 'bg-cyan-400 animate-pulse' : isConnected ? 'bg-emerald-400' : 'bg-amber-400'}`} />
              <span className="text-xs font-medium text-slate-300">{statusLabel}</span>
            </div>

            {/* Decorative activity waveform */}
            <div className="w-full mt-4 h-8 flex items-center justify-center" aria-hidden="true">
              <AudioWaveform isPlaying={isRecording || isSpeaking} />
            </div>
          </div>

          {/* Quick Inquiry Actions */}
          <div className="mt-8">
            <span className="text-[11px] font-semibold tracking-wider text-slate-500 uppercase">
              Frequent Support Inquiries
            </span>
            <div className="mt-3 space-y-2">
              {QUICK_ACTIONS.map(({ label, query, icon: Icon, color, hover }) => (
                <button
                  key={label}
                  type="button"
                  onClick={() => handleSendMessage(query)}
                  disabled={!isConnected}
                  className={`w-full text-left p-2.5 rounded-xl bg-slate-900/60 hover:bg-slate-900 border border-slate-800/80 ${hover} text-xs text-slate-300 transition-all flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400`}
                >
                  <Icon className={`w-3.5 h-3.5 ${color} flex-shrink-0`} aria-hidden="true" />
                  <span className="truncate">{label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Bottom Session Info */}
        <div className="pt-4 border-t border-slate-800/80 flex items-center gap-2 text-xs text-slate-400">
          <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-emerald-400' : connectionState === 'connecting' ? 'bg-amber-400 animate-pulse' : 'bg-rose-500'}`} />
          <span className="font-mono text-[11px]">
            Gateway: {isConnected ? 'connected' : connectionState === 'connecting' ? 'connecting…' : 'offline'}
          </span>
        </div>
      </aside>

      {/* Main Chat Workspace */}
      <main className="flex-1 min-w-0 flex flex-col h-full relative z-10 bg-[#080c14]">
        {/* Top App Bar */}
        <header className="h-16 border-b border-slate-800/80 px-3 sm:px-6 flex items-center justify-between gap-2 bg-slate-950/40 backdrop-blur-md z-20">
          <div className="flex items-center gap-3 min-w-0">
            <button
              type="button"
              onClick={() => router.push('/')}
              aria-label="Back to home"
              className="p-2 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-400 hover:text-white transition-colors lg:hidden"
            >
              <ArrowLeft className="w-4 h-4" />
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-bold text-white tracking-tight truncate">Support Session Console</h2>
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-semibold border flex-shrink-0 ${
                    isConnected
                      ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
                      : 'bg-amber-500/10 border-amber-500/20 text-amber-300'
                  }`}
                >
                  {isConnected ? 'Live' : connectionState === 'connecting' ? 'Connecting' : 'Offline'}
                </span>
              </div>
              <p className="text-[11px] text-slate-400 hidden sm:block">Automated Knowledge & Voice Resolution Pipeline</p>
            </div>
          </div>

          <div className="flex items-center gap-2 sm:gap-3 flex-shrink-0">
            <TelemetryHUD
              ttftMs={telemetry.ttftMs}
              tokensPerSec={telemetry.tokensPerSec}
              totalLatencyMs={telemetry.totalLatencyMs}
              traceId={telemetry.traceId}
              cacheHit={telemetry.cacheHit}
              circuitBreakerState={telemetry.circuitBreakerState}
            />

            <button
              type="button"
              onClick={() => router.push('/admin')}
              aria-label="Operations dashboard"
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-300 hover:text-white transition-colors flex items-center gap-1.5"
            >
              <Activity className="w-3.5 h-3.5 text-cyan-400" aria-hidden="true" />
              <span className="hidden sm:inline">Operations</span>
            </button>
          </div>
        </header>

        {/* Connection banner */}
        {connectionState === 'closed' && (
          <div role="status" className="px-4 py-2 bg-amber-500/10 border-b border-amber-500/30 text-amber-200 text-xs flex items-center justify-center gap-2">
            <WifiOff className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Connection to the support gateway was lost. Reconnecting automatically…</span>
          </div>
        )}

        {/* Messages Feed */}
        <div
          className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-4 max-w-4xl w-full mx-auto"
          role="log"
          aria-live="polite"
          aria-busy={isSpeaking}
          aria-label="Conversation"
        >
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex flex-col ${msg.sender === 'user' ? 'items-end' : 'items-start'} group`}
            >
              <div className="flex items-end gap-2 max-w-[92%] sm:max-w-[80%]">
                {msg.sender === 'assistant' && (
                  <div
                    className={`w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 mb-1 border ${
                      msg.isError
                        ? 'bg-amber-500/10 border-amber-500/30 text-amber-400'
                        : 'bg-indigo-600/20 border-indigo-500/30 text-indigo-400'
                    }`}
                    aria-hidden="true"
                  >
                    {msg.isError ? <AlertCircle className="w-3.5 h-3.5" /> : <Headphones className="w-3.5 h-3.5" />}
                  </div>
                )}

                <div
                  className={`min-w-0 p-4 rounded-2xl text-xs sm:text-sm leading-relaxed transition-all ${
                    msg.sender === 'user'
                      ? 'bg-indigo-600 text-white rounded-br-none shadow-md shadow-indigo-600/10'
                      : msg.isError
                        ? 'bg-amber-500/10 border border-amber-500/30 text-amber-100 rounded-bl-none'
                        : 'bg-slate-900/80 border border-slate-800 text-slate-200 rounded-bl-none shadow-sm'
                  }`}
                >
                  <div className="whitespace-pre-wrap break-words">
                    <RichText text={msg.content} />
                  </div>

                  {/* Metadata Footer (only for real, measured responses) */}
                  {msg.sender === 'assistant' && !msg.isError && msg.ttftMs !== undefined && (
                    <div className="mt-2.5 pt-2 border-t border-slate-800 flex items-center justify-between gap-3 text-[10px] text-slate-500 font-mono">
                      <div className="flex flex-wrap items-center gap-2">
                        {msg.cacheHit && (
                          <span className="text-emerald-400 font-semibold flex items-center gap-1">
                            <Zap className="w-2.5 h-2.5" aria-hidden="true" /> Cache hit
                          </span>
                        )}
                        <span>TTFT: <span className="text-cyan-400">{msg.ttftMs}ms</span></span>
                        {msg.totalLatencyMs !== undefined && <span>Total: {msg.totalLatencyMs}ms</span>}
                      </div>

                      <button
                        type="button"
                        onClick={() => copyToClipboard(msg.content, msg.id)}
                        className="hover:text-slate-300 transition-colors p-1 rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
                        aria-label="Copy response"
                        title="Copy response"
                      >
                        {copiedId === msg.id ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}

          {/* Live Interim Voice Transcript Bubble */}
          {isRecording && interimTranscript && (
            <div className="flex flex-col items-end">
              <div className="p-3.5 rounded-2xl rounded-br-none bg-indigo-600/40 border border-indigo-500/50 text-indigo-100 text-xs sm:text-sm max-w-[80%] flex items-center gap-2">
                <Mic className="w-3.5 h-3.5 text-pink-400 animate-pulse" aria-hidden="true" />
                <span>{interimTranscript}</span>
              </div>
              <span className="text-[10px] text-pink-400 font-mono mt-1 mr-1">Listening in real-time...</span>
            </div>
          )}

          {/* Mic Access Error Toast */}
          {micError && (
            <div role="alert" className="p-3 rounded-xl bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs flex items-center justify-between">
              <div className="flex items-center gap-2">
                <AlertCircle className="w-4 h-4 text-amber-400 flex-shrink-0" aria-hidden="true" />
                <span>{micError}</span>
              </div>
              <button
                type="button"
                onClick={() => setMicError(null)}
                className="text-slate-400 hover:text-white text-xs font-semibold ml-3"
              >
                Dismiss
              </button>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input Controls Bar */}
        <form
          className="p-3 sm:p-4 border-t border-slate-800/80 bg-slate-950/80 backdrop-blur-md"
          onSubmit={(e) => {
            e.preventDefault();
            handleSendMessage();
          }}
        >
          <div className="max-w-4xl mx-auto flex items-center gap-2.5">
            {/* Microphone Toggle Button */}
            <button
              type="button"
              onClick={handleToggleVoice}
              disabled={!isConnected && !isRecording}
              aria-pressed={isRecording}
              aria-label={isRecording ? 'Stop recording' : 'Speak via microphone'}
              title={isRecording ? 'Stop recording' : 'Speak via microphone'}
              className={`p-3 rounded-xl transition-all flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed ${
                isRecording
                  ? 'bg-pink-600 text-white animate-pulse shadow-lg shadow-pink-600/30'
                  : 'bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 hover:border-slate-700'
              }`}
            >
              {isRecording ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
            </button>

            {/* Text Input */}
            <div className="flex-1 relative">
              <label htmlFor="chat-input" className="sr-only">Message</label>
              <input
                id="chat-input"
                type="text"
                value={inputMessage}
                maxLength={MAX_MESSAGE_CHARS}
                autoComplete="off"
                onChange={(e) => setInputMessage(e.target.value)}
                placeholder={
                  isRecording
                    ? 'Listening to your speech...'
                    : isConnected
                      ? 'Type an order, refund or warranty question…'
                      : 'Waiting for the gateway connection…'
                }
                className="w-full bg-slate-900/90 border border-slate-800/90 rounded-xl px-4 py-3 text-xs sm:text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-indigo-500/80 focus:ring-1 focus:ring-indigo-500/80 transition-all"
              />
            </div>

            {/* Send Button */}
            <button
              type="submit"
              disabled={!inputMessage.trim() || !isConnected}
              aria-label="Send message"
              title="Send message"
              className={`p-3 rounded-xl transition-all flex items-center justify-center ${
                inputMessage.trim() && isConnected
                  ? 'bg-indigo-600 hover:bg-indigo-500 text-white shadow-md shadow-indigo-600/20'
                  : 'bg-slate-900 text-slate-600 border border-slate-800 cursor-not-allowed'
              }`}
            >
              <Send className="w-4 h-4" />
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
