"""Robust recursive file discovery with logging and error handling.

Handles symlinks, permission errors, and max-depth guards to prevent
runaway crawls on large or cyclic file systems.
"""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .logger import get_logger

log = get_logger("file_crawler")

# Extension groups by source type
EXTENSIONS = {
    "tableau": {".twb", ".tds", ".twbx", ".tdsx"},
    "looker": {".lkml", ".lookml"},
    "powerbi": {".pbix", ".pbit", ".pbip", ".bim", ".tmdl"},
    "denodo": {".vql"},
    "businessobjects": {".biar", ".unx", ".unv", ".json", ".xml"},
}


@dataclass
class DiscoveredFile:
    """Metadata about a discovered source file."""
    path: str
    name: str
    extension: str
    size_bytes: int
    source_type: str
    relative_path: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "source_type": self.source_type,
            "relative_path": self.relative_path,
        }


def discover_files(
    root_path: str,
    source_type: str | None = None,
    extensions: set[str] | None = None,
    max_depth: int = 10,
    max_files: int = 10000,
    follow_symlinks: bool = False,
) -> list[DiscoveredFile]:
    """Recursively discover source files under root_path.

    Args:
        root_path: Directory to crawl.
        source_type: One of 'tableau', 'looker', 'powerbi', 'denodo',
            'businessobjects'. Determines which extensions to match.
            Ignored if extensions is provided.
        extensions: Explicit set of extensions to match (e.g. {'.twb', '.tds'}).
        max_depth: Maximum directory depth to prevent runaway crawls.
        max_files: Safety cap on number of files returned.
        follow_symlinks: Whether to follow symbolic links.

    Returns:
        List of DiscoveredFile objects, sorted by path.

    Raises:
        FileDiscoveryError: If root_path doesn't exist or isn't readable.
    """
    from .errors import FileDiscoveryError

    root = Path(root_path).resolve()
    if not root.exists():
        raise FileDiscoveryError(
            f"Root path does not exist: {root_path}",
            context={"root_path": root_path},
        )
    if not root.is_dir():
        raise FileDiscoveryError(
            f"Root path is not a directory: {root_path}",
            context={"root_path": root_path},
        )

    # Determine which extensions to look for
    if extensions is None:
        if source_type and source_type in EXTENSIONS:
            target_exts = EXTENSIONS[source_type]
        elif source_type:
            raise FileDiscoveryError(
                f"Unknown source_type: {source_type}. "
                f"Valid types: {', '.join(sorted(EXTENSIONS.keys()))}",
                context={"source_type": source_type},
            )
        else:
            # All known extensions
            target_exts = set()
            for ext_set in EXTENSIONS.values():
                target_exts.update(ext_set)
    else:
        target_exts = extensions

    log.info(
        "Crawling %s for extensions %s (max_depth=%d, follow_symlinks=%s)",
        root, sorted(target_exts), max_depth, follow_symlinks,
    )

    results: list[DiscoveredFile] = []
    dirs_visited: set[str] = set()
    permission_errors: list[str] = []
    symlink_skips: list[str] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            log.warning("Max depth %d reached at %s — skipping deeper.", max_depth, current)
            return
        if len(results) >= max_files:
            log.warning("Max files cap %d reached — stopping crawl.", max_files)
            return

        real_path = str(current.resolve())
        if real_path in dirs_visited:
            log.warning("Cycle detected at %s — skipping.", current)
            return
        dirs_visited.add(real_path)

        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            permission_errors.append(str(current))
            log.warning("Permission denied: %s — skipping.", current)
            return
        except OSError as e:
            log.warning("OS error reading %s: %s — skipping.", current, e)
            return

        for entry in entries:
            if len(results) >= max_files:
                return

            # Handle symlinks
            if entry.is_symlink():
                if not follow_symlinks:
                    symlink_skips.append(str(entry))
                    continue
                try:
                    entry.resolve(strict=True)
                except OSError:
                    log.warning("Broken symlink: %s — skipping.", entry)
                    continue

            if entry.is_dir():
                _walk(entry, depth + 1)
            elif entry.is_file():
                ext = entry.suffix.lower()
                if ext in target_exts:
                    try:
                        st = entry.stat()
                        size = st.st_size
                    except OSError:
                        size = -1

                    # Infer source type from extension
                    file_source_type = source_type or _infer_source_type(ext)

                    results.append(
                        DiscoveredFile(
                            path=str(entry),
                            name=entry.name,
                            extension=ext,
                            size_bytes=size,
                            source_type=file_source_type,
                            relative_path=str(entry.relative_to(root)),
                        )
                    )

    _walk(root, depth=0)

    results.sort(key=lambda f: f.path)

    log.info(
        "Crawl complete: %d files found, %d dirs visited, "
        "%d permission errors, %d symlinks skipped.",
        len(results), len(dirs_visited),
        len(permission_errors), len(symlink_skips),
    )

    if permission_errors:
        log.warning("Directories with permission errors: %s", permission_errors)

    return results


def _infer_source_type(ext: str) -> str:
    """Infer the source type from a file extension."""
    for stype, exts in EXTENSIONS.items():
        if ext in exts:
            return stype
    return "unknown"
