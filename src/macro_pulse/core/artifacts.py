import tempfile
from pathlib import Path

from .logging import get_logger


logger = get_logger(__name__)


def create_temp_png_path(prefix: str) -> str:
    with tempfile.NamedTemporaryFile(
        prefix=f"macro_pulse_{prefix}_",
        suffix=".png",
        delete=False,
    ) as handle:
        return handle.name


def resolve_output_path(output_path: str | None, prefix: str) -> str:
    return output_path or create_temp_png_path(prefix)


def cleanup_files(file_paths) -> None:
    for file_path in file_paths:
        if not file_path:
            continue

        path = Path(file_path)
        if path.exists():
            path.unlink()
            logger.info("Removed temporary file: %s", path)
