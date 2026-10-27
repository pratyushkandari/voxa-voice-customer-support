/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './components/**/*.{ts,tsx}',
    './app/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        background: "#080c14",
        foreground: "#f8fafc",
        primary: "#6366f1"
      }
    },
  },
  plugins: [],
}
