# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "qiling-ai-sdk @ https://github.com/qilingAI/qiling-sdk-python/releases/download/v0.5.4/qiling_ai_sdk-0.5.4-py3-none-any.whl",
#   "openai>=2.45,<3",
#   "keyring>=25,<26",
#   "Pillow>=11,<12",
# ]
# ///
"""Qiling browser authorization and the shared comic image-job API runner."""
import argparse
import base64
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid

from image_jobs import SKILL_ROOT, locked, pack_references, prepare, read, record, write

# Keep the existing credential namespace so renaming the skill does not lose prior authorizations.
KEY_SERVICE = "anime-comic:qiling"


def settings():
    config = read(SKILL_ROOT / "references/qiling.json")
    url = os.getenv("QILING_API_URL") or os.getenv("QILING_BASE_URL") or config["api_url"]
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("器灵地址必须是无账号、无查询参数的 HTTPS 平台根地址")
    return dict(api_url=url, model=os.getenv("QILING_MODEL") or config["model"])


def secure_store():
    import keyring

    backend = keyring.get_keyring()
    candidates = getattr(backend, "backends", [backend])
    for item in candidates:
        if type(item).__module__.startswith(("keyring.backends.macOS", "keyring.backends.Windows",
                                             "keyring.backends.SecretService", "keyring.backends.libsecret",
                                             "keyring.backends.kwallet")):
            return item
    raise ValueError("没有可用的系统安全凭据库；请启用系统密钥环，或通过环境变量 QILING_API_KEY 提供密钥")


def authorize(config, *, store=None, authorization=None):
    if store is None:
        store = secure_store()
    if authorization is None:
        from qiling_ai_sdk import QilingAuthorizationClient
        authorization = QilingAuthorizationClient(config["api_url"], client_name="脑洞漫工厂 brain-comic")
    result = authorization.authorize(on_authorization_url=lambda url: print("请在浏览器确认器灵授权：" + url, flush=True))
    if result.get("status") != "authorized" or not result.get("api_key"):
        raise ValueError("授权未成功，未保存密钥")
    # The configured API origin remains authoritative; a verification/response URL cannot redirect credentials.
    store.set_password(KEY_SERVICE, config["api_url"], result["api_key"])
    return {"authorized": True, "api_url": config["api_url"], "model": config["model"]}


def api_client(config):
    from openai import OpenAI

    key = os.getenv("QILING_API_KEY")
    if not key:
        key = secure_store().get_password(KEY_SERVICE, config["api_url"])
    if not key:
        raise ValueError("尚未授权；请先运行 authorize，在浏览器确认后重试")
    return OpenAI(api_key=key, base_url=config["api_url"] + "/v1", max_retries=0, timeout=300)


def image_bytes(item):
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"], validate=True)
    elif item.get("url"):
        if urllib.parse.urlsplit(item["url"]).scheme != "https":
            raise ValueError("图片下载只接受 HTTPS 地址")
        # Never send the API Key to the image host.
        with urllib.request.urlopen(item["url"], timeout=60) as response:
            data = response.read(50 * 1024 * 1024 + 1)
    else:
        raise ValueError("接口没有返回可保存的图片")
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("图片超过 50MB 限制")
    from PIL import Image
    with Image.open(io.BytesIO(data)) as picture:
        picture.load()
        if picture.format == "PNG":
            return data
        output = io.BytesIO()
        picture.convert("RGBA").save(output, format="PNG")
        return output.getvalue()


