import esbuild from "esbuild";

await esbuild.build({
  entryPoints: ["src/main.ts"],
  bundle: true,
  external: ["obsidian", "electron", "@codemirror/*", "@lezer/*", "http", "crypto"],
  format: "cjs",
  platform: "node",
  target: "es2022",
  outfile: "main.js",
  logLevel: "info",
});
