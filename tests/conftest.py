"""pytest 兼容：保证仓库根目录在 sys.path 上（unittest 由 python -m 自动处理）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
