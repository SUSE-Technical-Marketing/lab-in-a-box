#!/usr/bin/env python3.11
"""Build static offline version of cube canvas webui with embedded schemas."""

import json
import subprocess
import sys
from pathlib import Path


def load_schemas():
    """Load addon schemas, with fallback to filesystem if schema.json is invalid."""
    schema_file = Path(__file__).parent.parent / "webui" / "htdocs" / "schema.json"

    # Try to load existing schema.json
    if schema_file.exists():
        try:
            with open(schema_file) as f:
                content = f.read().strip()
                if content:
                    data = json.loads(content)
                    if data.get("addons"):  # Valid and has addons
                        return data
        except json.JSONDecodeError:
            pass

    # Fallback: generate from install_*.py files
    print(f"Fallback: Generating addon list from install_*.py scripts", file=sys.stderr)
    scripts_dir = Path(__file__).parent
    addon_list = sorted([s.stem.replace("install_", "") for s in scripts_dir.glob("install_*.py")])

    return {
        "version": "1.0",
        "generated": True,
        "addons": addon_list,
        "infrastructure_addons": [],
        "schemas": {}
    }


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

    # Find where to inject schemas (right after <head> tag, BEFORE any scripts load)
    head_start = html.find("<head>") + len("<head>")

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

    html = html[:head_start] + schema_inject + html[head_start:]

    # Replace __LABVERSION__ with actual git commit hash
    html = html.replace("__LABVERSION__", version)

    # Find where to inject API overrides (after app.js loads, before closing </body>)
    body_end = html.rfind("</body>")

    api_override = """
  <script>
// Static mode CSS: hide server-dependent elements
const style = document.createElement('style');
style.textContent = `
  button[onclick*="validate"], button[onclick*="save"], button[onclick*="refresh"] { display: none !important; }
  #statusPanel { display: none !important; }
  .actions { opacity: 1; }
  .actions .btn.disabled { opacity: 0.5; cursor: not-allowed; }
`;
document.head.appendChild(style);

// Hide Refresh Images button by text content and attributes (can't use :has-text in CSS)
const hideButtonsByText = () => {
  Array.from(document.querySelectorAll('button')).forEach(btn => {
    const txt = btn.textContent.toLowerCase();
    const onclick = (btn.getAttribute('onclick') || '').toLowerCase();
    if (txt.includes('refresh') || txt.includes('validate') || txt.includes('save') ||
        onclick.includes('refresh') || onclick.includes('validate') || onclick.includes('save')) {
      btn.style.display = 'none !important';
      btn.disabled = true;
    }
  });
};
// Run after page loads
document.addEventListener('DOMContentLoaded', hideButtonsByText);
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', hideButtonsByText);
} else {
  hideButtonsByText();
}
// Also watch for dynamically added buttons
const observer = new MutationObserver(() => hideButtonsByText());
observer.observe(document.body, { childList: true, subtree: true });

// Override API functions for static mode
const originalApiGet = window.apiGet;
window.apiGet = async function(action, params = {}) {
  if (action === 'components') {
    if (!window.EMBEDDED_SCHEMAS) {
      const msg = 'EMBEDDED_SCHEMAS not loaded - check if schemas script is before this one';
      console.error(msg);
      alert(msg);
      return { components: [], infrastructure: [], count: 0, infrastructure_count: 0, scripts_dir: 'embedded' };
    }
    if (!window.EMBEDDED_SCHEMAS.schemas) {
      console.error('EMBEDDED_SCHEMAS.schemas is missing');
      return { components: [], infrastructure: [], count: 0, infrastructure_count: 0, scripts_dir: 'embedded' };
    }
    if (!window.EMBEDDED_SCHEMAS.addons) {
      console.error('EMBEDDED_SCHEMAS.addons array missing');
      return { components: [], infrastructure: [], count: 0, infrastructure_count: 0, scripts_dir: 'embedded' };
    }

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

    const addons = window.EMBEDDED_SCHEMAS.addons || [];
    const infraAddons = window.EMBEDDED_SCHEMAS.infrastructure_addons || [];
    const regular = addons.map(buildComponent);
    const infrastructure = infraAddons.map(buildComponent);

    console.log(`✓ API: Loaded ${regular.length} regular addons, ${infrastructure.length} infrastructure addons`);

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
