import './globals.css';
import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Voxa 2.0 — Distributed Real-Time Voice & AI Inference Gateway',
  description: 'Asynchronous, fault-tolerant voice-and-text AI platform with sub-200ms TTFT, dynamic micro-batching, and distributed circuit breakers.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[#090d16] text-slate-100 flex flex-col">
        {children}
      </body>
    </html>
  );
}
