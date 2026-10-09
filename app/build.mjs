// Bundles the app into one self-contained HTML file served by the Python server
// (MCP App iframes block external scripts/styles by default CSP).
import esbuild from "esbuild";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

const result = await esbuild.build({
  entryPoints: ["src/main.ts"], bundle: true, format: "esm", target: "es2022", minify: true,
  write: false, outdir: "out", loader: { ".css": "css" }, logLevel: "info",
});
const js = result.outputFiles.find((f) => f.path.endsWith(".js")).text;
const css = result.outputFiles.find((f) => f.path.endsWith(".css"))?.text ?? "";
const html = readFileSync("src/index.html", "utf8")
  .replace("/*CSS*/", () => css)
  .replace("/*JS*/", () => js.replaceAll("</script", "<\\/script"));
mkdirSync("../src/obsidian_mcp/app", { recursive: true });
writeFileSync("../src/obsidian_mcp/app/index.html", html);
console.log(`wrote ../src/obsidian_mcp/app/index.html (${(html.length / 1024).toFixed(0)} KiB)`);
