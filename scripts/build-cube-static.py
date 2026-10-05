#!/usr/bin/env python3.11
"""Build static offline version of cube canvas webui with embedded schemas."""

import json
import subprocess
import sys
from pathlib import Path


def load_schemas():
    """Load addon schemas."""
    schema_file = Path(__file__).parent.parent / "webui" / "htdocs" / "schema.json"
    if not schema_file.exists():
        print(f"Error: {schema_file} not found", file=sys.stderr)
        sys.exit(1)
    with open(schema_file) as f:
        return json.load(f)


def load_base_schema():
    """Load base schema."""
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


def get_version():
    """Get version from git or return a placeholder."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def build():
    """Build static cube canvas webui."""
    schemas = load_schemas()
    base_schema = load_base_schema()
    version = get_version()

    # Read original index.html (the cube canvas)
    index_path = Path(__file__).parent.parent / "webui" / "htdocs" / "index.html"
    with open(index_path) as f:
        html = f.read()

    # Find where to inject schemas (before </head>)
    head_end = html.find("</head>")

    # Create schema scripts
    schemas_json = json.dumps(schemas)
    base_json = json.dumps(base_schema)

    schema_inject = f"""
  <script id="embedded-schemas" type="application/json">
{schemas_json}
  </script>
  <script id="embedded-base-schema" type="application/json">
{base_json}
  </script>
  <script>
// Static mode overrides: intercept API calls to use embedded data
window.STATIC_MODE = true;
window.EMBEDDED_SCHEMAS = JSON.parse(document.getElementById('embedded-schemas').textContent);
window.EMBEDDED_BASE_SCHEMA = JSON.parse(document.getElementById('embedded-base-schema').textContent);
  </script>
"""

    html = html[:head_end] + schema_inject + html[head_end:]

    # Replace __LABVERSION__ with actual version
    html = html.replace("__LABVERSION__", version)

    # Find where to inject API overrides (after app.js loads, before closing </body>)
    body_end = html.rfind("</body>")

    api_override = """
  <script>
// Static mode CSS: hide server-dependent buttons
const style = document.createElement('style');
style.textContent = `
  button[onclick*="validate"], button[onclick*="save"], button[onclick*="refresh"], button:has-text("Refresh Images") { display: none !important; }
  button:contains("Refresh Images") { display: none !important; }
  .actions { opacity: 1; }
  .actions .btn.disabled { opacity: 0.5; cursor: not-allowed; }
`;
document.head.appendChild(style);

// Also hide by text content for Refresh Images button
setTimeout(() => {
  Array.from(document.querySelectorAll('button')).forEach(btn => {
    if (btn.textContent.includes('Refresh Images')) btn.style.display = 'none';
  });
}, 100);

// Override API functions for static mode
const originalApiGet = window.apiGet;
window.apiGet = async function(action, params = {}) {
  if (action === 'components') {
    const buildComponent = (name) => {
      const schema = window.EMBEDDED_SCHEMAS.schemas[name] || {};
      const fields = schema.fields || [];
      const countFields = (arr) => {
        if (!Array.isArray(arr)) return 0;
        return arr.reduce((n, f) => {
          if (f && typeof f === 'object' && f.name && f.type) return n + 1;
          if (f && f.fields) return n + countFields(f.fields);
          return n;
        }, 0);
      };
      return {
        name,
        title: schema.title || name,
        description: schema.description || '',
        field_count: countFields(fields),
        layers: (schema.capabilities && schema.capabilities.layers) || []
      };
    };

    const regular = (window.EMBEDDED_SCHEMAS.addons || []).map(buildComponent);
    const infrastructure = (window.EMBEDDED_SCHEMAS.infrastructure_addons || []).map(buildComponent);

    return {
      components: regular,
      infrastructure: infrastructure,
      count: regular.length,
      infrastructure_count: infrastructure.length,
      scripts_dir: 'embedded'
    };
  }
  if (action === 'schema') {
    const comp = params.name;
    if (!comp || !window.EMBEDDED_SCHEMAS.schemas[comp]) {
      throw new Error(`Schema not found for ${comp}`);
    }
    return window.EMBEDDED_SCHEMAS.schemas[comp];
  }
  if (action === 'base') {
    return window.EMBEDDED_BASE_SCHEMA;
  }
  if (action === 'status') {
    return { available: false };
  }
  if (action === 'validate') {
    return { valid: true, errors: [] };
  }
  if (action === 'save') {
    throw new Error('In static mode, use Download to save lab.json');
  }
  throw new Error(`Unsupported in static mode: ${action}`);
};
  </script>
"""

    html = html[:body_end] + api_override + html[body_end:]

    return html


def main():
    html = build()

    output_path = Path(__file__).parent.parent / "webui" / "htdocs" / "lab-builder-static.html"
    with open(output_path, "w") as f:
        f.write(html)

    print(f"✓ Built {output_path} ({len(html)} bytes)", file=sys.stderr)
    print(f"✓ Embedded {len(load_schemas()['addons'])} addon schemas", file=sys.stderr)
    print(output_path)


if __name__ == "__main__":
    main()
