///|
#derive(@json.ToJson, @json.FromJson)
priv struct Move {
  src : String
  dst : String
  identity : String
}

///|
#derive(@json.ToJson, @json.FromJson)
priv struct Output {
  staged : String
  dst : String
  sha256 : String
}

///|
#derive(@json.ToJson, @json.FromJson)
priv struct Plan {
  version : Int32
  id : String
  shows : String
  movies : String
  source : String
  moves : Array[Move]
  outputs : Array[Output]
  review : Array[String]
}

///|
fn video_ext(ext : String) -> Bool {
  [".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m2ts", ".ts", ".webm"].contains(
    ext,
  )
}

///|
fn companion_ext(ext : String) -> Bool {
  [
    ".ass", ".ssa", ".srt", ".sup", ".sub", ".idx", ".vtt", ".mka", ".aac", ".ac3",
    ".dts", ".flac",
  ].contains(ext)
}

///|
fn extra_dir(name : String) -> Bool {
  [
    "extras", "extra", "other", "trailers", "samples", "interviews", "featurettes",
    "behind the scenes", "deleted scenes", "clips", "shorts", "backdrops", "theme-music",
    "bdmv", "video_ts", "sp", "sps",
  ].contains(name.to_lower())
}

///|
fn roots(shows : String, movies : String) -> (String, String) raise {
  if shows.trim().is_empty() || movies.trim().is_empty() {
    raise TagError("Set both --shows-root and --movies-root.")
  }
  let s = fs_string("resolve", shows)
  let m = fs_string("resolve", movies)
  let a = fs_stat(s)
  let b = fs_stat(m)
  if text(a, "kind") != "directory" || text(b, "kind") != "directory" {
    raise TagError("Both library roots must be existing directories.")
  }
  if fs_string("key", s) == fs_string("key", m) || inside(s, m) || inside(m, s) {
    raise TagError(
      "Library roots must be distinct and cannot contain each other.",
    )
  }
  if text(a, "device") != text(b, "device") {
    raise TagError("Both libraries must be on the same volume.")
  }
  (s, m)
}

///|
fn library_path(file : String, shows : String, movies : String) -> String raise {
  let resolved = fs_string("resolve", file)
  if inside(resolved, shows) || inside(resolved, movies) {
    resolved
  } else {
    raise TagError("Path must be below a library root: \{file}")
  }
}

///|
fn state_dir(data : String, shows : String, movies : String) -> String raise {
  if data.trim().is_empty() {
    raise TagError("--data cannot be empty.")
  }
  let p = fs_string("resolve", data)
  for root in [shows, movies] {
    if fs_string("key", p) == fs_string("key", root) ||
      inside(p, root) ||
      inside(root, p) {
      raise TagError(
        "The state directory and the media libraries must not overlap.",
      )
    }
  }
  p
}

///|
fn ready(info : Json) -> Bool {
  text(info, "kind") == "file" &&
  !(prop(info, "empty") is True) &&
  prop(info, "ageSeconds") is Number(age, ..) &&
  age >= 120.0
}

///|
fn scan_dir(dir : String, output : Array[String]) -> Unit raise {
  if kind(join(dir, ".ignore")) != "" {
    return
  }
  for name in list(dir) {
    let p = join(dir, name)
    let info = Ok(fs_stat(p)) catch { e => Err(e) }
    if info is Ok(st) {
      if text(st, "kind") == "directory" && !extra_dir(name) {
        scan_dir(p, output)
      }
      let (stem, ext) = parts(name)
      if video_ext(ext) &&
        ready(st) &&
        kind(join(dir, stem + ".nfo")) == "" &&
        kind(join(dir, "movie.nfo")) == "" {
        output.push(p)
      }
    }
  }
}

///|
fn subject(id : Int32, proxy : String) -> Json raise {
  let s = api("/v0/subjects/\{id}", proxy)
  if whole(prop(s, "id")) != Some(id) || whole(prop(s, "type")) != Some(2) {
    raise TagError("Subject \{id} is not a matching anime entry.")
  }
  s
}

///|
fn chapters(id : Int32, proxy : String) -> Array[Json] raise {
  let ep : Array[Json] = []
  for page in 0..<100 {
    let response = api(
      "/v0/episodes?subject_id=\{id}&type=0&limit=100&offset=\{page * 100}",
      proxy,
    )
    let rows = items(response, "data")
    let total = whole(prop(response, "total")).unwrap_or(-1)
    if total < 0 || (rows.is_empty() && ep.length() < total) {
      raise TagError("Bangumi returned incomplete episode data.")
    }
    for row in rows {
      ep.push(row)
    }
    if ep.length() >= total {
      return ep
    }
  }
  raise TagError("Bangumi episode list exceeded the pagination limit.")
}

