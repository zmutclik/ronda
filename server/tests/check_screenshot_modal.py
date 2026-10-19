"""Runnable assert-based self-check for screenshot panel and fullscreen modal."""
import os
from datetime import datetime, UTC
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "mysql+asyncmy://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "selfchecksecrettestkey123456789012")

from app.core.templating import templates

h = SimpleNamespace(id=1, name="PC-TEST")
d = SimpleNamespace(online=True)
user = SimpleNamespace(can=lambda p: True)
shot = SimpleNamespace(
    id=99,
    status="uploaded",
    requested_at=datetime.now(UTC),
    width=1920,
    height=1080,
    reason="periksa keluhan",
    expires_at=datetime.now(UTC),
)
shots = [(shot, "operator")]

tmpl = templates.get_template("hosts/_tab_screenshot.html")
out = tmpl.render(h=h, d=d, shots=shots, current_user=user, error=None, reason="")

assert '<img src="/hosts/1/screenshots/99"' in out, "Latest screenshot image missing in panel shot"
assert 'dialog id="shot-modal-99" class="modal-fs"' in out, "Fullscreen modal missing"
assert 'data-target="shot-modal-99"' in out, "Modal trigger missing"
assert 'data-action="close-dialog"' in out, "Close dialog button missing"
print("PASS: screenshot modal check passed.")
