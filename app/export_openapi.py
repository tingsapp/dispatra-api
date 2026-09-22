"""Run with python -m app.export_openapi, then npm run generate:api in client."""
import json
from pathlib import Path
from .main import app
if __name__ == '__main__':
    Path('openapi.json').write_text(json.dumps(app.openapi(), indent=2) + '\n')
