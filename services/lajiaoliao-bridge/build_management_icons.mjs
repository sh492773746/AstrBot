// Generate static assets from the Dashboard's existing Lucide dependency.
import { createRequire } from "node:module";
import { copyFile, mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../../dashboard/package.json", import.meta.url));
const lucide = require("@lucide/vue");
const destination = fileURLToPath(new URL(
  "../../data/plugins/astrbot_plugin_lajiaoliao/pages/management/icons/", import.meta.url,
));
const escape = value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
await mkdir(destination, { recursive: true });
await copyFile(require.resolve("@lucide/vue/LICENSE"), `${destination}/LICENSE`);
for (const name of ["RefreshCw", "Save", "Plus", "ChevronLeft", "ChevronRight", "Eye", "Check"]) {
  const { iconNode, name: file } = lucide[name]({}, { slots: {} }).props;
  const children = iconNode.map(([tag, attributes]) => `<${tag} ${Object.entries(attributes)
    .filter(([key]) => key !== "key").map(([key, value]) => `${key}="${escape(value)}"`).join(" ")}/>`).join("");
  await writeFile(`${destination}/${file}.svg`,
    `<!-- Lucide, ISC license; @lucide/vue 1.23.0 -->\n<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#272a30" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">${children}</svg>\n`);
}
console.log("Generated seven local Lucide icons; no network or production dependencies added.");
