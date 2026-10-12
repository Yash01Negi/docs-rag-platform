import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development the frontend calls relative `/api/...` URLs, and Vite forwards them to the
// backend. This avoids CORS in local development. Inside Docker Compose the target is the
// `backend` service; when running on the host it defaults to localhost.
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: apiProxyTarget, changeOrigin: true },
    },
  },
})
