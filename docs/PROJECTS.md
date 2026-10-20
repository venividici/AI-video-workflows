# 从自己的分镜开始

这个项目把**剧本记录**和**可执行空间布局**分开。导入器负责保留每一行、核对时长与列映射；它不会自行推测场景、造出角色或把所有题材替换成示例物体。`scene.json` 中的布局由创作者/Codex 根据分镜明确编写，Blender 再据此执行。

核心 Python 命令只使用标准库，支持 Python 3.11 及以上。以下命令从仓库根目录执行。

## 1. 建立独立项目

新建空项目：

```bash
python -m aivw init projects/my-film --title "我的短片" --fps 24 --width 1280 --height 720
```

复制可运行的通用示例：

```bash
python -m aivw init projects/product-test --example product-orbit
python -m aivw init projects/dialogue-test --example dialogue-blocking
```

`--example` 也可以是一个含 `project.json` 的本地模板目录。目标必须不存在或为空；`init` 不覆盖已有项目。空项目本来就没有镜头和布局，因此此时 `validate` 报“尚无镜头”是预期行为。复制示例时不能通过 `--fps` 悄悄改变它的镜头时间，必须显式重算分镜帧数并调整布局动画。

每个项目的 `build/`、`renders/`、`outputs/` 是运行目录，项目内 `.gitignore` 会忽略它们。参考素材放 `references/`；演员照片、私人剧本等是否入库应按项目自己的权限决定。公共示例只包含自拟题材。

## 2. 检查输入，明确工作表和表头

```bash
python -m aivw inspect-input story.xlsx
python -m aivw inspect-input story.xlsx --sheet "逐镜表" --header-row 2
python -m aivw inspect-input story.csv --header-row 1
python -m aivw inspect-input story.json
```

多工作表 XLSX 必须明确 `--sheet`。CSV、TSV、XLSX 在**导入**时必须提供从 1 开始的 `--header-row`；`inspect-input` 的默认第 1 行只是预览候选，不会成为导入授权。表头上方的标题不会作为镜头。CSV/TSV 的“行”是 CSV 记录序号，带换行的引号单元格仍算一个记录。

XLSX 使用标准库 ZIP/XML 只读解析，不启动 Excel，不修改原表，不执行单元格公式。支持共享字符串、内联字符串及富文本。公式有缓存时读取缓存并给出“未验证新鲜度”警告；没有缓存时保留公式和原行，报告错误。应在原表软件中明确核验/重算后重新导入，不能把缺值当成零秒。

JSON 输入可以是对象数组，也可以是 `{"rows": [...]}` 或 `{"shots": [...]}`；每条记录必须为对象。JSON 已有字段名，因此不用 `--header-row`。

## 3. 使用明确的列映射

仓库有 `templates/columns-zh.json` 和 `templates/columns-en.json`。中英文常见列名会辅助识别，但同一字段同时匹配两列时会报错，不选择第一列蒙混通过。

最小自定义映射示例：

```json
{
  "shot_number": "镜头编号",
  "duration_seconds": "预计秒数",
  "description": "镜头描述",
  "scene_id": "地点",
  "dialogue": "旁白"
}
```

映射键可以是 `shot_number`、`duration_seconds`、`description`、`dialogue`、`scene_id`、`camera`、`framing`、`notes`。值使用**精确表头**或列键。XLSX/CSV 的 `"@G"` 明确表示 G 列；无歧义时也可写 `"G"`。JSON 使用对象字段名。把一个可选字段设为 `null` 会禁用该字段的自动匹配。修改模板时删除不存在的可选列；映射到不存在的列会报错。

```bash
python -m aivw import-shots --project projects/my-film --input story.xlsx --sheet "逐镜表" --header-row 2 --mapping my-columns.json
```

新题材快速练习：

```bash
python -m aivw init projects/cart-demo --title "配送车演示"
python -m aivw import-shots --project projects/cart-demo --input templates/storyboard-example.csv --header-row 1 --mapping templates/columns-en.json
```

得到的内容是配送车分镜，仍需为这些新镜头编写对应布局；不会复用产品转台的画面来冒充结果。

## 4. 行号、重复镜号与缺失数据

每个有效非空记录都有独立 ID，例如 CSV 的 `shot_r000002`、XLSX 的 `shot_4a8f13ab_r000003`。后者包含工作表名的稳定哈希，再带原始行号。再次导入相同表和行会得到相同 ID；插入/删除上方行会改变行号，因此已开工项目应先锁定源表结构。

原镜号保存在 `source.shot_number`。重复镜号会警告，但每条都保留，不自动重编号、不覆盖。`source.raw` 保留原始单元格内容，公式另保存在 `source.formulas`。没有原镜号时仍可用源行 ID 工作，但会得到警告。

