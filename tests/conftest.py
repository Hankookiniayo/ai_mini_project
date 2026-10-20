"""pytest 공용 설정 — 프로젝트 루트를 import 경로에 넣는다.

실행: python -m pytest -q   (외부 서비스·API 키 없이 전부 오프라인으로 돈다)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
