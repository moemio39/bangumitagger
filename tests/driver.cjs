"use strict";

// Standalone fixture driver; the release launcher does not use mock responses.
const path = require("node:path");
const { dispatch } = require("../platform.cjs");
const jpeg = Buffer.concat([Buffer.from([255, 216, 255]), Buffer.alloc(64)]).toString("base64");
const anime = { id: 42, type: 2, name: "Test & Show", date: "2025-01-02",
  platform: "TV", summary: "<Plot>", images: { large: "https://lain.bgm.tv/fake.jpg" } };
const film = { id: 43, type: 2, name: "Test Film", date: "2025-01-02", platform: "\u5267\u573a\u7248" };
globalThis.bangumiHost = (op, raw) => {
  if (op !== "fetch") return dispatch(op, raw);
  const p = JSON.parse(raw);
  const url = new URL(p.url);
  const replies = {
    "/v0/search/subjects": { data: [anime, film] },
    "/v0/subjects/42": anime,
    "/v0/subjects/43": film,
    "/v0/episodes": { total: 2, data: [
      { id: 1001, type: 0, ep: 1, sort: 1, name: "One" },
      { id: 1002, type: 0, ep: 2, sort: 2, name: "Two" },
    ] },
  };
  if (p.poster) return JSON.stringify({ ok: true, result: { ext: ".jpg", bytes: jpeg } });
  if (!(url.pathname in replies)) return JSON.stringify({ ok: false, error: "Unexpected fixture request." });
  return JSON.stringify({ ok: true, result: JSON.stringify(replies[url.pathname]) });
};
require(path.join(__dirname, "..", "_build", "js", "debug", "build", "cmd", "main", "main.js"));
