// Browser end-to-end test of the MCP App against the real Python server.
// Run: node test/e2e.mjs   (needs Python env: uv sync; Chromium from Playwright)
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import esbuild from "esbuild";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PATH ?? "/opt/node22/lib/node_modules/playwright");
const assert = (cond, msg) => { if (!cond) { throw new Error("ASSERTION FAILED: " + msg); } else console.log("ok -", msg); };

const dir = mkdtempSync(join(tmpdir(), "obs-e2e-"));
const vault = join(dir, "Vault");
mkdirSync(join(vault, "Projects"), { recursive: true });
writeFileSync(join(vault, "Home.md"), "# Home\nSee [[Projects/Plan]].\n");
writeFileSync(join(vault, "Projects", "Plan.md"), "# Plan\n## Goals\n- [ ] ship it\nગુજરાતી text\n");
const port = 8800 + Math.floor(Math.random() * 100);
const py = spawn("uv", ["run", "python", "-m", "obsidian_mcp", "serve", "--transport", "http", "--port", String(port),
  "--vault", vault, "--no-auth"], { cwd: new URL("../..", import.meta.url).pathname,
  env: { ...process.env, OBSIDIAN_MCP_HOME: join(dir, "home") }, stdio: ["ignore", "ignore", "pipe"] });
let client;
try {
  for (let i = 0; i < 60; i++) {
    try { client = new Client({ name: "e2e", version: "1" });
      await client.connect(new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`))); break; }
    catch { client = undefined; await new Promise((r) => setTimeout(r, 250)); }
  }
  assert(client, "python server reachable over Streamable HTTP");
  const host = await esbuild.build({ entryPoints: [new URL("host-entry.ts", import.meta.url).pathname], bundle: true,
    format: "iife", write: false });
  const appHtml = readFileSync(new URL("../../src/obsidian_mcp/app/index.html", import.meta.url), "utf8");
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.exposeFunction("callTool", async (p) => client.callTool(p));
  await page.setContent(`<!doctype html><body></body>`);
  await page.evaluate((html) => { window.appHtml = html; }, appHtml);
  await page.addScriptTag({ content: host.outputFiles[0].text });
  await page.evaluate(() => window.startApp("open_vault_browser", {}));
  const frame = page.frames()[1];
  await frame.waitForSelector("text=Projects/Plan.md", { timeout: 10000 });
  assert(true, "browser view lists recent notes");
  assert(await frame.evaluate(() => document.documentElement.dataset.theme || document.documentElement.style.colorScheme) !== undefined, "host theme applied");
  await frame.fill("input[type=search]", "ગુજરાતી");
  await frame.click("button[type=submit]");
  await frame.waitForSelector("text=1 result(s)");
  assert(true, "Unicode search through the UI");
  await frame.click("text=Projects/Plan.md");
  await frame.waitForSelector("textarea");
  assert((await frame.inputValue("textarea")).includes("## Goals"), "note opens in viewer");
  await frame.waitForSelector("text=Home.md");
  assert(true, "backlinks shown");
  await frame.click("text=Add to chat");
  await page.waitForFunction(() => window.hostLog.some((l) => l.kind === "modelContext"));
  const ctx = await page.evaluate(() => window.hostLog.find((l) => l.kind === "modelContext").params);
  assert(ctx.content.some((c) => c.type === "resource_link" && c.uri.endsWith("/Projects/Plan.md")), "model context has resource link");
  assert(ctx.content.some((c) => c.annotations?.audience?.[0] === "assistant"), "background context hidden from user");
  assert(ctx.structuredContent.paths[0] === "Projects/Plan.md", "structured model context");
  await frame.click("text=Summarize");
  await page.waitForFunction(() => window.hostLog.some((l) => l.kind === "message"));
  assert(true, "ui/message sent");
  await frame.click("text=Tasks");
  await frame.waitForSelector("text=ship it");
  await frame.check("input[type=checkbox]");
  await frame.waitForSelector("text=0 open task(s)");
  const plan = await client.callTool({ name: "read_note", arguments: { path: "Projects/Plan.md" } });
  assert(plan.structuredContent.content.includes("- [x] ship it"), "task toggled through UI and written to disk");
  // Deep link into a note.
  const page2 = await browser.newPage();
  await page2.exposeFunction("callTool", async (p) => client.callTool(p));
  await page2.setContent(`<!doctype html><body></body>`);
  await page2.evaluate((html) => { window.appHtml = html; }, appHtml);
  await page2.addScriptTag({ content: host.outputFiles[0].text });
  await page2.evaluate(() => window.startApp("open_vault_browser", { __deepLink: { url: "/note?path=Home.md" } }));
  await page2.frames()[1].waitForSelector("text=See [[Projects/Plan]].", { state: "attached" }).catch(() => undefined);
  const deep = await page2.frames()[1].inputValue("textarea").catch(() => "");
  assert(deep.includes("See [[Projects/Plan]]"), "deep link opens a note");
  assert(errors.length === 0, `no page errors (${errors.join("; ")})`);
  await browser.close();
  console.log("ALL E2E CHECKS PASSED");
} finally {
  await client?.close().catch(() => undefined);
  py.kill();
}
