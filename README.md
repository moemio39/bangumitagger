# BangumiTagger

用 Bangumi 元数据整理本地动画，生成 Jellyfin 可读的 NFO 和封面。支持剧集、电影和同名外挂字幕/音轨。先预览、再执行；只做同卷移动，不复制或重新编码视频。

支持 Windows、Python 3.10 及以上。提供中文 GUI 和 CLI，不需要安装第三方 Python 包；使用 GUI 时请安装 Python 自带的 Tcl/Tk 组件。

## 打开与使用

从 GitHub 的 **Code → Download ZIP** 下载并解压，双击 **启动工具.cmd**（或运行 `py -3 scraper.py gui`）。首次启动时选择两个已存在、互不嵌套、位于同一卷的目录：例如剧集 `D:\Media\Shows`、电影 `D:\Media\Movies`。首次使用建议在测试目录用少量片源试跑。

默认设置：

| 设置 | 默认值 |
|---|---|
| 剧集根目录 | 需设置 |
| 电影根目录 | 需设置 |
| HTTP 代理 | 留空直连；可填本机实际代理地址 |
| 稳定时间 | 120 秒内修改过的文件暂不处理 |

1. **扫描新增**：只读扫描；有有效本地 NFO 的视频跳过，`.ignore` 下的资料不扫描。
2. **批量搜索建议**：查询 Bangumi；已配置代理时通过该代理访问。选中左侧组，可改关键词、搜索当前组；点击候选后点 **确认并保存当前组绑定**。
3. **生成预览**：仅生成工具自己的记录/元数据暂存文件，媒体文件不改动。逐页检查移动目标、旧 NFO 删除和待确认文件。
4. **执行预览**：再次确认后原位移动视频及前缀匹配的外挂字幕/音轨，生成 NFO 与真实封面。只改预览中的文件，不复制视频、不覆盖其他视频。
5. 自行在 Jellyfin 刷新媒体库。工具不会调用扫描/刷新接口，也不需要 Jellyfin 管理员凭据。

确认过的源目录绑定会保存在工具 `data/selections.json`；已整理季目录中的可靠 Bangumi 条目 ID 可用于识别新集。**“接受高置信建议”是显式批量确认**，要求名称相似度 ≥94% 且领先第二候选 ≥12%；它不是正确作品/季号的保证，仍需看预览。不够确定的文件始终留在待确认列表，不会强行变成特别篇或正片。

### 第二季、绝对编号和特别篇

- `条目 ID`：这一季对应的 Bangumi 条目 ID。
- `根作品 ID`：第一季/已有剧集的 Bangumi ID；合并第二季时填写，并把季号改为 `2`。留空则以所选条目建独立作品目录。
- `集号减去的偏移`：文件从 14 开始而本季从 1 开始时可填 `13`。
- `auto` 同时看 Bangumi `ep`/`sort`；结果冲突则拒绝猜测，改为 `ep` 或 `sort` 后重新预览。
- 合集 `01-02` 保留为 `S01E01-E02`；不切割、不重新编码。
- SP/OVA/OAD 不自动混入 TV 正片。选择对应条目、Season 00，或逐文件指定章节类型；小数章节和未知特典不自动编号。
- 多部电影在同一发布目录内会分别要求确认，避免把整套合集误认成同一电影的多版本。
- 原盘 BDMV/VIDEO_TS 不自动整理；建议另行确认正片流或自行无损封装后再处理。

逐文件覆盖填 **JSON 对象**，键为左侧显示的相对路径，例：

```json
{
  "Anime [OVA].mkv": {"number": 1, "episode_type": 1},
  "Anime unknown.mkv": {"number": 3},
  "Interview.mkv": {"role": "extra"}
}
```

`episode_type: 1` 表示 Bangumi SP；独立 OVA 条目的普通章节通常是 `0`，请按实际 API 章节选择，不要盲套例子。没有唯一匹配的视频留原位；字幕仅按同目录完整视频前缀匹配，无法匹配的字幕不乱绑定。

### NFO、资料与 Jellyfin 服务

- 默认保留目标目录已有 NFO/海报，只为新增视频补齐元数据。
- 需要重做旧 NFO 时勾选 **替换旧 NFO（直接删除，不备份）**：预览列出的旧 NFO 直接删除，工具不保存内容；回滚不能恢复它们。
- 此选项只影响当前待处理组涉及的 NFO，不会强制重刮扫描时已跳过的全部作品；当前工具以增量整理新片源为主。
- 已完整处理的未整理发布包，可把剩余非视频资料原位移至作品的 `资料/发布包-*`；加空 `.ignore` 防止 Jellyfin 扫到资料，未匹配资源不会丢弃。仍有未确认视频的发布包不会被整体隐藏。
- 如遇媒体占用，可手工暂停 Jellyfin，或以管理员身份运行启动脚本，勾选 **整理时暂停 Jellyfin，结束恢复原状态**。原本运行则在成功/失败后尝试恢复运行；原本停止则保持停止。
- 没勾暂停选项时，工具不动服务。播放中/下载中的文件可能导致执行停止，停止时保留记录，不自动回滚或复制视频。