///|
fn series_folder(root : String, s : Json) -> String raise {
  let sid = whole(prop(s, "id")).unwrap_or(0)
  let marker = " [bangumiid-\{sid}]"
  let date = text(s, "date")
  let year = if date.length() >= 4 { date[:4].to_owned() } else { "" }
  let folder = safe_name(title(s), 65) +
    (if number(year) is Some(_) { " (\{year})" } else { "" }) +
    marker
  let owners : Array[String] = []
  for name in list(root) {
    if name.has_suffix(marker) {
      let p = join(root, name)
      if kind(p) == "directory" {
        owners.push(p)
      }
    }
  }
  if owners.length() > 1 {
    raise TagError("More than one folder is tagged with Bangumi ID \{sid}.")
  }
  if owners.length() == 1 {
    owners[0]
  } else {
    join(root, folder)
  }
}

///|
fn add_move(plan : Plan, src : String, dst : String) -> Unit raise {
  let a = library_path(src, plan.shows, plan.movies)
  let b = library_path(dst, plan.shows, plan.movies)
  let info = fs_stat(a)
  if text(info, "kind") != "file" || kind(b) != "" {
    raise TagError("Missing source or occupied destination: \{a} -> \{b}")
  }
  plan.moves.push({ src: a, dst: b, identity: text(info, "identity"), })
}

///|
fn stage_text(
  plan : Plan,
  run : String,
  dst : String,
  contents : String,
) -> Unit raise {
  let p = library_path(dst, plan.shows, plan.movies)
  if kind(p) == "file" {
    return
  }
  if kind(p) != "" {
    raise TagError("Occupied metadata destination: \{p}")
  }
  let staged = "output-\{plan.outputs.length()}.nfo"
  write(join(run, staged), contents)
  plan.outputs.push({ staged, dst: p, sha256: hash(contents), })
}

///|
fn stage_poster(
  plan : Plan,
  run : String,
  dest : String,
  s : Json,
  proxy : String,
) -> String raise {
  for ext in [".jpg", ".png", ".webp"] {
    if kind(join(dest, "poster" + ext)) == "file" {
      return "poster" + ext
    }
  }
  let url = text(prop(s, "images"), "large")
  if url.is_empty() {
    return ""
  }
  let outcome = Ok(
    host("fetch", {
      "url": Json::string(url),
      "poster": Json::boolean(true),
      "proxy": Json::string(proxy),
    }),
  ) catch {
    e => Err(e)
  }
  let cover = match outcome {
    Ok(p) => p
    Err(err) => {
      plan.review.push("Poster unavailable: \{title(s)} (\{error_text(err)})")
      return ""
    }
  }
  let ext = text(cover, "ext")
  if ![".jpg", ".png", ".webp"].contains(ext) {
    raise TagError("Unexpected image format.")
  }
  let staged = "output-\{plan.outputs.length()}\{ext}"
  let file = join(run, staged)
  ignore(
    host("write_bytes", {
      "file": Json::string(file),
      "bytes": Json::string(text(cover, "bytes")),
    }),
  )
  let dst = library_path(join(dest, "poster" + ext), plan.shows, plan.movies)
  plan.outputs.push({ staged, dst, sha256: fs_string("file_hash", file), })
  "poster" + ext
}

///|
fn validate_paths(plan : Plan, run : String) -> Unit raise {
  let seen : Map[String, Bool] = Map([])
  for entry in plan.moves {
    for p in [entry.src, entry.dst] {
      let key = fs_string("key", library_path(p, plan.shows, plan.movies))
      if seen.contains(key) {
        raise TagError("Conflicting path: \{p}")
      }
      seen[key] = true
    }
    if !inside(entry.src, plan.source) ||
      inside(entry.dst, plan.source) ||
      entry.identity.is_empty() {
      raise TagError("Invalid move scope.")
    }
  }
  for output in plan.outputs {
    let dst = library_path(output.dst, plan.shows, plan.movies)
    let key = fs_string("key", dst)
    let (_, ext) = parts(dst)
    let file = fs_string("resolve", join(run, output.staged))
    if seen.contains(key) ||
      inside(dst, plan.source) ||
      !inside(file, run) ||
      ![".nfo", ".jpg", ".png", ".webp"].contains(ext) ||
      fs_string("file_hash", file) != output.sha256 {
      raise TagError("Invalid or changed metadata output: \{dst}")
    }
    seen[key] = true
  }
}

