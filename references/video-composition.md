# 第四步：漫画图片合成视频与 BGM

输入是本主题第三步已生成、检查并整理完成的图片集。只按 `stage3-prompts.json` 的剧情顺序、`images/comic-manifest.json` 当前选中的图片合成；不扫描图片目录来猜顺序，避免混入旧版本、参考图或其他主题。文件名已规范化时读取同步后的路径，不写死shot-01或v2。用户只要图片集时不进入本阶段；需要视频时默认完成选曲和实际 MP4 输出，开发时才按用户要求暂停。

## BGM 由 agent 选择

默认是**自媒体平台常用的热门歌曲**，不是按故事年代找乐器素材，也不是泛指“流行风格”的伴奏。优先用户提供的热歌文件夹，再检查已配置且能提供实际音频的音乐接口。当前用户已明确不要古筝类素材库伴奏，不自动降级到 Openverse、氛围音效或 AI 生成的仿流行乐。

用户明确提供的 BGM 文件夹优先于这些默认选曲偏好，包括其中的电子音乐、素材音乐或 AI 音乐；直接在该目录选曲，不因文件名或来源拒绝，不声称这些本地曲目是平台热歌。目录路径保存到对应主题配置，不把开发者的个人绝对路径写死在便携 skill 中。

1. 先看内容情绪与自媒体节奏，从可用热歌里选有辨识度、开场能进入情绪的段落。历史科普也可以用平台常见流行歌、DJ 版或热门伴奏版；“讲古代”不等于“古筝配乐”。不把“科普必须无歌词、安静”作为规则。用户指定歌名/版本时优先。
2. 有目录时列出 MP3/WAV/M4A/OGG/FLAC 等可解码音乐，结合文件名、分类子目录和可用标签判断。能试听就试听；不能试听且命名不明时明确把握有限，不声称已经听过。可读用户提供的曲目说明；不能仅靠文件格式判断音乐情绪。选曲是宿主 agent 的工作，脚本只负责列出、保存和合成。
3. 没有目录时，核实平台曲目/热门使用情况，给出适合题材的歌名与版本。旧热歌歌单不能当成实时排行榜。只取得试听地址不等于已接通可混音音源，不把试听片段循环冒充完整歌曲。
4. 没有可用音源时，向用户说明缺口。用户确认留到平台配乐后，设置 `bgm_mode: "platform"`，导出明确标记“待平台加音乐”的无音轨视频；状态记为 `awaiting_platform_music`，不能声称已完成带 BGM 的成片。普通混音模式缺少音乐仍报错，不悄悄静音。
5. 用户提供音源后恢复混音，保存曲名、版本、来源、选曲理由和文件哈希。发布平台配乐与本地音频分清，不将“可播放”宣称为取得跨平台发布授权。

只有用户明确接受素材库伴奏时才用 Openverse，调用加 `--source openverse`；保留 CC0/CC BY 候选并核实原作者页面和署名信息。该接口能找到音乐，不能证明曲目是平台热歌。

## 执行

脚本路径相对本 skill 根目录；运行时使用实际安装位置，不硬编码开发电脑路径。需要 Python 3.10+、`ffmpeg` 和 `ffprobe`。没有时说明缺少依赖，不自动修改系统安装。

```sh
# 本地目录优先；也可在主题 video.json 中设置 bgm_dir。
python3 scripts/compose_video.py music --topic 主题目录 --bgm-dir /用户/BGM目录

# 只有用户明确接受开放素材库时才启用此来源。
python3 scripts/compose_video.py music --topic 主题目录 --source openverse --query 'pop upbeat'

# 读返回的候选，检查来源后由 agent 选择。不是按第一个结果或随机选择。
python3 scripts/compose_video.py select-music --topic 主题目录 --id 候选ID \
  --story-type '历史科普' --reason '从用户热歌曲库选择，情绪与画面推进匹配'
python3 scripts/compose_video.py plan --topic 主题目录
python3 scripts/compose_video.py render --topic 主题目录
```

`music` 将候选保存到主题 `video/music-candidates.json`，不自动下载所有曲目。`select-music` 验证选中的实际音频，写入 `video.json`，远程音乐保存在 `video/music/`。目录在本主题之外可以作为用户指定音源读取，其他会话数据不作为选曲素材。

已配置接口取得用户要的音频后，用 `music --bgm-dir 音频所在目录` 导入，再选择并合成，补记实际来源与服务返回的使用条件。`select-music` 会恢复 `bgm_mode: "embedded"`。本 skill 不内置热歌供应商客户端，不跨项目搜密钥；生成新音乐的 API 不能代替用户要的现成热门歌曲。

## 视频默认值与配置

主题 `video.json` 可设置 `bgm_dir`、`music_source`（默认 `popular`）、`bgm_mode`（默认 `embedded`）、`seconds_per_image`（默认 3.5 秒）、逐张 `durations`（`{"shot-01": 3}`）、`fps`（默认 24）、`width`/`height`（正偶数）、`bgm_volume`（默认 0.8），以及下文独立的转场和图片运动配置。`bgm` 由选曲命令保存，不手填虚构音频哈希。

