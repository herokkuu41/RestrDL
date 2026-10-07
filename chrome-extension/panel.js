const MAX_ENTRIES = 5000;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_DOM_BYTES = 2 * 1024 * 1024;
const MAX_EXPORT_BYTES = 8 * 1024 * 1024;
const REDACTED_HEADERS = new Set(["authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"]);
const REDACTED_QUERY_NAMES = /^(access_?token|api_?key|auth|authorization|code|id_?token|key|password|secret|session|token)$/i;

const ui = Object.fromEntries([
  "start", "stop", "clear", "responseBodies", "domSnapshot", "state", "detail",
  "networkCount", "consoleCount", "errorCount"
].map((id) => [id, document.getElementById(id)]));

let capture = emptyCapture();
let pendingBodies = new Set();

function emptyCapture() {
  return {
    active: false,
    startedAt: null,
    stoppedAt: null,
    requestEntries: [],
    consoleEvents: [],
    pageErrors: [],
    navigations: [],
    dropped: { requests: 0, console: 0, errors: 0, responseBodies: 0 },
    responseBodies: false,
    domSnapshot: true
  };
}

function setStatus(state, detail) {
  ui.state.textContent = state;
  ui.detail.textContent = detail;
}

function render() {
  ui.networkCount.textContent = capture.requestEntries.length.toString();
  ui.consoleCount.textContent = capture.consoleEvents.length.toString();
  ui.errorCount.textContent = capture.pageErrors.length.toString();
  ui.start.disabled = capture.active;
  ui.stop.disabled = !capture.active;
  ui.clear.disabled = capture.active;
}

function bytes(text) {
  return new TextEncoder().encode(String(text)).length;
}

function capText(text, maxBytes) {
  text = String(text ?? "");
  if (bytes(text) <= maxBytes) return { value: text, truncated: false };
  let low = 0;
  let high = text.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (bytes(text.slice(0, middle)) <= maxBytes) low = middle;
    else high = middle - 1;
  }
  return { value: `${text.slice(0, low)}\n[truncated by AI Debug Capture]`, truncated: true };
}

function redactUrl(value) {
  try {
    const url = new URL(value);
    for (const name of [...url.searchParams.keys()]) {
      if (REDACTED_QUERY_NAMES.test(name)) url.searchParams.set(name, "[REDACTED]");
    }
    return url.toString();
  } catch (_) {
    return value;
  }
}

function scrub(value, key = "") {
  if (typeof value === "string") {
    return /url$/i.test(key) ? redactUrl(value) : value;
  }
  if (Array.isArray(value)) return value.map((item) => scrub(item, key));
  if (!value || typeof value !== "object") return value;
  const result = {};
  for (const [name, item] of Object.entries(value)) {
    if (/headers?/i.test(name) && Array.isArray(item)) {
      result[name] = item.map((header) => ({
        ...header,
        value: REDACTED_HEADERS.has(String(header.name).toLowerCase()) ? "[REDACTED]" : scrub(header.value, header.name)
      }));
    } else if (REDACTED_HEADERS.has(name.toLowerCase()) || REDACTED_QUERY_NAMES.test(name)) {
      result[name] = "[REDACTED]";
    } else {
      result[name] = scrub(item, name);
    }
  }
  return result;
}

function asyncEval(expression) {
  return new Promise((resolve, reject) => {
    chrome.devtools.inspectedWindow.eval(expression, (result, exceptionInfo) => {
      if (exceptionInfo?.isException) reject(new Error(exceptionInfo.value || "Page evaluation failed"));
      else resolve(result);
    });
  });
}

function sendDownload(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      const error = chrome.runtime.lastError;
      if (error) reject(new Error(error.message));
      else if (!response?.ok) reject(new Error(response?.error || "Chrome did not start the download"));
      else resolve(response);
    });
  });
}

