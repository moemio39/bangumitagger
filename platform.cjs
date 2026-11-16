"use strict";

// Node host for the MoonBit JS target: filesystem primitives, hashes and HTTPS.
// The CLI, matching, metadata, plan validation and recovery policy are in MoonBit.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const http = require("node:http");
const https = require("node:https");
const tls = require("node:tls");
const { spawnSync } = require("node:child_process");

const LIMIT = 12 * 1024 * 1024;
const USER_AGENT = "moemio39/bangumitagger/2.0.0 (https://github.com/moemio39/bangumitagger)";

function checked(file) {
  const absolute = path.resolve(file);
  const parsed = path.parse(absolute);
  let p = parsed.root;
  for (const name of absolute.slice(p.length).split(path.sep).filter(Boolean)) {
    p = path.join(p, name);
    const st = fs.lstatSync(p, { throwIfNoEntry: false });
    if (st?.isSymbolicLink()) throw new Error("Symbolic link or junction: " + p);
  }
  return absolute;
}

function stat(file) {
  const p = checked(file);
  const v = fs.lstatSync(p, { throwIfNoEntry: false, bigint: true });
  if (!v) return null;
  return {
    kind: v.isFile() ? "file" : v.isDirectory() ? "directory" : "other",
    identity: [v.dev, v.ino, v.size, v.mtimeNs].join(":"),
    device: String(v.dev),
    ageSeconds: (Date.now() - Number(v.mtimeMs)) / 1000,
    empty: v.size === 0n,
  };
}

function inside(file, root) {
  const relative = path.relative(path.resolve(root), path.resolve(file));
  return relative !== "" && relative !== ".." &&
    !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
}

