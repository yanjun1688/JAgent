import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'three-vendor': ['three', '@react-three/fiber', '@react-three/drei'],
          'ui-vendor': ['motion', 'lucide-react'],
          'particles-vendor': ['@tsparticles/react', '@tsparticles/slim'],
          'query-vendor': ['@tanstack/react-query', 'zustand'],
        },
      },
    },
  },
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
