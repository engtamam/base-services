import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@base-ui': fileURLToPath(new URL('../../packages/ui/src', import.meta.url)) } },
  test: { environment: 'jsdom' },
});
