#!/usr/bin/env python3.11
"""Build a self-contained, static webui HTML file with embedded schemas."""

import json
import subprocess
import sys
from pathlib import Path

def load_schemas():
    """Load the generated schemas."""
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
    return None

def load_files():
    """Load HTML, CSS, and JS files."""
    base = Path(__file__).parent.parent / "webui" / "htdocs"
    files = {}
    for fname in ["index.html", "style.css", "app.js", "theme.js"]:
        fpath = base / fname
        if not fpath.exists():
            print(f"Warning: {fpath} not found, skipping", file=sys.stderr)
            continue
        with open(fpath) as f:
            files[fname] = f.read()
    return files

def extract_head(html):
    """Extract everything before <body> from index.html."""
    try:
        head_end = html.index("<body")
        return html[:head_end]
    except ValueError:
        return html

def extract_body(html):
    """Extract everything from <body> tag onwards."""
    try:
        body_start = html.index("<body")
        return html[body_start:]
    except ValueError:
        return "<body></body>"

def build_static_html(schemas, base_schema, files):
    """Assemble the static HTML with embedded schemas and assets."""

    html_content = files.get("index.html", "")
    css_content = files.get("style.css", "")
    js_content = files.get("app.js", "")
    theme_content = files.get("theme.js", "")

    # Extract head and body from original HTML
    head = extract_head(html_content)
    body = extract_body(html_content)

    # Update description for static version
    body = body.replace(
        "Forms generated live from each component's <code>--schema</code>. Nothing here is hard&#8209;coded.",
        "Static webui: schemas embedded at build time. Run locally with <code>webui/run-local.py</code> for live updates."
    )

    # Build the embedded schemas
    schemas_json = json.dumps(schemas)
    base_schema_json = json.dumps(base_schema or {})
    schema_script = f"""<script id="embedded-schemas" type="application/json">
{schemas_json}
</script>
<script id="embedded-base-schema" type="application/json">
{base_schema_json}
</script>"""

    # Build final HTML with:
    # 1. Original head
    # 2. Embedded CSS
    # 3. Embedded schema
    # 4. Original body
    # 5. Embedded JS (theme first, then app)

    output = f"""{head}
  <style>
{css_content}
  </style>
  {schema_script}
</head>
{body}
  <script>
{theme_content}
  </script>
  <script>
// Load embedded schemas before app.js runs
window.EMBEDDED_SCHEMAS = JSON.parse(document.getElementById('embedded-schemas').textContent);
window.EMBEDDED_BASE_SCHEMA = JSON.parse(document.getElementById('embedded-base-schema').textContent);
  </script>

{js_content}

// Override apiGet for static mode (after app.js loaded its definition)
const _originalApiGet = window.apiGet;
window.apiGet = async function(action, params = {{}}) {{
  if (action === 'components') {{
    return {{
      components: window.EMBEDDED_SCHEMAS.addons.map(name => {{
        const schema = window.EMBEDDED_SCHEMAS.schemas[name] || {{}};
        const fields = schema.fields || [];
        const countFields = (arr) => (Array.isArray(arr)
          ? arr.reduce((n, f) => n + (f && typeof f === 'object' && f.name && f.type ? 1 : countFields(f.fields || [])), 0)
          : 0);
        return {{
          name,
          title: schema.title || name,
          description: schema.description || '',
          field_count: countFields(fields),
          layers: (schema.capabilities && schema.capabilities.layers) || []
        }};
      }}),
      count: window.EMBEDDED_SCHEMAS.addons.length,
      scripts_dir: 'embedded'
    }};
  }}
  if (action === 'schema') {{
    const comp = params.name;
    if (!comp || !window.EMBEDDED_SCHEMAS.schemas[comp]) {{
      throw new Error(`Schema not found for ${{comp}}`);
    }}
    return window.EMBEDDED_SCHEMAS.schemas[comp];
  }}
  if (action === 'base') {{
    return window.EMBEDDED_BASE_SCHEMA;
  }}
  if (action === 'status') {{
    return {{ available: false }};
  }}
  if (action === 'validate') {{
    return {{ valid: true, errors: [] }};
  }}
  if (action === 'save') {{
    throw new Error('Save requires a backend. Use Download instead to save lab.json locally.');
  }}
  throw new Error(`Unsupported in static mode: ${{action}}`);
}};
  </script>
</body>
</html>"""

    return output

def main():
    """Main entry point."""
    schemas = load_schemas()
    base_schema = load_base_schema()
    files = load_files()

    output_path = Path(__file__).parent.parent / "webui" / "htdocs" / "index-static.html"

    static_html = build_static_html(schemas, base_schema, files)

    with open(output_path, "w") as f:
        f.write(static_html)

    print(f"✓ Built {output_path} ({len(static_html)} bytes)", file=sys.stderr)
    print(f"✓ Embedded {len(schemas['addons'])} addon schemas", file=sys.stderr)
    print(output_path)

if __name__ == "__main__":
    main()
