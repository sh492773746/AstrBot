"""Refresh only server runtime routes and their referenced schemas."""

from pathlib import Path

import yaml
from fastapi import FastAPI

from astrbot.dashboard.api.plugins import router


def update():
    """Keep unrelated API declarations unchanged while exporting runtime types."""
    path = Path(__file__).resolve().parents[1] / "openspec/openapi-v1.yaml"
    document = yaml.safe_load(path.read_text())
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/plugins")
    generated = app.openapi()
    paths = {
        name: value
        for name, value in generated["paths"].items()
        if name.startswith("/api/v1/plugins/runtime/")
    }
    schemas = {}

    def collect(value):
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            reference = value.get("$ref", "")
            if reference.startswith("#/components/schemas/"):
                name = reference.rsplit("/", 1)[-1]
                if name not in schemas:
                    schemas[name] = generated["components"]["schemas"][name]
                    collect(schemas[name])
            for item in value.values():
                collect(item)

    collect(paths)
    document["paths"].update(paths)
    document.setdefault("components", {}).setdefault("schemas", {}).update(schemas)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    print(f"Exported {len(paths)} server routes and {len(schemas)} referenced schemas.")


if __name__ == "__main__":
    update()
