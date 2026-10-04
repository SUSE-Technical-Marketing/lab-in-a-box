#!/usr/bin/env python3.11
"""Extract addon schemas for static webui generation."""

import json
import subprocess
import sys
from pathlib import Path

def extract_addon_schemas():
    """Extract schemas from all install_*.py scripts."""
    scripts_dir = Path(__file__).parent
    schemas = {}
    addon_list = []

    # Addons that are infrastructure/internal, not user-facing
    skip_addons = {"pxe"}

    # Find all install_*.py scripts
    addon_scripts = sorted(scripts_dir.glob("install_*.py"))

    print(f"Extracting schemas from {len(addon_scripts)} addons...", file=sys.stderr)

    for script_path in addon_scripts:
        addon_name = script_path.stem.replace("install_", "")

        # Skip internal infrastructure addons
        if addon_name in skip_addons:
            print(f"  ⊘ {addon_name}: infrastructure only", file=sys.stderr)
            continue

        try:
            result = subprocess.run(
                ["python3.11", str(script_path), "--schema"],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0 and result.stdout.strip():
                schema = json.loads(result.stdout)
                addon = schema.get("addon", addon_name)
                schemas[addon] = schema
                addon_list.append(addon)
                print(f"  ✓ {addon}", file=sys.stderr)
            else:
                print(f"  ✗ {addon_name}: no schema", file=sys.stderr)
        except Exception as e:
            print(f"  ✗ {addon_name}: {e}", file=sys.stderr)

    return {
        "version": "1.0",
        "generated": True,
        "addons": addon_list,
        "schemas": schemas
    }

def main():
    """Main entry point."""
    schemas = extract_addon_schemas()

    # Output to stdout
    print(json.dumps(schemas, indent=2))

    print(f"\nExtracted {len(schemas['addons'])} addon schemas", file=sys.stderr)

if __name__ == "__main__":
    main()
