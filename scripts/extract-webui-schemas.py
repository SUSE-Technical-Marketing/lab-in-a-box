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
    infrastructure_addons = []

    # Find all install_*.py scripts
    addon_scripts = sorted(scripts_dir.glob("install_*.py"))

    print(f"Extracting schemas from {len(addon_scripts)} addons...", file=sys.stderr)
    print(f"Scripts dir: {scripts_dir}", file=sys.stderr)
    print(f"Python: {sys.executable}", file=sys.stderr)

    failed_count = 0
    for script_path in addon_scripts:
        addon_name = script_path.stem.replace("install_", "")

        try:
            cmd = ["python3.11", str(script_path), "--schema"]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and result.stdout.strip():
                try:
                    schema = json.loads(result.stdout)
                    addon = schema.get("addon", addon_name)
                    schemas[addon] = schema

                    # Check if addon declares itself as infrastructure via metadata
                    is_infrastructure = schema.get("metadata", {}).get("infrastructure", False)

                    if is_infrastructure:
                        infrastructure_addons.append(addon)
                        print(f"  ✓ {addon} (infrastructure)", file=sys.stderr)
                    else:
                        addon_list.append(addon)
                        print(f"  ✓ {addon}", file=sys.stderr)
                except json.JSONDecodeError as je:
                    print(f"  ✗ {addon_name}: invalid JSON - {je}", file=sys.stderr)
                    failed_count += 1
            else:
                if result.returncode != 0:
                    print(f"  ✗ {addon_name}: exit code {result.returncode}", file=sys.stderr)
                    if result.stderr:
                        print(f"      stderr: {result.stderr[:150]}", file=sys.stderr)
                else:
                    print(f"  ✗ {addon_name}: no output", file=sys.stderr)
                failed_count += 1
        except subprocess.TimeoutExpired:
            print(f"  ✗ {addon_name}: timeout (>10s)", file=sys.stderr)
            failed_count += 1
        except Exception as e:
            print(f"  ✗ {addon_name}: {type(e).__name__}: {e}", file=sys.stderr)
            failed_count += 1

    print(f"Extraction complete: {len(addon_list)} regular + {len(infrastructure_addons)} infrastructure, {failed_count} failed", file=sys.stderr)

    return {
        "version": "1.0",
        "generated": True,
        "addons": addon_list,
        "infrastructure_addons": infrastructure_addons,
        "schemas": schemas
    }

def main():
    """Main entry point."""
    try:
        schemas = extract_addon_schemas()

        # Output to stdout (always output valid JSON, even if empty)
        output = json.dumps(schemas, indent=2)
        print(output)

        if not output:
            print("ERROR: JSON output is empty!", file=sys.stderr)
            sys.exit(1)

        print(f"\nExtracted {len(schemas['addons'])} addon schemas", file=sys.stderr)
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        # Output empty but valid JSON as fallback
        fallback = {"version": "1.0", "generated": False, "addons": [], "infrastructure_addons": [], "schemas": {}, "error": str(e)}
        print(json.dumps(fallback, indent=2))
        sys.exit(1)

if __name__ == "__main__":
    # Flush all output immediately to prevent buffering issues
    sys.stderr.flush()
    main()
    sys.stdout.flush()
    sys.stderr.flush()
