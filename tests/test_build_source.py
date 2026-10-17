import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import re
import shutil
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).parents[1]
PACK = runpy.run_path(str(ROOT / "scripts/prepare_build_source.py"))
INPUTS = PACK["BUILD_INPUTS"]
GENERATED = PACK["GENERATED_INPUTS"]
prepare, verify = PACK["prepare"], PACK["verify"]
PackagingError = PACK["PackagingError"]


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "workspace"
    for name in INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("public synthetic input: " + name + "\n").encode())
    return root


def test_archive_and_staging_are_exact_allowlist_without_reading_excluded_files(source_tree, tmp_path, monkeypatch):
    excluded = [".env", ".env.production", ".aws/credentials", ".codex/session.json",
                "credentials.json", "application_default_credentials.json", "local-data/runtime/state.json",
                "local-data/cloud_run_preparation.json", "local-data/synthetic_test_runs/private.json",
                "reference/spec.pdf", "logs/private.log", "thesis/private.py",
                "src/line_assistant/private_credentials.py", "src/line_assistant/__pycache__/private.pyc",
                "fixtures/private.json", "scripts/run_one_synthetic_expense.py"]
    canary = b"SYNTHETIC_PRIVATE_DATA_MUST_NEVER_BE_READ_OR_PACKAGED"
    for name in excluded:
        path = source_tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canary)
    original_open = os.open
    forbidden_names = {Path(name).name for name in excluded}
    reads = []
    def guard(path, flags, *args, **kwargs):
        name = os.fsdecode(path)
        assert Path(name).name not in forbidden_names
        reads.append(name)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", guard)
    output = tmp_path / "package"
    review = prepare(source_tree, output)
    assert verify(output) == review
    assert review["archive_file_count"] == len(INPUTS) + len(GENERATED)
    assert review["upload_build_deploy_authorized"] is False
    assert all(Path(name).name not in forbidden_names for name in reads)
    with tarfile.open(output / "source.tar.gz") as archive:
        assert set(archive.getnames()) == set(INPUTS) | set(GENERATED)
        for member in archive.getmembers():
            assert member.isfile() and member.uid == member.gid == member.mtime == 0
            assert canary not in archive.extractfile(member).read()
    manifest = json.loads((output / "source/SOURCE-MANIFEST.json").read_bytes())
    assert manifest["runtime_config_included"] is False
    assert set(entry["path"] for entry in manifest["files"]) == set(INPUTS) | {".gcloudignore", "SHA256SUMS"}
    for entry in manifest["files"]:
        data = (output / "source" / entry["path"]).read_bytes()
        assert entry["bytes"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()


def test_packaging_is_reproducible_across_paths_metadata_and_prior_outputs(source_tree, tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    review = prepare(source_tree, first)
    for name in INPUTS:
        os.utime(source_tree / name, (1234567890, 1234567890))
        (source_tree / name).chmod(0o600)
    assert prepare(source_tree, second) == review
    assert (first / "source.tar.gz").read_bytes() == (second / "source.tar.gz").read_bytes()


@pytest.mark.parametrize("name", ["Dockerfile", "src/line_assistant/config.py", "src/line_assistant/data/mock_sheet.json"])
def test_missing_required_input_fails_before_creating_output(source_tree, tmp_path, name):
    (source_tree / name).unlink()
    output = tmp_path / "package"
    with pytest.raises(OSError):
        prepare(source_tree, output)
    assert not output.exists()


@pytest.mark.parametrize("link_type", ["file", "directory", "hardlink", "fifo"])
def test_named_input_cannot_redirect_to_private_contents(source_tree, tmp_path, link_type):
    private = tmp_path / "private"
    private.mkdir()
    (private / ".env").write_text("synthetic-private-only")
    target = source_tree / "Dockerfile"
    if link_type == "directory":
        shutil.rmtree(source_tree / "src")
        (source_tree / "src").symlink_to(private, target_is_directory=True)
    else:
        target.unlink()
        if link_type == "file":
            target.symlink_to(private / ".env")
        elif link_type == "hardlink":
            os.link(private / ".env", target)
        else:
            os.mkfifo(target)
    output = tmp_path / "package"
    with pytest.raises((OSError, PackagingError)):
        prepare(source_tree, output)
    assert not output.exists()


def test_output_cannot_overwrite_existing_work(source_tree, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        prepare(source_tree, output)
    assert (output / "keep.txt").read_text() == "keep"
    assert list(output.iterdir()) == [output / "keep.txt"]


@pytest.mark.parametrize("changed", ["Dockerfile", ".gcloudignore", "SHA256SUMS", "SOURCE-MANIFEST.json"])
def test_staged_changes_invalidate_review(source_tree, tmp_path, changed):
    output = tmp_path / "package"
    prepare(source_tree, output)
    with (output / "source" / changed).open("ab") as target:
        target.write(b"changed")
    with pytest.raises(PackagingError):
        verify(output)


def test_added_private_file_is_rejected_by_name_before_open(source_tree, tmp_path, monkeypatch):
    output = tmp_path / "package"
    prepare(source_tree, output)
    (output / "source/.env").write_text("synthetic-private")
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: pytest.fail("Opened file before name rejection"))
    with pytest.raises(PackagingError, match="allowlist"):
        verify(output)


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "symlink", "content"])
def test_archive_tampering_is_rejected_even_with_updated_archive_sidecar(source_tree, tmp_path, mutation):
    output = tmp_path / "package"
    prepare(source_tree, output)
    packed = io.BytesIO()
    with tarfile.open(output / "source.tar.gz") as original, tarfile.open(fileobj=packed, mode="w:gz") as changed:
        for member in original.getmembers():
            data = original.extractfile(member).read()
            if member.name == "Dockerfile" and mutation == "symlink":
                member.type, member.linkname, member.size = tarfile.SYMTYPE, ".env", 0
                changed.addfile(member)
            else:
                if member.name == "Dockerfile" and mutation == "content":
                    data = b"x" * len(data)
                changed.addfile(member, io.BytesIO(data))
        if mutation in ("extra", "duplicate"):
            extra = tarfile.TarInfo(".env" if mutation == "extra" else "Dockerfile")
            extra.size = 7
            changed.addfile(extra, io.BytesIO(b"private"))
    data = packed.getvalue()
    (output / "source.tar.gz").write_bytes(data)
    record = json.loads((output / "review.json").read_bytes())
    record.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    (output / "review.json").write_text(json.dumps(record))
    with pytest.raises(PackagingError):
        verify(output)


