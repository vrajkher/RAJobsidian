"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/main.ts
var main_exports = {};
__export(main_exports, {
  default: () => McpBridgePlugin
});
module.exports = __toCommonJS(main_exports);
var import_obsidian = require("obsidian");
var http = __toESM(require("http"));
var crypto = __toESM(require("crypto"));
var DEFAULT_SETTINGS = { port: 27125, token: "" };
var MAX_BODY = 5 * 1024 * 1024;
var MAX_EVENTS = 500;
var HttpError = class extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
};
var McpBridgePlugin = class extends import_obsidian.Plugin {
  settings = { ...DEFAULT_SETTINGS };
  server = null;
  events = [];
  seq = 0;
  waiters = [];
  lastEditorEvent = 0;
  async onload() {
    await this.loadSettings();
    if (!this.settings.token) {
      this.settings.token = crypto.randomBytes(32).toString("hex");
      await this.saveData(this.settings);
    }
    this.addSettingTab(new BridgeSettingTab(this.app, this));
    this.registerEvents();
    this.app.workspace.onLayoutReady(() => this.startServer());
  }
  onunload() {
    this.stopServer();
    for (const wake of this.waiters.splice(0)) wake();
  }
  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }
  // ---- Events -------------------------------------------------------------------------
  push(type, path, extra = {}) {
    this.seq += 1;
    this.events.push({ seq: this.seq, time: Date.now(), type, path, ...extra });
    if (this.events.length > MAX_EVENTS) this.events.splice(0, this.events.length - MAX_EVENTS);
    for (const wake of this.waiters.splice(0)) wake();
  }
  registerEvents() {
    const refs = [
      this.app.workspace.on("file-open", (file) => this.push("file-open", file?.path)),
      this.app.workspace.on("active-leaf-change", (leaf) => this.push("active-leaf-change", void 0, { detail: { viewType: leaf?.view.getViewType() } })),
      this.app.workspace.on("layout-change", () => this.push("layout-change")),
      this.app.workspace.on("editor-change", (_editor, info) => {
        const now = Date.now();
        if (now - this.lastEditorEvent < 1e3) return;
        this.lastEditorEvent = now;
        this.push("editor-change", info.file?.path);
      }),
      this.app.vault.on("create", (f) => this.push("create", f.path)),
      this.app.vault.on("modify", (f) => this.push("modify", f.path)),
      this.app.vault.on("delete", (f) => this.push("delete", f.path)),
      this.app.vault.on("rename", (f, oldPath) => this.push("rename", f.path, { oldPath })),
      this.app.metadataCache.on("resolved", () => this.push("metadata-resolved"))
    ];
    for (const ref of refs) this.registerEvent(ref);
  }
  // ---- HTTP server ----------------------------------------------------------------------
  startServer() {
    this.stopServer();
    const server = http.createServer((req, res) => {
      this.handle(req, res).catch((err) => {
        const status = err instanceof HttpError ? err.status : 500;
        const message = err instanceof Error ? err.message : String(err);
        this.send(res, status, { error: message });
      });
    });
    server.on("error", (err) => new import_obsidian.Notice(`MCP Bridge could not start: ${err.message}`));
    server.listen(this.settings.port, "127.0.0.1");
    this.server = server;
  }
  stopServer() {
    this.server?.close();
    this.server = null;
  }
  send(res, status, body) {
    if (res.headersSent) return;
    const data = JSON.stringify(body ?? null);
    res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    res.end(data);
  }
  authorized(req) {
    const host = (req.headers.host ?? "").replace(/:\d+$/, "");
    if (!["127.0.0.1", "localhost", "[::1]"].includes(host)) return false;
    if (req.headers.origin) return false;
    const header = req.headers.authorization ?? "";
    const expected = Buffer.from(`Bearer ${this.settings.token}`);
    const given = Buffer.from(header);
    return given.length === expected.length && crypto.timingSafeEqual(given, expected);
  }
  readBody(req) {
    return new Promise((resolve, reject) => {
      let size = 0;
      const chunks = [];
      req.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_BODY) {
          reject(new HttpError(413, "Body too large"));
          req.destroy();
        } else chunks.push(chunk);
      });
      req.on("end", () => {
        if (!chunks.length) return resolve({});
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
        } catch {
          reject(new HttpError(400, "Invalid JSON"));
        }
      });
      req.on("error", reject);
    });
  }
  async handle(req, res) {
    if (!this.authorized(req)) throw new HttpError(401, "Unauthorized");
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const route = `${req.method} ${url.pathname}`;
    const body = req.method === "POST" ? await this.readBody(req) : {};
    switch (route) {
      case "GET /status":
        return this.send(res, 200, this.status());
      case "GET /active":
        return this.send(res, 200, this.active());
      case "POST /editor":
        return this.send(res, 200, this.editor(body));
      case "GET /workspace":
        return this.send(res, 200, this.workspace());
      case "POST /workspace/open":
        return this.send(res, 200, await this.open(body));
      case "POST /file/rename":
        return this.send(res, 200, await this.rename(body));
      case "POST /file/trash":
        return this.send(res, 200, await this.trash(body));
      case "POST /frontmatter":
        return this.send(res, 200, await this.frontmatter(body));
      case "GET /metadata":
        return this.send(res, 200, this.metadata(url.searchParams.get("path") ?? ""));
      case "POST /link":
        return this.send(res, 200, this.link(body));
      case "GET /events":
        return this.send(res, 200, await this.poll(url));
      default:
        throw new HttpError(404, `Unknown route ${route}`);
    }
  }
  // ---- Handlers -------------------------------------------------------------------------
  file(path) {
    if (typeof path !== "string" || !path) throw new HttpError(400, "path is required");
    const f = this.app.vault.getAbstractFileByPath((0, import_obsidian.normalizePath)(path));
    if (!(f instanceof import_obsidian.TFile)) throw new HttpError(404, `No file at ${path}`);
    return f;
  }
  status() {
    return {
      plugin: this.manifest.version,
      vault: this.app.vault.getName(),
      activeFile: this.app.workspace.getActiveFile()?.path ?? null,
      eventSeq: this.seq,
      features: [
        "active",
        "editor",
        "workspace",
        "open",
        "rename",
        "trash",
        "frontmatter",
        "metadata",
        "link",
        "events"
      ]
    };
  }
  markdownView() {
    const view = this.app.workspace.getActiveViewOfType(import_obsidian.MarkdownView);
    if (!view) throw new HttpError(409, "No Markdown editor is active");
    return view;
  }
  active() {
    const file = this.app.workspace.getActiveFile();
    const view = this.app.workspace.getActiveViewOfType(import_obsidian.MarkdownView);
    const result = {
      file: file?.path ?? null,
      viewType: this.app.workspace.getMostRecentLeaf()?.view.getViewType() ?? null
    };
    if (view) {
      const editor = view.editor;
      result.mode = view.getMode();
      result.selection = editor.getSelection();
      result.selections = editor.listSelections();
      result.cursor = { from: editor.getCursor("from"), to: editor.getCursor("to"), head: editor.getCursor("head") };
      result.lineCount = editor.lineCount();
    }
    return result;
  }
  editor(body) {
    const editor = this.markdownView().editor;
    const action = body.action;
    const text = typeof body.text === "string" ? body.text : "";
    const from = body.from;
    const to = body.to ?? from;
    const expected = body.expected;
    if (action === "replace_selection") {
      if (expected != null && editor.getSelection() !== expected) throw new HttpError(409, "Selection changed");
      editor.replaceSelection(text, "mcp-bridge");
    } else if (action === "replace_range") {
      if (!from) throw new HttpError(400, "from is required");
      if (expected != null && editor.getRange(from, to) !== expected) throw new HttpError(409, "Range changed");
      editor.replaceRange(text, from, to, "mcp-bridge");
    } else if (action === "insert_at_cursor") {
      const cursor = editor.getCursor("head");
      editor.replaceRange(text, cursor, cursor, "mcp-bridge");
    } else if (action === "set_selection") {
      if (!from) throw new HttpError(400, "from is required");
      editor.setSelection(from, to);
    } else {
      throw new HttpError(400, `Unknown editor action ${action}`);
    }
    return this.active();
  }
  workspace() {
    const leaves = [];
    const active = this.app.workspace.getMostRecentLeaf();
    this.app.workspace.iterateAllLeaves((leaf) => {
      const state = leaf.getViewState();
      leaves.push({
        index: leaves.length,
        type: state.type,
        file: state.state?.file ?? null,
        pinned: state.pinned ?? false,
        active: leaf === active,
        title: leaf.getDisplayText()
      });
    });
    return { leaves, layout: this.app.workspace.getLayout() };
  }
  async open(body) {
    const file = this.file(body.path);
    const newLeaf = body.newLeaf === true ? "tab" : body.newLeaf || false;
    const leaf = this.app.workspace.getLeaf(newLeaf);
    const line = typeof body.line === "number" ? body.line : void 0;
    await leaf.openFile(file, line !== void 0 ? { eState: { line: Math.max(0, line - 1) } } : {});
    return { opened: file.path };
  }
  async rename(body) {
    const src = this.app.vault.getAbstractFileByPath((0, import_obsidian.normalizePath)(String(body.path ?? "")));
    if (!src) throw new HttpError(404, `No file at ${body.path}`);
    const dest = (0, import_obsidian.normalizePath)(String(body.newPath ?? ""));
    if (!dest) throw new HttpError(400, "newPath is required");
    if (this.app.vault.getAbstractFileByPath(dest)) throw new HttpError(409, `${dest} already exists`);
    const parent = dest.includes("/") ? dest.slice(0, dest.lastIndexOf("/")) : "";
    if (parent && !(this.app.vault.getAbstractFileByPath(parent) instanceof import_obsidian.TFolder)) {
      await this.app.vault.createFolder(parent);
    }
    await this.app.fileManager.renameFile(src, dest);
    return { from: body.path, to: dest };
  }
  async trash(body) {
    const target = this.app.vault.getAbstractFileByPath((0, import_obsidian.normalizePath)(String(body.path ?? "")));
    if (!target) throw new HttpError(404, `No file at ${body.path}`);
    await this.app.fileManager.trashFile(target);
    return { trashed: body.path };
  }
  async frontmatter(body) {
    const file = this.file(body.path);
    const set = body.set ?? {};
    const remove = body.remove ?? [];
    await this.app.fileManager.processFrontMatter(file, (fm) => {
      for (const [k, v] of Object.entries(set)) fm[k] = v;
      for (const k of remove) delete fm[k];
    });
    return { path: file.path, frontmatter: this.app.metadataCache.getFileCache(file)?.frontmatter ?? null };
  }
  metadata(path) {
    const file = this.file(path);
    const cache = this.app.metadataCache.getFileCache(file);
    const resolved = this.app.metadataCache.resolvedLinks[file.path] ?? {};
    const unresolved = this.app.metadataCache.unresolvedLinks[file.path] ?? {};
    return { path: file.path, cache, resolvedLinks: resolved, unresolvedLinks: unresolved };
  }
  link(body) {
    const file = this.file(body.path);
    const source = String(body.source ?? "");
    const markdown = this.app.fileManager.generateMarkdownLink(
      file,
      source,
      body.subpath,
      body.alias
    );
    return { link: markdown };
  }
  async poll(url) {
    const since = Number(url.searchParams.get("since") ?? 0);
    const timeout = Math.min(30, Math.max(0, Number(url.searchParams.get("timeout") ?? 0))) * 1e3;
    if (timeout > 0 && this.seq <= since) {
      await new Promise((resolve) => {
        const timer = window.setTimeout(resolve, timeout);
        this.waiters.push(() => {
          window.clearTimeout(timer);
          resolve();
        });
      });
    }
    const events = this.events.filter((e) => e.seq > since);
    const dropped = this.events.length > 0 && this.events[0].seq > since + 1;
    return { events, latest: this.seq, missedEvents: dropped };
  }
};
var BridgeSettingTab = class extends import_obsidian.PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }
  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("p", {
      text: "Lets the obsidian-mcp server read the active editor and workspace. Listens on 127.0.0.1 only."
    });
    new import_obsidian.Setting(containerEl).setName("Port").setDesc("Local port (restart the plugin after changing).").addText((t) => t.setValue(String(this.plugin.settings.port)).onChange(async (v) => {
      const port = Number(v);
      if (Number.isInteger(port) && port > 1023 && port < 65536) {
        this.plugin.settings.port = port;
        await this.plugin.saveData(this.plugin.settings);
      }
    }));
    new import_obsidian.Setting(containerEl).setName("Access token").setDesc("Copy into: obsidian-mcp config set bridge_token <token>. Keep it private.").addButton((b) => b.setButtonText("Copy").onClick(async () => {
      await navigator.clipboard.writeText(this.plugin.settings.token);
      new import_obsidian.Notice("Token copied");
    })).addButton((b) => b.setButtonText("Regenerate").setWarning().onClick(async () => {
      this.plugin.settings.token = crypto.randomBytes(32).toString("hex");
      await this.plugin.saveData(this.plugin.settings);
      new import_obsidian.Notice("New token generated; update obsidian-mcp config.");
    }));
  }
};
