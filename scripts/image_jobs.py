"""Shared image task preparation and result storage; the host invokes its API/tool."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile

SKILL_ROOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r"\{\{ref:([CGSP]\d{2})\}\}")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".image-jobs-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def scoped_path(topic, relative):
    path = (topic / relative).resolve()
    if not path.is_relative_to(topic) or not path.is_file():
        raise ValueError("Reference image is missing or outside the topic")
    return path


def update_summary(state, key):
    entries = state[key]
    summary = state.setdefault("summary", {})
    summary.update(requested=max(summary.get("requested", 0), len(entries)),
                   generated=sum(e.get("status") == "generated" for e in entries),
                   provided=sum(e.get("status") == "provided" for e in entries),
                   failed=sum(e.get("status") == "failed" for e in entries))


def style_prompt(style_id):
    config = read(SKILL_ROOT / "references/styles.json")
    selected = style_id or config["default"]
    for item in config["presets"]:
        if item["id"] == selected:
            return selected, item["prompt"]
    raise ValueError("Unknown style_id")


def prepare(topic, stage):
    topic = Path(topic).resolve()
    assets_path = topic / "images/manifest.json"
    assets = read(assets_path) if assets_path.exists() else {"assets": []}
    catalog = {a["id"]: a for a in assets["assets"]}
    if stage == 2:
        style_id, style = style_prompt(assets.get("style_id"))
        rules = (SKILL_ROOT / "references/asset-extraction.md").read_text()
        prefixes = {k: re.search(r"<!-- " + k + r"_PREFIX -->\s*```text\n(.*?)\n```", rules, re.S)[1]
                    for k in "CGSP"}
        labels = {"C": "角色描述：", "G": "群像描述：", "S": "场景描述：", "P": "道具描述："}
        entries = []
        for aid, name, body in re.findall(r"^### ([CGSP]\d{2})｜([^\n]+)\n(.*?)(?=^### |^## |\Z)",
                                         (topic / "assets.md").read_text(), re.M | re.S):
            supplied = catalog.get(aid, {})
            if supplied.get("origin") == "user_provided":
                image = scoped_path(topic, "images/" + supplied["file"])
                if hashlib.sha256(image.read_bytes()).hexdigest() != supplied["sha256"]:
                    raise ValueError("User character reference changed; import the new version first")
                continue
            frames = re.search(r"出现画面：(.+?)（共", body)[1].split("、")
            if len(set(frames)) < 2:
                raise ValueError("An asset must appear in at least two content frames")
            description = re.search(r"资产自身描述：\n\n(.*?)\n\n最终生成提示词：", body, re.S)[1]
            prompt = "\n\n".join([style, prefixes[aid[0]], labels[aid[0]] + "\n" + description])
            entries.append(dict(id=aid, title=name, prompt=prompt, references=[]))
        mode, folder = "text_to_image", "candidates/assets"
    elif stage == 3:
        doc = read(topic / "stage3-prompts.json")
        if hashlib.sha256(scoped_path(topic, doc["source_content"]).read_bytes()).hexdigest() != doc["source_sha256"]:
            raise ValueError("Content has changed; regenerate the shot prompts first")
        style_id, style = style_prompt(doc["style_id"])
        entries = []
        for shot in doc["shots"]:
            references = shot["references"]
            ids = [r["asset_id"] for r in references]
            if len(ids) != len(set(ids)) or [r["image_index"] for r in references] != list(range(1, len(ids) + 1)):
                raise ValueError("Duplicate references or inconsistent image order")
            if set(MARKER.findall(shot["prompt_template"])) != set(ids):
                raise ValueError("Prompt markers and reference images do not match")
            resolved, character_rules = [], []
            for ref in references:
                aid = ref["asset_id"]
                asset = catalog[aid]
                expected = "images/" + asset["file"]
                if asset["status"] not in {"generated", "provided"} or ref["file"] != expected:
                    raise ValueError("Stale or unsuccessful reference image")
                image = scoped_path(topic, ref["file"])
                digest = hashlib.sha256(image.read_bytes()).hexdigest()
                if digest != ref["sha256"]:
                    raise ValueError("Reference image changed; recompile the shot bindings")
                resolved.append(dict(ref, path=str(image)))
                if asset.get("origin") == "user_provided":
                    if digest != asset["sha256"]:
                        raise ValueError("User character reference changed; import the new version first")
                    character_rules.append(
                        f"图{ref['image_index']}是用户指定的IP角色「{asset['name']}」。"
                        "以该实际参考图锁定角色身份，保留物种、脸型、五官、发型、标志、配色及身体比例；"
                        "服装默认沿用参考，用户明确指定的换装除外。姿势、表情按本张剧情变化，"
                        "不换成新设计的人物，不把非人角色改成人类。"
                        + ("已确认的外观：" + asset["description"] + "。" if asset.get("description") else "")
                        + ("用户锁定特征：" + "；".join(asset["locked_features"]) + "。" if asset.get("locked_features") else ""))
            lookup = {r["asset_id"]: "图" + str(r["image_index"]) for r in references}
            character_lock = ("\n\n用户指定人物约束（优先于默认画风和泛化人物描述，"
                              "不因Q版默认比例重塑该角色）：\n" + "\n".join(character_rules)) if character_rules else ""
            prompt = style + character_lock + "\n\n" + MARKER.sub(lambda m: lookup[m[1]], shot["prompt_template"])
            entries.append(dict(id=shot["id"], title=shot["title"], prompt=prompt, references=resolved))
        mode, folder = "image_to_image", "candidates/comic"
    else:
        raise ValueError("Stage must be 2 or 3")
    jobs = []
    for entry in entries:
        refs = entry["references"]
        identity = dict(prompt=entry["prompt"], references=[{k: r[k] for k in ("asset_id", "image_index", "sha256")} for r in refs])
        jobs.append(dict(entry, stage=stage, mode=mode, style_id=style_id, topic=str(topic),
                         reference_images=[r["path"] for r in refs], output_dir="images/" + folder,
                         job_key=hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()))
    return jobs


def pack_references(job, max_images):
    """Package input references without redrawing or changing source image pixels."""
    if max_images < 1:
        raise ValueError("max_images must be positive")
    if len(job["reference_images"]) <= max_images:
        return job
    from PIL import Image, ImageDraw, ImageFont

    groups = [[r] for r in job["references"]]
    while len(groups) > max_images:
        pair = None
        for category in "CSGP":
            candidates = [i for i, group in enumerate(groups) if all(r["asset_id"].startswith(category) for r in group)]
            if len(candidates) >= 2:
                pair = candidates[:2]
                break
        first, second = pair or (0, 1)
        groups[first] += groups.pop(second)
    topic = Path(job["topic"])
    pack_dir = topic / "images/reference-packs"
    pack_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default(size=28)
    labels, paths, logical, inputs = {}, [], [], []
    for input_index, group in enumerate(groups, 1):
        regions = []
        if len(group) == 1:
            path = Path(group[0]["path"])
        else:
            signature = hashlib.sha256(json.dumps([(r["asset_id"], r["sha256"]) for r in group]).encode()).hexdigest()[:16]
            path = pack_dir / (signature + ".png")
            if not path.exists():
                images = []
                for ref in group:
                    with Image.open(ref["path"]) as image:
                        images.append(image.convert("RGB"))
                canvas = Image.new("RGB", (max(im.width for im in images), sum(im.height + 48 for im in images)), "white")
                draw = ImageDraw.Draw(canvas)
                top = 0
                for region_index, (ref, image) in enumerate(zip(group, images)):
                    region = chr(65 + region_index)
                    draw.text((12, top + 7), f"REF-{region}  {ref['asset_id']}", fill="black", font=font)
                    canvas.paste(image, (0, top + 48))
                    top += image.height + 48
                canvas.save(path)
        for region_index, ref in enumerate(group):
            region = chr(65 + region_index) if len(group) > 1 else None
            labels[ref["image_index"]] = f"图{input_index}" + (f"的{region}区域" if region else "")
            logical.append(dict(ref, input_image_index=input_index, region=region))
            regions.append(dict(asset_id=ref["asset_id"], region=region))
        paths.append(str(path))
        inputs.append(dict(image_index=input_index, file=str(path.relative_to(topic)), regions=regions,
                           sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    prompt = re.sub(r"图(\d+)", lambda m: labels.get(int(m[1]), m[0]), job["prompt"])
    prompt = ("输入说明：部分参考图是带REF-A、REF-B等区名的素材板，每个区域都是独立资产。"
              "区域标签只用于识别，不要把标签、素材板排版或全部人物照搬到漫画中。\n\n" + prompt)
    identity = dict(prompt=prompt, inputs=inputs)
    return dict(job, prompt=prompt, references=logical, reference_images=paths, input_images=inputs,
                job_key=hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest())


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def record(job, source, *, route, review="pending"):
    """Copy a selected generated file and atomically update the stage's manifest."""
    if review not in {"pending", "passed", "needs_revision"}:
        raise ValueError("Invalid review state")
    if not re.fullmatch(r"(?:[CGSP]\d{2}|shot-\d{2})", job["id"]):
        raise ValueError("Invalid image ID")
    topic = Path(job["topic"]).resolve()
    kind = "assets" if job["stage"] == 2 else "comic"
    if job["output_dir"] not in {"images/" + kind, "images/candidates/" + kind}:
        raise ValueError("Invalid output directory")
    root = topic / "images"
    manifest = root / ("manifest.json" if job["stage"] == 2 else "comic-manifest.json")
    key = "assets" if job["stage"] == 2 else "shots"
    data = Path(source).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        raise ValueError("Expected a generated PNG file")
    width, height = struct.unpack(">II", data[16:24])
    with locked(root / ".image-jobs.lock"):
        state = read(manifest) if manifest.exists() else {key: []}
        previous = next((x for x in state[key] if x["id"] == job["id"]), None)
        if job["stage"] == 2 and previous and previous.get("origin") == "user_provided":
            raise ValueError("Cannot replace a user-provided character with an autogenerated asset")
        folder = root / ("not-selected" if review == "needs_revision" else "candidates") / kind
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (job["id"] + ".png")
        version = 2
        while dest.exists():
            dest = folder / f"{job['id']}-v{version}.png"
            version += 1
        shutil.copy2(source, dest)
        result = dict(previous or {}, id=job["id"], name=job["title"], status="generated",
                      file=str(dest.relative_to(root)), review=review, style_id=job["style_id"],
                      generation_prompt=job["prompt"], references=job["references"],
                      reference_images=job["reference_images"], input_images=job.get("input_images", []),
                      mode=job["mode"],
                      job_key=job["job_key"], route=route, width=width, height=height,
                      sha256=hashlib.sha256(data).hexdigest())
        if job.get("provider"):
            result["provider"] = job["provider"]
        if previous:
            result["previous_files"] = previous.get("previous_files", []) + [previous["file"]]
        state[key] = [x for x in state[key] if x["id"] != job["id"]] + [result]
        state[key].sort(key=lambda x: x["id"])
        update_summary(state, key)
        state.update(stage=job["stage"], style_id=job["style_id"], route=route,
                     updated_at=datetime.now(timezone.utc).isoformat())
        write(manifest, state)
    return str(dest)