def test_generated_gcloudignore_uses_exact_files_without_workspace_glob_or_gitignore_include():
    text = PACK["gcloudignore"]().decode()
    rules = text.splitlines()
    assert "**" in rules and not any("#!include" in line for line in rules)
    assert set(line[1:] for line in rules if line.startswith("!") and not line.endswith("/")) == set(INPUTS) | set(GENERATED)
    assert not any("*.py" in line or ".env" in line or "local-data" in line for line in rules)


def test_cloudbuild_config_is_build_only_fixed_target_and_requires_reviewed_digests():
    config = json.loads((ROOT / "deploy/cloudbuild.json").read_bytes())
    assert "serviceAccount" not in config  # selected privately by the deployment operator
    assert config["options"] == {"logging": "CLOUD_LOGGING_ONLY"}
    assert config["timeout"] == "600s" and len(config["steps"]) == 1
    assert config["images"] == ["${_IMAGE}:${_REVIEWED_TAG}"]
    text = json.dumps(config)
    assert not any(word in text for word in ("gcloud", "secretEnv", "availableSecrets", "PILOT_LAUNCH_AUTHORIZED"))
    substitutions = config["substitutions"]
    assert re.fullmatch(r"gcr\.io/cloud-builders/docker@sha256:[0-9a-f]{64}", substitutions["_DOCKER_BUILDER_IMAGE"])
    assert re.fullmatch(r"python:3\.11-slim@sha256:[0-9a-f]{64}", substitutions["_PYTHON_BASE_IMAGE"])
    assert substitutions["_REVIEWED_TAG"] == "REQUIRES_REVIEW"
    assert substitutions["_SOURCE_CHECKSUMS_SHA256"] == "REQUIRES_REVIEW"
    script = config["steps"][0]["args"][1].replace("${_IMAGE}", "example.invalid/assistant").replace("$$", "$")
    assert "sha256sum -c SHA256SUMS" in script
    assert "--build-arg" in script and "PYTHON_BASE=$PYTHON_BASE_IMAGE" in script
    result = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "FROM ${PYTHON_BASE}" in (ROOT / "Dockerfile").read_text()


