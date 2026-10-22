import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from image_jobs import import_character, locked, pack_references, prepare, record, write
from image_collection import finalize_collection


class ImageJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.topic = Path(self.temp.name)
        self.image = self.topic / "images/assets/C01.png"
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + struct.pack(">II", 100, 120))
        (self.topic / "content.md").write_text("test story")
        digest = hashlib.sha256(self.image.read_bytes()).hexdigest()
        write(self.topic / "images/manifest.json", dict(style_id="2d-chibi-comic", assets=[
            dict(id="C01", name="Hero", status="generated", file="assets/C01.png")]))
        self.doc = dict(style_id="2d-chibi-comic", source_content="content.md",
                        source_sha256=hashlib.sha256(b"test story").hexdigest(), shots=[
                            dict(id="shot-01", title="Test", prompt_template="{{ref:C01}} and {{ref:C01}}",
                                 references=[dict(asset_id="C01", marker="{{ref:C01}}", image_index=1,
                                                  file="images/assets/C01.png", sha256=digest)])])
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        write(self.topic / "stage3-prompts.json", self.doc)

    def test_compiles_repeated_marker_and_passes_actual_image_once(self):
        job = prepare(self.topic, 3)[0]
        self.assertEqual(job["reference_images"], [str(self.image.resolve())])
        self.assertTrue(job["prompt"].endswith("图1 and 图1"))
        self.assertEqual(job["mode"], "image_to_image")

    def test_rejects_inconsistent_upload_order(self):
        self.doc["shots"][0]["references"][0]["image_index"] = 2
        self.save()
        with self.assertRaises(ValueError):
            prepare(self.topic, 3)

    def test_rejects_unbound_markers_and_changed_image(self):
        self.doc["shots"][0]["prompt_template"] = "{{ref:C02}}"
        self.save()
        with self.assertRaises(ValueError):
            prepare(self.topic, 3)
        self.doc["shots"][0]["prompt_template"] = "{{ref:C01}}"
        self.save()
        self.image.write_bytes(b"changed image")
        with self.assertRaises(ValueError):
            prepare(self.topic, 3)

    def test_stage2_uses_same_job_shape_without_images(self):
        (self.topic / "assets.md").write_text(
            "### C01｜Hero\n出现画面：1、2（共2张）。\n\n资产自身描述：\n\n"
            "A young character in gray.\n\n最终生成提示词：\n\nold preview\n")
        job = prepare(self.topic, 2)[0]
        self.assertEqual(job["mode"], "text_to_image")
        self.assertEqual(job["reference_images"], [])
        self.assertIn("40%", job["prompt"])
        self.assertNotIn("old preview", job["prompt"])

    def test_record_preserves_previous_output_and_asset_manifest(self):
        job = prepare(self.topic, 3)[0]
        source_manifest = (self.topic / "images/manifest.json").read_bytes()
        first = Path(record(job, self.image, route="test"))
        second = Path(record(job, self.image, route="test"))
        self.assertTrue(first.exists() and second.exists())
        self.assertNotEqual(first, second)
        state = json.loads((self.topic / "images/comic-manifest.json").read_text())
        self.assertEqual(state["shots"][0]["previous_files"], ["candidates/comic/shot-01.png"])
        self.assertEqual((self.topic / "images/manifest.json").read_bytes(), source_manifest)

    def test_non_image_result_is_not_recorded_as_success(self):
        job = prepare(self.topic, 3)[0]
        bad = self.topic / "error.txt"
        bad.write_text("generation failed")
        with self.assertRaises(ValueError):
            record(job, bad, route="test")
        self.assertFalse((self.topic / "images/comic-manifest.json").exists())

    def make_ip(self, path=None):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is needed to validate imported character images")
        path = path or self.image
        Image.new("RGB", (100, 120), "red").save(path)
        return path

    def test_import_character_keeps_original_bytes_versions_and_other_assets(self):
        with tempfile.TemporaryDirectory() as external:
            source = self.make_ip(Path(external) / "mascot.webp")
            before = source.read_bytes()
            one = import_character(self.topic, source, "C02", "小红", locked_features=["红色耳朵"])
            two = import_character(self.topic, source, "C02", "小红")
        self.assertEqual((self.topic / "images" / one["file"]).read_bytes(), before)
        self.assertEqual(one["status"], "provided")
        self.assertEqual(one["source_sha256"], one["sha256"])
        self.assertNotEqual(one["file"], two["file"])
        self.assertIn(one["file"], two["previous_files"])
        state = json.loads((self.topic / "images/manifest.json").read_text())
        self.assertEqual({a["id"] for a in state["assets"]}, {"C01", "C02"})
        self.assertEqual(state["summary"], dict(requested=2, generated=1, provided=1, failed=0))

    def test_stage2_skips_user_ip_even_in_one_shot_and_generates_missing_cast(self):
        import_character(self.topic, self.make_ip(), "C01", "用户IP")
        (self.topic / "assets.md").write_text(
            "### C01｜用户IP\n出现画面：1（共1张）。\n\n资产自身描述：\n\n用户指定。\n\n最终生成提示词：\n\n复用原图\n\n"
            "### C02｜配角\n出现画面：1、2（共2张）。\n\n资产自身描述：\n\nGray clothes.\n\n最终生成提示词：\n\npreview\n")
        jobs = prepare(self.topic, 2)
        self.assertEqual([job["id"] for job in jobs], ["C02"])
        self.assertEqual(jobs[0]["reference_images"], [])

    def test_stage3_passes_user_image_and_prioritizes_ip_identity_over_default_style(self):
        entry = import_character(self.topic, self.make_ip(), "C01", "小红", locked_features=["红色耳朵"])
        ref = self.doc["shots"][0]["references"][0]
        ref.update(file="images/" + entry["file"], sha256=entry["sha256"])
        self.save()
        job = prepare(self.topic, 3)[0]
        self.assertEqual(job["reference_images"], [str((self.topic / ref["file"]).resolve())])
        self.assertIn("用户指定的IP角色「小红」", job["prompt"])
        self.assertIn("红色耳朵", job["prompt"])
        self.assertIn("优先于默认画风", job["prompt"])
        self.assertIn("图1", job["prompt"])
        self.assertNotIn("{{ref:", job["prompt"])
        (self.topic / ref["file"]).write_bytes(b"changed")
        ref["sha256"] = hashlib.sha256(b"changed").hexdigest()
        self.save()
        with self.assertRaises(ValueError):
            prepare(self.topic, 3)

    def test_stale_generation_cannot_overwrite_imported_ip(self):
        (self.topic / "assets.md").write_text(
            "### C01｜Hero\n出现画面：1、2（共2张）。\n\n资产自身描述：\n\nGray clothes.\n\n最终生成提示词：\n\npreview\n")
        job = prepare(self.topic, 2)[0]
        import_character(self.topic, self.make_ip(), "C01", "用户IP")
        before = (self.topic / "images/manifest.json").read_bytes()
        with self.assertRaises(ValueError):
            record(job, self.image, route="test")
        self.assertEqual((self.topic / "images/manifest.json").read_bytes(), before)

    def test_invalid_user_image_does_not_change_manifest(self):
        self.make_ip()
        invalid = self.topic / "broken.jpg"
        invalid.write_text("not an image")
        before = (self.topic / "images/manifest.json").read_bytes()
        with self.assertRaises((OSError, ValueError)):
            import_character(self.topic, invalid, "C02", "坏图")
        self.assertEqual((self.topic / "images/manifest.json").read_bytes(), before)

    def test_reference_packing_preserves_all_assets_and_image_pixels(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is only needed for reference packing")
        Image.new("RGB", (100, 120), "red").save(self.image)
        second = self.image.with_name("C02.png")
        Image.new("RGB", (100, 120), "blue").save(second)
        refs = [dict(asset_id=f"C{i}", image_index=n, path=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                for i, n, p in (("01", 1, self.image), ("02", 2, second))]
        base = dict(id="shot-01", topic=str(self.topic), prompt="图1是甲，图2是乙。", references=refs,
                    reference_images=[str(self.image), str(second)])
        job = pack_references(base, 1)
        self.assertEqual(len(job["reference_images"]), 1)
        self.assertEqual({r["asset_id"] for r in job["references"]}, {"C01", "C02"})
        self.assertIn("图1的A区域是甲，图1的B区域是乙", job["prompt"])
        with Image.open(job["reference_images"][0]) as sheet:
            self.assertEqual(sheet.getpixel((50, 60)), (255, 0, 0))
            self.assertEqual(sheet.getpixel((50, 220)), (0, 0, 255))
        self.assertIs(pack_references(base, 2), base)

    def setup_collection(self):
        self.make_ip()
        asset = json.loads((self.topic / "images/manifest.json").read_text())
        asset["assets"][0].update(review="passed", sha256=hashlib.sha256(self.image.read_bytes()).hexdigest())
        write(self.topic / "images/manifest.json", asset)
        self.doc["shots"][0]["references"][0]["sha256"] = asset["assets"][0]["sha256"]
        self.save()
        job = prepare(self.topic, 3)[0]
        first = Path(record(job, self.image, route="test", review="needs_revision"))
        second = Path(record(job, self.image, route="test", review="passed"))
        return first, second

    def test_collection_is_ordered_clean_reentrant_and_preserves_bindings(self):
        rejected, selected = self.setup_collection()
        before = selected.read_bytes()
        result = finalize_collection(self.topic)
        final = self.topic / result["images"][0]["file"]
        self.assertEqual(final.name, "001-Test.png")
        self.assertEqual(final.read_bytes(), before)
        self.assertTrue(rejected.is_file())
        self.assertFalse(selected.exists())
        self.assertEqual(len(list((self.topic / "images/comic").glob("*.png"))), 1)
        self.assertEqual(len(prepare(self.topic, 3)), 1)
        finalize_collection(self.topic)
        with zipfile.ZipFile(self.topic / "images/comic.zip") as package:
            self.assertEqual(package.namelist(), ["001-Test.png"])
            self.assertEqual(package.read("001-Test.png"), before)

    def test_unreviewed_or_duplicate_images_cannot_replace_a_collection(self):
        self.setup_collection()
        comic_path = self.topic / "images/comic-manifest.json"
        comic = json.loads(comic_path.read_text())
        comic["shots"][0]["review"] = "pending"
        write(comic_path, comic)
        before = comic_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "review"):
            finalize_collection(self.topic)
        self.assertEqual(comic_path.read_bytes(), before)
        comic["shots"][0]["review"] = "passed"
        source = self.topic / "images" / comic["shots"][0]["file"]
        duplicate = source.with_name("shot-02.png")
        from PIL import Image
        with Image.open(source) as picture:
            picture.save(duplicate, compress_level=0)
        self.assertNotEqual(source.read_bytes(), duplicate.read_bytes())
        comic["shots"].append(dict(comic["shots"][0], id="shot-02", file="candidates/comic/shot-02.png",
                                   sha256=hashlib.sha256(duplicate.read_bytes()).hexdigest()))
        self.doc["shots"].append(dict(self.doc["shots"][0], id="shot-02", title="Second"))
        self.save()
        write(comic_path, comic)
        with self.assertRaisesRegex(ValueError, "Duplicate comic image"):
            finalize_collection(self.topic)
        self.assertTrue(source.exists() and duplicate.exists())

    def test_new_selected_image_archives_old_final_and_updates_current_doc(self):
        self.setup_collection()
        first = finalize_collection(self.topic)
        final = self.topic / first["images"][0]["file"]
        original = final.read_bytes()
        from PIL import Image
        replacement = self.topic / "new.png"
        Image.new("RGB", (100, 120), "blue").save(replacement)
        job = prepare(self.topic, 3)[0]
        record(job, replacement, route="test", review="passed")
        second = finalize_collection(self.topic)
        self.assertEqual(final.read_bytes(), replacement.read_bytes())
        self.assertTrue(any(p.read_bytes() == original for p in (self.topic / "images/not-selected/comic").glob("*.png")))
        doc = json.loads((self.topic / "stage3-prompts.json").read_text())
        self.assertEqual(doc["shots"][0]["file"], second["images"][0]["file"])

    def test_collection_rolls_back_image_moves_when_saving_fails(self):
        _, selected = self.setup_collection()
        original = (self.topic / "images/manifest.json").read_bytes()
        with patch("image_collection.write", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                finalize_collection(self.topic)
        self.assertTrue(selected.exists())
        self.assertEqual((self.topic / "images/manifest.json").read_bytes(), original)
        self.assertFalse((self.topic / "images/comic.zip").exists())

    def test_asset_only_finalize_updates_existing_shot_reference_paths(self):
        self.setup_collection()
        finalize_collection(self.topic, stage=2)
        self.assertEqual(len(prepare(self.topic, 3)), 1)
        self.assertFalse((self.topic / "images/comic.zip").exists())

    def test_windows_lock_releases_after_an_exception(self):
        backend = Mock(LK_LOCK=1, LK_UNLCK=0)
        path = self.topic / "job.lock"
        with patch("image_jobs.fcntl", None), patch.dict("sys.modules", {"msvcrt": backend}):
            with self.assertRaises(ValueError):
                with locked(path):
                    raise ValueError("cancelled")
        self.assertEqual([c.args[1:] for c in backend.locking.call_args_list], [(1, 1), (0, 1)])


if __name__ == "__main__":
    unittest.main()