def import_character(topic, source, asset_id, name, *, description="", locked_features=None):
    """Copy an explicitly supplied character reference unchanged into this topic."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Importing character images requires Pillow") from exc

    if not re.fullmatch(r"C\d{2}", asset_id) or not name.strip():
        raise ValueError("A character needs a C01-style ID and a name")
    source, topic = Path(source).resolve(), Path(topic).resolve()
    with Image.open(source) as picture:
        extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(picture.format)
        if extension is None or getattr(picture, "n_frames", 1) != 1:
            raise ValueError("Use a still PNG, JPEG or WebP character reference")
        width, height = picture.size
        picture.verify()
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    root = topic / "images"
    manifest = root / "manifest.json"
    with locked(root / ".image-jobs.lock"):
        state = read(manifest) if manifest.exists() else {"assets": []}
        previous = next((a for a in state["assets"] if a["id"] == asset_id), None)
        folder = root / "assets"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / (asset_id + "-provided" + extension)
        version = 2
        while dest.exists():
            dest = folder / f"{asset_id}-provided-v{version}{extension}"
            version += 1
        dest.write_bytes(data)
        entry = dict(id=asset_id, name=name.strip(), status="provided", origin="user_provided",
                     file=str(dest.relative_to(root)), source_name=source.name, sha256=digest,
                     source_sha256=digest, width=width, height=height,
                     description=description, locked_features=locked_features or [], route="user:reference")
        if previous:
            entry["previous_files"] = previous.get("previous_files", []) + [previous["file"]]
        state["assets"] = sorted([a for a in state["assets"] if a["id"] != asset_id] + [entry], key=lambda a: a["id"])
        update_summary(state, "assets")
        state.update(stage=2, updated_at=datetime.now(timezone.utc).isoformat())
        write(manifest, state)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--topic", type=Path, required=True)
    prep.add_argument("--stage", type=int, choices=(2, 3), required=True)
    prep.add_argument("--max-reference-images", type=int, help="Pack references only for a host with an input limit")
    save = commands.add_parser("record")
    save.add_argument("--source", type=Path, required=True)
    save.add_argument("--route", required=True)
    save.add_argument("--review", choices=("pending", "passed", "needs_revision"), default="pending")
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--topic", type=Path, required=True)
    finalize.add_argument("--stage", type=int, choices=(2, 3), default=3)
    supplied = commands.add_parser("import-character")
    supplied.add_argument("--topic", type=Path, required=True)
    supplied.add_argument("--source", type=Path, required=True)
    supplied.add_argument("--id", required=True)
    supplied.add_argument("--name", required=True)
    supplied.add_argument("--description", default="")
    supplied.add_argument("--lock", action="append", default=[], help="Stable identity feature; repeat as needed")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.topic, args.stage)
            if args.max_reference_images is not None:
                result = [pack_references(job, args.max_reference_images) for job in result]
        elif args.command == "finalize":
            from image_collection import finalize_collection
            result = finalize_collection(args.topic, args.stage)
        elif args.command == "import-character":
            result = import_character(args.topic, args.source, args.id, args.name,
                                      description=args.description, locked_features=args.lock)
        else:
            result = record(json.load(sys.stdin), args.source, route=args.route, review=args.review)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Image task failed: {type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