const PAGE_HOOKS = `(() => {
  if (window.__aiDebugCapture) return true;
  const state = window.__aiDebugCapture = { console: [], errors: [], max: 1000 };
  const safe = (value, depth = 0, seen = new WeakSet()) => {
    if (value == null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
    if (typeof value === "bigint") return value.toString() + "n";
    if (typeof value === "function") return "[Function " + (value.name || "anonymous") + "]";
    if (value instanceof Error) return { name: value.name, message: value.message, stack: value.stack };
    if (typeof Node !== "undefined" && value instanceof Node) return "[DOM " + value.nodeName + "]";
    if (depth > 3 || typeof value !== "object") return Object.prototype.toString.call(value);
    if (seen.has(value)) return "[Circular]";
    seen.add(value);
    if (Array.isArray(value)) return value.slice(0, 20).map(v => safe(v, depth + 1, seen));
    const out = {};
    for (const key of Object.keys(value).slice(0, 30)) {
      try { out[key] = safe(value[key], depth + 1, seen); } catch (_) { out[key] = "[Unreadable]"; }
    }
    return out;
  };
  const push = (target, event) => { if (target.length < state.max) target.push(event); };
  for (const level of ["log", "info", "warn", "error", "debug"]) {
    const original = console[level];
    console[level] = function (...args) {
      push(state.console, { at: new Date().toISOString(), level, args: args.map(arg => safe(arg)) });
      return original.apply(this, args);
    };
  }
  addEventListener("error", event => push(state.errors, {
    at: new Date().toISOString(), type: "error", message: event.message,
    filename: event.filename, line: event.lineno, column: event.colno,
    stack: event.error?.stack || null
  }));
  addEventListener("unhandledrejection", event => push(state.errors, {
    at: new Date().toISOString(), type: "unhandledrejection", reason: safe(event.reason)
  }));
  return true;
})()`;

const PAGE_DIAGNOSTICS = `(() => {
  const now = new Date().toISOString();
  const state = window.__aiDebugCapture || { console: [], errors: [] };
  const nav = performance.getEntriesByType("navigation")[0];
  const resources = performance.getEntriesByType("resource").slice(-500).map(r => ({
    name: r.name, initiatorType: r.initiatorType, startTime: r.startTime,
    duration: r.duration, transferSize: r.transferSize, encodedBodySize: r.encodedBodySize,
    decodedBodySize: r.decodedBodySize
  }));
  return {
    collectedAt: now,
    page: {
      url: location.href, title: document.title, referrer: document.referrer,
      readyState: document.readyState, visibilityState: document.visibilityState,
      charset: document.characterSet, baseURI: document.baseURI,
      viewport: { width: innerWidth, height: innerHeight, devicePixelRatio },
      userAgent: navigator.userAgent, language: navigator.language,
      online: navigator.onLine, cookiesEnabled: navigator.cookieEnabled
    },
    navigationTiming: nav ? nav.toJSON() : null,
    resourceTiming: resources,
    consoleEvents: state.console || [],
    pageErrors: state.errors || [],
    dom: document.documentElement ? document.documentElement.outerHTML : ""
  };
})()`;

async function attachHooks() {
  try {
    await asyncEval(PAGE_HOOKS);
  } catch (error) {
    capture.pageErrors.push({ at: new Date().toISOString(), type: "capture-hook", message: error.message });
  }
}

function acceptedResponse(entry) {
  const contentType = (entry.response.headers || []).find((header) => header.name.toLowerCase() === "content-type")?.value || "";
  return /^(application\/(json|javascript|xml)|text\/|[^/]+\/[^;]+\+json)/i.test(contentType);
}

function responseContent(entry) {
  return new Promise((resolve) => {
    try {
      entry.getContent((content, encoding) => resolve({ content: content ?? "", encoding: encoding || "" }));
    } catch (error) {
      resolve({ error: error.message });
    }
  });
}

async function recordRequest(entry) {
  if (!capture.active) return;
  if (capture.requestEntries.length >= MAX_ENTRIES) {
    capture.dropped.requests += 1;
    return;
  }
  const clean = scrub(JSON.parse(JSON.stringify(entry)));
  capture.requestEntries.push(clean);
  render();
  if (!capture.responseBodies || !acceptedResponse(entry)) return;

  const task = (async () => {
    const body = await responseContent(entry);
    if (!body.error) {
      const capped = capText(body.content, MAX_BODY_BYTES);
      clean.response.content = {
        ...(clean.response.content || {}),
        text: capped.value,
        encoding: body.encoding,
        capturedBy: "AI Debug Capture",
        truncated: capped.truncated
      };
      if (capped.truncated) capture.dropped.responseBodies += 1;
    }
  })();
  pendingBodies.add(task);
  try { await task; } finally { pendingBodies.delete(task); }
}

