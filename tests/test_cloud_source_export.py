import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "cloud_export", Path(__file__).resolve().parents[1] / "scripts/export_cloud_source.py"
)
cloud_export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_export)


@pytest.mark.parametrize("role", cloud_export.ROLES)
def test_export_exactly_matches_deployed_source_manifest(role, tmp_path):
    destination = tmp_path / role
    sources = cloud_export.verified_sources(role)
    assert cloud_export.export(role, destination) == len(sources)
    assert {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()} == {
        name for name, _ in sources
    }
    for name, source in sources:
        assert (destination / name).read_bytes() == source.read_bytes()


def test_existing_directory_is_not_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        cloud_export.export("web", tmp_path)


def test_unknown_role_rejected():
    with pytest.raises(ValueError):
        cloud_export.verified_sources("../invalid")
