"""Promote reviewed images into a clean, ordered collection without duplicate copies."""
import hashlib
from pathlib import Path
import re
import tempfile
import zipfile

from image_jobs import locked, read, scoped_path, write

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def safe_title(title):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:60] or "画面"


def finalize_collection(topic, stage=3):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Finalizing images requires Pillow") from exc

    topic = Path(topic).resolve()
    root = topic / "images"
    with locked(root / ".image-jobs.lock"):
        documents, selected, catalogs = {}, [], []
        asset_manifest = root / "manifest.json"
        if asset_manifest.exists():
            catalogs.append(("assets", asset_manifest, read(asset_manifest)["assets"]))
        if stage == 3:
            doc_path = topic / "stage3-prompts.json"
            doc = read(doc_path)
            if hashlib.sha256(scoped_path(topic, doc["source_content"]).read_bytes()).hexdigest() != doc["source_sha256"]:
                raise ValueError("Content changed; update the comic before exporting")
            manifest = root / "comic-manifest.json"
            shots = read(manifest)["shots"]
            ids, selected_ids = [s["id"] for s in doc["shots"]], [s["id"] for s in shots]
            if not ids or len(set(ids)) != len(ids) or len(set(selected_ids)) != len(selected_ids) or set(ids) != set(selected_ids):
                raise ValueError("Comic frames are missing or duplicated")
            by_id = {s["id"]: s for s in shots}
            ordered = [dict(by_id[s["id"]], collection_title=s["title"]) for s in doc["shots"]]
            catalogs.append(("comic", manifest, ordered))
            documents[doc_path] = doc
        elif stage != 2:
            raise ValueError("Stage must be 2 or 3")
        if not catalogs:
            raise ValueError("No image assets to finalize")
        for path in (topic / "stage3-prompts.json", root / "comic-manifest.json"):
            if path.exists() and path not in documents:
                documents[path] = read(path)

        pixel_hashes = set()
        folders = []
        for kind, path, entries in catalogs:
            documents[path] = read(path)
            for relative in (kind, "candidates/" + kind, "not-selected/" + kind):
                folder = root / relative
                if not folder.resolve().is_relative_to(topic):
                    raise ValueError("Image folder is outside the topic")
            folders.extend([root / kind, root / "candidates" / kind])
            for index, item in enumerate(entries, 1):
                if not (item.get("status") == "provided" and kind == "assets"):
                    if item.get("status") != "generated" or item.get("review") != "passed":
                        raise ValueError("Image has not passed review: " + item["id"])
                source = scoped_path(topic, "images/" + item["file"])
                if source.parent not in {root / kind, root / "candidates" / kind}:
                    raise ValueError("Selected image must be in its managed image folder")
                if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("Image changed: " + item["id"])
                with Image.open(source) as picture:
                    pixels = picture.convert("RGBA")
                    signature = hashlib.sha256(str(pixels.size).encode() + pixels.tobytes()).hexdigest()
                if kind == "comic":
                    if signature in pixel_hashes:
                        raise ValueError("Duplicate comic image: " + item["id"])
                    pixel_hashes.add(signature)
                prefix = f"{index:0{max(3, len(str(len(entries))))}d}" if kind == "comic" else item["id"]
                title = item.get("collection_title", item.get("name", item["id"]))
                dest = root / kind / (prefix + "-" + safe_title(title) + source.suffix.lower())
                selected.append(dict(kind=kind, item=item, source=source, dest=dest))
        sources = [s["source"] for s in selected]
        if not sources:
            raise ValueError("No image assets to finalize")
        if len(set(sources)) != len(sources):
            raise ValueError("Multiple assets selected the same file")

        # Move through a temporary directory; every move can be reversed on failure.
        stage_dir = Path(tempfile.mkdtemp(dir=root, prefix=".collection-"))
        moves, mapping, backups = [], {}, {}

        def move(source, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ValueError("Refusing to overwrite an image: " + str(dest))
            source.rename(dest)
            moves.append((source, dest))

        def remember(source, dest):
            old, new = str(source.relative_to(topic)), str(dest.relative_to(topic))
            mapping[old] = new
            mapping[str(source)] = str(dest)
            mapping[str(source.relative_to(root))] = str(dest.relative_to(root))

        def save(path, data):
            backups.setdefault(path, path.read_bytes() if path.exists() else None)
            if isinstance(data, (dict, list)):
                write(path, data)
            elif isinstance(data, str):
                path.write_text(data, encoding="utf-8")
            else:
                path.write_bytes(data)

        try:
            for index, entry in enumerate(selected):
                entry["temporary"] = stage_dir / (str(index) + entry["source"].suffix)
                move(entry["source"], entry["temporary"])
                remember(entry["source"], entry["dest"])
            archived = 0
            for folder in folders:
                if not folder.exists():
                    continue
                kind = folder.name
                for source in sorted(folder.iterdir()):
                    if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    dest = root / "not-selected" / kind / source.name
                    version = 2
                    while dest.exists():
                        dest = dest.with_name(f"{source.stem}-archived-{version}{source.suffix}")
                        version += 1
                    move(source, dest)
                    remember(source, dest)
                    archived += 1
            for entry in selected:
                move(entry["temporary"], entry["dest"])

            text_mapping = {k: v for k, v in mapping.items() if k.startswith("images/") or Path(k).is_absolute()}
            pattern = re.compile("|".join(re.escape(k) for k in sorted(text_mapping, key=len, reverse=True)))

            def remap(value):
                if isinstance(value, dict):
                    return {k: remap(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [remap(v) for v in value]
                if isinstance(value, str):
                    return mapping.get(value, pattern.sub(lambda match: text_mapping[match[0]], value))
                return value

            for path, data in documents.items():
                updated = remap(data)
                if path.name == "stage3-prompts.json" and stage == 3:
                    current = {e["item"]["id"]: e for e in selected if e["kind"] == "comic"}
                    for shot in updated["shots"]:
                        shot.update(file=str(current[shot["id"]]["dest"].relative_to(topic)),
                                    status="generated", review="passed")
                save(path, updated)
            for relative in ("assets.md", "stage3-prompts.md", "images/preview.md", "images/comic-preview.md"):
                path = topic / relative
                if path.exists():
                    save(path, remap(path.read_text(encoding="utf-8")))
            entries = [dict(id=e["item"]["id"], title=e["item"].get("collection_title", e["item"].get("name", "")),
                            file=str(e["dest"].relative_to(topic)), sha256=e["item"]["sha256"])
                       for e in selected if e["kind"] == ("comic" if stage == 3 else "assets")]
            catalog = dict(stage=stage, images=entries, archived_this_run=archived,
                           moves={k: v for k, v in mapping.items() if k.startswith("images/")})
            collection_path = root / ("comic-collection.json" if stage == 3 else "asset-collection.json")
            if collection_path.exists():
                previous = {k: mapping.get(v, v) for k, v in read(collection_path).get("moves", {}).items()}
                catalog["moves"] = {**previous, **catalog["moves"]}
            save(collection_path, catalog)
            if stage == 3:
                preview = "# 漫画图片集\n\n按剧情顺序排列，每张仅保留当前通过检查的版本。\n\n"
                for i, item in enumerate(entries, 1):
                    preview += f"## {i:03d} {item['title']}\n\n![{item['title']}]({topic / item['file']})\n\n"
                save(root / "comic-preview.md", preview)
                archive = stage_dir / "comic.zip"
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
                    for item in entries:
                        package.write(topic / item["file"], arcname=Path(item["file"]).name)
                save(root / "comic.zip", archive.read_bytes())
                archive.unlink()
        except Exception:
            for old, current in reversed(moves):
                if current.exists():
                    current.rename(old)
            for path, data in backups.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            raise
        finally:
            # Never remove a staging directory that still contains recoverable source images.
            if not any(stage_dir.iterdir()):
                stage_dir.rmdir()
        return catalog
