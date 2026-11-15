import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const BACKEND = 'http://127.0.0.1:8021'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    strictPort: true,
    // The Python process serves the built panel too, so in development Vite
    // proxies every API path to keep one origin in both modes.
    proxy: {
      '/api': BACKEND,
      '/chat': BACKEND,
      '/runs': BACKEND,
      '/healthz': BACKEND,
    },
  },
  preview: { port: 5273, strictPort: true },
})
