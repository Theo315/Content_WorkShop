import hashlib
import json
from pathlib import Path
import stat
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from tools import prepare_git


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    files = {
        "README.md": b"Project instructions\n",
        ".env.example": b"APP_PASSWORD=\nSESSION_SECRET=YOUR_SESSION_SECRET\nS3_REGION=auto\n",
        "vercel.json": b"{}\n",
        "backend/main.py": b"app = object()\n",
        "frontend/src/main.tsx": b"export {}\n",
        "frontend/src/style.css": b"body { color: white; }\n",
        "api/index.py": b"from backend.main import app\n",
        "assets/car.png": b"runtime-png",
        "assets/reference.pdf": b"runtime-pdf",
        "assets/fonts/bold.ttf": b"runtime-font",
        "tests/fixtures/plate.png": b"regression-fixture",
        "tools/dev.mjs": b"// launcher\n",
        "docs/DEPLOY.md": b"Steps\n",
        "docs/STORAGE-CORS.json": b"[]\n",
        "vendor/hexph/hexph/plate_text.py": b"# source\n",
        "vendor/hexph/template_config.example.json": b"{}\n",
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root, files


def add_files(root: Path, relative_paths: list[str]) -> None:
    for relative in relative_paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"must-not-be-exported")


def test_source_allowlist_preserves_runtime_assets_and_test_fixtures(project):
    root, expected = project
    excluded = [
        ".env", ".env.local", "backend/.env.production", "frontend/.env.secret",
        "backend/production.env", "backend/.envrc",
        "backend/key.pem", "assets/key.pfx", "backend/account.p12", "assets/id_rsa",
        "backend/credentials.json", "backend/users.sqlite3", ".git/config",
        ".venv/Lib/site.py", "frontend/node_modules/lib/index.js", "node_modules/lib.js",
        "frontend/dist/index.html", "frontend/build/index.html", "frontend/file.tsbuildinfo",
        "backend/__pycache__/main.pyc", ".pytest_cache/data", "tmp/storage/private.png",
        "backend/tmp/private.png", "output/user.png", ".vercel/project.json",
        "tools/inspect_sources.py", "tools/disassemble_sds.py", "tools/run.log",
        "docs/private.png", "docs/private.JPG", "docs/screenshots/report.svg",
        "vendor/originals/app.exe", "vendor/hexph/real/private.png",
        "vendor/hexph/selfcheck-artifacts/report.png", "random-private-notes.md",
        "private-business-data/customers.csv", "Dockerfile", "render.yaml",
    ]
    add_files(root, excluded)
    sources = prepare_git.collect_sources(root)
    assert {item.path for item in sources} == set(expected)
    assert all(item.size == len(expected[item.path]) for item in sources)


def test_archive_has_relative_paths_matching_manifest_hashes(project, tmp_path):
    root, expected = project
    output = tmp_path / "release.zip"
    sources = prepare_git.collect_sources(root)
    assert prepare_git.create_archive(root, output, sources) == output
    with ZipFile(output) as archive:
        names = archive.namelist()
        assert set(names) == set(expected) | {prepare_git.MANIFEST_NAME}
        assert all(not name.startswith(("/", "project/")) and ".." not in Path(name).parts for name in names)
        manifest = json.loads(archive.read(prepare_git.MANIFEST_NAME))
        assert manifest["source_file_count"] == len(expected)
        assert manifest["source_total_bytes"] == sum(map(len, expected.values()))
        assert manifest["warning"] == "no cloud deploy performed"
        assert len(manifest["files"]) == len(expected)
        for item in manifest["files"]:
            payload = archive.read(item["path"])
            assert payload == expected[item["path"]]
            assert item["size"] == len(payload)
            assert item["sha256"] == hashlib.sha256(payload).hexdigest()


def test_existing_output_is_never_overwritten(project, tmp_path):
    root, _ = project
    output = tmp_path / "release.zip"
    output.write_bytes(b"original-user-archive")
    with pytest.raises(prepare_git.PackageError, match="already exists"):
        prepare_git.create_archive(root, output, prepare_git.collect_sources(root))
    assert output.read_bytes() == b"original-user-archive"


