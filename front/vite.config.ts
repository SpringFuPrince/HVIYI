import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api/import': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/api/query': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/api/local': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
