#!/usr/bin/env python3.11
"""Build a self-contained, static webui HTML file with embedded schemas."""

import json
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

def build_static_html(schemas, files):
    """Assemble the static HTML with embedded schemas and assets."""

    html_content = files.get("index.html", "")
    css_content = files.get("style.css", "")
    js_content = files.get("app.js", "")
    theme_content = files.get("theme.js", "")

    # Extract head and body from original HTML
    head = extract_head(html_content)
    body = extract_body(html_content)

    # Build the embedded schema script
    schemas_json = json.dumps(schemas)
    schema_script = f"""<script id="embedded-schemas" type="application/json">
{schemas_json}
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
// Embedded schemas for static generation
window.EMBEDDED_SCHEMAS = JSON.parse(document.getElementById('embedded-schemas').textContent);

// Override apiGet for schema/components to use embedded data
const originalApiGet = window.apiGet || (async (action) => {{}});
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
  // Static mode: disable status (no hypervisor connection)
  if (action === 'status') {{
    return {{ available: false }};
  }}
  // Static mode: disable validate/save (no backend)
  if (action === 'validate' || action === 'save') {{
    throw new Error(`${{action}} requires a backend (not available in static mode)`);
  }}
  // Try original for 'base' and others, but catch failures
  try {{
    return await originalApiGet.call(this, action, params);
  }} catch (e) {{
    console.warn(`API call ${{action}} failed (static mode?):`, e.message);
    throw e;
  }}
}};

{js_content}
  </script>
</body>
</html>"""

    return output

def main():
    """Main entry point."""
    schemas = load_schemas()
    files = load_files()

    output_path = Path(__file__).parent.parent / "webui" / "htdocs" / "index-static.html"

    static_html = build_static_html(schemas, files)

    with open(output_path, "w") as f:
        f.write(static_html)

    print(f"✓ Built {output_path} ({len(static_html)} bytes)", file=sys.stderr)
    print(f"✓ Embedded {len(schemas['addons'])} addon schemas", file=sys.stderr)
    print(output_path)

if __name__ == "__main__":
    main()
