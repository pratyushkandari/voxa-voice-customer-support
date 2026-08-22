/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ["class"],
  content: [
    './pages/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './app/**/*.{ts,tsx}',
    './src/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        background: "#090d16",
        foreground: "#f8fafc",
        card: "#0f172a",
        primary: {
          DEFAULT: "#6366f1",
          hover: "#4f46e5",
          glow: "rgba(99, 102, 241, 0.4)"
        },
        secondary: {
          DEFAULT: "#06b6d4",
          glow: "rgba(6, 182, 212, 0.4)"
        },
        accent: "#ec4899"
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'glow': 'glow 2s ease-in-out infinite alternate',
      },
      keyframes: {
        glow: {
          '0%': { boxShadow: '0 0 15px rgba(99, 102, 241, 0.4)' },
          '100%': { boxShadow: '0 0 35px rgba(6, 182, 212, 0.8)' },
        }
      }
    },
  },
  plugins: [],
}