def run_job(job, config, client, *, size=None, new_attempt=False):
    """Submit once; resume received results locally rather than generating again."""
    topic = Path(job["topic"]).resolve()
    if not (topic / job["output_dir"]).resolve().is_relative_to(topic):
        raise ValueError("图片输出目录必须在当前主题内")
    refs = job.get("input_images") or job["references"]
    paths = [str((topic / r["file"]).resolve()) if "path" not in r else r["path"] for r in refs]
    if paths != job["reference_images"]:
        raise ValueError("参考图片顺序与任务绑定不一致")
    for ref, name in zip(refs, paths):
        path = Path(name).resolve()
        if not path.is_relative_to(topic) or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
            raise ValueError("参考图已改变，请重新编译任务")
    signature = hashlib.sha256(json.dumps(dict(job_key=job["job_key"], **config, size=size), sort_keys=True).encode()).hexdigest()
    journal_path = topic / ".qiling-jobs" / (signature + ".json")
    if not journal_path.resolve().is_relative_to(topic):
        raise ValueError("器灵任务记录目录必须在当前主题内")
    with locked(journal_path.with_suffix(".lock")):
        state = read(journal_path) if journal_path.exists() else None
        if state and new_attempt:
            if state["status"] not in {"saved", "failed"}:
                raise ValueError("原任务结果未明，不能创建新请求；请先核对原任务")
            write(journal_path.with_name(signature + "-" + state["request_id"] + ".json"), state)
            state = None
        if state and state["status"] == "saved":
            existing = (topic / state["file"]).resolve()
            if not existing.exists():
                for filename in ("comic-collection.json", "asset-collection.json"):
                    catalog = topic / "images" / filename
                    if catalog.exists():
                        moved = read(catalog).get("moves", {}).get(state["file"])
                        if moved:
                            existing = (topic / moved).resolve()
                            break
            if existing.is_relative_to(topic) and existing.is_file() and hashlib.sha256(existing.read_bytes()).hexdigest() == state["sha256"]:
                state["file"] = str(existing.relative_to(topic))
                write(journal_path, state)
                return {k: state[k] for k in ("file", "request_id", "call_id", "status")}
            raise ValueError("上次结果已移动或改变，请先按请求号核对，不自动再次生成")
        if state and state["status"] != "received":
            raise ValueError("此任务已有提交记录；请先核对原任务，不自动重发。请求号：" + state["request_id"])
        if not state:
            state = dict(status="submitted", request_id="comic-" + uuid.uuid4().hex, call_id=None,
                         model=config["model"], job_key=job["job_key"])
            write(journal_path, state)
            options = dict(model=config["model"], prompt=job["prompt"], n=1,
                           extra_headers={"Idempotency-Key": state["request_id"]})
            if size:
                options["size"] = size
            try:
                with ExitStack() as stack:
                    if job["reference_images"]:
                        images = [stack.enter_context(open(path, "rb")) for path in job["reference_images"]]
                        result = client.images.edit(image=images, **options)
                    else:
                        result = client.images.generate(**options)
                payload = result.model_dump(mode="json")
                state.update(status="received", result=payload, call_id=(payload.get("qiling") or {}).get("id"))
                write(journal_path, state)
            except Exception as error:
                body = getattr(error, "body", None)
                if isinstance(body, dict):
                    detail = body.get("error") or body
                    if isinstance(detail, dict):
                        state["call_id"] = detail.get("call_id") or (body.get("qiling") or {}).get("id")
                status = getattr(error, "status_code", None)
                state.update(status="failed" if status in {401, 402, 403} and not state["call_id"] else "unknown",
                             error_type=type(error).__name__, http_status=status)
                write(journal_path, state)
                raise
        payload = state["result"]
        if len(payload.get("data") or []) != 1:
            raise ValueError("接口未返回恰好一张最终图片；保留原请求记录待核对")
        data = image_bytes(payload["data"][0])
        provider = dict(name="qiling", api_url=config["api_url"], model=config["model"],
                        request_id=state["request_id"], call_id=state["call_id"],
                        billing=(payload.get("qiling") or {}).get("billing"))
        output = None
        manifest = topic / "images" / ("manifest.json" if job["stage"] == 2 else "comic-manifest.json")
        if manifest.exists():
            entries = read(manifest)["assets" if job["stage"] == 2 else "shots"]
            for entry in entries:
                if entry.get("provider", {}).get("request_id") == state["request_id"]:
                    candidate = (topic / "images" / entry["file"]).resolve()
                    if candidate.is_relative_to(topic) and candidate.is_file() and candidate.read_bytes() == data:
                        output = candidate
                        break
        if output is None:
            with tempfile.TemporaryDirectory(dir=journal_path.parent) as temp:
                source = Path(temp) / "result.png"
                source.write_bytes(data)
                output = Path(record(dict(job, provider=provider), source, route="api:qiling"))
        state.pop("result", None)
        state.update(status="saved", file=str(output.relative_to(topic)), sha256=hashlib.sha256(data).hexdigest())
        write(journal_path, state)
        return {k: state[k] for k in ("file", "request_id", "call_id", "status")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("authorize")
    commands.add_parser("forget")
    commands.add_parser("config")
    commands.add_parser("models")
    task = commands.add_parser("task")
    task.add_argument("--call-id", required=True)
    run = commands.add_parser("run")
    run.add_argument("--topic", type=Path, required=True)
    run.add_argument("--stage", type=int, choices=(2, 3), required=True)
    run.add_argument("--id", required=True)
    run.add_argument("--size", help="Use only a size supported by the selected model's current schema")
    run.add_argument("--max-reference-images", type=int, help="Only use an input limit verified from the model documentation")
    run.add_argument("--new-attempt", action="store_true", help="Explicitly request a new image after a completed/confirmed failed attempt")
    args = parser.parse_args()
    try:
        config = settings()
        if args.command == "config":
            result = config
        elif args.command == "authorize":
            result = authorize(config)
        elif args.command == "forget":
            secure_store().delete_password(KEY_SERVICE, config["api_url"])
            result = {"local_key_removed": True, "note": "仅清除本技能保存的Key，不停用其他应用共用的Key；环境变量需用户自行清除"}
        else:
            with api_client(config) as client:
                if args.command == "models":
                    model = client.models.retrieve(config["model"]).model_dump(mode="json")
                    docs = client.get(config["api_url"] + "/api/v1/open/model-docs", cast_to=dict)
                    result = {"model": model, "documentation": docs}
                elif args.command == "task":
                    result = client.get("/model-calls/" + urllib.parse.quote(args.call_id, safe=""), cast_to=dict)
                else:
                    job = next((j for j in prepare(args.topic, args.stage) if j["id"] == args.id), None)
                    if job is None:
                        raise ValueError("没有该任务，或该角色使用用户原图，无需生图")
                    if args.max_reference_images is not None:
                        job = pack_references(job, args.max_reference_images)
                    result = run_job(job, config, client, size=args.size, new_attempt=args.new_attempt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print("已取消；未完成的生图须先核对原任务。", file=sys.stderr)
        return 1
    except Exception as error:
        # No raw SDK exception/body: upstream errors may echo request data or credentials.
        status = getattr(error, "status_code", getattr(error, "status", None))
        message = str(error) if type(error) is ValueError else type(error).__name__
        print(f"器灵操作未完成：{message}" + (f"（HTTP {status}）" if status else ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