- 默认按原漫画比例、最长边 1440 输出，整图等比缩放、留白补齐；避免裁掉中文、缩放推拉让对白移出屏幕。
- 默认每张 3.5 秒，通常按 3–4 秒安排，12 张约 42 秒。首张和结尾可微调，不再按字数把停留拉长到十几秒；用户明确要求慢读再调整。图片可做下文轻微运动，角色本身不生成动作，不添加配音。
- 输出 H.264/yuv420p + AAC/48kHz/双声道 MP4，启用 faststart。整段音乐循环/裁剪到成片长度，统一响度并淡入淡出；优先选适合循环或足够长的片段。
- 图片、音乐哈希及第一步内容版本改变时先报错，防止旧结果悄悄混入新版本。输入错误不交付无声成片；只有用户明确选择平台配乐时输出无音轨版本。
- 混音输出 `video/comic.mp4`，平台待配乐版输出 `video/comic-platform.mp4`；同名 `.json` 保存时间线与来源，`.credits.txt` 保存配乐信息或待配乐说明。重复运行递增 `-v2`，保留已有成片。

导出后用 ffprobe 确认分辨率、比例、总时长和换图间隔；混音版必须有音轨且音画时长一致，平台待配乐版应无音轨并明确标记。检查首张、末张及换图位置，抽查中文不被裁切。具备试听能力时检查听感；只做解码/音量检查时如实说明，不能宣称已试听。

## 转场与图片运动分别配置

默认使用统一交叉淡化，关闭推拉。`transition` 控制两图之间的交接；`motion` 控制单张图片内部的镜头运动，各自可设 `type: "none"` 关闭。示例：

```json
{
  "seconds_per_image": 3.5,
  "transition": {"type": "fade", "duration": 0.5},
  "motion": {"type": "none"}
}
```

- `transition.type` 支持 `none`、`fade`、`fadefast`、`fadeslow`、`dissolve`、`hblur`、`smoothleft`、`smoothright`、`slideleft`、`slideright`、`slideup`、`slidedown`。默认 `fade`；默认时长 0.5 秒，以输出帧率取整，并须短于所有画面的换图间隔。模糊和明显方向运动作为按需选项，不默认每张随机切换。
- `motion.type` 默认为 `none`，画面在转场之外保持静止。只有用户要求推拉时才设 `zoom`，`amount` 默认 0.03（3%），允许范围大于 0 至 0.1。开启后逐张交替缓慢推近、拉远；整张图先等比缩小并预留缩放空间，最大比例时仍完整保留图像边缘。
- 换图间隔与转场时长独立：后一张在时间线记录的 `start` 开始淡入，前一张额外保留转场尾段；首尾总长仍为各张 `duration` 之和。12 张各 3.5 秒、0.5 秒转场仍是 42 秒，不会因 11 次重叠丢掉 5.5 秒，也不额外延长到 47.5 秒。时间线同时记录原换图帧数 `frames` 和含尾段的 `render_frames`。
- 画面内已经有烘焙文字时，文字会随整图轻微缩放，并在转场的短时间内叠化；不能声称已分离字幕。以后有独立字幕层时，再放到图片运动与转场之后叠加。
- 其他转场及轻微推拉保留为按需选项；用户没有要求时，统一使用 `fade/0.5s` 并保持图片静止。

实现会统一各路图片的尺寸、像素比例、帧率、时间基准和起始时间，使用 `zoompan` 做图片运动，`xfade` 做过渡，输入数量较多时限制滤镜线程和解码线程。特效能力以本机 `ffmpeg -h filter=xfade` 为准；不支持时报错，不用硬切伪装转场成功。参考 [FFmpeg xfade](https://ffmpeg.org/ffmpeg-filters.html#xfade) 与 [zoompan](https://ffmpeg.org/ffmpeg-filters.html#zoompan)。

实际检查过渡中间帧包含前后两张内容、正文停留期画面稳定、无黑闪、首末张完整；开启/关闭转场与运动后都应保持时间线总长一致。

## 接口与研究来源

2026-10-22 通过 find-skills 实际检索 `background music`，阅读以下项目；用户试听后明确要求平台热歌，已取消开放素材库的自动兜底：

- [ElevenLabs 官方 music skill](https://github.com/elevenlabs/skills/blob/main/music/SKILL.md)：检索时约 10.2K 安装，官方仓库 500 stars；支持按题材生成纯音乐，但需要本项目的 `ELEVENLABS_API_KEY`，不是无需配置就能调用。
- [ByteDance deer-flow music-generation](https://github.com/bytedance/deer-flow/blob/main/skills/public/music-generation/SKILL.md)：使用 MiniMax 音乐接口，也需要 API Key。
- [Soundfetch](https://github.com/Losera/soundfetch)：演示 Openverse 无凭证检索、保存音频来源及许可的方法；项目较新，未引入其整个依赖链。此处使用标准库直接访问 Openverse 公共音频接口，实际下载来自原提供者。
- [Openverse 开源项目](https://github.com/WordPress/openverse)：`GET https://api.openverse.org/v1/audio/?q=...&license=cc0,by&page_size=20`。匿名接口存在限流和上游失效风险，不保证任何检索都有合适配乐。

平台热歌接口参考：[TikHub 音乐搜索](https://docs.tikhub.io/370212797e0) 提供曲目信息及播放地址，但需要其服务配置，当前未接入；不能把接口文档当成可立即执行的能力。没有本地/服务音源时，按前文约定交付待平台配乐版或向用户索取音源，不再默认寻找安静的素材配乐。
