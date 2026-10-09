/*
 * MCP Bridge: a minimal, desktop-only Obsidian plugin that exposes a few public Plugin API
 * features over HTTP on 127.0.0.1 for the obsidian-mcp server.
 *
 * Security:
 *  - Binds to 127.0.0.1 only; rejects requests whose Host header is not localhost
 *    (DNS-rebinding protection) and requests carrying a browser Origin.
 *  - Every request needs "Authorization: Bearer <token>" (random 256-bit, shown in settings).
 *  - Bodies are limited to 5 MB. No arbitrary code execution, no command execution
 *    (app.commands is not public API; the server uses the official CLI for commands).
 */
import {
	App,
	Editor,
	EventRef,
	MarkdownView,
	Notice,
	Plugin,
	PluginSettingTab,
	Setting,
	TAbstractFile,
	TFile,
	TFolder,
	normalizePath,
} from "obsidian";
import * as http from "http";
import * as crypto from "crypto";

interface BridgeSettings {
	port: number;
	token: string;
}

interface BridgeEvent {
	seq: number;
	time: number;
	type: string;
	path?: string;
	oldPath?: string;
	detail?: Record<string, unknown>;
}

const DEFAULT_SETTINGS: BridgeSettings = { port: 27125, token: "" };
const MAX_BODY = 5 * 1024 * 1024;
const MAX_EVENTS = 500;

class HttpError extends Error {
	constructor(public status: number, message: string) {
		super(message);
	}
}

export default class McpBridgePlugin extends Plugin {
	settings: BridgeSettings = { ...DEFAULT_SETTINGS };
	private server: http.Server | null = null;
	private events: BridgeEvent[] = [];
	private seq = 0;
	private waiters: Array<() => void> = [];
	private lastEditorEvent = 0;

	async onload(): Promise<void> {
		await this.loadSettings();
		if (!this.settings.token) {
			this.settings.token = crypto.randomBytes(32).toString("hex");
			await this.saveData(this.settings);
		}
		this.addSettingTab(new BridgeSettingTab(this.app, this));
		this.registerEvents();
		this.app.workspace.onLayoutReady(() => this.startServer());
	}

	onunload(): void {
		this.stopServer();
		for (const wake of this.waiters.splice(0)) wake();
	}

	async loadSettings(): Promise<void> {
		this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
	}

	// ---- Events -------------------------------------------------------------------------
	private push(type: string, path?: string, extra: Partial<BridgeEvent> = {}): void {
		this.seq += 1;
		this.events.push({ seq: this.seq, time: Date.now(), type, path, ...extra });
		if (this.events.length > MAX_EVENTS) this.events.splice(0, this.events.length - MAX_EVENTS);
		for (const wake of this.waiters.splice(0)) wake();
	}

	private registerEvents(): void {
		const refs: EventRef[] = [
			this.app.workspace.on("file-open", (file: TFile | null) => this.push("file-open", file?.path)),
			this.app.workspace.on("active-leaf-change", (leaf) =>
				this.push("active-leaf-change", undefined, { detail: { viewType: leaf?.view.getViewType() } })),
			this.app.workspace.on("layout-change", () => this.push("layout-change")),
			this.app.workspace.on("editor-change", (_editor: Editor, info) => {
				const now = Date.now();
				if (now - this.lastEditorEvent < 1000) return; // debounce typing
				this.lastEditorEvent = now;
				this.push("editor-change", info.file?.path);
			}),
			this.app.vault.on("create", (f: TAbstractFile) => this.push("create", f.path)),
			this.app.vault.on("modify", (f: TAbstractFile) => this.push("modify", f.path)),
			this.app.vault.on("delete", (f: TAbstractFile) => this.push("delete", f.path)),
			this.app.vault.on("rename", (f: TAbstractFile, oldPath: string) =>
				this.push("rename", f.path, { oldPath })),
			this.app.metadataCache.on("resolved", () => this.push("metadata-resolved")),
		];
		for (const ref of refs) this.registerEvent(ref);
	}

	// ---- HTTP server ----------------------------------------------------------------------
	startServer(): void {
		this.stopServer();
		const server = http.createServer((req, res) => {
			this.handle(req, res).catch((err: unknown) => {
				const status = err instanceof HttpError ? err.status : 500;
				const message = err instanceof Error ? err.message : String(err);
				this.send(res, status, { error: message });
			});
		});
		server.on("error", (err) => new Notice(`MCP Bridge could not start: ${err.message}`));
		server.listen(this.settings.port, "127.0.0.1");
		this.server = server;
	}