function hash(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function fileHash(file) {
  const p = checked(file);
  const h = crypto.createHash("sha256");
  const fd = fs.openSync(p, "r");
  try {
    const data = Buffer.allocUnsafe(128 * 1024);
    for (let count; (count = fs.readSync(fd, data)) > 0;) h.update(data.subarray(0, count));
  } finally {
    fs.closeSync(fd);
  }
  return h.digest("hex");
}

function withTempWrite(dst, bytes) {
  // Publish a fully flushed temporary file without overwriting a destination.
  const p = checked(dst);
  const tmp = path.join(path.dirname(p), ".bangumitagger-" + crypto.randomUUID() + ".tmp");
  fs.writeFileSync(tmp, bytes, { flag: "wx", flush: true });
  try {
    fs.linkSync(tmp, p);
  } finally {
    fs.unlinkSync(tmp);
  }
}

function transfer(src, dst, identity) {
  const from = checked(src);
  const to = checked(dst);
  if (stat(from)?.identity !== identity) throw new Error("Source changed: " + from);
  const existing = stat(to);
  if (!existing) fs.linkSync(from, to); // Atomic no-overwrite on the same volume.
  if (stat(to)?.identity !== identity || stat(from)?.identity !== identity) {
    throw new Error("File changed during transfer: " + from);
  }
  fs.unlinkSync(from);
}

function lockedPath(file) {
  return checked(file);
}

function unlock(file, staleOnly = false) {
  const p = lockedPath(file);
  if (staleOnly) {
    if (!fs.existsSync(p)) return;
    const pid = Number(fs.readFileSync(p, "utf8"));
    if (!Number.isSafeInteger(pid) || pid < 1) throw new Error("Invalid lock; inspect it manually.");
    try {
      process.kill(pid, 0);
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
      fs.unlinkSync(p);
      return;
    }
    throw new Error("An execution is still running.");
  }
  if (Number(fs.readFileSync(p, "utf8")) !== process.pid) throw new Error("Lock owner changed.");
  fs.unlinkSync(p);
}

function getAgent(target, proxy) {
  if (!proxy) return Promise.resolve(new https.Agent({ rejectUnauthorized: true }));
  const p = new URL(proxy);
  if (p.protocol !== "http:" || p.username || p.password || p.pathname !== "/" || p.search || p.hash) {
    throw new Error("Use an HTTP proxy URL without credentials or a path.");
  }
  return new Promise((resolve, reject) => {
    const authority = target.hostname + ":443";
    const connect = http.request({
      hostname: p.hostname, port: p.port || 80, method: "CONNECT", path: authority,
      headers: { Host: authority }, timeout: 12000,
    });
    connect.on("timeout", () => connect.destroy(new Error("Proxy timed out.")));
    connect.on("error", reject);
    connect.on("connect", (response, socket, head) => {
      if (response.statusCode !== 200) {
        socket.destroy();
        reject(new Error("Proxy CONNECT failed: HTTP " + response.statusCode));
        return;
      }
      if (head.length) socket.unshift(head);
      const secure = tls.connect({ socket, servername: target.hostname, rejectUnauthorized: true });
      secure.once("error", reject);
      secure.once("secureConnect", () => {
        const agent = new https.Agent({ rejectUnauthorized: true });
        agent.createConnection = () => secure;
        resolve(agent);
      });
    });
    connect.end();
  });
}

async function request({ url, proxy, body, poster }, redirects = 0) {
  const target = new URL(url);
  if (target.protocol !== "https:" || target.username || target.password || target.port || target.hash ||
      (poster ? !/^(?:[a-z0-9-]+\.)*(?:bgm\.tv|bangumi\.tv)$/.test(target.hostname) :
        target.hostname !== "api.bgm.tv" || !target.pathname.startsWith("/v0/"))) {
    throw new Error("Unexpected Bangumi endpoint.");
  }
  const headers = { "User-Agent": USER_AGENT, Accept: poster ? "image/*" : "application/json" };
  const token = process.env.BGM_TOKEN;
  if (!poster && token) {
    if (!/^[A-Za-z0-9._~-]{1,512}$/.test(token)) throw new Error("Invalid BGM_TOKEN format.");
    headers.Authorization = "Bearer " + token;
  }
  if (body) headers["Content-Type"] = "application/json";
  const agent = await getAgent(target, proxy);
  try {
    const result = await new Promise((resolve, reject) => {
      const req = https.request(target, { method: body ? "POST" : "GET", headers, agent, timeout: 15000 }, res => {
        const parts = []; let size = 0;
        res.on("data", chunk => {
          size += chunk.length;
          if (size > LIMIT) req.destroy(new Error("Bangumi response exceeded the size limit."));
          else parts.push(chunk);
        });
        res.on("end", () => resolve({ status: res.statusCode, location: res.headers.location, bytes: Buffer.concat(parts) }));
        res.on("error", reject);
      });
      req.on("timeout", () => req.destroy(new Error("Bangumi request timed out.")));
      req.on("error", reject);
      req.end(body || undefined);
    });
    if (poster && [301, 302, 303, 307, 308].includes(result.status) && result.location && redirects < 3) {
      return await request({ url: new URL(result.location, target).href, proxy, poster }, redirects + 1);
    }
    if (result.status !== 200) throw new Error("Bangumi returned HTTP " + result.status + ".");
    if (!poster) return result.bytes.toString("utf8");
    const b = result.bytes;
    const ext = b.subarray(0, 3).equals(Buffer.from([255,216,255])) ? ".jpg" :
      b.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ? ".png" :
      b.toString("ascii", 0, 4) === "RIFF" && b.toString("ascii", 8, 12) === "WEBP" ? ".webp" : "";
    if (!ext || b.length < 32) throw new Error("Bangumi did not return a recognized poster image.");
    return { ext, bytes: b.toString("base64") };
  } finally {
    agent.destroy();
  }
}

function fetchSync(input) {
  const child = spawnSync(process.execPath, [__filename], {
    input: JSON.stringify(input), encoding: "utf8", timeout: 35000,
    maxBuffer: 2 * LIMIT, windowsHide: true,
  });
  if (child.error || child.status !== 0) throw new Error("Bangumi network worker did not complete.");
  const answer = JSON.parse(child.stdout);
  if (!answer.ok) throw new Error(answer.error);
  return answer.result;
}

const ops = {
  args: () => process.argv.slice(2),
  fail: ({ text }) => { console.error("Error: " + text); process.exitCode = 1; },
  resolve: ({ file }) => checked(file),
  join: ({ root, name }) => path.join(root, name),
  relative: ({ file, root }) => path.relative(root, file),
  inside: ({ file, root }) => inside(file, root),
  parent: ({ file }) => path.dirname(file),
  name: ({ file }) => path.basename(file),
  key: ({ file }) => process.platform === "win32" ? file.toLowerCase() : file,
  stat: ({ file }) => stat(file),
  list: ({ file }) => fs.readdirSync(checked(file)).sort(),
  mkdir: ({ file }) => fs.mkdirSync(checked(file), { recursive: true }),
  remove_empty: ({ file }) => {
    try { fs.rmdirSync(checked(file)); }
    catch (error) { if (!["ENOENT", "ENOTEMPTY", "EEXIST"].includes(error.code)) throw error; }
  },
  read: ({ file }) => fs.readFileSync(checked(file), "utf8"),
  write: ({ file, text }) => withTempWrite(file, text),
  write_bytes: ({ file, bytes }) => withTempWrite(file, Buffer.from(bytes, "base64")),
  publish: ({ src, dst, digest }) => {
    const bytes = fs.readFileSync(checked(src));
    if (hash(bytes) !== digest) throw new Error("Staged metadata changed.");
    withTempWrite(dst, bytes);
  },
  hash: ({ text }) => hash(text),
  file_hash: ({ file }) => fileHash(file),
  move: ({ src, dst, identity }) => transfer(src, dst, identity),
  unlink_output: ({ file, digest }) => {
    const p = checked(file);
    if (fileHash(p) !== digest) throw new Error("Metadata changed: " + p);
    fs.unlinkSync(p);
  },
  lock: ({ file }) => fs.writeFileSync(lockedPath(file), String(process.pid), { flag: "wx", flush: true }),
  unlock: ({ file, stale }) => unlock(file, stale),
  run_id: () => new Date().toISOString().replace(/[^0-9]/g, "") + "-" + crypto.randomUUID().slice(0,8),
  fetch: input => fetchSync(input),
};

function dispatch(action, payload) {
  try {
    const result = ops[action](JSON.parse(payload));
    return JSON.stringify({ ok: true, result: result ?? null });
  } catch (error) {
    return JSON.stringify({ ok: false, error: error.message });
  }
}

module.exports = { dispatch };

if (require.main === module) {
  (async () => {
    try {
      const result = await request(JSON.parse(fs.readFileSync(0, "utf8")));
      process.stdout.write(JSON.stringify({ ok: true, result }));
    } catch (error) {
      process.stdout.write(JSON.stringify({ ok: false, error: error.message }));
    }
  })();
}