def test_changed_source_removes_only_its_new_partial_archive(project, tmp_path):
    root, _ = project
    sources = prepare_git.collect_sources(root)
    (root / "backend/main.py").write_text("changed\n")
    output = tmp_path / "release.zip"
    with pytest.raises(prepare_git.PackageError, match="Source changed"):
        prepare_git.create_archive(root, output, sources)
    assert not output.exists()
    assert (root / "backend/main.py").read_text() == "changed\n"


@pytest.mark.parametrize("relative", ["backend/outside.py", "assets/external"])
def test_symlinks_to_outside_source_are_rejected(project, tmp_path, relative):
    root, _ = project
    target = tmp_path / "private-secret.txt"
    target.write_bytes(b"outside")
    link = root / relative
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is not permitted on this host")
    with pytest.raises(prepare_git.PackageError, match="Links and junctions"):
        prepare_git.collect_sources(root)


def test_vendor_parent_symlink_is_rejected(project, tmp_path):
    root, _ = project
    separate_root = tmp_path / "linked-project"
    separate_root.mkdir()
    try:
        (separate_root / "vendor").symlink_to(root / "vendor", target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is not permitted on this host")
    with pytest.raises(prepare_git.PackageError, match="Links and junctions"):
        prepare_git.collect_sources(separate_root)


@pytest.mark.parametrize("target_name", ["backend/main.py", "vendor", "."])
def test_windows_reparse_points_are_rejected_without_following_them(project, monkeypatch, target_name):
    root, _ = project
    target = root / target_name
    original_lstat = Path.lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)

    def reparse_lstat(path, *args, **kwargs):
        original = original_lstat(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=original.st_mode, st_file_attributes=reparse_flag)
        return original

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(prepare_git.PackageError, match="Links and junctions"):
        prepare_git.collect_sources(root)


@pytest.mark.parametrize("secret", [
    "gh" + "p_" + "A" * 36,
    "AK" + "IA" + "A" * 16,
    "-----BEGIN " + "PRIVATE KEY-----",
])
def test_obvious_credentials_block_packaging_without_leaking_values(project, secret):
    root, _ = project
    (root / "backend/accident.py").write_text(f'credential = "{secret}"\n')
    with pytest.raises(prepare_git.PackageError) as caught:
        prepare_git.collect_sources(root)
    assert "backend/accident.py" in str(caught.value)
    assert secret not in str(caught.value)


def test_nested_example_environment_cannot_contain_real_secret(project):
    root, _ = project
    secret = "not-a-placeholder-use-a-secret-manager"
    (root / "backend/.env.example").write_text(f"S3_SECRET_ACCESS_KEY={secret}\n")
    with pytest.raises(prepare_git.PackageError, match="Non-placeholder credential") as caught:
        prepare_git.collect_sources(root)
    assert secret not in str(caught.value)


def test_github_oversize_file_is_rejected_without_reading_it(project, monkeypatch):
    root, _ = project
    monkeypatch.setattr(prepare_git, "MAX_FILE_BYTES", 10)
    with pytest.raises(prepare_git.PackageError, match="exceeds the 100 MiB"):
        prepare_git.collect_sources(root)


@pytest.mark.parametrize("mode", ["--check", "--list"])
def test_validation_modes_never_create_archive(project, capsys, mode):
    root, expected = project
    assert prepare_git.main(["--root", str(root), mode]) == 0
    captured = capsys.readouterr()
    assert "No cloud deploy performed" in captured.out
    assert str(len(expected)) in captured.out
    assert not (root / "tmp").exists()
    if mode == "--list":
        assert "backend/main.py" in captured.out


def test_default_cli_output_is_under_tmp_and_exclusive(project, capsys):
    root, _ = project
    assert prepare_git.main(["--root", str(root)]) == 0
    output = root / "tmp/releases/content-workshop-git.zip"
    assert output.is_file()
    original = output.read_bytes()
    assert prepare_git.main(["--root", str(root)]) == 1
    assert output.read_bytes() == original
    assert "already exists" in capsys.readouterr().err
