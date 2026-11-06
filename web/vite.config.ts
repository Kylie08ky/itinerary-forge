import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Vite 7 默认只监听 localhost（可能解析成 IPv6），固定成 127.0.0.1，文档、测试、浏览器里的地址才一致
export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173 },
  preview: { host: "127.0.0.1", port: 4173 },
});
