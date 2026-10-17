import { defineConfig } from 'vite';

export default defineConfig({
  base: '/experiments/',
  build: { outDir: '../../experiments', emptyOutDir: true },
});
