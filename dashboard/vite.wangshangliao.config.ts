import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const bridgeSource = readFileSync(
  new URL("../astrbot/dashboard/plugin_page_bridge.js", import.meta.url),
  "utf8",
);

export default defineConfig({
  root: fileURLToPath(new URL("./plugin-pages/wangshangliao", import.meta.url)),
  base: "./",
  publicDir: false,
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  plugins: [
    vue(),
    {
      name: "wangshangliao-self-contained-page",
      enforce: "post",
      generateBundle(_options, bundle) {
        const entry = Object.values(bundle).find(
          (item) => item.type === "chunk" && item.isEntry,
        );
        if (!entry || entry.type !== "chunk") {
          throw new Error("Missing Wangshangliao page bundle");
        }
        const css = Object.values(bundle)
          .filter(
            (item) => item.type === "asset" && item.fileName.endsWith(".css"),
          )
          .map((item) => (item.type === "asset" ? String(item.source) : ""))
          .join("\n");
        // Opaque sandbox origins cannot authenticate module or stylesheet fetches.
        const scripts = [bridgeSource, entry.code].map((source) =>
          source.replace(/<\/script/gi, "<\\/script"),
        );
        this.emitFile({
          type: "asset",
          fileName: "index.html",
          source: `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>旺商聊管理</title>
<style>${css.replace(/<\/style/gi, "<\\/style")}</style>
</head>
<body>
<div id="app"><p role="status">加载中...</p></div>
<script data-astrbot-bridge="/api/plugin/page/bridge-sdk.js" data-cfasync="false">${
            scripts[0]
          }</script>
<script data-cfasync="false">${scripts[1]}</script>
</body>
</html>`,
        });
        // The page embeds these assets; do not ship duplicate, unused files.
        delete bundle[entry.fileName];
        for (const [fileName, item] of Object.entries(bundle)) {
          if (item.type === "asset" && fileName.endsWith(".css")) {
            delete bundle[fileName];
          }
        }
      },
    },
  ],
  build: {
    outDir: fileURLToPath(
      new URL(
        "../astrbot/builtin_stars/wangshangliao_moderation/pages/management",
        import.meta.url,
      ),
    ),
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false,
    lib: {
      entry: fileURLToPath(
        new URL("./plugin-pages/wangshangliao/main.ts", import.meta.url),
      ),
      name: "WangshangliaoManagementPage",
      formats: ["iife"],
      fileName: () => "management.js",
      cssFileName: "management",
    },
  },
});
