import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        ws: true,
        configure: (proxy) => {
          proxy.on('error', (err) => {
            const code = (err as NodeJS.ErrnoException).code
            if (code === 'ECONNRESET' || code === 'ECONNREFUSED' || code === 'ECONNABORTED') return
            // AggregateError from Node 16+ connection failures
            if ((err as Error).name === 'AggregateError') return
            console.error('Vite proxy error:', err)
          })
        },
      },
    },
  },
})
