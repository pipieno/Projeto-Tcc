import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/sessions': 'http://agente:8080',
      '/chat':   'http://agente:8080',
      '/export': 'http://agente:8080',
    },
  },
})
