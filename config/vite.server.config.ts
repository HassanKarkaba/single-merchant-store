import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
export default defineConfig({
  publicDir: false,
  build: {
    ssr: fileURLToPath(new URL('../app/views/storefront.tsx', import.meta.url)),
    outDir: 'dist/server', emptyOutDir: true,
    rollupOptions: {
      external: (id) => id === 'react' || id.startsWith('react/') || id === 'react-dom' || id.startsWith('react-dom/'),
      output: { entryFileNames: 'browser.mjs' }
    }
  }
});
