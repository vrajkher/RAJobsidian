// Minimal MCP Apps host for browser tests: renders the app in an iframe and forwards tool
// calls to window.callTool (provided by the Playwright test, which talks to the real server).
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";

declare global {
  interface Window { callTool(p: unknown): Promise<any>; appHtml: string; hostLog: any[]; startApp(tool: string, args: any): Promise<void>; }
}
window.hostLog = [];

window.startApp = async (tool: string, args: any) => {
  const iframe = document.createElement("iframe");
  iframe.style.cssText = "width:1000px;height:800px;border:0";
  iframe.srcdoc = window.appHtml;
  document.body.append(iframe);
  await new Promise((r) => iframe.addEventListener("load", r, { once: true }));
  const bridge = new AppBridge(null, { name: "test-host", version: "1" }, {
    serverTools: {}, openLinks: {}, updateModelContext: { text: {}, resourceLink: {}, structuredContent: {} },
    message: { text: {} }, experimental: { "openai/modelContext": {}, "openai/message": {} },
  } as any, { hostContext: { theme: "dark", displayMode: "fullscreen", "openai/deepLink": args.__deepLink } as any });
  bridge.oncalltool = async (params) => window.callTool(params);
  bridge.onupdatemodelcontext = async (params) => { window.hostLog.push({ kind: "modelContext", params }); return {} as any; };
  bridge.onmessage = async (params) => { window.hostLog.push({ kind: "message", params }); return {} as any; };
  const initialized = new Promise<void>((r) => { bridge.oninitialized = () => r(); });
  await bridge.connect(new PostMessageTransport(iframe.contentWindow!, iframe.contentWindow!));
  await initialized;
  delete args.__deepLink;
  await bridge.sendToolInput({ arguments: args });
  const result = await window.callTool({ name: tool, arguments: args });
  await bridge.sendToolResult(result);
};
