///|
fn number(value : String) -> Int32? {
  if value.is_empty() || value.length() > 9 {
    return None
  }
  let mut n = 0
  for c in value {
    if c < '0' || c > '9' {
      return None
    }
    n = n * 10 + c.to_int() - '0'.to_int()
  }
  Some(n)
}

///|
fn pad(n : Int32) -> String {
  if n < 10 {
    "0\{n}"
  } else {
    n.to_string()
  }
}

///|
fn parts(name : String) -> (String, String) {
  let pieces = name.split(".").collect()
  if pieces.length() < 2 {
    return (name, "")
  }
  let stem = StringBuilder()
  for i in 0..<(pieces.length() - 1) {
    if i > 0 {
      stem.write_char('.')
    }
    stem.write_view(pieces[i])
  }
  (stem.to_string(), "." + pieces[pieces.length() - 1].to_owned().to_lower())
}

///|
fn safe_name(value : String, limit : Int32) -> String {
  let out = StringBuilder()
  let mut count = 0
  for c in value.trim() {
    if count >= limit {
      break
    }
    if "<>:\"/\\|?*".contains_char(c) || c.to_int() < 32 {
      out.write_char(' ')
    } else {
      out.write_char(c)
    }
    count += 1
  }
  let name = out.to_string().trim(chars=" .").to_owned()
  if name.is_empty() {
    return "Untitled"
  }
  let upper = name.to_upper()
  let prefix = match upper.split_once(".") {
    Some((x, _)) => x.to_owned()
    None => upper
  }
  if [
      "CON", "PRN", "AUX", "NUL", "COM0", "COM1", "COM2", "COM3", "COM4", "COM5",
      "COM6", "COM7", "COM8", "COM9", "LPT0", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
      "LPT6", "LPT7", "LPT8", "LPT9",
    ].contains(prefix) {
    "_" + name
  } else {
    name
  }
}

///|
fn title(subject : Json) -> String {
  let translated = text(subject, "name_cn")
  if !translated.is_empty() {
    return translated
  }
  let original = text(subject, "name")
  if original.is_empty() {
    "Untitled"
  } else {
    original
  }
}

///|
fn whole(v : Json) -> Int32? {
  match v {
    Number(n, ..) if n >= 0.0 && n <= 999999999.0 && n == n.to_int().to_double() =>
      Some(n.to_int())
    _ => None
  }
}

///|
fn digits_prefix(view : StringView) -> (String, String) {
  let mut end = 0
  for c in view {
    if c < '0' || c > '9' {
      break
    }
    end += 1
  }
  (view[:end].to_owned(), view[end:].to_owned())
}

///|
fn episode_from_filename(name : String) -> (Int32?, Int32, Int32) raise {
  let (stem, _) = parts(name)
  let lowered = stem.to_lower()
  for
    marker in [
      "[ova", "[oad", "[sp", "[ncop", "[nced", "[pv", "creditless", "trailer",
    ] {
    if lowered.contains(marker) {
      raise TagError("Special or extra requires manual handling: \{name}")
    }
  }
  let mut explicit : (Int32?, Int32, Int32)? = None
  for token in lowered.split(" ") {
    if token.strip_prefix("s") is Some(after_s) {
      let (season, after_num) = digits_prefix(after_s)
      if (number(season), after_num.strip_prefix("e"))
        is (Some(s), Some(after_e)) {
        let (start, tail) = digits_prefix(after_e)
        if number(start) is Some(n) {
          let end = if tail.is_empty() {
            n
          } else {
            let rest = match tail.strip_prefix("-e") {
              Some(v) => v
              None => raise TagError("Unrecognized episode range: \{name}")
            }
            match number(rest.to_owned()) {
              Some(v) => v
              None => raise TagError("Unrecognized episode range: \{name}")
            }
          }
          if explicit is Some(_) {
            raise TagError("Multiple episode codes: \{name}")
          }
          explicit = Some((Some(s), n, end))
        }
      }
    }
  }
  if explicit is Some(hit) {
    return hit
  }
  let found : Array[Int32] = []
  for chunk in stem.split("[") {
    if chunk.split_once("]") is Some((left, _)) {
      if left.length() <= 3 && number(left.to_owned()) is Some(n) {
        found.push(n)
      }
    }
  }
  if found.length() == 1 {
    return (None, found[0], found[0])
  }
  if found.length() > 1 {
    raise TagError("Ambiguous episode numbers: \{name}")
  }
  let tokens = stem.split(" - ").collect()
  if tokens.length() > 1 {
    let (digits, remaining) = digits_prefix(tokens[tokens.length() - 1])
    if remaining.is_empty() ||
      remaining.has_prefix(" [") ||
      remaining.has_prefix(" (") {
      if number(digits) is Some(n) {
        return (None, n, n)
      }
    }
  }
  raise TagError("No unambiguous episode number: \{name}")
}

