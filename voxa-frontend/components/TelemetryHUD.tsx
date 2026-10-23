'use client';

import React, { useState } from 'react';
import { Activity, ChevronDown, ChevronUp, Zap, Cpu, ShieldCheck } from 'lucide-react';

interface TelemetryHUDProps {
  ttftMs: number | null;
  tokensPerSec: number | null;
  totalLatencyMs: number | null;
  traceId: string | null;
  cacheHit?: boolean;
  circuitBreakerState?: string | null;
}

const fmt = (value: number | null, unit: string, digits = 0) =>
  value === null || value === undefined ? '—' : `${value.toFixed(digits)} ${unit}`;

/**
 * Compact telemetry pill with an expandable popover. Shows only values measured from
 * the latest server response; everything is "—" until the first answer arrives.
 */
export const TelemetryHUD: React.FC<TelemetryHUDProps> = ({
  ttftMs,
  tokensPerSec,
  totalLatencyMs,
  traceId,
  cacheHit = false,
  circuitBreakerState
}) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const hasData = ttftMs !== null;
  const breakerOk = !circuitBreakerState || circuitBreakerState === 'CLOSED';

  return (
    <div className="relative font-mono text-xs z-50">
      {/* Header Inline Pill Badge */}
      <button
        type="button"
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-900/90 hover:bg-slate-800 border border-slate-800 hover:border-slate-700 text-slate-200 transition-all select-none shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
        onClick={() => setIsExpanded(!isExpanded)}
        aria-expanded={isExpanded}
        aria-controls="telemetry-popover"
        title="View latency telemetry for the latest response"
      >
        <span className={`w-2 h-2 rounded-full ${hasData ? (breakerOk ? 'bg-emerald-400' : 'bg-amber-400') : 'bg-slate-500'}`} />
        <span className="font-semibold text-[11px] text-slate-300 hidden sm:inline">Telemetry</span>

        {hasData && (
          <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${cacheHit ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-indigo-500/10 text-indigo-300 border border-indigo-500/20'}`}>
            {cacheHit ? 'Cache' : 'Direct'}
          </span>
        )}
        <span className="text-cyan-400 font-semibold text-[11px]">{hasData ? `${ttftMs}ms` : '—'}</span>
        {isExpanded ? <ChevronUp className="w-3.5 h-3.5 text-slate-400" /> : <ChevronDown className="w-3.5 h-3.5 text-slate-400" />}
      </button>

      {/* Collapsible Dropdown Metrics Popover */}
      {isExpanded && (
        <div
          id="telemetry-popover"
          className="absolute right-0 top-10 mt-1.5 w-[min(20rem,calc(100vw-1.5rem))] rounded-xl p-4 bg-slate-950/95 border border-slate-800 text-slate-200 shadow-2xl backdrop-blur-xl space-y-3 z-50"
        >
          <div className="flex items-center justify-between pb-2 border-b border-slate-800">
            <span className="font-semibold text-xs text-slate-300">Latest Response Metrics</span>
            {circuitBreakerState && (
              <span className={`text-[10px] font-mono font-medium px-2 py-0.5 rounded border ${breakerOk ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20' : 'text-amber-300 bg-amber-500/10 border-amber-500/20'}`}>
                {circuitBreakerState}
              </span>
            )}
          </div>

          {!hasData ? (
            <p className="text-[11px] text-slate-400">Send a message to see measured latency.</p>
          ) : (
            <div className="space-y-2 text-[11px]">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-slate-400">
                  <Zap className="w-3.5 h-3.5 text-indigo-400" aria-hidden="true" /> Time to First Token:
                </span>
                <span className="text-emerald-400 font-semibold">{fmt(ttftMs, 'ms')}</span>
              </div>

              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-slate-400">
                  <Cpu className="w-3.5 h-3.5 text-cyan-400" aria-hidden="true" /> Stream Throughput:
                </span>
                <span className="text-cyan-400 font-semibold">{fmt(tokensPerSec, 'words/s', 1)}</span>
              </div>

              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-slate-400">
                  <Activity className="w-3.5 h-3.5 text-slate-400" aria-hidden="true" /> End-to-End Latency:
                </span>
                <span className="text-slate-200 font-semibold">{fmt(totalLatencyMs, 'ms')}</span>
              </div>

              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 text-slate-400">
                  <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" aria-hidden="true" /> Circuit Breaker:
                </span>
                <span className={`font-semibold ${breakerOk ? 'text-emerald-300' : 'text-amber-300'}`}>{circuitBreakerState || '—'}</span>
              </div>

              <div className="pt-2 border-t border-slate-800 flex flex-col gap-1">
                <span className="text-[10px] text-slate-500 font-medium uppercase">Correlation Trace ID:</span>
                <span className="text-[10px] text-slate-400 truncate bg-slate-900/80 p-2 rounded-lg border border-slate-800 font-mono select-all">
                  {traceId || '—'}
                </span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
