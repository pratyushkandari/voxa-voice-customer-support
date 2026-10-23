/** @type {import('next').NextConfig} */
// NEXT_PUBLIC_API_URL / NEXT_PUBLIC_WS_URL are inlined by Next.js at build time; the code
// falls back to http://localhost:8088 and derives the WebSocket URL from the API URL.
const nextConfig = {
  reactStrictMode: true
};

module.exports = nextConfig;
