import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"
import { resolve } from "path"
import AutoImport from "unplugin-auto-import/vite"
import Components from "unplugin-vue-components/vite"
import { ElementPlusResolver } from "unplugin-vue-components/resolvers"

export default defineConfig({
  base: "/",
  plugins: [
    vue(),
    AutoImport({
      resolvers: [ElementPlusResolver()],
    }),
    Components({
      resolvers: [ElementPlusResolver()],
    }),
  ],
  resolve: {
    alias: {
      "@": resolve(__dirname, "src"),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      // 顺序: 越具体的路径越靠前, 避免 /api/chat 前缀吞掉 /api/chat-svc
      "/api/assist/ws": {
        target: "http://localhost:8001",
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/api\/assist\/ws/, "/api/ws"),
      },
      "/api/bot/chat": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/bot\/chat/, "/api/chat"),
      },
      "/api/chat-svc": {
        target: "http://localhost:8092",
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/api\/chat-svc/, "/api"),
      },
      "/api/chat": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/api/kb": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/api/health": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/api/auth": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/api/notify": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      "/api/session": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      "/api/hold": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      "/api/resume": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      "/api/review": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      "/api/feedback": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      "/api/analyze": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
      "/api/ws": {
        target: "http://localhost:8001",
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
