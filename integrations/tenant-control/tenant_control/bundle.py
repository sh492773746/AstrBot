"""Package the AstrBot plugin from the canonical, shared billing implementation."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build_plugin(output: Path) -> Path:
    """Build an installable ZIP without deployment code, credentials or databases.

    Args:
        output: New ZIP path; an existing archive is never overwritten.

    Returns:
        Absolute path of the generated archive.
    """
    source = Path(__file__).resolve().parent
    plugin = source.parent / "astrbot_plugin_tenant_control"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name in (
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "requirements.txt",
        ):
            archive.write(plugin / name, name)
        for name in ("__init__.py", "control.py", "store.py", "runtime.py"):
            archive.write(source / name, f"tenant_control/{name}")
        for name in ("README.md", "controller-config.example.json"):
            archive.write(source.parent / name, name)
    return output
