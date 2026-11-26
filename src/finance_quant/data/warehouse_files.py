"""Files declared by an immutable warehouse manifest."""

from pathlib import Path


def warehouse_files(root: Path, manifest: dict):
    declared = [
        (Path("reference") / name, digest) for name, digest in sorted(manifest.get("reference_files", {}).items())
    ]
    declared.extend((Path(asset["file"]), asset["sha256"]) for asset in manifest["assets"].values())
    seen = set()
    for relative, digest in declared:
        path = (root / relative).resolve()
        key = relative.as_posix()
        if relative.is_absolute() or not path.is_relative_to(root) or key in seen or not path.is_file():
            raise ValueError("Historical warehouse contains an invalid file path")
        seen.add(key)
        yield path, relative, digest
