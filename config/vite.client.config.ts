import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  publicDir: 'app/views/public',
  build: {
    outDir: 'dist/client', emptyOutDir: true, minify: true,
    rollupOptions: {
      input: fileURLToPath(new URL('../app/views/storefront.tsx', import.meta.url)),
      output: { entryFileNames: 'browser.js', assetFileNames: (asset) => asset.names?.some(name => name.endsWith('.css')) ? 'browser.css' : 'assets/[name]-[hash][extname]', codeSplitting: false }
    }
  }
});