## 记录、恢复、回滚

每次预览有独立 `data/runs/日期-ID/`，包含：

- `plan.json`、校验摘要：完整预览（不要手工改这个计划，改绑定后重新生成）。
- `staged/`：**新** NFO/封面暂存，**没有视频或旧 NFO 备份**。
- `journal.jsonl`：执行前意图与完成记录。
- `verification.json`：文件身份/大小/修改时间和新元数据 SHA-256/XML 的验证结果。

执行中断后，选择对应历史记录并点 **恢复中断**；工具先核对整个已执行/未执行状态，拒绝覆盖冲突文件。进程崩溃留下执行锁时，点 **清理失效执行锁**，只允许清理没有活进程的锁。

**回滚**：恢复本次移动的原路径，删除未被修改的新生成元数据；不恢复主动删除的旧 NFO。若新文件被 Jellyfin 或你手工修改，回滚会拒绝删除。回滚只清理记录内的空目录，不递归删除目录。

## 命令行

在本目录 PowerShell 中运行：

```powershell
$shows = 'D:\Media\Shows'
$movies = 'D:\Media\Movies'
python .\scraper.py scan --shows-root $shows --movies-root $movies
python .\scraper.py search "葬送的芙莉莲"
python .\scraper.py plan --selections .\我的绑定.json --shows-root $shows --movies-root $movies
# 先检查上一步输出的运行目录中的 plan.json，再执行：
python .\scraper.py apply --yes
python .\scraper.py verify
```

`resume --yes` 与 `rollback --yes` 是中断恢复和撤销的独立操作，不是每次运行的步骤。

未指定 `--run` 使用最近预览；指定历史记录可加 `--run "完整记录目录"`。修改媒体必须带 `--yes`；计划命令只预览。CLI 的 NFO 删除开关是 `plan --replace-nfo`；暂停恢复服务开关是 `apply/resume/rollback --pause-jellyfin`。

`scan` 生成的 `data/scan.json` 有每组的 `id`。绑定文件格式：

```json
{
  "scan.json 中的组 id": {
    "confirmed": true,
    "subject_id": 395378,
    "kind": "show",
    "season": 1,
    "offset": 0,
    "numbering": "auto"
  }
}
```

GUI 的 `data/selections.json` 也可直接用于 CLI 的 `--selections`。

可在命令前用 `--data "记录目录"` 指定工具状态位置；命令后用 `--shows-root "目录" --movies-root "目录" --proxy "http://127.0.0.1:7890"` 指定设置。两个媒体目录须在同一卷，不得相同/嵌套；拒绝跨卷复制、重解析点及根目录删除。

### Token 与代理

公开数据通常无需 Token。GUI 不保存输入的 Token；CLI 可以使用当前进程的 `BGM_TOKEN` 环境变量。请勿把 Token 写入启动脚本、绑定 JSON 或提交到仓库。`data/` 含本机目录、绑定、元数据和操作记录，不应共享。

HTTP 代理用于 API 和封面；不支持 SOCKS URL，也不会在代理故障时静默直连。请保持 Clash/Mihomo 对应端口运行。清空代理表示显式选择直连。

## 测试

```powershell
python -m unittest discover -s .\tests -v
```

测试只用临时目录及模拟 API/服务，不会触碰真实媒体库或 Jellyfin 服务。覆盖原位移动、字幕、资料、拒绝覆盖、NFO 无备份删除、增量扫描、中断恢复、回滚、路径边界及服务原状态恢复。

## 官方参考

- [Bangumi API](https://bangumi.github.io/api/)
- [Jellyfin NFO](https://jellyfin.org/docs/general/server/metadata/nfo/)
- [剧集命名与字幕](https://jellyfin.org/docs/general/server/media/shows/)
- [电影命名](https://jellyfin.org/docs/general/server/media/movies/)
- [`.ignore` 排除目录](https://jellyfin.org/docs/general/server/media/excluding-directory/)

这是本地文件整理器，不是 Jellyfin 插件或服务器常驻监控服务；当前没有注册定时任务。Bangumi 的特典章节顺序不保证与 TMDB/TVDB 相同。NFO 使用 Bangumi 自定义 provider ID，但不冒充 TMDB/TVDB ID；本地 NFO 的支持格式以 Jellyfin 官方说明为准。
