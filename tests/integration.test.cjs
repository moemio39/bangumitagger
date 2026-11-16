"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const driver = path.join(__dirname, "driver.cjs");

function cli(args, status = 0) {
  const result = spawnSync(process.execPath, [driver, ...args], { encoding: "utf8" });
  assert.ifError(result.error);
  assert.equal(result.status, status, result.stdout + "\n" + result.stderr);
  return result.stdout + result.stderr;
}

function file(p, text) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text);
  const old = new Date(Date.now() - 180000);
  fs.utimesSync(p, old, old);
  return p;
}

function fixture(t) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "bangumitagger-test-"));
  t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
  const shows = path.join(tmp, "Shows");
  const movies = path.join(tmp, "Movies");
  const src = path.join(shows, "Release");
  const data = path.join(tmp, "Data");
  fs.mkdirSync(src, { recursive: true });
  fs.mkdirSync(movies);
  const roots = ["--shows-root", shows, "--movies-root", movies];
  const plan = (extra = [], sid = "42") => {
    const out = cli(["plan", ...roots, "--source", src, "--subject", sid, "--data", data, ...extra]);
    const id = out.match(/Plan ([0-9a-f-]+):/)[1];
    const run = path.join(data, "runs", id);
    return { run, payload: JSON.parse(fs.readFileSync(path.join(run, "plan.json"))), args: ["--data", data, "--run", id] };
  };
  return { shows, movies, src, data, roots, plan };
}

test("scan, search, preview, pin identity, publish NFO and poster, then roll back", t => {
  const f = fixture(t);
  const v = file(path.join(f.src, "Release [01].mkv"), "fake-video");
  const subtitle = file(path.join(f.src, "Release [01].en.ass"), "fake-subtitle");
  const unmatched = file(path.join(f.src, "Other.ass"), "leave this alone");
  const ignore = file(path.join(f.shows, "Ignored", ".ignore"), "");
  const hidden = file(path.join(path.dirname(ignore), "Hidden.mkv"), "invisible");
  const seen = cli(["scan", ...f.roots]);
  assert.ok(seen.includes(v));
  assert.ok(!seen.includes(hidden));
  assert.match(cli(["search", "--query", "Test Show"]), /42\s+Test & Show/);
  const p = f.plan();
  assert.equal(p.payload.moves.length, 2);
  assert.equal(p.payload.outputs.length, 5);
  assert.ok(fs.existsSync(v) && fs.existsSync(subtitle));
  assert.ok(p.payload.outputs.every(x => !fs.existsSync(x.dst)));
  assert.match(cli(["apply", ...p.args], 1), /add --yes/);
  const inode = fs.statSync(v, { bigint: true }).ino;
  cli(["apply", ...p.args, "--yes"]);
  cli(["verify", ...p.args]);
  assert.equal(fs.statSync(p.payload.moves[0].dst, { bigint: true }).ino, inode);
  assert.ok(p.payload.moves[1].dst.endsWith(".en.ass"));
  assert.ok(fs.existsSync(unmatched));
  for (const output of p.payload.outputs.filter(x => x.dst.endsWith(".nfo"))) {
    assert.match(fs.readFileSync(output.dst, "utf8"), /uniqueid type="bangumi"/);
  }
  cli(["rollback", ...p.args, "--yes"]);
  assert.equal(fs.readFileSync(v, "utf8"), "fake-video");
  assert.equal(fs.readFileSync(subtitle, "utf8"), "fake-subtitle");
  assert.ok(p.payload.outputs.every(x => !fs.existsSync(x.dst)));
});

test("reject conflicts and plan edits before moving anything", t => {
  const f = fixture(t);
  const v = file(path.join(f.src, "Release [01].mkv"), "original");
  const p = f.plan(["--no-poster"]);
  const dst = p.payload.moves[0].dst;
  file(dst, "unrelated");
  assert.match(cli(["apply", ...p.args, "--yes"], 1), /File identity changed/);
  assert.equal(fs.readFileSync(dst, "utf8"), "unrelated");
  assert.equal(fs.readFileSync(v, "utf8"), "original");
  fs.unlinkSync(dst);
  fs.appendFileSync(path.join(p.run, "plan.json"), " ");
  assert.match(cli(["apply", ...p.args, "--yes"], 1), /saved plan has changed/);
  assert.ok(fs.existsSync(v));
});

test("resume an interrupted hard-link move, then restore the movie", t => {
  const f = fixture(t);
  const v = file(path.join(f.src, "The Film.mkv"), "movie-bytes");
  const p = f.plan(["--kind", "movie"], "43");
  const dst = p.payload.moves[0].dst;
  assert.ok(dst.startsWith(f.movies + path.sep));
  assert.equal(p.payload.outputs.length, 1);
  assert.match(cli(["resume", ...p.args, "--yes"], 1), /has not started/);
  fs.writeFileSync(path.join(p.run, "journal-apply-start"), "");
  fs.writeFileSync(path.join(p.run, "journal-move-0"), "");
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.linkSync(v, dst);
  assert.ok(fs.existsSync(v) && fs.existsSync(dst));
  cli(["resume", ...p.args, "--yes"]);
  assert.ok(!fs.existsSync(v));
  cli(["verify", ...p.args]);
  cli(["rollback", ...p.args, "--yes"]);
  assert.equal(fs.readFileSync(v, "utf8"), "movie-bytes");
  assert.ok(!fs.existsSync(dst));
});

test("reject overlapping library roots and keep ambiguous OVA files untouched", t => {
  const f = fixture(t);
  assert.match(cli(["scan", "--shows-root", f.shows, "--movies-root", f.src], 1), /cannot contain each other/);
  const ova = file(path.join(f.src, "Release [OVA][01].mkv"), "ova");
  file(path.join(f.src, "Release S01E01-E02.mkv"), "range");
  const p = f.plan(["--no-poster"]);
  assert.equal(p.payload.moves.length, 1);
  assert.match(p.payload.moves[0].dst, /S01E01-E02/);
  assert.ok(p.payload.review.some(x => x.includes("manual handling")));
  cli(["apply", ...p.args, "--yes"]);
  assert.ok(fs.existsSync(ova));
});

test("rollback never deletes library metadata edited since apply", t => {
  const f = fixture(t);
  const v = file(path.join(f.src, "Release [01].mkv"), "original");
  const p = f.plan(["--no-poster"]);
  cli(["apply", ...p.args, "--yes"]);
  const out = p.payload.outputs[0].dst;
  fs.appendFileSync(out, "<!-- edited -->");
  assert.match(cli(["rollback", ...p.args, "--yes"], 1), /Metadata destination is occupied or has changed/);
  assert.match(fs.readFileSync(out, "utf8"), /<!-- edited -->/);
  assert.ok(!fs.existsSync(v) && fs.existsSync(p.payload.moves[0].dst));
});
