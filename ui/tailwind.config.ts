import type { Config } from 'tailwindcss'

/**
 * Visual tokens: City Pop / 80s retro-futurism, applied with the
 * landing-page-design skill's Part B discipline (one type scale, flat
 * surfaces, motion on the base curve). Night-city navy surfaces carry the
 * neon; colour only ever marks state or live traffic. Every colour is a CSS
 * variable so tuning the palette is one file (src/index.css).
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        night: {
          900: 'rgb(var(--night-900) / <alpha-value>)',
          800: 'rgb(var(--night-800) / <alpha-value>)',
          700: 'rgb(var(--night-700) / <alpha-value>)',
          600: 'rgb(var(--night-600) / <alpha-value>)',
          500: 'rgb(var(--night-500) / <alpha-value>)',
        },
        neon: {
          magenta: 'rgb(var(--neon-magenta) / <alpha-value>)',
          cyan: 'rgb(var(--neon-cyan) / <alpha-value>)',
          amber: 'rgb(var(--neon-amber) / <alpha-value>)',
          violet: 'rgb(var(--neon-violet) / <alpha-value>)',
        },
        led: {
          off: 'rgb(var(--led-off) / <alpha-value>)',
          flow: 'rgb(var(--led-flow) / <alpha-value>)',
          live: 'rgb(var(--led-live) / <alpha-value>)',
          ok: 'rgb(var(--led-ok) / <alpha-value>)',
          warn: 'rgb(var(--led-warn) / <alpha-value>)',
          bad: 'rgb(var(--led-bad) / <alpha-value>)',
          human: 'rgb(var(--led-human) / <alpha-value>)',
        },
        text: {
          primary: 'rgb(var(--text-primary) / <alpha-value>)',
          secondary: 'rgb(var(--text-secondary) / <alpha-value>)',
          tertiary: 'rgb(var(--text-tertiary) / <alpha-value>)',
        },
      },
      fontFamily: {
        display: ['Orbitron', 'Rajdhani', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
        sans: ['Rajdhani', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono Variable', 'JetBrains Mono', 'Cascadia Mono', 'Microsoft YaHei', 'ui-monospace', 'monospace'],
      },
      transitionTimingFunction: {
        base: 'cubic-bezier(0.22,1,0.36,1)',
      },
      keyframes: {
        'led-pulse': {
          '0%': { opacity: '1', transform: 'scale(1)' },
          '35%': { opacity: '0.28', transform: 'scale(0.72)' },
          '100%': { opacity: '1', transform: 'scale(1)' },
        },
        'led-breathe': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.45' },
        },
        'row-in': {
          '0%': { opacity: '0', transform: 'translateY(-4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'lane-flash': {
          '0%': { opacity: '0.35', 'box-shadow': '0 0 0 rgb(var(--neon-cyan) / 0)' },
          '30%': { opacity: '1', 'box-shadow': '0 0 18px 2px rgb(var(--neon-cyan) / 0.35)' },
          '100%': { opacity: '1', 'box-shadow': '0 0 0 rgb(var(--neon-cyan) / 0)' },
        },
        'neon-in': {
          '0%': { opacity: '0', transform: 'translateY(2px)' },
          '60%': { opacity: '0.7' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        caret: {
          '0%, 49%': { opacity: '1' },
          '50%, 100%': { opacity: '0' },
        },
        'chrome-shine': {
          '0%, 100%': { 'background-position': '0% 50%' },
          '50%': { 'background-position': '100% 50%' },
        },
      },
      animation: {
        'led-pulse': 'led-pulse 420ms cubic-bezier(0.22,1,0.36,1)',
        'led-breathe': 'led-breathe 1.6s cubic-bezier(0.22,1,0.36,1) infinite',
        'row-in': 'row-in 500ms cubic-bezier(0.22,1,0.36,1)',
        'lane-flash': 'lane-flash 700ms cubic-bezier(0.22,1,0.36,1)',
        'neon-in': 'neon-in 420ms cubic-bezier(0.22,1,0.36,1)',
        caret: 'caret 1.1s steps(1, end) infinite',
        'chrome-shine': 'chrome-shine 6s ease-in-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config
