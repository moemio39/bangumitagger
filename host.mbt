///|
priv suberror TagError {
  TagError(String)
}

///|
extern "js" fn host_raw(action : String, payload : String) -> String =
  #| (action, payload) => globalThis.bangumiHost(action, payload)

///|
fn prop(value : Json, key : String) -> Json {
  if value is Object(fields) {
    fields.get(key).unwrap_or(Json::null())
  } else {
    Json::null()
  }
}

///|
fn text(value : Json, key : String) -> String {
  if prop(value, key) is String(s) {
    s
  } else {
    ""
  }
}

///|
fn items(value : Json, key : String) -> Array[Json] {
  if prop(value, key) is Array(a) {
    a
  } else {
    []
  }
}

///|
fn host(action : String, fields : Map[String, Json]) -> Json raise {
  let response = @json.parse(host_raw(action, Json::object(fields).stringify()))
  if prop(response, "ok") is True {
    prop(response, "result")
  } else {
    raise TagError(text(response, "error"))
  }
}

///|
fn host_string(action : String, fields : Map[String, Json]) -> String raise {
  if host(action, fields) is String(s) {
    s
  } else {
    ""
  }
}

///|
fn fs_string(action : String, file : String) -> String raise {
  host_string(action, { "file": Json::string(file) })
}

///|
fn fs_do(action : String, file : String) -> Unit raise {
  ignore(host(action, { "file": Json::string(file) }))
}

///|
fn join(root : String, name : String) -> String raise {
  host_string("join", { "root": Json::string(root), "name": Json::string(name) })
}

///|
fn fs_stat(file : String) -> Json raise {
  host("stat", { "file": Json::string(file) })
}

///|
fn kind(file : String) -> String raise {
  text(fs_stat(file), "kind")
}

///|
fn inside(file : String, root : String) -> Bool raise {
  host("inside", { "file": Json::string(file), "root": Json::string(root) })
  is True
}

///|
fn list(dir : String) -> Array[String] raise {
  @json.from_json(host("list", { "file": Json::string(dir) }))
}

///|
fn write(file : String, content : String) -> Unit raise {
  ignore(
    host("write", { "file": Json::string(file), "text": Json::string(content) }),
  )
}

///|
fn hash(content : String) -> String raise {
  host_string("hash", { "text": Json::string(content) })
}

///|
fn api(path : String, proxy : String, body~ : String = "") -> Json raise {
  let result = host_string("fetch", {
    "url": Json::string("https://api.bgm.tv" + path),
    "proxy": Json::string(proxy),
    "body": Json::string(body),
    "poster": Json::boolean(false),
  })
  @json.parse(result, max_nesting_depth=80)
}