默认保留所有非空数据行，包括看起来像汇总的行。确实不是镜头的行须明确排除：

```bash
python -m aivw import-shots --project projects/my-film --input story.xlsx --sheet "逐镜表" --header-row 2 --exclude-row 167
```

可重复使用 `--exclude-row`。排除行的原文和原因会进入审计，空行也会记录；导入数、空行数、明确排除数必须与来源记录总数相符。

缺时长、负时长、无法解析的时长、零帧时长、缺画面描述、列映射冲突、无缓存公式都会报错。默认不写入错误结果。需要把问题分镜留作整理草稿时，可使用 `--allow-unresolved`；它会保留全部记录，未解决的 `duration_seconds`/`frames` 为 `null`，命令仍返回退出码 2，后续验证不能通过。修正源数据或映射后重新导入；不会拿默认 5 秒补齐。

已有非空 `shots.json` 不会被默认覆盖。确认重新导入的范围后使用 `--force`。导入只改变目标项目的 `shots.json`，从不修改输入文件。

## 5. 时长与帧数

`duration_seconds` 保存分镜的原始时长。支持 `0.5`、`1.5s`、`2秒`；不支持含义不明确的 `1-2`、`01:02`。生成器最短时长与剪辑时长是两回事，导入器不会因为视频接口最低生成时长而拉长分镜。

帧数使用十进制 **ROUND_HALF_UP**：

```text
frames = ROUND_HALF_UP(duration_seconds × fps)
```

例如 24 fps 下，0.5 秒是 12 帧；0.0625 秒是 1.5 帧，明确舍入成 2 帧。后者仍保留原始 0.0625 秒，并在 `import.quantization` 中记录实际 2/24 秒、差值和舍入方式。小到舍入为 0 帧的时长是错误，不偷偷改成 1 帧。

项目 fps 必须是正整数；H.264 预演要求宽、高为正偶数。导入后修改 fps 会让验证失败，提示重新导入/重算，防止影片节奏悄悄改变。

## 6. 编写布局并验证

`scene.json` 的 `shots` 是以镜头 ID 为键的对象，每个镜头都要明确所属场景和摄影机。例如：

```json
{
  "shots": {
    "shot_r000002": {
      "scene": "courtyard",
      "camera": "wide",
      "animation": [],
      "camera_keyframes": []
    }
  }
}
```

这只是布局片段，不是可直接渲染的完整场景。还需定义 `scenes`、`objects`、`materials`、`cameras`，并遵守 [Blender 文档](BLENDER.md) 和 `schemas/scene.schema.json`。布局中的 `scene` 必须和分镜 `scene_id` 完全相同。中文场景名可保留；镜头 ID 必须适合文件名。没有场次的导入行会得到 `unassigned`，请显式整理和建模。

复杂的 MCP/手工 Blender 工程可以通过 `adopt-blend` 接入，具体参数见 [Blender 文档](BLENDER.md)。成功接入后，对应布局标记为 `{"mode":"external_blend","scene":"对应场次"}`。该镜头的物体、材质和摄影机存在于 `.blend` 快照中，不要求再次简化成 `scene.json` 基础几何体；`scene` 仍须与原镜头场次一致。`validate` 检查标记和分镜数据，并核对快照是否有有效当前凭据；快照缺失或过期会产生明确警告，实际渲染仍由 Blender 模块拒绝。`.blend` 的内部检查由 `adopt-blend` 执行。不要只手写 external 标记来冒充已完成接入。

```bash
python -m aivw validate --project projects/my-film
python -m aivw validate --project projects/my-film --source-audit
```

验证检查项目路径边界、路径冲突、ID 唯一性、原始行重复、帧数/时长/fps 一致性、分辨率、导入行数核对，以及每镜是否有布局。`--source-audit` 额外读取导入时记录的源文件并核验 SHA-256；源文件搬走会明确报告无法核验，变化则要求重新导入或恢复正确来源。

项目路径必须落在项目内部；导入输入可以是用户明确给出的项目外文件。表格文本永远只是数据，不能作为 Python、shell 或 Blender 脚本执行。

## Python 接口

`aivw.storyboard.read_input(path, sheet=None, header_row=None, encoding="utf-8-sig")` 返回只读来源记录。

`aivw.storyboard.normalize_rows(table, fps, mapping=None, exclude_rows=(), id_prefix="shot")` 返回完整 `shots.json` 对象和问题记录，不写文件。

`aivw.storyboard.duration_to_frames(value, fps)` 返回 `(原始秒数, 帧数, 可选量化报告)`。

`aivw.project.validate_project(project, root, source_audit=False)` 返回结构化验证报告，不改文件。两个模块均通过 `register(subparsers)` 注册命令，处理器返回 0 或 2。