@pytest.mark.parametrize("invalid", ["REVIEWED_TAG", "PYTHON_BASE_IMAGE", "SOURCE_CHECKSUMS_SHA256"])
def test_build_guard_stops_unreviewed_inputs_before_hash_build_or_push(invalid):
    config = json.loads((ROOT / "deploy/cloudbuild.json").read_bytes())
    script = config["steps"][0]["args"][1].replace("${_IMAGE}", "example.invalid/assistant").replace("$$", "$")
    values = {"REVIEWED_TAG": "review-local-test", "PYTHON_BASE_IMAGE": "python:3.11-slim@sha256:" + "a" * 64,
              "SOURCE_CHECKSUMS_SHA256": "b" * 64}
    values[invalid] = "REQUIRES_REVIEW"
    result = subprocess.run([shutil.which("bash"), "-ceu", script], env={**values, "PATH": ""}, capture_output=True, text=True)
    assert result.returncode == 2
    assert "required" in result.stdout and result.stderr == ""


@pytest.mark.parametrize("mutation", ["none", "source", "checksums"])
def test_reviewed_build_script_verifies_hashes_before_only_a_fake_docker(mutation, source_tree, tmp_path):
    config = json.loads((ROOT / "deploy/cloudbuild.json").read_bytes())
    script = config["steps"][0]["args"][1].replace("${_IMAGE}", "example.invalid/assistant").replace("$$", "$")
    output = tmp_path / "package"
    review = prepare(source_tree, output)
    if mutation == "source":
        (output / "source/Dockerfile").write_bytes(b"tampered")
    elif mutation == "checksums":
        (output / "source/SHA256SUMS").write_bytes(b"tampered")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    hasher = fake_bin / "sha256sum"
    hasher.write_text(f"#!{sys.executable}\n" + """import hashlib, pathlib, sys
assert sys.argv[1] == '-c'
text = sys.stdin.read() if sys.argv[2] == '-' else pathlib.Path(sys.argv[2]).read_text()
for line in text.splitlines():
    digest, name = line.split('  ', 1)
    if hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest() != digest:
        raise SystemExit(1)
""")
    docker = fake_bin / "docker"
    docker.write_text(f"#!{sys.executable}\n" + """import json, os, pathlib, sys
pathlib.Path(os.environ['FAKE_DOCKER_RECORD']).write_text(json.dumps(sys.argv[1:]))
""")
    hasher.chmod(0o700)
    docker.chmod(0o700)
    record = tmp_path / "fake-docker.json"
    env = {"PATH": str(fake_bin), "REVIEWED_TAG": "review-local-test",
           "PYTHON_BASE_IMAGE": "python:3.11-slim@sha256:" + "a" * 64,
           "SOURCE_CHECKSUMS_SHA256": review["source_checksums_sha256"],
           "FAKE_DOCKER_RECORD": str(record)}
    result = subprocess.run([shutil.which("bash"), "-ceu", script], env=env, cwd=output / "source",
                            capture_output=True, text=True)
    if mutation == "none":
        assert result.returncode == 0, result.stderr
        assert json.loads(record.read_text()) == ["build", "--file", "deploy/history-production.Dockerfile", "--build-arg", "PYTHON_BASE=" + env["PYTHON_BASE_IMAGE"],
                "--tag", "example.invalid/assistant:review-local-test", "."]
    else:
        assert result.returncode != 0 and not record.exists()


def test_actual_repository_bundle_is_locally_verifiable_without_private_inputs(tmp_path):
    output = tmp_path / "actual"
    review = prepare(ROOT, output)
    assert verify(output) == review
    assert review["upload_build_deploy_authorized"] is False
    manifest = json.loads((output / "source/SOURCE-MANIFEST.json").read_bytes())
    assert manifest["image_digests_status"] == "builder_and_python_pinned_in_config_container_not_built"
    assert b"SOURCE-MANIFEST.json" in gzip.decompress((output / "source.tar.gz").read_bytes())


def test_packaged_bootstrap_imports_staged_sources_and_stays_disabled(tmp_path):
    output = tmp_path / "actual"
    prepare(ROOT, output)
    code = """import pathlib, socket, sys
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError('Unexpected secret/network access')
socket.socket.connect = forbidden
from fastapi.testclient import TestClient
from line_assistant.cloud_run import create_cloud_run_app
import line_assistant.cloud_run
assert pathlib.Path(line_assistant.cloud_run.__file__).is_relative_to(pathlib.Path(sys.argv[1]))
with TestClient(create_cloud_run_app(values={}, secret_loader=forbidden)) as client:
    assert client.get('/health').json()['mode'] == 'launch_disabled'
    assert client.post('/webhooks/line', content=b'{"events":[]}').status_code == 503
print('staged disabled bootstrap passed')
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(output / "source/src")],
                            cwd=output / "source", env={"PATH": ""}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "staged disabled bootstrap passed"