///|
fn build_plan(flags : Map[String, String]) -> Unit raise {
  let (shows, movies) = roots(
    option(flags, "shows-root"),
    option(flags, "movies-root"),
  )
  let data = state_dir(option(flags, "data", fallback="data"), shows, movies)
  let source = library_path(required(flags, "source"), shows, movies)
  if kind(source) != "directory" ||
    kind(join(source, ".ignore")) != "" ||
    kind(join(source, "tvshow.nfo")) != "" ||
    kind(join(source, "movie.nfo")) != "" ||
    kind(join(source, "season.nfo")) != "" ||
    extra_dir(fs_string("name", source)) {
    raise TagError(
      "Choose an unorganized release folder, not a managed season, ignored folder, or extras folder.",
    )
  }
  let sid = numeric(flags, "subject", 0, 1, 999999999)
  let root_id = numeric(flags, "root-subject", sid, 1, 999999999)
  let media_kind = option(flags, "kind", fallback="show")
  let season = numeric(flags, "season", 1, 1, 99)
  let offset = numeric(flags, "episode-offset", 0, 0, 999999)
  let numbering = option(flags, "numbering", fallback="auto")
  if !["show", "movie"].contains(media_kind) ||
    !["auto", "ep", "sort"].contains(numbering) {
    raise TagError("Use --kind show|movie and --numbering auto|ep|sort.")
  }
  if media_kind == "movie" &&
    (
      root_id != sid ||
      flags.contains("season") ||
      flags.contains("episode-offset") ||
      flags.contains("numbering")
    ) {
    raise TagError("Season and episode options apply only to shows.")
  }
  let files = list(source)
  let videos : Array[String] = []
  for name in files {
    if video_ext(parts(name).1) {
      let p = join(source, name)
      if !ready(fs_stat(p)) {
        raise TagError(
          "Video is empty or was modified in the last 120 seconds: \{p}",
        )
      }
      videos.push(name)
    }
  }
  if videos.is_empty() || (media_kind == "movie" && videos.length() != 1) {
    raise TagError(
      "The source needs videos directly inside it; a movie source needs exactly one.",
    )
  }
  let proxy = option(flags, "proxy")
  let s = subject(sid, proxy)
  let platform = text(s, "platform")
  // Bangumi platform values are API data, not UI strings.
  let film = platform == "\u5267\u573A\u7248" || platform.to_lower() == "movie"
  if (media_kind == "movie" && !film) || (media_kind == "show" && film) {
    raise TagError(
      "The Bangumi platform does not match the requested media kind.",
    )
  }
  let root_subject = if root_id == sid { s } else { subject(root_id, proxy) }
  let episodes = if media_kind == "movie" { [] } else { chapters(sid, proxy) }
  let id = host_string("run_id", Map([]))
  let run = join(join(data, "runs"), id)
  let plan : Plan = {
    version: 1,
    id,
    shows,
    movies,
    source,
    moves: [],
    outputs: [],
    review: [],
  }
  let owner = series_folder(
    if media_kind == "movie" {
      movies
    } else {
      shows
    },
    root_subject,
  )
  if fs_string("key", owner) == fs_string("key", source) ||
    inside(owner, source) ||
    inside(source, owner) {
    raise TagError("The source and destination owner must not overlap.")
  }
  let dest = if media_kind == "movie" {
    owner
  } else {
    join(owner, "Season " + pad(season))
  }
  if media_kind == "movie" && kind(owner) == "directory" {
    for name in list(owner) {
      if video_ext(parts(name).1) && kind(join(owner, name)) == "file" {
        raise TagError("The movie destination already contains a video.")
      }
    }
  }
  fs_do("mkdir", run)
  let video_map : Map[String, String] = Map([])
  let used_episodes : Map[Int32, Bool] = Map([])
  for name in videos {
    let (stem, ext) = parts(name)
    let src = join(source, name)
    if kind(join(source, stem + ".nfo")) != "" {
      plan.review.push("Already has an NFO; left untouched: \{name}")
      continue
    }
    let dst = if media_kind == "movie" {
      join(dest, fs_string("name", owner) + ext)
    } else {
      let (hint, first, last) = match
        (Ok(episode_from_filename(name)) catch { e => Err(e) }) {
        Ok(result) => result
        Err(err) => {
          plan.review.push(error_text(err))
          continue
        }
      }
      if hint is Some(n) && n != season {
        plan.review.push("Season in filename differs from --season: \{name}")
        continue
      }
      let episode = first - offset
      let end = last - offset
      if episode < 1 || end < episode || end - episode > 100 || end > 999999 {
        plan.review.push("Invalid episode range after applying offset: \{name}")
        continue
      }
      let row = match
        (Ok(choose_episode(episodes, episode, numbering)) catch { e => Err(e) }) {
        Ok(result) => result
        Err(err) => {
          plan.review.push("\{name}: \{error_text(err)}")
          continue
        }
      }
      let mut valid = true
      for n in episode..<(end + 1) {
        if used_episodes.contains(n) {
          raise TagError("Overlapping episode files: \{name}")
        }
        if !(try {
            ignore(choose_episode(episodes, n, numbering))
            true
          } catch {
            _ => false
          }) {
          valid = false
        }
      }
      if !valid {
        plan.review.push(
          "At least one chapter in the range could not be matched: \{name}",
        )
        continue
      }
      for n in episode..<(end + 1) {
        used_episodes[n] = true
      }
      let code = "S\{pad(season)}E\{pad(episode)}" +
        (if end > episode { "-E\{pad(end)}" } else { "" })
      let prefix = safe_name(title(root_subject), 60) + " " + code
      let ep_title = title(row)
      let basename = if ep_title == "Untitled" {
        prefix
      } else {
        prefix + " - " + safe_name(ep_title, 55)
      }
      let path = join(dest, basename + ext)
      if kind(join(dest, basename + ".nfo")) != "" {
        raise TagError(
          "An episode NFO already exists at the proposed destination: \{basename}",
        )
      }
      stage_text(
        plan,
        run,
        join(dest, basename + ".nfo"),
        episode_nfo(row, sid, season, episode, end, title(root_subject)),
      )
      path
    }
    add_move(plan, src, dst)
    video_map[name] = dst
  }
  if video_map.is_empty() {
    for issue in plan.review {
      println(issue)
    }
    raise TagError("No matched videos; no executable plan was saved.")
  }
  for name in files {
    let (stem, ext) = parts(name)
    if video_ext(ext) {
      continue
    }
    if !companion_ext(ext) {
      plan.review.push("Non-media item left at source: \{name}")
      continue
    }
    let matches : Array[String] = []
    for video in videos {
      let vp = parts(video).0
      if stem.to_lower() == vp.to_lower() ||
        stem.to_lower().has_prefix(vp.to_lower() + ".") {
        matches.push(video)
      }
    }
    if matches.length() == 1 && video_map.get(matches[0]) is Some(linked) {
      let (vstem, _) = parts(fs_string("name", linked))
      let original = parts(matches[0]).0
      let suffix = stem[original.length():].to_owned()
      add_move(plan, join(source, name), join(dest, vstem + suffix + ext))
    } else {
      plan.review.push("Companion has no unique matched video: \{name}")
    }
  }

  let (nfo_name, tag) = if media_kind == "movie" {
    ("movie.nfo", "movie")
  } else {
    ("tvshow.nfo", "tvshow")
  }
  let existing = join(owner, nfo_name)
  if kind(existing) == "file" {
    let old = fs_string("read", existing)
    if !old.contains("<bangumiid>\{root_id}</bangumiid>") {
      raise TagError(
        "An existing library NFO could not be verified against the root subject ID.",
      )
    }
  }
  let poster = if flags.contains("no-poster") {
    ""
  } else {
    stage_poster(plan, run, owner, root_subject, proxy)
  }
  stage_text(plan, run, existing, subject_nfo(tag, root_subject, poster))
  if media_kind == "show" {
    let season_nfo = join(dest, "season.nfo")
    if kind(season_nfo) == "file" &&
      !fs_string("read", season_nfo).contains("<bangumiid>\{sid}</bangumiid>") {
      raise TagError(
        "The existing season belongs to another or unknown Bangumi subject.",
      )
    }
    let cover = if flags.contains("no-poster") {
      ""
    } else {
      stage_poster(plan, run, dest, s, proxy)
    }
    stage_text(plan, run, season_nfo, subject_nfo("season", s, cover, season~))
  }
  validate_paths(plan, run)
  let contents = @json.to_json(plan).stringify(indent=2)
  write(join(run, "plan.json"), contents)
  write(join(run, "plan.sha256"), hash(contents))
  println(
    "Plan \{id}: \{video_map.length()} videos, \{plan.moves.length() - video_map.length()} companions, \{plan.outputs.length()} metadata files.",
  )
  for issue in plan.review {
    println("Review: \{issue}")
  }
  println(
    "Review \{join(run, "plan.json")} before using apply --run \{id} --yes.",
  )
}
