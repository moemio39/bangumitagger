///|
fn help() -> Unit {
  let usage =
    #|BangumiTagger - MoonBit CLI for Bangumi and Jellyfin
    #|
    #|  search --query TITLE [--proxy URL]
    #|  scan --shows-root DIR --movies-root DIR
    #|  plan --shows-root DIR --movies-root DIR --source DIR --subject ID
    #|       [--kind show|movie] [--root-subject ID] [--season N]
    #|       [--episode-offset N] [--numbering auto|ep|sort] [--no-poster]
    #|       [--proxy URL] [--data DIR]
    #|  apply|resume|rollback --run ID [--data DIR] --yes
    #|  verify --run ID [--data DIR]
    #|  unlock --data DIR --yes
    #|
    #|Search, scan and plan do not change media files.
    #|Use --help to show this screen.
  println(usage)
}

///|
fn option(
  flags : Map[String, String],
  name : String,
  fallback~ : String = "",
) -> String {
  flags.get(name).unwrap_or(fallback)
}

///|
fn required(flags : Map[String, String], name : String) -> String raise {
  let value = option(flags, name)
  if value.trim().is_empty() {
    raise TagError("Missing --\{name}.")
  }
  value
}

///|
fn numeric(
  flags : Map[String, String],
  name : String,
  fallback : Int32,
  min : Int32,
  max : Int32,
) -> Int32 raise {
  let value = number(option(flags, name, fallback=fallback.to_string()))
  match value {
    Some(n) if n >= min && n <= max => n
    _ =>
      raise TagError("--\{name} must be an integer between \{min} and \{max}.")
  }
}

///|
fn options(
  args : Array[String],
  allowed : Array[String],
) -> Map[String, String] raise {
  let flags : Map[String, String] = Map([])
  let mut i = 1
  while i < args.length() {
    let name = match args[i].strip_prefix("--") {
      Some(p) => p.to_owned()
      None => raise TagError("Unexpected argument: \{args[i]}")
    }
    if !allowed.contains(name) || flags.contains(name) {
      raise TagError("Unknown or duplicate option --\{name}.")
    }
    i += 1
    if name == "no-poster" || name == "yes" {
      flags[name] = "true"
      continue
    }
    if i >= args.length() || args[i].has_prefix("--") {
      raise TagError("Missing value for --\{name}.")
    }
    flags[name] = args[i]
    i += 1
  }
  flags
}

///|
fn search(flags : Map[String, String]) -> Unit raise {
  let query = required(flags, "query")
  let body = Json::object({
    "keyword": Json::string(query),
    "sort": Json::string("match"),
    "filter": Json::object({ "type": Json::array([Json::number(2.0)]) }),
  }).stringify()
  let result = api(
    "/v0/search/subjects?limit=12",
    option(flags, "proxy"),
    body~,
  )
  let rows = items(result, "data")
  if rows.is_empty() {
    println("No anime entries found.")
  }
  for row in rows {
    println(
      "\{whole(prop(row, "id")).unwrap_or(0)}\t\{title(row)}\t\{text(row, "date")}\t\{text(row, "platform")}",
    )
  }
}

///|
fn dispatch_cli(args : Array[String]) -> Unit raise {
  if args.is_empty() || ["help", "--help", "-h"].contains(args[0]) {
    help()
    return
  }
  let command = args[0]
  match command {
    "search" => search(options(args, ["query", "proxy"]))
    "scan" => {
      let flags = options(args, ["shows-root", "movies-root"])
      let (shows, movies) = roots(
        option(flags, "shows-root"),
        option(flags, "movies-root"),
      )
      let output : Array[String] = []
      scan_dir(shows, output)
      scan_dir(movies, output)
      for p in output {
        println(p)
      }
      println("Found \{output.length()} candidates without local NFO files.")
    }
    "plan" =>
      build_plan(
        options(args, [
          "shows-root", "movies-root", "source", "subject", "kind", "root-subject",
          "season", "episode-offset", "numbering", "proxy", "data", "no-poster",
        ]),
      )
    "apply" | "resume" | "rollback" =>
      apply_command(command, options(args, ["run", "data", "yes"]))
    "verify" => apply_command(command, options(args, ["run", "data"]))
    "unlock" => {
      let flags = options(args, ["data", "yes"])
      if !flags.contains("yes") {
        raise TagError("Explicit --yes is required.")
      }
      let lock = join(
        fs_string("resolve", required(flags, "data")),
        "execution.lock",
      )
      ignore(
        host("unlock", {
          "file": Json::string(lock),
          "stale": Json::boolean(true),
        }),
      )
      println("No stale execution lock remains.")
    }
    _ => raise TagError("Unknown command \{command}. Use --help.")
  }
}

///|
fn error_text(e : Error) -> String {
  if e is TagError(message) {
    message
  } else {
    e.to_string()
  }
}

///|
pub fn run() -> Unit {
  try {
    let args : Array[String] = @json.from_json(host("args", Map([])))
    dispatch_cli(args)
  } catch {
    e =>
      ignore(host("fail", { "text": Json::string(error_text(e)) })) catch {
        _ => ()
      }
  }
}
