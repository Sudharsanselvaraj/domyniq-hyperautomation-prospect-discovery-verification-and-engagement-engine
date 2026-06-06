"""
conftest.py — Shared pytest fixtures

Loaded automatically by pytest before any test file.
"""

import sys
from pathlib import Path

# Ensure the project root is on sys.path so imports work without installing
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
