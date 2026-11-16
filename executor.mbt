///|
fn mark_path(run : String, name : String) -> String raise {
  join(run, "journal-" + name)
}

///|
fn marked(run : String, name : String) -> Bool raise {
  kind(mark_path(run, name)) == "file"
}

///|
fn mark(run : String, name : String) -> Unit raise {
  if !marked(run, name) {
    write(mark_path(run, name), "")
  }
}

///|
fn plan_at(data : String, id : String) -> (String, Plan) raise {
  if id.length() < 3 || id.length() > 64 {
    raise TagError("Invalid run ID.")
  }
  for c in id {
    if !(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f') && c != '-' {
      raise TagError("Invalid run ID.")
    }
  }
  let run = fs_string("resolve", join(join(data, "runs"), id))
  let raw = fs_string("read", join(run, "plan.json"))
  if fs_string("read", join(run, "plan.sha256")) != hash(raw) {
    raise TagError("The saved plan has changed. Generate a new preview.")
  }
  let plan : Plan = @json.from_json(
    @json.parse(raw, reject_duplicate_keys=true),
  )
  let (shows, movies) = roots(plan.shows, plan.movies)
  if plan.version != 1 ||
    plan.id != id ||
    plan.shows != shows ||
    plan.movies != movies ||
    plan.moves.is_empty() ||
    plan.source != library_path(plan.source, shows, movies) {
    raise TagError("Invalid plan format or library roots.")
  }
  ignore(state_dir(data, plan.shows, plan.movies))
  validate_paths(plan, run)
  (run, plan)
}

///|
fn present_identity(file : String, expected : String) -> Bool raise {
  let info = fs_stat(file)
  if info is Null {
    return false
  }
  if text(info, "kind") != "file" || text(info, "identity") != expected {
    raise TagError("File identity changed: \{file}")
  }
  true
}

///|
fn present_output(output : Output) -> Bool raise {
  let value = kind(output.dst)
  if value.is_empty() {
    return false
  }
  if value != "file" || fs_string("file_hash", output.dst) != output.sha256 {
    raise TagError(
      "Metadata destination is occupied or has changed: \{output.dst}",
    )
  }
  true
}

///|
fn verify_run(plan : Plan) -> Unit raise {
  for entry in plan.moves {
    if kind(entry.src) != "" || !present_identity(entry.dst, entry.identity) {
      raise TagError("Move is not complete: \{entry.src}")
    }
  }
  for output in plan.outputs {
    if !present_output(output) {
      raise TagError("Missing metadata: \{output.dst}")
    }
  }
  println(
    "Verified \{plan.moves.length()} moved files and \{plan.outputs.length()} metadata files.",
  )
}

///|
fn preflight(run : String, plan : Plan) -> Unit raise {
  for index, entry in plan.moves {
    let src = present_identity(entry.src, entry.identity)
    let dst = present_identity(entry.dst, entry.identity)
    if (!src && !dst) || (dst && !marked(run, "move-\{index}")) {
      raise TagError("Unexpected move state: \{entry.src} -> \{entry.dst}")
    }
  }
  for index, output in plan.outputs {
    if present_output(output) && !marked(run, "output-\{index}") {
      raise TagError("Refusing to touch untracked metadata: \{output.dst}")
    }
  }
}

///|
fn transfer(src : String, dst : String, identity : String) -> Unit raise {
  fs_do("mkdir", fs_string("parent", dst))
  ignore(
    host("move", {
      "src": Json::string(src),
      "dst": Json::string(dst),
      "identity": Json::string(identity),
    }),
  )
}

///|
fn perform_apply(run : String, plan : Plan, resuming : Bool) -> Unit raise {
  if marked(run, "rollback-start") {
    raise TagError("This run has already entered rollback.")
  }
  if marked(run, "apply-complete") {
    raise TagError("This run was already applied. Use verify.")
  }
  let started = marked(run, "apply-start")
  if started != resuming {
    raise TagError(
      if started {
        "This run has started; use resume --yes."
      } else {
        "This run has not started; use apply --yes."
      },
    )
  }
  preflight(run, plan)
  mark(run, "apply-start")
  for index, entry in plan.moves {
    if present_identity(entry.src, entry.identity) {
      mark(run, "move-\{index}")
      transfer(entry.src, entry.dst, entry.identity)
    }
  }
  for index, output in plan.outputs {
    if !present_output(output) {
      mark(run, "output-\{index}")
      fs_do("mkdir", fs_string("parent", output.dst))
      ignore(
        host("publish", {
          "src": Json::string(join(run, output.staged)),
          "dst": Json::string(output.dst),
          "digest": Json::string(output.sha256),
        }),
      )
    }
  }
  fs_do("remove_empty", plan.source)
  verify_run(plan)
  mark(run, "apply-complete")
  println(
    "Finished. No video bytes were copied or changed. Refresh Jellyfin manually.",
  )
}

///|
fn perform_rollback(run : String, plan : Plan) -> Unit raise {
  if !marked(run, "apply-start") || marked(run, "rollback-complete") {
    raise TagError("No applied or interrupted run to roll back.")
  }
  preflight(run, plan)
  mark(run, "rollback-start")
  for index, output in plan.outputs {
    if marked(run, "output-\{index}") && present_output(output) {
      ignore(
        host("unlink_output", {
          "file": Json::string(output.dst),
          "digest": Json::string(output.sha256),
        }),
      )
    }
  }
  for i in 0..<plan.moves.length() {
    let index = plan.moves.length() - i - 1
    let entry = plan.moves[index]
    if marked(run, "move-\{index}") &&
      present_identity(entry.dst, entry.identity) {
      transfer(entry.dst, entry.src, entry.identity)
    }
  }
  // Do not remove destination directories; they may have existed before this run.
  for entry in plan.moves {
    if !present_identity(entry.src, entry.identity) || kind(entry.dst) != "" {
      raise TagError("Rollback incomplete: \{entry.src}")
    }
  }
  mark(run, "rollback-complete")
  println("Restored source paths and removed newly generated metadata.")
}

///|
fn with_lock(data : String, action : () -> Unit raise) -> Unit raise {
  let lock = join(data, "execution.lock")
  fs_do("lock", lock)
  let result = Ok(action()) catch { e => Err(e) }
  ignore(
    host("unlock", { "file": Json::string(lock), "stale": Json::boolean(false) }),
  )
  if result is Err(error) {
    raise error
  }
}

///|
fn apply_command(command : String, flags : Map[String, String]) -> Unit raise {
  let data = fs_string("resolve", option(flags, "data", fallback="data"))
  let id = required(flags, "run")
  if command == "verify" {
    let (_, plan) = plan_at(data, id)
    verify_run(plan)
    return
  }
  if !flags.contains("yes") {
    raise TagError("Review the plan first, then add --yes.")
  }
  // Validate the state directory before placing a lock in it.
  ignore(plan_at(data, id))
  with_lock(data, fn() raise {
    let (run, plan) = plan_at(data, id)
    if command == "rollback" {
      perform_rollback(run, plan)
    } else {
      perform_apply(run, plan, command == "resume")
    }
  })
}
