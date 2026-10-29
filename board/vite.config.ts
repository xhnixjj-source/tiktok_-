import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    allowedHosts: true,
    port: 5174,
    strictPort: true,
    proxy: {
      "/scenes": "http://127.0.0.1:8790",
      "/products": "http://127.0.0.1:8790",
      "/boards": "http://127.0.0.1:8790",
      "/variants": "http://127.0.0.1:8790",
      "/scheduler": "http://127.0.0.1:8790",
      "/people": "http://127.0.0.1:8790",
      "/prompts": "http://127.0.0.1:8790",
      "/templates": "http://127.0.0.1:8790",
      "/jobs": "http://127.0.0.1:8790",
      "/hits": "http://127.0.0.1:8790",
      "/speeches": "http://127.0.0.1:8790",
      "/files": "http://127.0.0.1:8790",
    },
  },
});
