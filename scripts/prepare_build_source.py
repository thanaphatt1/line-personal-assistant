"""Prepare/verify a local, deterministic build bundle. Never invoke gcloud/Docker.

Read only named public build inputs, not a recursive workspace scan. Generated
sidecars are review records, not approval to upload, build or deploy anything.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile


BUILD_INPUTS = (
    "Dockerfile", ".dockerignore", "requirements.lock", "deploy/cloudbuild.json",
    "src/line_assistant/data/mock_sheet.json", "scripts/container_entrypoint.py",
    "scripts/verify_signed_startup.py",
    "src/line_assistant/__init__.py",
    "src/line_assistant/adapters.py",
    "src/line_assistant/action_trace.py",
    "src/line_assistant/read_cache.py",
    "src/line_assistant/loading.py",
    "src/line_assistant/expense_storage.py",
    "src/line_assistant/adc_preflight.py",
    "src/line_assistant/api_sandbox.py",
    "src/line_assistant/auth_ports.py",
    "src/line_assistant/calendar_api.py",
    "src/line_assistant/catalog.py",
    "src/line_assistant/source_spans.py",
    "src/line_assistant/conversation.py",
    "src/line_assistant/conversation_dates.py",
    "src/line_assistant/conversation_evidence.py",
    "src/line_assistant/conversation_ui.py",
    "src/line_assistant/enrollment.py",
    "src/line_assistant/events.py",
    "src/line_assistant/intent_api.py",
    "src/line_assistant/taxonomy.py",
    "src/line_assistant/canary.py",
    "src/line_assistant/cloud_run.py",
    "src/line_assistant/config.py",
    "src/line_assistant/confirmed_target.py",
    "src/line_assistant/contracts.py",
    "src/line_assistant/deployment_plan.py",
    "src/line_assistant/durable_mocks.py",
    "src/line_assistant/firestore_state.py",
    "src/line_assistant/google_auth.py",
    "src/line_assistant/google_requests.py",
    "src/line_assistant/line_api.py",
    "src/line_assistant/language.py",
    "src/line_assistant/line_auth_check.py",
    "src/line_assistant/reply_check.py",
    "src/line_assistant/reply_display.py",
    "src/line_assistant/name_matching.py",
    "src/line_assistant/local_runtime.py",
    "src/line_assistant/main.py",
    "src/line_assistant/mock_api.py",
    "src/line_assistant/model_api.py",
    "src/line_assistant/persistence.py",
    "src/line_assistant/pilot_config.py",
    "src/line_assistant/pilot_http.py",
    "src/line_assistant/planner.py",
    "src/line_assistant/preflight.py",
    "src/line_assistant/readonly_preflight.py",
    "src/line_assistant/runtime_config.py",
    "src/line_assistant/service.py",
    "src/line_assistant/sheet_dates.py",
    "src/line_assistant/snapshot_codec.py",
    "src/line_assistant/sheet_metadata.py",
    "src/line_assistant/sheets_api.py",
    "src/line_assistant/state.py",
    "src/line_assistant/synthetic_integration.py",
    "src/line_assistant/transport.py",
)
GENERATED_INPUTS = (".gcloudignore", "SHA256SUMS", "SOURCE-MANIFEST.json")
MAX_FILE_BYTES = 2 * 1024 * 1024


class PackagingError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def read_named_file(root, name):
    """Open beneath root without following any symlink, including parent folders."""
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or any(p in (".", "..") for p in parts):
        raise PackagingError("Unsafe input name")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise PackagingError("Build inputs must be regular files without hard links")
            data = source.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise PackagingError("Build input exceeds size limit")
            return data
    finally:
        os.close(directory)


def gcloudignore():
    names = sorted((*BUILD_INPUTS, *GENERATED_INPUTS))
    parents = {str(parent) + "/" for name in names for parent in PurePosixPath(name).parents
               if str(parent) != "."}
    return ("# Generated exact allowlist; no include of .gitignore or workspace files.\n**\n"
            + "\n".join("!" + name for name in sorted(parents, key=lambda p: (p.count("/"), p)))
            + "\n" + "\n".join("!" + name for name in names) + "\n").encode()


def generated_payload(inputs):
    payload = dict(inputs)
    payload[".gcloudignore"] = gcloudignore()
    payload["SHA256SUMS"] = "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(payload.items())).encode()
    try:
        substitutions = json.loads(inputs["deploy/cloudbuild.json"]).get("substitutions", {})
    except (ValueError, TypeError, AttributeError):
        substitutions = {}
    pinned = (re.fullmatch(r"gcr\.io/cloud-builders/docker@sha256:[0-9a-f]{64}", str(substitutions.get("_DOCKER_BUILDER_IMAGE", "")))
              and re.fullmatch(r"python:3\.11-slim@sha256:[0-9a-f]{64}", str(substitutions.get("_PYTHON_BASE_IMAGE", ""))))
    manifest = {
        "format": "line-assistant-build-source-v1",
        "upload_build_deploy_authorized": False,
        "runtime_config_included": False,
        "image_digests_status": "builder_and_python_pinned_in_config_container_not_built" if pinned else "unresolved_requires_separate_review",
        "files": [{"path": name, "bytes": len(data), "sha256": sha256(data)}
                  for name, data in sorted(payload.items())],
        "manifest_self_hash": "recorded_in_review_json_to_avoid_self_reference",
    }
    payload["SOURCE-MANIFEST.json"] = json_bytes(manifest)
    return payload


def archive_bytes(payload):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(payload.items()):
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def prepare(root, output):
    root, output = Path(root), Path(output)
    # Complete named reads before making output; never read excluded file contents.
    payload = generated_payload({name: read_named_file(root, name) for name in BUILD_INPUTS})
    packed = archive_bytes(payload)
    review = {
        "format": "line-assistant-build-review-v1",
        "archive": "source.tar.gz", "sha256": sha256(packed), "bytes": len(packed),
        "source_manifest_sha256": sha256(payload["SOURCE-MANIFEST.json"]),
        "source_checksums_sha256": sha256(payload["SHA256SUMS"]),
        "archive_file_count": len(payload),
        "upload_build_deploy_authorized": False,
    }
    output.mkdir(mode=0o700, parents=True, exist_ok=False)  # No overwrite of prior work.
    staging = output / "source"
    staging.mkdir(mode=0o700)
    for name, data in payload.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
    (output / "source.tar.gz").write_bytes(packed)
    (output / "review.json").write_bytes(json_bytes(review))
    verify(output)
    return review


def verify(output):
    output = Path(output)
    staging = output / "source"
    expected_names = set((*BUILD_INPUTS, *GENERATED_INPUTS))
    expected_folders = {str(parent) for name in expected_names for parent in PurePosixPath(name).parents
                        if str(parent) != "."}
    # Inspect extra names only; never open them or follow directory links.
    actual_names = set()
    for directory, folders, files in os.walk(staging, followlinks=False):
        for folder in folders:
            candidate = Path(directory) / folder
            if candidate.is_symlink() or candidate.relative_to(staging).as_posix() not in expected_folders:
                raise PackagingError("Unexpected directory/symlink in staging")
        actual_names.update((Path(directory) / name).relative_to(staging).as_posix() for name in files)
    if actual_names != expected_names:
        raise PackagingError("Staging does not match exact allowlist")
    expected = generated_payload({name: read_named_file(staging, name) for name in BUILD_INPUTS})
    for name in GENERATED_INPUTS:
        if read_named_file(staging, name) != expected[name]:
            raise PackagingError("Generated manifest/checksums/ignore changed")
    packed = read_named_file(output, "source.tar.gz")
    review = json.loads(read_named_file(output, "review.json"))
    if (review.get("sha256") != sha256(packed)
            or review.get("source_manifest_sha256") != sha256(expected["SOURCE-MANIFEST.json"])
            or review.get("source_checksums_sha256") != sha256(expected["SHA256SUMS"])
            or review.get("archive_file_count") != len(expected)
            or review.get("bytes") != len(packed)
            or review.get("upload_build_deploy_authorized") is not False):
        raise PackagingError("Review record mismatch")
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) != len(expected) or {m.name for m in members} != expected_names:
            raise PackagingError("Archive does not match exact allowlist")
        for member in members:
            if not member.isfile() or member.size != len(expected[member.name]):
                raise PackagingError("Unexpected archive member type/size")
            with archive.extractfile(member) as source:
                if source.read() != expected[member.name]:
                    raise PackagingError("Archive content mismatch")
    return review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if (args.output is None) == (args.verify is None):
        parser.error("Choose exactly one of --output (new directory) or --verify")
    try:
        review = verify(args.verify) if args.verify else prepare(args.root, args.output)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(2, f"Local packaging failed: {type(error).__name__}\n")
    print(json.dumps(review, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
