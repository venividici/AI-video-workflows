# 原片对齐与最终直切初剪

`conform` 把明确选择的原片片段对齐到原分镜的帧数、项目帧率和分辨率；`assemble-final` 按 `shots.json` 顺序把全部已验证片段拼成 `outputs/final.mp4`。这两个命令只使用本地 FFmpeg / ffprobe，不调用生成 API。

原始生成片、CG 原片和源分镜不被改写。裁切起点必须由操作员明确给出，不能因为 `timing.json` 有计划区间，就假定生成结果已经按计划完成动作。

## 1. 从已下载的 Seedance take 对齐

先用 `seedance download` 下载或登记原片，然后观看生成结果，选择要保留的起点：

```bash
python -m aivw seedance download --project ./my-project --shot s001 --take take001
python -m aivw conform --project ./my-project --shot s001 --take take001 --start-seconds 0.5
```

例如 `s001` 的原分镜是 24 fps 下的 36 帧，以上命令取原片从 **0.5 秒开始的 1.5 秒**。`--start-seconds` 从该视频第一帧起算，必须是有限的非负数；工具会处理容器自带的时间戳偏移。

`--take` 默认 `take001`。`conform` 会验证该 take 已下载、任务包 manifest 与下载回执一致、原片哈希一致，以及 take 对应当前镜头记录。如果源镜头的描述或时长已变化，旧 take 会被拒绝；经人工重新审阅后仍想复用其原片，可以用下一节的显式 `--input` 选择，并留下新的来源记录。

输出为：

| 路径 | 内容 |
| --- | --- |
| `outputs/final-clips/s001.mp4` | 对齐后的 H.264 / AAC 片段 |
| `outputs/final-clips/s001.json` | 原片来源、哈希、源镜头哈希、裁切区间、音频处理和实测帧数 |
| `outputs/seedance/s001/take001/result/original.mp4` 或 `.mov` | 原始生成文件，保持原始字节 |

项目若自定义 `paths.outputs`，上述输出均使用自定义目录。

一次可以重复 `--shot`，但同一次调用的 `--take`、`--start-seconds` 和音频选项对所有所选镜头相同。不同起点应分开执行。多镜头对齐会按项目镜头顺序逐个完成；后续镜头出错时，已经成功生成的片段仍然保留。

## 2. 使用纯 CG、实拍或其他明确原片

`--input` 直接选择一个本地文件，替代 Seedance take。一个输入只对应一个镜头：

```bash
python -m aivw conform --project ./my-project --shot s002 --input ./cg-renders/s002-original.mp4 --start-seconds 0
```

这条路径记录为 `explicit-file`，校验的是你指定的真实文件和当前镜头，不声称该文件来自 Seedance，也不声称已经验证其 Blender 布局。它适合把精确道具、文字、接触动作等 CG 镜头纳入同一初剪。

原片在项目目录内时，回执保存项目相对路径；项目外的显式输入保存绝对路径。以后合片仍需访问该原片来验证哈希，所以交接项目时应连同原片保留，或先把原片放入项目再执行 `conform`。不能用当前 `outputs/final.mp4` 或该镜头的 `final-clips/<shot>.mp4` 作为输入，以免导出覆盖自己的来源。

## 3. 帧数、画幅和时长规则

`shots.json` 的 `frames` 是最终剪辑长度的依据，目标时长为 `frames / project.fps`。未解决的帧数、与帧数不一致的源时长和重复镜头 ID 都会拒绝处理。

- 视频原片必须足够覆盖“起点 + 目标时长”。不足时直接报错，不加速、不减速、不保持尾帧来掩盖缺失。
- 原片与项目帧率不同时，FFmpeg 在正常播放时间线上重新采样到项目 fps，可能重复或丢弃帧；这不是运动插帧，也不改变播放速度。回执记录 `speed: 1.0` 和 `video_padding: false`。
- 分辨率按比例缩放到项目画幅，余下空间补黑边，不拉伸、不自动裁掉画面。当前输出为 8-bit `yuv420p`，项目宽高必须是正偶数。
- 导出后用 ffprobe 实数视频帧，必须与 `frames` 完全一致，并再次核对 fps、宽高和音轨格式；不满足要求不会提交为正式片段。

