///|
test "episode filenames, ranges, and extensions" {
  assert_eq(parts("Title.v2.MKV"), ("Title.v2", ".mkv"))
  assert_eq(episode_from_filename("[Team] Title [07][1080p].mkv"), (None, 7, 7))
  assert_eq(episode_from_filename("Title S02E04-E06.mkv"), (Some(2), 4, 6))
  assert_eq(episode_from_filename("Title - 12 [1080p].mkv"), (None, 12, 12))
  assert_true(
    (Ok(episode_from_filename("Title [02][03].mkv")) catch { e => Err(e) })
    is Err(_),
  )
  assert_true(
    (Ok(episode_from_filename("Title [OVA][01].mkv")) catch { e => Err(e) })
    is Err(_),
  )
}

///|
test "numbers and titles are safe for local filenames" {
  assert_eq(safe_name(" CON.txt ", 80), "_CON.txt")
  assert_eq(safe_name("A/B: C", 80), "A B  C")
  assert_eq(safe_name("..", 80), "Untitled")
  assert_eq(number("02"), Some(2))
  assert_eq(number("2.5"), None)
  assert_eq(number("9999999999"), None)
}

///|
test "episode numbering refuses different ep and sort matches" {
  let rows = items(
    @json.parse(
      (
        #|{"data":[{"id":7,"type":0,"ep":1,"sort":13},{"id":8,"type":0,"ep":2,"sort":1},{"id":9,"type":1,"ep":1,"sort":1}]}
      ),
    ),
    "data",
  )
  assert_eq(whole(prop(choose_episode(rows, 1, "ep"), "id")), Some(7))
  assert_eq(whole(prop(choose_episode(rows, 1, "sort"), "id")), Some(8))
  assert_true(
    (Ok(choose_episode(rows, 1, "auto")) catch { e => Err(e) }) is Err(_),
  )
}

///|
test "NFO escapes XML and retains a valid range and provider IDs" {
  let row = @json.parse("{\"id\":123,\"name\":\"A & B\",\"desc\":\"<plot>\"}")
  let result = episode_nfo(row, 42, 2, 3, 4, "Show <TV>")
  assert_true(result.contains("<title>A &amp; B</title>"))
  assert_true(result.contains("<plot>&lt;plot&gt;</plot>"))
  assert_true(result.contains("<episodeend>4</episodeend>"))
  assert_true(result.contains("<bangumisubjectid>42</bangumisubjectid>"))
  assert_eq(xml_escape("\"'\u0001"), "&quot;&apos;")
}
