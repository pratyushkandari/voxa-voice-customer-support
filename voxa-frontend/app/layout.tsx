import './globals.css';
import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'Voxa — Real-Time Voice & Text Customer Support',
  description: 'Streaming customer-support console with order tracking, refund checks, escalation tickets and pgvector knowledge-base answers, backed by Redis Streams workers and a distributed circuit breaker.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-background text-slate-100 flex flex-col">
        {children}
      </body>
    </html>
  );
}
