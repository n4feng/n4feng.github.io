import { defineConfig } from 'vite';

export default defineConfig({
  base: '/experiments/',
  // Other standalone experiments live beside this app. Never erase them when
  // rebuilding the tracker (only this app's own assets are overwritten).
  build: { outDir: '../../experiments', emptyOutDir: false },
});
