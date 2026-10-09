// Loads the built plugin with a mocked "obsidian" module and serves it on a port.
// Used by tests/test_bridge_contract.py to verify the HTTP contract with the Python client.
const Module = require("module");
const path = require("path");

class TAbstractFile { constructor(p) { this.path = p; } }
class TFile extends TAbstractFile {}
class TFolder extends TAbstractFile {}
class Events { constructor() { this.h = {}; } on(n, cb) { (this.h[n] ||= []).push(cb); return { n, cb }; }
  trigger(n, ...a) { (this.h[n] || []).forEach((cb) => cb(...a)); } }

const files = new Map([["Note.md", new TFile("Note.md")], ["Folder", new TFolder("Folder")]]);
let text = "line one\nline two";
let selection = { anchor: { line: 0, ch: 0 }, head: { line: 0, ch: 4 } };
const pos = (p) => p.split("\n");
const offset = (p) => { const lines = pos(text); let o = 0; for (let i = 0; i < p.line; i++) o += lines[i].length + 1; return o + p.ch; };
const editor = {
  getSelection: () => text.slice(offset(selection.anchor), offset(selection.head)),
  listSelections: () => [selection],
  getCursor: (s) => (s === "from" ? selection.anchor : selection.head),
  lineCount: () => pos(text).length,
  getRange: (a, b) => text.slice(offset(a), offset(b)),
  replaceRange: (t, a, b) => { text = text.slice(0, offset(a)) + t + text.slice(offset(b || a)); },
  replaceSelection: (t) => { text = text.slice(0, offset(selection.anchor)) + t + text.slice(offset(selection.head)); },
  setSelection: (a, h) => { selection = { anchor: a, head: h || a }; },
};
class MarkdownView { constructor() { this.editor = editor; this.file = files.get("Note.md"); }
  getMode() { return "source"; } getViewType() { return "markdown"; } }
const view = new MarkdownView();
const leaf = { view, getViewState: () => ({ type: "markdown", state: { file: "Note.md" } }), getDisplayText: () => "Note",
  openFile: async (f, s) => { leaf.opened = [f.path, s]; } };
const workspace = Object.assign(new Events(), {
  onLayoutReady: (cb) => cb(), getActiveFile: () => files.get("Note.md"), getMostRecentLeaf: () => leaf,
  getActiveViewOfType: (cls) => (cls === MarkdownView ? view : null), iterateAllLeaves: (cb) => cb(leaf),
  getLayout: () => ({ main: { type: "split" } }), getLeaf: () => leaf,
});
const vault = Object.assign(new Events(), {
  getName: () => "MockVault", getAbstractFileByPath: (p) => files.get(p) || null,
  createFolder: async (p) => files.set(p, new TFolder(p)),
});
const fileManager = {
  renameFile: async (f, dest) => { const old = f.path; files.delete(old); f.path = dest; files.set(dest, f); vault.trigger("rename", f, old); },
  trashFile: async (f) => { files.delete(f.path); vault.trigger("delete", f); },
  processFrontMatter: async (f, fn) => { const fm = {}; fn(fm); fileManager.lastFm = fm; },
  generateMarkdownLink: (f, src, sub, alias) => `[[${f.path.replace(/\.md$/, "")}${sub || ""}${alias ? "|" + alias : ""}]]`,
};
const metadataCache = Object.assign(new Events(), { getFileCache: () => ({ frontmatter: fileManager.lastFm || null }),
  resolvedLinks: {}, unresolvedLinks: {} });
const app = { workspace, vault, fileManager, metadataCache };

class Plugin { constructor(a, m) { this.app = a; this.manifest = m; this._data = null; }
  async loadData() { return { port: Number(process.env.PORT), token: process.env.TOKEN }; }
  async saveData(d) { this._data = d; } addSettingTab() {} registerEvent() {} }
const obsidian = { App: class {}, Editor: class {}, MarkdownView, Notice: class { constructor(m) { console.error(m); } },
  Plugin, PluginSettingTab: class {}, Setting: class {}, TAbstractFile, TFile, TFolder, normalizePath: (p) => p };

const origLoad = Module._load;
Module._load = function (request, ...rest) { return request === "obsidian" ? obsidian : origLoad.call(this, request, ...rest); };
global.window = { setTimeout, clearTimeout };
const PluginClass = require(path.join(__dirname, "..", "main.js")).default;
const plugin = new PluginClass(app, { version: "test" });
plugin.onload().then(() => { console.log("READY"); });
setTimeout(() => vault.trigger("modify", files.get("Note.md")), 300);