	stopServer(): void {
		this.server?.close();
		this.server = null;
	}

	private send(res: http.ServerResponse, status: number, body: unknown): void {
		if (res.headersSent) return;
		const data = JSON.stringify(body ?? null);
		res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
		res.end(data);
	}

	private authorized(req: http.IncomingMessage): boolean {
		const host = (req.headers.host ?? "").replace(/:\d+$/, "");
		if (!["127.0.0.1", "localhost", "[::1]"].includes(host)) return false;
		if (req.headers.origin) return false; // never serve browsers
		const header = req.headers.authorization ?? "";
		const expected = Buffer.from(`Bearer ${this.settings.token}`);
		const given = Buffer.from(header);
		return given.length === expected.length && crypto.timingSafeEqual(given, expected);
	}

	private readBody(req: http.IncomingMessage): Promise<Record<string, unknown>> {
		return new Promise((resolve, reject) => {
			let size = 0;
			const chunks: Buffer[] = [];
			req.on("data", (chunk: Buffer) => {
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

	private async handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
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
	private file(path: unknown): TFile {
		if (typeof path !== "string" || !path) throw new HttpError(400, "path is required");
		const f = this.app.vault.getAbstractFileByPath(normalizePath(path));
		if (!(f instanceof TFile)) throw new HttpError(404, `No file at ${path}`);
		return f;
	}

	private status(): Record<string, unknown> {
		return {
			plugin: this.manifest.version,
			vault: this.app.vault.getName(),
			activeFile: this.app.workspace.getActiveFile()?.path ?? null,
			eventSeq: this.seq,
			features: ["active", "editor", "workspace", "open", "rename", "trash", "frontmatter", "metadata",
				"link", "events"],
		};
	}

	private markdownView(): MarkdownView {
		const view = this.app.workspace.getActiveViewOfType(MarkdownView);
		if (!view) throw new HttpError(409, "No Markdown editor is active");
		return view;
	}

	private active(): Record<string, unknown> {
		const file = this.app.workspace.getActiveFile();
		const view = this.app.workspace.getActiveViewOfType(MarkdownView);
		const result: Record<string, unknown> = {
			file: file?.path ?? null,
			viewType: this.app.workspace.getMostRecentLeaf()?.view.getViewType() ?? null,
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

	private editor(body: Record<string, unknown>): Record<string, unknown> {
		const editor = this.markdownView().editor;
		const action = body.action as string;
		const text = typeof body.text === "string" ? body.text : "";
		const from = body.from as { line: number; ch: number } | undefined;
		const to = (body.to as { line: number; ch: number } | undefined) ?? from;
		const expected = body.expected as string | undefined | null;
		if (action === "replace_selection") {
			if (expected != null && editor.getSelection() !== expected) throw new HttpError(409, "Selection changed");
			editor.replaceSelection(text, "mcp-bridge");
		} else if (action === "replace_range") {
			if (!from) throw new HttpError(400, "from is required");
			if (expected != null && editor.getRange(from, to!) !== expected) throw new HttpError(409, "Range changed");
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

	private workspace(): Record<string, unknown> {
		const leaves: Array<Record<string, unknown>> = [];
		const active = this.app.workspace.getMostRecentLeaf();
		this.app.workspace.iterateAllLeaves((leaf) => {
			const state = leaf.getViewState();
			leaves.push({
				index: leaves.length,
				type: state.type,
				file: (state.state as { file?: string } | undefined)?.file ?? null,
				pinned: state.pinned ?? false,
				active: leaf === active,
				title: leaf.getDisplayText(),
			});
		});
		return { leaves, layout: this.app.workspace.getLayout() };
	}

	private async open(body: Record<string, unknown>): Promise<Record<string, unknown>> {
		const file = this.file(body.path);
		const newLeaf = body.newLeaf === true ? "tab" : (body.newLeaf as "tab" | "split" | "window" | false) || false;
		const leaf = this.app.workspace.getLeaf(newLeaf);
		const line = typeof body.line === "number" ? body.line : undefined;
		await leaf.openFile(file, line !== undefined ? { eState: { line: Math.max(0, line - 1) } } : {});
		return { opened: file.path };
	}

	private async rename(body: Record<string, unknown>): Promise<Record<string, unknown>> {
		const src = this.app.vault.getAbstractFileByPath(normalizePath(String(body.path ?? "")));
		if (!src) throw new HttpError(404, `No file at ${body.path}`);
		const dest = normalizePath(String(body.newPath ?? ""));
		if (!dest) throw new HttpError(400, "newPath is required");
		if (this.app.vault.getAbstractFileByPath(dest)) throw new HttpError(409, `${dest} already exists`);
		const parent = dest.includes("/") ? dest.slice(0, dest.lastIndexOf("/")) : "";
		if (parent && !(this.app.vault.getAbstractFileByPath(parent) instanceof TFolder)) {
			await this.app.vault.createFolder(parent);
		}
		await this.app.fileManager.renameFile(src, dest); // updates links per user settings
		return { from: body.path, to: dest };
	}

	private async trash(body: Record<string, unknown>): Promise<Record<string, unknown>> {
		const target = this.app.vault.getAbstractFileByPath(normalizePath(String(body.path ?? "")));
		if (!target) throw new HttpError(404, `No file at ${body.path}`);
		await this.app.fileManager.trashFile(target); // respects the user's "Deleted files" setting
		return { trashed: body.path };
	}

	private async frontmatter(body: Record<string, unknown>): Promise<Record<string, unknown>> {
		const file = this.file(body.path);
		const set = (body.set ?? {}) as Record<string, unknown>;
		const remove = (body.remove ?? []) as string[];
		await this.app.fileManager.processFrontMatter(file, (fm: Record<string, unknown>) => {
			for (const [k, v] of Object.entries(set)) fm[k] = v;
			for (const k of remove) delete fm[k];
		});
		return { path: file.path, frontmatter: this.app.metadataCache.getFileCache(file)?.frontmatter ?? null };
	}

	private metadata(path: string): Record<string, unknown> {
		const file = this.file(path);
		const cache = this.app.metadataCache.getFileCache(file);
		const resolved = this.app.metadataCache.resolvedLinks[file.path] ?? {};
		const unresolved = this.app.metadataCache.unresolvedLinks[file.path] ?? {};
		return { path: file.path, cache, resolvedLinks: resolved, unresolvedLinks: unresolved };
	}

	private link(body: Record<string, unknown>): Record<string, unknown> {
		const file = this.file(body.path);
		const source = String(body.source ?? "");
		const markdown = this.app.fileManager.generateMarkdownLink(
			file, source, body.subpath as string | undefined, body.alias as string | undefined);
		return { link: markdown };
	}

	private async poll(url: URL): Promise<Record<string, unknown>> {
		const since = Number(url.searchParams.get("since") ?? 0);
		const timeout = Math.min(30, Math.max(0, Number(url.searchParams.get("timeout") ?? 0))) * 1000;
		if (timeout > 0 && this.seq <= since) {
			await new Promise<void>((resolve) => {
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
}

class BridgeSettingTab extends PluginSettingTab {
	constructor(app: App, private plugin: McpBridgePlugin) {
		super(app, plugin);
	}

	display(): void {
		const { containerEl } = this;
		containerEl.empty();
		containerEl.createEl("p", {
			text: "Lets the obsidian-mcp server read the active editor and workspace. Listens on 127.0.0.1 only.",
		});
		new Setting(containerEl)
			.setName("Port")
			.setDesc("Local port (restart the plugin after changing).")
			.addText((t) => t.setValue(String(this.plugin.settings.port)).onChange(async (v) => {
				const port = Number(v);
				if (Number.isInteger(port) && port > 1023 && port < 65536) {
					this.plugin.settings.port = port;
					await this.plugin.saveData(this.plugin.settings);
				}
			}));
		new Setting(containerEl)
			.setName("Access token")
			.setDesc("Copy into: obsidian-mcp config set bridge_token <token>. Keep it private.")
			.addButton((b) => b.setButtonText("Copy").onClick(async () => {
				await navigator.clipboard.writeText(this.plugin.settings.token);
				new Notice("Token copied");
			}))
			.addButton((b) => b.setButtonText("Regenerate").setWarning().onClick(async () => {
				this.plugin.settings.token = crypto.randomBytes(32).toString("hex");
				await this.plugin.saveData(this.plugin.settings);
				new Notice("New token generated; update obsidian-mcp config.");
			}));
	}
}
