"""Inventory/select BGM, then render the selected comic frames with FFmpeg."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from image_jobs import read, scoped_path, write

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
OPENVERSE = "https://api.openverse.org/v1/audio/"
TRANSITIONS = {"none", "fade", "fadefast", "fadeslow", "dissolve", "hblur",
               "smoothleft", "smoothright", "slideleft", "slideright", "slideup", "slidedown"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                             "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def audio_duration(path):
    info = probe(path)
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise ValueError("BGM 文件中没有音轨")
    seconds = float(info["format"]["duration"])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("BGM 时长无效")
    return seconds


def fetch(url, limit=32 * 1024 * 1024):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("音乐下载仅支持 HTTPS")
    req = urllib.request.Request(url, headers={"User-Agent": "brain-comic/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("音乐响应超过大小限制")
    return data


def music_candidates(topic, bgm_dir=None, query=None, source=None):
    topic = Path(topic).resolve()
    config = read(topic / "video.json") if (topic / "video.json").exists() else {}
    folder = bgm_dir or config.get("bgm_dir")
    candidates = []
    if folder:
        folder = (topic / Path(folder).expanduser()).resolve()
        if not folder.is_dir():
            raise ValueError("BGM 文件夹不存在；请修正路径")
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                seconds = audio_duration(path)
            except (ValueError, subprocess.CalledProcessError):
                continue
            candidates.append(dict(id=str(len(candidates) + 1), title=path.stem,
                                   file=str(path), relative_name=str(path.relative_to(folder)),
                                   duration_seconds=seconds, route="local-folder",
                                   tags=probe(path)["format"].get("tags", {})))
        if not candidates:
            raise ValueError("BGM 文件夹里没有可解码的音频；未自动改用网络来源")
        source = str(folder)
        config["bgm_dir"] = source
        write(topic / "video.json", config)
    else:
        source_mode = source or config.get("music_source", "popular")
        if source_mode != "openverse":
            raise ValueError("当前使用平台热歌模式：请提供 BGM 文件夹或已配置的热歌音源。"
                             "素材库伴奏仅在用户明确选择 --source openverse 时检索")
        if not query:
            raise ValueError("没有 BGM 文件夹时，请由 agent 根据故事提供 --query 检索词")
        params = urllib.parse.urlencode(dict(q=query, license="cc0,by", page_size=20))
        source = OPENVERSE + "?" + params
        response = json.loads(fetch(source, limit=2 * 1024 * 1024))
        for item in response.get("results", []):
            if item.get("license") not in {"cc0", "by"} or (item.get("duration") or 0) < 20000:
                continue
            candidates.append({**item, "duration_seconds": item["duration"] / 1000,
                               "route": "openverse"})
        if not candidates:
            raise ValueError("没有合适的音乐候选；请调整检索词，不生成无声成片")
    write(topic / "video/music-candidates.json", dict(source=source, query=query,
                                                     candidates=candidates))
    return candidates


def select_music(topic, candidate_id, reason, story_type):
    topic = Path(topic).resolve()
    candidates = read(topic / "video/music-candidates.json")["candidates"]
    item = next((c for c in candidates if c["id"] == candidate_id), None)
    if not item:
        raise ValueError("音乐候选不存在；请先检索/列出文件夹")
    if not reason.strip() or not story_type.strip():
        raise ValueError("请记录故事类型和选曲理由")
    if item["route"] == "openverse":
        if item.get("license") not in {"cc0", "by"}:
            raise ValueError("该候选未使用支持的音乐许可")
        if not re.fullmatch(r"[a-zA-Z0-9-]+", candidate_id):
            raise ValueError("无效音乐 ID")
        folder = topic / "video/music"
        folder.mkdir(parents=True, exist_ok=True)
        # Probe the downloaded audio before promoting it to a usable BGM file.
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            downloaded = Path(temp) / "download.audio"
            downloaded.write_bytes(fetch(item["url"]))
            seconds = audio_duration(downloaded)
            path = folder / (candidate_id + ".audio")
            if path.exists() and digest(path) != digest(downloaded):
                raise ValueError("同一音乐 ID 的内容已改变；保留旧文件，请先检查来源")
            if not path.exists():
                shutil.copyfile(downloaded, path)
        stored_file = str(path.relative_to(topic))
    else:
        path = Path(item["file"]).resolve()
        seconds = audio_duration(path)
        stored_file = str(path.relative_to(topic)) if path.is_relative_to(topic) else str(path)
    config_path = topic / "video.json"
    config = read(config_path) if config_path.exists() else {}
    config["story_type"] = story_type
    config["bgm_mode"] = "embedded"
    config["bgm"] = {**item, "file": stored_file, "sha256": digest(path),
                     "duration_seconds": seconds, "reason": reason}
    write(config_path, config)
    return config["bgm"]


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " 必须为正数")
    return value


def plan_video(topic):
    topic = Path(topic).resolve()
    doc = read(topic / "stage3-prompts.json")
    manifest_path = topic / "images/comic-manifest.json"
    manifest = read(manifest_path)
    config = read(topic / "video.json")
    if digest(scoped_path(topic, doc["source_content"])) != doc["source_sha256"]:
        raise ValueError("第一步内容已改变，请先更新后续画面")
    source_shots = doc["shots"]
    source_ids = [s["id"] for s in source_shots]
    selected_ids = [s["id"] for s in manifest["shots"]]
    if (not source_ids or len(set(source_ids)) != len(source_ids)
            or len(set(selected_ids)) != len(selected_ids) or set(source_ids) != set(selected_ids)):
        raise ValueError("漫画画面缺失或编号重复")
    selected = {s["id"]: s for s in manifest["shots"]}
    fps = config.get("fps", 24)
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 60:
        raise ValueError("fps 必须是 1–60 的整数")
    seconds_per_image = positive(config.get("seconds_per_image", 3.5), "默认换图间隔")
    overrides = config.get("durations", {})
    if set(overrides) - set(source_ids):
        raise ValueError("时长配置含有不存在的画面 ID")
    timeline, cursor = [], 0
    for shot in source_shots:
        chosen = selected[shot["id"]]
        if chosen.get("status") != "generated" or chosen.get("review") != "passed":
            raise ValueError("存在未生成或未检查的画面：" + shot["id"])
        path = scoped_path(topic, "images/" + chosen["file"])
        if digest(path) != chosen["sha256"]:
            raise ValueError("漫画图片已改变：" + shot["id"])
        seconds = overrides.get(shot["id"], seconds_per_image)
        frames = max(1, math.ceil(positive(seconds, "画面时长") * fps))
        timeline.append(dict(id=shot["id"], title=shot.get("title", shot["id"]),
                             file=str(path.relative_to(topic)), sha256=chosen["sha256"],
                             start=cursor / fps, duration=frames / fps, frames=frames))
        cursor += frames
    bgm = config.get("bgm", {})
    bgm_mode = config.get("bgm_mode", "embedded")
    if bgm_mode not in {"embedded", "platform"}:
        raise ValueError("bgm_mode 必须为 embedded 或 platform")
    if bgm_mode == "embedded":
        if not bgm.get("file"):
            raise ValueError("未选择 BGM；请先列出候选并选择音乐")
        bgm_path = (topic / Path(bgm["file"]).expanduser()).resolve()
        if not bgm_path.is_file() or digest(bgm_path) != bgm.get("sha256"):
            raise ValueError("BGM 不存在或内容已改变；请重新选择")
        audio_duration(bgm_path)
    first = next(s for s in probe(topic / timeline[0]["file"])["streams"] if s["codec_type"] == "video")
    width = config.get("width", round(1440 * first["width"] / max(first["width"], first["height"]) / 2) * 2)
    height = config.get("height", round(1440 * first["height"] / max(first["width"], first["height"]) / 2) * 2)
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 2 or n % 2 for n in (width, height)):
        raise ValueError("视频宽高必须是正的偶数")
    volume = positive(config.get("bgm_volume", 0.8), "BGM 音量")
    if volume > 2:
        raise ValueError("BGM 音量不能大于 2")
    transition = config.get("transition", {})
    transition_type = transition.get("type", "fade")
    if transition_type not in TRANSITIONS:
        raise ValueError("不支持的转场类型")
    transition_frames = 0
    if transition_type != "none" and len(timeline) > 1:
        transition_frames = max(1, round(positive(transition.get("duration", 0.5), "转场时长") * fps))
        if transition_frames >= min(s["frames"] for s in timeline):
            raise ValueError("转场时长必须短于每张画面的换图间隔")
    motion = config.get("motion", {})
    motion_type = motion.get("type", "none")
    if motion_type not in {"none", "zoom"}:
        raise ValueError("图片运动仅支持 none 或 zoom")
    amount = positive(motion.get("amount", 0.03), "缩放幅度") if motion_type == "zoom" else 0
    if amount > 0.1:
        raise ValueError("漫画缩放幅度不能超过 10%")
    for i, shot in enumerate(timeline):
        # Outgoing overlap extends each source clip, preserving the original change interval and total duration.
        shot["render_frames"] = shot["frames"] + (transition_frames if i < len(timeline) - 1 else 0)
    return dict(stage=4, story_type=config.get("story_type", ""), fps=fps, width=width,
                height=height, duration=cursor / fps, timeline=timeline, bgm=bgm,
                bgm_mode=bgm_mode, bgm_volume=volume, source_manifest_sha256=digest(manifest_path),
                transition=dict(type=transition_type, duration=transition_frames / fps),
                motion=dict(type=motion_type, amount=amount))


def image_filter(plan, shot, index):
    width, height = plan["width"], plan["height"]
    amount = plan["motion"]["amount"]
    if plan["motion"]["type"] == "none":
        return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1")
    # Fit the entire image inside the maximum zoom; text at the edges is never cropped.
    fit_w = max(2, math.floor(width * 2 / (1 + amount) / 2) * 2)
    fit_h = max(2, math.floor(height * 2 / (1 + amount) / 2) * 2)
    fraction = f"on/{max(1, shot['render_frames'] - 1)}"
    zoom = f"1+{amount}*({fraction})" if index % 2 == 0 else f"1+{amount}*(1-{fraction})"
    return (f"scale={fit_w}:{fit_h}:force_original_aspect_ratio=decrease,"
            f"pad={width*2}:{height*2}:(ow-iw)/2:(oh-ih)/2:color=white,"
            f"zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
            f"d=1:s={width}x{height}:fps={plan['fps']},setsar=1")


def join_images(plan, temp):
    clips = [temp / f"{i:04d}.mp4" for i in range(len(plan["timeline"]))]
    if plan["transition"]["duration"] == 0:
        concat = temp / "clips.txt"
        concat.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
        return ["-f", "concat", "-safe", "1", "-i", str(concat)]
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-filter_complex_threads", "1"]
    filters = []
    for i, clip in enumerate(clips):
        command += ["-threads", "1", "-i", str(clip)]
        filters.append(f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps={plan['fps']},format=yuv420p[v{i}]")
    previous = "v0"
    for i in range(1, len(clips)):
        current = f"joined{i}"
        filters.append(f"[{previous}][v{i}]xfade=transition={plan['transition']['type']}:"
                       f"duration={plan['transition']['duration']}:offset={plan['timeline'][i]['start']}[{current}]")
        previous = current
    output = temp / "transitions.mp4"
    command += ["-filter_complex", ";".join(filters), "-map", f"[{previous}]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "20",
                "-pix_fmt", "yuv420p", "-threads", "2", "-r", str(plan["fps"]),
                "-frames:v", str(sum(s["frames"] for s in plan["timeline"])), str(output)]
    print("合成柔和转场…", flush=True)
    subprocess.run(command, check=True)
    return ["-i", str(output)]


def render_video(topic):
    topic = Path(topic).resolve()
    plan = plan_video(topic)
    folder = topic / "video"
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder, prefix=".render-") as temp:
        temp = Path(temp)
        for index, shot in enumerate(plan["timeline"]):
            print(f"合成画面 {index + 1}/{len(plan['timeline'])}：{shot['title']}", flush=True)
            filters = image_filter(plan, shot, index)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                            "-loop", "1", "-framerate", str(plan["fps"]), "-i", str(topic / shot["file"]),
                            "-frames:v", str(shot["render_frames"]), "-vf", filters, "-an", "-c:v", "libx264",
                            "-preset", "veryfast", "-tune", "stillimage", "-crf", "20", "-pix_fmt", "yuv420p",
                            "-threads", "2", str(temp / f"{index:04d}.mp4")], check=True)
        image_input = join_images(plan, temp)
        duration = plan["duration"]
        fade = min(2, duration / 2)
        afilter = (f"loudnorm=I=-21:TP=-2:LRA=11,volume={plan['bgm_volume']},alimiter=limit=0.89:level=false,"
                   f"afade=t=in:d={min(1, duration / 2)},afade=t=out:st={duration-fade}:d={fade}")
        bgm = plan["bgm"]
        platform_music = plan["bgm_mode"] == "platform"
        if platform_music:
            credits = "尚未添加 BGM：请在发布平台选择热门歌曲。此文件不含音轨。\n"
        else:
            credits = bgm.get("attribution") or f"BGM: {bgm['title']} (user-provided)"
            if bgm.get("creator_url"):
                credits += "\n" + bgm["creator_url"]
            credits += "\nSource: " + bgm.get("foreign_landing_url", bgm["file"])
            credits += "\nProcessing: trimmed/looped, loudness adjusted, fade in/out.\n"
        pending = temp / "comic.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n"] + image_input
        if platform_music:
            command += ["-map", "0:v:0", "-c:v", "copy", "-an"]
        else:
            command += ["-stream_loop", "-1", "-i", str((topic / Path(bgm["file"]).expanduser()).resolve()),
                        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", afilter,
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        command += ["-t", str(duration), "-metadata", "comment=" + credits, "-movflags", "+faststart", str(pending)]
        subprocess.run(command, check=True)
        info = probe(pending)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
        if (abs(float(video["duration"]) - duration) > 1 / plan["fps"] + 0.01
                or (platform_music and audio is not None)
                or (not platform_music and (audio is None or abs(float(audio["duration"]) - duration) > 0.1))):
            raise ValueError("合成音画时长不一致；未保存为成片")
        # Atomic link within the same filesystem preserves old exports and avoids partial MP4 files.
        version = 1
        stem = "comic-platform" if platform_music else "comic"
        while True:
            output = folder / (f"{stem}.mp4" if version == 1 else f"{stem}-v{version}.mp4")
            try:
                os.link(pending, output)
                break
            except FileExistsError:
                version += 1
        plan.update(file=str(output.relative_to(topic)), sha256=digest(output),
                    video_codec=video["codec_name"], audio_codec=audio["codec_name"] if audio else None,
                    status="awaiting_platform_music" if platform_music else "generated")
        write(output.with_suffix(".json"), plan)
        output.with_suffix(".credits.txt").write_text(credits, encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("music", "select-music", "plan", "render"):
        command = commands.add_parser(name)
        command.add_argument("--topic", required=True, type=Path)
        if name == "music":
            command.add_argument("--bgm-dir")
            command.add_argument("--query")
            command.add_argument("--source", choices=("popular", "openverse"))
        elif name == "select-music":
            command.add_argument("--id", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--story-type", required=True)
    args = parser.parse_args()
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise ValueError("请先安装 ffmpeg 和 ffprobe")
        topic = args.topic.resolve()
        if args.command == "music":
            candidates = music_candidates(topic, args.bgm_dir, args.query, args.source)
            fields = ("id", "title", "creator", "relative_name", "duration_seconds", "license",
                      "foreign_landing_url", "tags", "route")
            result = [{key: item[key] for key in fields if key in item} for item in candidates]
        elif args.command == "select-music":
            result = select_music(topic, args.id, args.reason, args.story_type)
        elif args.command == "plan":
            result = plan_video(topic)
        else:
            result = str(render_video(topic))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print("视频制作失败：" + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