本步骤没有 HDR / 色彩空间管理或专业母版输出承诺。原片始终保留，需要专业调色、透明通道、无损编码或其他交付规格时，应使用保留的原片和裁切记录进入后期软件。

## 4. 音频处理必须可见

默认 `--audio preserve`：有一个音轨时，保留对应裁切区间，并转换成 48 kHz 双声道 AAC；它是重新编码，不是原音频字节直拷。输入没有音轨时，增加同长度的静音轨，这样有声与无声镜头能够按统一规格拼接。原音轨短于画面或有时间戳空隙时，空隙填静音，视频长度不延长。

需要明确静音时：

```bash
python -m aivw conform --project ./my-project --shot s001 --take take001 --start-seconds 0.5 --audio mute
```

每个回执记录请求的音频选项、原片是否有声音，以及实际执行的 `preserved-and-normalized`、`silent-track-for-silent-source` 或 `explicit-muted`。

输入有多个音轨时，默认拒绝静默丢弃音轨。请先在后期软件完成所需混音，或者明确选择 `--audio mute`。这些命令不会自动生成对白、选择音乐、对口型、做响度匹配或完成对白与音乐混音。

## 5. 按全部分镜合成 final.mp4

所有镜头完成对齐后执行：

```bash
python -m aivw assemble-final --project ./my-project
```

程序严格采用 `shots.json` 的镜头顺序，不按文件名排序，也不跳过缺失镜头。每个镜头都需要 `final-clips/<shot>.mp4` 及有效回执。以下情况会拒绝合片：

- 有镜头没有对齐片段或缺少回执。
- 原片、Seedance 下载回执、对齐片段或对齐配方哈希已改变。
- 当前源镜头记录、项目 fps 或分辨率与对齐时不一致。
- 对齐片段实测帧数、帧率、分辨率或音轨规格不匹配。

通过检查后，程序用精确的 `frames / fps` 镜头边界直切拼接，重新编码为 H.264 / AAC，并验证总帧数等于全部镜头帧数之和。FFmpeg 完成后还会复查来源，避免合片过程中源片段变化。

| 输出 | 含义 |
| --- | --- |
| `outputs/final.mp4` | 全部镜头的直切初剪 |
| `outputs/final.json` | 成片哈希、实测媒体信息、每镜起止帧和片段哈希 |
| `outputs/previs.mp4` | Blender 预演合片，由 `assemble` 管理，与最终初剪分别保存 |

时间线使用 `start_frame_inclusive` / `end_frame_exclusive`：例如 36 帧镜头后接 48 帧镜头，其区间是 `[0,36)` 和 `[36,84)`，最终 84 帧。AAC 编码可能产生少量音频封装补齐，镜头切点仍按视频帧数决定。

`final.mp4` 表示已完成这套流程的直切初剪，不表示已经完成导演审片、连续性修正、对白音乐混音、调色或正式交付审核。人物、空间、动作和镜头衔接仍需要观看确认。

## 6. 修改与恢复

重新运行 `conform` 或 `assemble-final` 时，先在临时目录生成并验证新片。成功提交前，旧导出及旧回执会移到 `outputs/finishing-history/` 的时间戳子目录，原片不移动。失败的处理不会覆盖现有正式导出。

每个目标文件有独立本地互斥锁。若进程被强制终止而留下 `.lock`，先确认对应操作已经停止，再删除残留锁后重试。锁不会授权自动重发任何生成任务。

修改分镜、fps、分辨率、原片或来源回执后，应重新审阅并执行 `conform`。只修改 `shots.json` 中的镜头排列顺序而不改变各镜头内容，可以直接重新 `assemble-final`，生成新的合片顺序。

两个命令都支持 `--ffmpeg`、`--ffprobe` 指定可执行文件，以及 `--timeout` 设置一次转码的最长秒数（默认 120，最大 1800）。处理很长的视频时可显式提高超时。

## 7. 本地验证

```bash
python -m unittest discover -s tests -p test_finishing.py -v
```

测试通过真实 FFmpeg 制作两段通用短视频，再跑对齐和最终合片。检查包括精确帧数、切点顺序、有声/无声段、静音模式、24→30 fps 保持时长、原片不足拒绝、缺镜与过期哈希拒绝、原片不变和旧导出归档；还包含无 API 的 Seedance 手动下载到 `conform` 流程。没有外部请求或付费任务。
