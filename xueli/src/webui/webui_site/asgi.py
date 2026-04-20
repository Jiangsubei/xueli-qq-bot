import os
import sys
from pathlib import Path

from django.core.asgi import get_asgi_application


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webui_site.settings")

application = get_asgi_application()
