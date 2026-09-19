import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Local development only (`npm run dev` + `uvicorn app.main:app`):
    // in the container stack Caddy does this routing.
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
