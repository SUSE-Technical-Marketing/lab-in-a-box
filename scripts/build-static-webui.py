#!/usr/bin/env python3.11
"""Build a minimal static webui HTML with embedded schemas (no backend needed)."""

import json
import subprocess
import sys
from pathlib import Path


def load_schemas():
    """Load the generated addon schemas."""
    schema_file = Path(__file__).parent.parent / "webui" / "htdocs" / "schema.json"
    if not schema_file.exists():
        print(f"Error: {schema_file} not found. Run extract-webui-schemas.py first.", file=sys.stderr)
        sys.exit(1)
    with open(schema_file) as f:
        return json.load(f)


def load_base_schema():
    """Load the base schema from lab_schema --base."""
    try:
        result = subprocess.run(
            ["python3.11", str(Path(__file__).parent / "lab_schema"), "--base"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        print(f"Warning: Could not load base schema: {e}", file=sys.stderr)
    return {}


def build_static_html(schemas, base_schema):
    """Build minimal static webui HTML with embedded schemas."""

    html_file = Path(__file__).parent.parent / "webui" / "htdocs" / "lab-builder-static.html"
    with open(html_file) as f:
        html_content = f.read()

    # Embed addon schemas
    schemas_json = json.dumps(schemas)
    html_content = html_content.replace(
        '{"addons":[],"schemas":{}}',
        schemas_json,
        1
    )

    # Embed base schema
    base_json = json.dumps(base_schema)
    html_content = html_content.replace(
        '{"sections":{}}',
        base_json,
        1
    )

    return html_content


def main():
    """Main entry point."""
    schemas = load_schemas()
    base_schema = load_base_schema()

    output_path = Path(__file__).parent.parent / "webui" / "htdocs" / "lab-builder-static.html"

    static_html = build_static_html(schemas, base_schema)

    with open(output_path, "w") as f:
        f.write(static_html)

    print(f"✓ Built {output_path} ({len(static_html)} bytes)", file=sys.stderr)
    print(f"✓ Embedded {len(schemas['addons'])} addon schemas", file=sys.stderr)
    print(output_path)


if __name__ == "__main__":
    main()
