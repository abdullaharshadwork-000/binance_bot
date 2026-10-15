"""Load the embedded dashboard single-page app.

The HTML/JS template lives in dashboard_template.html next to this module so
editors and linters can treat it as a real file. DASHBOARD_HTML remains a
plain string with identical content for backwards compatibility.
"""

from pathlib import Path

DASHBOARD_HTML = Path(__file__).with_name("dashboard_template.html").read_text(
    encoding="utf-8"
)
