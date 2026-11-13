"""보드에 파일로 저장하는 곳 (서버를 껐다 켜도 남게).

data/                       (ONMAEUM_DATA_DIR 로 바꿀 수 있음)
  escalators.json           앱에서 새로 등록한 설비
  inspections/<점검번호>.json  제출한 점검 (지난 점검 상세 보기용)
  reports/<점검번호>.html      점검 보고서 (폰 브라우저에서 열고 인쇄·PDF 저장)

TODO(정래원): SQLite 로 바꾸면 이 파일의 함수 안만 바꾸면 됨 (main.py 는 그대로).
"""
import json  # 파일에 JSON 으로 저장
import os  # 환경변수
import re  # 파일 이름 검사
from pathlib import Path  # 경로

DATA_DIR = Path(os.environ.get("ONMAEUM_DATA_DIR", "data"))  # 저장 폴더
_SAFE = re.compile(r"^[A-Za-z0-9_\-]+$")  # 파일 이름으로 써도 되는 글자만 (../ 같은 경로 공격 막기)


def _write(path: Path, text: str):  # 반쯤 쓰다 꺼져도 파일이 깨지지 않게: 임시 파일에 쓰고 바꿔치기
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ───────────── 설비 ─────────────
def load_escalators() -> dict:  # 등록한 설비 전부 {escalatorId: 설비}
    p = DATA_DIR / "escalators.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_escalators(escalators: dict):  # 등록한 설비 전부 저장
    _write(DATA_DIR / "escalators.json", json.dumps(escalators, ensure_ascii=False, indent=2))


# ───────────── 제출한 점검 ─────────────
def save_inspection(insp: dict):  # 제출한 점검 하나 저장
    iid = insp["inspectionId"]
    if not _SAFE.match(iid):
        return
    data = {k: v for k, v in insp.items() if k not in ("seen", "skipped", "pending", "log", "lastSpeech")}  # 진행 중에만 쓰던 값은 빼고
    _write(DATA_DIR / "inspections" / f"{iid}.json", json.dumps(data, ensure_ascii=False, indent=2))


def load_inspections() -> list:  # 저장된 점검 전부 (오래된 것부터)
    d = DATA_DIR / "inspections"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:  # 깨진 파일은 건너뜀
            print("점검 파일을 읽지 못함:", p, e)
    return sorted(out, key=lambda x: x.get("submittedAt") or "")


# ───────────── 보고서 ─────────────
def save_report(iid: str, html: str):
    if _SAFE.match(iid):
        _write(DATA_DIR / "reports" / f"{iid}.html", html)


def report_path(iid: str):  # 보고서 파일 경로 (없으면 None)
    if not _SAFE.match(iid):
        return None
    p = DATA_DIR / "reports" / f"{iid}.html"
    return p if p.exists() else None
