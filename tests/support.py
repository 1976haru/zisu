import shutil
import uuid
from pathlib import Path

class WorkspaceTemp:
    """Python 3.14/managed-Windows friendly temporary directory."""
    def __init__(self):
        self.path = Path.cwd()/".test_tmp"/("case_"+uuid.uuid4().hex)
    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=False)
        return self.path
    def __exit__(self, *_):
        shutil.rmtree(self.path, ignore_errors=True)