chrome.devtools.network.onRequestFinished.addListener((entry) => {
  void recordRequest(entry);
});

chrome.devtools.network.onNavigated.addListener((url) => {
  if (!capture.active) return;
  capture.navigations.push({ at: new Date().toISOString(), url: redactUrl(url) });
  void attachHooks();
});

async function getDiagnostics() {
  const diagnostics = await asyncEval(PAGE_DIAGNOSTICS);
  const scrubbed = scrub(diagnostics);
  if (!capture.domSnapshot) delete scrubbed.dom;
  else {
    const capped = capText(scrubbed.dom || "", MAX_DOM_BYTES);
    scrubbed.dom = capped.value;
    scrubbed.domTruncated = capped.truncated;
  }
  return scrubbed;
}

function timestampForFile() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function shrinkBundle(bundle) {
  let output = JSON.stringify(bundle, null, 2);
  if (bytes(output) <= MAX_EXPORT_BYTES) return output;
  bundle.exportWarnings.push("Export exceeded 8 MiB; DOM snapshot was removed.");
  delete bundle.pageDiagnostics.dom;
  while (bytes(output = JSON.stringify(bundle, null, 2)) > MAX_EXPORT_BYTES && bundle.network.requests.length) {
    bundle.network.requests.pop();
  }
  if (bytes(output) > MAX_EXPORT_BYTES) {
    bundle.exportWarnings.push("Network requests were truncated to stay within Chrome download limits.");
    output = JSON.stringify(bundle, null, 2);
  }
  return output;
}

async function start() {
  capture = emptyCapture();
  capture.active = true;
  capture.startedAt = new Date().toISOString();
  capture.responseBodies = ui.responseBodies.checked;
  capture.domSnapshot = ui.domSnapshot.checked;
  try {
    capture.navigations.push({ at: capture.startedAt, url: redactUrl(await asyncEval("location.href")) });
  } catch (error) {
    capture.navigations.push({ at: capture.startedAt, url: "[unavailable]", error: error.message });
  }
  await attachHooks();
  setStatus("Recording", "Reproduce the issue now. Network requests after Start are included.");
  render();
}

async function stopAndDownload() {
  capture.active = false;
  capture.stoppedAt = new Date().toISOString();
  setStatus("Finalizing", "Collecting page diagnostics and response previews…");
  render();
  await Promise.allSettled([...pendingBodies]);

  let diagnostics;
  try {
    diagnostics = await getDiagnostics();
  } catch (error) {
    diagnostics = { collectionError: error.message };
  }
  const bundle = {
    format: "ai-debug-capture/v1",
    createdAt: new Date().toISOString(),
    capture: {
      startedAt: capture.startedAt,
      stoppedAt: capture.stoppedAt,
      durationMs: Date.parse(capture.stoppedAt) - Date.parse(capture.startedAt),
      options: { responseBodies: capture.responseBodies, domSnapshot: capture.domSnapshot },
      limits: { maxEntries: MAX_ENTRIES, maxResponsePreviewBytes: MAX_BODY_BYTES, maxDomBytes: MAX_DOM_BYTES },
      dropped: capture.dropped,
      navigations: capture.navigations
    },
    pageDiagnostics: diagnostics,
    network: {
      harVersion: "1.2",
      requests: capture.requestEntries
    },
    exportWarnings: []
  };
  const payload = shrinkBundle(bundle);
  const dataUrl = `data:application/json;charset=utf-8,${encodeURIComponent(payload)}`;
  await sendDownload({
    type: "download",
    url: dataUrl,
    filename: `ai-debug-capture-${timestampForFile()}.json`
  });
  setStatus("Downloaded", `${capture.requestEntries.length} requests saved. Attach the JSON file to your AI debugging chat.`);
  render();
}

ui.start.addEventListener("click", () => start().catch((error) => setStatus("Could not start", error.message)));
ui.stop.addEventListener("click", () => stopAndDownload().catch((error) => {
  setStatus("Download failed", error.message);
  render();
}));
ui.clear.addEventListener("click", () => {
  capture = emptyCapture();
  setStatus("Idle", "Nothing is being recorded.");
  render();
});

render();
