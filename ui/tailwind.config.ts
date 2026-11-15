import type { Config } from 'tailwindcss'

/**
 * Visual tokens come from the landing-page-design skill (Part B), pushed toward
 * a monochrome terminal look: flat surfaces, one typeface, motion on the base
 * curve. Every colour is a CSS variable so the black/white swap is one attribute
 * on <html>, not a second stylesheet.
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          900: 'rgb(var(--ink-900) / <alpha-value>)',
          800: 'rgb(var(--ink-800) / <alpha-value>)',
          700: 'rgb(var(--ink-700) / <alpha-value>)',
          600: 'rgb(var(--ink-600) / <alpha-value>)',
          500: 'rgb(var(--ink-500) / <alpha-value>)',
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
        sans: ['Geist Variable', 'Geist', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
        mono: ['Geist Mono', 'Cascadia Mono', 'Microsoft YaHei', 'ui-monospace', 'monospace'],
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
        'flow-arrive': {
          '0%': { opacity: '0.25', transform: 'scaleY(0.6)' },
          '100%': { opacity: '1', transform: 'scaleY(1)' },
        },
        'row-in': {
          '0%': { opacity: '0', transform: 'translateY(-4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        caret: {
          '0%, 49%': { opacity: '1' },
          '50%, 100%': { opacity: '0' },
        },
        clack: {
          '0%': { opacity: '0.9', transform: 'scale(1)' },
          '100%': { opacity: '0', transform: 'scale(2.1)' },
        },
        'counter-tick': {
          '0%': { transform: 'scale(1.09)' },
          '100%': { transform: 'scale(1)' },
        },
        'led-clack': {
          '0%, 100%': { opacity: '1' },
          '12%': { opacity: '0.15' },
          '24%': { opacity: '1' },
          '36%': { opacity: '0.15' },
          '48%': { opacity: '1' },
        },
      },
      animation: {
        'led-pulse': 'led-pulse 420ms cubic-bezier(0.22,1,0.36,1)',
        'led-breathe': 'led-breathe 1.6s cubic-bezier(0.22,1,0.36,1) infinite',
        'flow-arrive': 'flow-arrive 500ms cubic-bezier(0.22,1,0.36,1)',
        'row-in': 'row-in 500ms cubic-bezier(0.22,1,0.36,1)',
        caret: 'caret 1.1s steps(1, end) infinite',
        clack: 'clack 520ms cubic-bezier(0.22,1,0.36,1) forwards',
        'led-clack': 'led-clack 500ms steps(1, end)',
        'counter-tick': 'counter-tick 220ms cubic-bezier(0.22,1,0.36,1)',
      },
    },
  },
  plugins: [],
} satisfies Config
