"""Host image-source location shared by authoring and submission tools."""

from pathlib import Path


def image_source_root(env_dir: Path) -> Path:
    """Every environment authors image inputs in image/."""
    return env_dir / "image"
