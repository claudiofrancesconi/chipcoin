#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import { createReadStream, existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { extname, normalize, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

class OperationalError extends Error {}

const browser = parseBrowserArg();
await runCommand("npm", ["run", "build:mldsa:browser-test"]);

try {
  if (browser === "firefox") {
    const result = await runFirefox();
    emitGitHubNotice(`${browser} ML-DSA result`, formatBrowserResultNotice(result));
    console.log(JSON.stringify({ browser, ...result }, null, 2));
  } else {
    const result = await runChromium();
    emitGitHubNotice(`${browser} ML-DSA result`, formatBrowserResultNotice(result));
    console.log(JSON.stringify({ browser, ...result }, null, 2));
  }
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  emitGitHubNotice(`${browser} ML-DSA failure`, `reason=${message}`);
  console.error(`FAIL  ${browser} ML-DSA browser test`);
  console.error(`reason: ${message}`);
  process.exit(error instanceof OperationalError ? 2 : 1);
}

function parseBrowserArg() {
  const index = process.argv.indexOf("--browser");
  return index === -1 ? "firefox" : process.argv[index + 1];
}

async function runFirefox() {
  const firefox = findExecutable(["firefox", "firefox-esr"]);
  if (!firefox) {
    throw new OperationalError("Firefox executable not found.");
  }
  const profile = mkdtempSync(`${tmpdir()}/chipcoin-mldsa-firefox-`);
  const port = 9232;
  const proc = spawn(firefox, [
    "--headless",
    "--no-remote",
    "--profile",
    profile,
    "--remote-debugging-port",
    String(port),
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  let stderr = "";
  proc.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  try {
    const wsUrl = await waitForFirefoxWebSocket(() => stderr);
    const client = await connectBidi(wsUrl);
    await client.send("session.new", { capabilities: { alwaysMatch: {} } });
    const created = await client.send("browsingContext.create", { type: "tab" });
    const context = created.result?.context;
    if (!context) {
      throw new Error(`Firefox BiDi did not return a browsing context: ${JSON.stringify(created)}`);
    }
    const url = pathToFileURL(resolve("dist-mldsa-browser-test/tests/browser/mldsa44_browser_harness.html")).href;
    await client.send("browsingContext.navigate", { context, url, wait: "complete" });
    const payload = await pollHarnessResult(client, context);
    await client.close();
    if (!payload.ok) {
      throw new Error(`browser harness failed: ${JSON.stringify(payload)}`);
    }
    return payload;
  } finally {
    proc.kill();
    await new Promise((resolveProcess) => {
      proc.once("exit", resolveProcess);
      setTimeout(resolveProcess, 1500);
    });
    removeTemporaryProfile(profile);
  }
}

async function runChromium() {
  const chromium = findExecutable(["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]);
  if (!chromium) {
    throw new OperationalError("Chromium/Chrome executable not found. Install chromium or google-chrome to run this test.");
  }
  const version = spawnSync(chromium, ["--version"], { encoding: "utf8" });
  console.error(`chromium_path=${chromium}`);
  console.error(`chromium_version=${(version.stdout || version.stderr).trim()}`);
  const profile = mkdtempSync(`${tmpdir()}/chipcoin-mldsa-chromium-`);
  const cdpPort = await getFreePort();
  const staticServer = await startStaticServer(resolve("dist-mldsa-browser-test"));
  const proc = spawn(chromium, [
    "--headless=new",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-sandbox",
    "--remote-allow-origins=*",
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${cdpPort}`,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  let stderr = "";
  proc.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  try {
    const wsUrl = await waitForChromiumWebSocket(cdpPort, () => stderr, proc);
    const client = await connectCdp(wsUrl);
    const browserVersion = await client.send("Browser.getVersion");
    const created = await client.send("Target.createTarget", { url: "about:blank" });
    const targetId = created.result?.targetId;
    if (!targetId) {
      throw new Error(`Chromium CDP did not return a target: ${JSON.stringify(created)}`);
    }
    const attached = await client.send("Target.attachToTarget", { targetId, flatten: true });
    const sessionId = attached.result?.sessionId;
    if (!sessionId) {
      throw new Error(`Chromium CDP did not return a session: ${JSON.stringify(attached)}`);
    }
    await client.send("Page.enable", {}, sessionId);
    await client.send("Runtime.enable", {}, sessionId);
    const url = `${staticServer.url}/tests/browser/mldsa44_browser_harness.html`;
    console.error(`chromium_harness_url=${url}`);
    await client.send("Page.navigate", { url }, sessionId);
    const payload = await pollCdpHarnessResult(client, sessionId);
    await client.send("Target.closeTarget", { targetId });
    await client.close();
    if (!payload.ok) {
      throw new Error(`browser harness failed: ${JSON.stringify(payload)}`);
    }
    return {
      browserProduct: browserVersion.result?.product,
      browserUserAgent: browserVersion.result?.userAgent,
      ...payload,
    };
  } finally {
    proc.kill();
    await new Promise((resolveProcess) => {
      proc.once("exit", resolveProcess);
      setTimeout(resolveProcess, 1500);
    });
    await staticServer.close();
    removeTemporaryProfile(profile);
  }
}

function removeTemporaryProfile(profile) {
  rmSync(profile, {
    recursive: true,
    force: true,
    maxRetries: 10,
    retryDelay: 100,
  });
}

async function pollHarnessResult(client, context) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const response = await client.send("script.evaluate", {
      target: { context },
      expression: "JSON.stringify(window.__CHIPCOIN_MLDSA_BROWSER_RESULT__ ?? null)",
      awaitPromise: true,
    });
    const value = response.result?.result?.value;
    if (typeof value === "string" && value !== "null") {
      return JSON.parse(value);
    }
    await sleep(100);
  }
  throw new Error("browser harness did not produce a result before timeout");
}

async function connectBidi(wsUrl) {
  const url = new URL(wsUrl);
  if (url.pathname === "/") {
    url.pathname = "/session";
  }
  const socket = new WebSocket(url.href);
  await new Promise((resolveOpen, rejectOpen) => {
    socket.addEventListener("open", resolveOpen, { once: true });
    socket.addEventListener("error", () => {
      rejectOpen(new OperationalError(`could not open Firefox WebDriver BiDi websocket at ${url.href}`));
    }, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  });
  return {
    send(method, params = {}) {
      const id = ++nextId;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolveMessage, rejectMessage) => {
        const timeout = setTimeout(() => {
          pending.delete(id);
          rejectMessage(new Error(`Firefox BiDi command timed out: ${method}`));
        }, 10_000);
        pending.set(id, (message) => {
          clearTimeout(timeout);
          if (message.error) {
            rejectMessage(new Error(`${method} failed: ${JSON.stringify(message)}`));
          } else {
            resolveMessage(message);
          }
        });
      });
    },
    close() {
      socket.close();
    },
  };
}

async function connectCdp(wsUrl) {
  const socket = new WebSocket(wsUrl);
  let timeout;
  await new Promise((resolveOpen, rejectOpen) => {
    timeout = setTimeout(() => {
      rejectOpen(new OperationalError(`timed out opening Chromium DevTools websocket at ${wsUrl}`));
    }, 10_000);
    socket.addEventListener("open", resolveOpen, { once: true });
    socket.addEventListener("error", () => {
      rejectOpen(new OperationalError(`could not open Chromium DevTools websocket at ${wsUrl}`));
    }, { once: true });
  }).finally(() => {
    clearTimeout(timeout);
  });
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
  });
  return {
    send(method, params = {}, sessionId = undefined) {
      const id = ++nextId;
      const command = sessionId ? { id, method, params, sessionId } : { id, method, params };
      socket.send(JSON.stringify(command));
      return new Promise((resolveMessage, rejectMessage) => {
        const timeout = setTimeout(() => {
          pending.delete(id);
          rejectMessage(new Error(`Chromium CDP command timed out: ${method}`));
        }, 10_000);
        pending.set(id, (message) => {
          clearTimeout(timeout);
          if (message.error) {
            rejectMessage(new Error(`${method} failed: ${JSON.stringify(message)}`));
          } else {
            resolveMessage(message);
          }
        });
      });
    },
    close() {
      socket.close();
    },
  };
}

async function waitForFirefoxWebSocket(stderrProvider) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const match = stderrProvider().match(/WebDriver BiDi listening on (ws:\/\/[^\s]+)/);
    if (match) {
      return match[1];
    }
    await sleep(100);
  }
  throw new Error(`Firefox did not expose WebDriver BiDi endpoint. stderr: ${stderrProvider()}`);
}

async function waitForChromiumWebSocket(port, stderrProvider, proc) {
  const endpoint = `http://127.0.0.1:${port}/json/version`;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (proc.exitCode !== null) {
      throw new Error(`Chromium exited before DevTools was available. exit=${proc.exitCode} stderr: ${stderrProvider()}`);
    }
    try {
      const response = await fetch(endpoint);
      if (response.ok) {
        const payload = await response.json();
        if (typeof payload.webSocketDebuggerUrl === "string") {
          return payload.webSocketDebuggerUrl;
        }
      }
    } catch {
      // Chromium is still starting.
    }
    await sleep(100);
  }
  throw new Error(`Chromium did not expose DevTools endpoint at ${endpoint}. stderr: ${stderrProvider()}`);
}

async function pollCdpHarnessResult(client, sessionId) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    const response = await client.send("Runtime.evaluate", {
      expression: "JSON.stringify(window.__CHIPCOIN_MLDSA_BROWSER_RESULT__ ?? null)",
      awaitPromise: true,
      returnByValue: true,
    }, sessionId);
    const value = response.result?.result?.value;
    if (typeof value === "string" && value !== "null") {
      return JSON.parse(value);
    }
    await sleep(100);
  }
  throw new Error("browser harness did not produce a result before timeout");
}

function findExecutable(candidates) {
  if (process.env.CHROME_PATH) {
    if (existsSync(process.env.CHROME_PATH)) {
      return process.env.CHROME_PATH;
    }
    throw new OperationalError(`CHROME_PATH does not exist: ${process.env.CHROME_PATH}`);
  }
  for (const candidate of candidates) {
    const result = spawnSync("which", [candidate], { encoding: "utf8" });
    if (result.status === 0 && result.stdout.trim()) {
      return result.stdout.trim();
    }
  }
  return null;
}

async function getFreePort() {
  const server = createNetServer();
  await new Promise((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  await new Promise((resolveClose) => server.close(resolveClose));
  if (!address || typeof address === "string") {
    throw new Error("could not allocate a local port");
  }
  return address.port;
}

async function startStaticServer(root) {
  const port = await getFreePort();
  const server = createServer((request, response) => {
    const requestUrl = new URL(request.url || "/", `http://127.0.0.1:${port}`);
    const requestedPath = normalize(decodeURIComponent(requestUrl.pathname)).replace(/^([/\\])+/, "");
    const filePath = resolve(root, requestedPath || "tests/browser/mldsa44_browser_harness.html");
    if (!filePath.startsWith(root + sep) && filePath !== root) {
      response.writeHead(403);
      response.end("forbidden");
      return;
    }
    if (!existsSync(filePath) || !statSync(filePath).isFile()) {
      response.writeHead(404);
      response.end("not found");
      return;
    }
    response.writeHead(200, {
      "content-type": contentType(filePath),
      "cache-control": "no-store",
    });
    createReadStream(filePath).pipe(response);
  });
  await new Promise((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(port, "127.0.0.1", resolveListen);
  });
  return {
    url: `http://127.0.0.1:${port}`,
    close() {
      return new Promise((resolveClose) => server.close(resolveClose));
    },
  };
}

function contentType(filePath) {
  switch (extname(filePath)) {
    case ".html":
      return "text/html; charset=utf-8";
    case ".js":
      return "text/javascript; charset=utf-8";
    case ".json":
      return "application/json; charset=utf-8";
    case ".css":
      return "text/css; charset=utf-8";
    default:
      return "application/octet-stream";
  }
}

function formatBrowserResultNotice(result) {
  const benchmark = result.benchmark || {};
  return [
    `ok=${result.ok}`,
    `browserProduct=${result.browserProduct || "unknown"}`,
    `publicKeyMatches=${result.publicKeyMatches}`,
    `privateKeyMatches=${result.privateKeyMatches}`,
    `signatureMatches=${result.signatureMatches}`,
    `signatureVerifies=${result.signatureVerifies}`,
    `pythonSignatureVerifies=${result.pythonSignatureVerifies}`,
    `alteredSignatureRejected=${result.alteredSignatureRejected}`,
    `alteredDigestRejected=${result.alteredDigestRejected}`,
    `wrongPublicKeyRejected=${result.wrongPublicKeyRejected}`,
    `invalidDigestRejected=${result.invalidDigestRejected}`,
    `invalidSignatureRejected=${result.invalidSignatureRejected}`,
    `keygenMs=${benchmark.keygenMs ?? "unknown"}`,
    `signDigestMs=${benchmark.signDigestMs ?? "unknown"}`,
    `verifyDigestMs=${benchmark.verifyDigestMs ?? "unknown"}`,
    `sign10Ms=${benchmark.sign10Ms ?? "unknown"}`,
    `verify10Ms=${benchmark.verify10Ms ?? "unknown"}`,
  ].join(" ");
}

function emitGitHubNotice(title, message) {
  if (!process.env.GITHUB_ACTIONS) {
    return;
  }
  console.log(`::notice title=${escapeGitHubCommand(title)}::${escapeGitHubCommand(message)}`);
}

function escapeGitHubCommand(value) {
  return String(value)
    .replaceAll("%", "%25")
    .replaceAll("\r", "%0D")
    .replaceAll("\n", "%0A")
    .replaceAll(":", "%3A")
    .replaceAll(",", "%2C");
}

async function runCommand(command, args) {
  await new Promise((resolveRun, rejectRun) => {
    const proc = spawn(command, args, { stdio: "inherit" });
    proc.on("exit", (code) => {
      if (code === 0) {
        resolveRun();
      } else {
        rejectRun(new Error(`${command} ${args.join(" ")} failed with exit code ${code}`));
      }
    });
    proc.on("error", rejectRun);
  });
}

function sleep(ms) {
  return new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
}
