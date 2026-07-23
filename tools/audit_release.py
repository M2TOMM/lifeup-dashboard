"""Fail a release when it contains LifeUp user data or local configuration."""

import argparse
import os
from pathlib import Path, PurePosixPath
import sys
import zipfile


FORBIDDEN_BASENAMES = {
    ".env",
    ".lifeup_dashboard_config.json",
    "desktop-config.json",
    "lifeup_cloud_config.json",
    "lifeup_goal_mappings.json",
}
FORBIDDEN_PARTS = {
    "browser-imports",
    "databases",
    "exports",
    "restores",
    "snapshots",
    "work",
    "workspaces",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".jsonl",
    ".key",
    ".log",
    ".pem",
    ".sqlite",
    ".sqlite3",
}


def normalize(name):
    return PurePosixPath(str(name).replace("\\", "/"))


def violation(name):
    path = normalize(name)
    lowered_parts = [part.casefold() for part in path.parts if part not in ("", ".")]
    if not lowered_parts:
        return None
    basename = lowered_parts[-1]
    if basename in FORBIDDEN_BASENAMES:
        return "local configuration"
    if any(part in FORBIDDEN_PARTS for part in lowered_parts[:-1]):
        return "managed user-data directory"
    if "lifeupbackup" in basename.replace("-", "").replace("_", ""):
        return "LifeUp backup"
    if any(basename.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        return "private or temporary file type"
    return None


def directory_entries(root):
    root = Path(root).resolve()
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        directories[:] = [
            name for name in directories if not (current / name).is_symlink()
        ]
        for filename in filenames:
            path = current / filename
            if path.is_symlink():
                yield path.relative_to(root).as_posix() + " (symlink)"
            else:
                yield path.relative_to(root).as_posix()


def executable_entries(path):
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:
        raise RuntimeError(
            "Auditing a PyInstaller EXE requires requirements-desktop.txt"
        ) from exc
    archive = CArchiveReader(str(path))
    return list(archive.toc.keys())


def entries_for(path):
    path = Path(path)
    if path.is_dir():
        return list(directory_entries(path))
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path, "r") as archive:
            return archive.namelist()
    if path.suffix.casefold() == ".exe":
        return executable_entries(path)
    raise RuntimeError(f"Unsupported release target: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+", help="Release directory, ZIP, or PyInstaller EXE")
    args = parser.parse_args()
    failures = []
    total = 0
    for raw_target in args.targets:
        target = Path(raw_target).resolve()
        entries = entries_for(target)
        total += len(entries)
        for entry in entries:
            reason = violation(entry)
            if reason:
                failures.append(f"{target}: {entry} ({reason})")
    if failures:
        print("RELEASE AUDIT FAILED", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"release audit ok: {len(args.targets)} target(s), {total} entries checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
