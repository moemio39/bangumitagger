"use strict";

const path = require("node:path");
const { spawnSync } = require("node:child_process");

function launch() {
  if (Number(process.versions.node.split(".")[0]) < 22) {
    throw new Error("Node.js 22 or newer is required.");
  }
  const cwd = __dirname;
  const build = spawnSync("moon", ["build", "--target", "js"], {
    cwd, stdio: "inherit", windowsHide: true,
  });
  if (build.error) throw new Error("Could not start moon. Install MoonBit and add it to PATH.");
  if (build.status !== 0) {
    process.exitCode = build.status || 1;
    return;
  }
  globalThis.bangumiHost = require("./platform.cjs").dispatch;
  require(path.join(cwd, "_build", "js", "debug", "build", "cmd", "main", "main.js"));
}

try {
  launch();
} catch (error) {
  console.error("Error: " + error.message);
  process.exitCode = 1;
}