///|
fn choose_episode(
  episodes : Array[Json],
  episode : Int32,
  mode : String,
) -> Json raise {
  let matches : Map[Int32, Json] = Map([])
  for row in episodes {
    if whole(prop(row, "type")) != Some(0) {
      continue
    }
    if (mode != "sort" && whole(prop(row, "ep")) == Some(episode)) ||
      (mode != "ep" && whole(prop(row, "sort")) == Some(episode)) {
      if whole(prop(row, "id")) is Some(id) {
        matches[id] = row
      }
    }
  }
  if matches.length() != 1 {
    raise TagError(
      "Episode \{episode} has \{matches.length()} matches; verify numbering and offset.",
    )
  }
  for _, row in matches {
    return row
  }
  raise TagError("Missing episode")
}

///|
fn xml_escape(value : String) -> String {
  let b = StringBuilder()
  for c in value {
    match c {
      '&' => b.write_string("&amp;")
      '<' => b.write_string("&lt;")
      '>' => b.write_string("&gt;")
      '"' => b.write_string("&quot;")
      '\'' => b.write_string("&apos;")
      _ =>
        if (c.to_int() >= 32 && c != '\uFFFE' && c != '\uFFFF') ||
          c == '\t' ||
          c == '\r' ||
          c == '\n' {
          b.write_char(c)
        }
    }
  }
  b.to_string()
}

///|
fn xml_field(name : String, value : String) -> String {
  if value.is_empty() {
    ""
  } else {
    "  <\{name}>\{xml_escape(value)}</\{name}>\n"
  }
}

///|
fn nfo(root : String, fields : Array[(String, String)], id : Int32) -> String {
  let b = StringBuilder()
  b.write_string("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<\{root}>\n")
  for field in fields {
    b.write_string(xml_field(field.0, field.1))
  }
  b.write_string(
    "  <uniqueid type=\"bangumi\" default=\"true\">\{id}</uniqueid>\n",
  )
  b.write_string("</\{root}>\n")
  b.to_string()
}

///|
fn subject_nfo(
  root : String,
  subject : Json,
  poster : String,
  season~ : Int32 = -1,
) -> String {
  let id = whole(prop(subject, "id")).unwrap_or(0)
  let fields = [
    ("title", title(subject)),
    ("originaltitle", text(subject, "name")),
    ("premiered", text(subject, "date")),
    ("plot", text(subject, "summary")),
    ("genre", "Animation"),
    ("bangumiid", id.to_string()),
    ("website", "https://bgm.tv/subject/\{id}"),
    ("thumb", poster),
    ("lockdata", "true"),
  ]
  if season >= 0 {
    fields.push(("seasonnumber", season.to_string()))
  }
  if prop(prop(subject, "rating"), "score") is Number(rating, ..) {
    if rating > 0.0 {
      fields.push(("rating", rating.to_string()))
    }
  }
  nfo(root, fields, id)
}

///|
fn episode_nfo(
  data : Json,
  sid : Int32,
  season : Int32,
  episode : Int32,
  end : Int32,
  showtitle : String,
) -> String {
  let id = whole(prop(data, "id")).unwrap_or(0)
  let ep_title = title(data)
  let fields = [
    (
      "title",
      if ep_title == "Untitled" {
        "Episode \{episode}"
      } else {
        ep_title
      },
    ),
    ("originaltitle", text(data, "name")),
    ("showtitle", showtitle),
    ("season", season.to_string()),
    ("episode", episode.to_string()),
    ("aired", text(data, "airdate")),
    ("plot", text(data, "desc")),
    ("bangumiid", id.to_string()),
    ("bangumisubjectid", sid.to_string()),
    ("website", "https://bgm.tv/ep/\{id}"),
    ("lockdata", "true"),
  ]
  if end > episode {
    fields.push(("episodeend", end.to_string()))
  }
  nfo("episodedetails", fields, id)
}
