"""모시응 보드 API 서버 (FastAPI) — 노트북을 '가짜 보드'로 쓰는 시작 버전.

실행:  pip install -r requirements.txt
       uvicorn main:app --host 0.0.0.0 --port 8080
앱 연결: 앱의 useServer = true, 핸즈프리 화면 '주소 바꾸기'에 노트북·보드 주소 (에뮬레이터면 10.0.2.2)
모델:  환경변수로 켬 (pipeline.py 위쪽 설명, README.md 참고). 안 정하면 '글자로 보내기' 테스트용.

저장소는 메모리(dict)라 서버를 끄면 사라진다. TODO(정래원): SQLite로 교체.
"""
import itertools  # 점검 번호를 1, 2, 3… 차례로 만드는 카운터
import re  # 글자만 남기기 (잡음 거르기)
import time  # 처리 시간 재기
import threading  # 잠금 (한 번에 발화 하나만 처리)
from contextlib import asynccontextmanager  # 서버 시작·종료 때 할 일
from datetime import datetime  # 현재 시각 (점검 번호, 기록 시각)

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # FastAPI: 서버 본체 / File·Form: 업로드 파일·폼 값 받기 / HTTPException: 오류 응답 / UploadFile: 받은 파일
from fastapi.concurrency import run_in_threadpool  # 오래 걸리는 모델 실행을 별도 스레드에서 (서버가 멈추지 않게)
from fastapi.responses import FileResponse, HTMLResponse  # 음성 파일·보고서 보내기

import pipeline as P  # STT·정규화·LLM·파싱·규칙 엔진 로직 (pipeline.py)
import report as RPT  # 점검 보고서 HTML 만들기 (report.py)
import storage as S  # 보드에 파일로 저장 (storage.py, 나중에 SQLite 로 교체)


@asynccontextmanager
async def lifespan(app):  # 서버가 켜질 때 / 꺼질 때
    P.load_models()  # 모델은 켜질 때 한 번만 불러옴 (요청마다 불러오면 한 문장에 수십 초)
    restore()  # 보드에 저장해 둔 설비·지난 점검 불러오기
    yield
    P.unload_models()  # 꺼질 때 NPU 자원 해제


app = FastAPI(title="모시응 보드 API", lifespan=lifespan)  # 서버 객체 만들기 (uvicorn main:app 의 app)
_proc_lock = threading.Lock()  # 발화는 한 번에 하나씩 (순서·중복 방지)

# ───────────────────────── 메모리 저장소 (앱의 on_device_db.dart와 같은 시연 데이터) ─────────────────────────
ESCALATORS = {  # 설비 목록: QR 값(esc-7) → 설비 정보. 앱 Equipment.fromJson 이 읽는 필드 이름 그대로
    "esc-1": {"escalatorId": "esc-1", "name": "1호기 에스컬레이터", "location": "정문 1층",
              "spec": {"elevatorNo": "2104-381", "manufacturer": "가나승강기", "ratedSpeed": 0.5, "driveType": "인버터식"}},
    "esc-2": {"escalatorId": "esc-2", "name": "2호기 에스컬레이터", "location": "B동 1층",
              "spec": {"elevatorNo": "2104-382", "manufacturer": "가나승강기", "ratedSpeed": 0.5, "driveType": "인버터식"}},
    "esc-8": {"escalatorId": "esc-8", "name": "8호기 에스컬레이터", "location": "C동 1층",
              "spec": {"elevatorNo": "3307-116", "manufacturer": "다라엘리베이터", "ratedSpeed": 0.5, "driveType": "직결식"}},
    "esc-3": {"escalatorId": "esc-3", "name": "3호기 에스컬레이터", "location": "B동 2층",
              "spec": {"elevatorNo": "2104-383", "manufacturer": "가나승강기", "ratedSpeed": 0.5, "driveType": "와이델타식"}},
    "esc-7": {"escalatorId": "esc-7", "name": "7호기 에스컬레이터", "location": "C동 지하 1층",
              "spec": {"elevatorNo": "3307-115", "manufacturer": "다라엘리베이터", "ratedSpeed": 0.5, "driveType": "인버터식"}},
}
HISTORIES = {  # 설비별 점검 이력. 앱 InspectionRecord.fromJson 이 읽는 모양 그대로
    "esc-7": [{"inspectionId": "insp-0915-07", "inspectedAt": "2026-09-15T10:30:00", "inspector": "박철우",
               "durationMinutes": 14, "status": "NORMAL", "checkedItems": ["3.6.2", "3.10"], "cautionItems": [], "note": None}],
}
INSPECTIONS = {}  # 점검: 점검 번호(inspectionId) → 점검 상태 (진행 중 + 제출한 것)
CUSTOM = {}  # 앱에서 새로 등록한 설비 (보드 data/escalators.json 에 저장)
_seq = itertools.count(1)  # 점검 번호 뒤에 붙는 일련번호 (1부터)


def history_entry(insp):  # 제출한 점검 → 앱 이력 화면(InspectionRecord)이 읽는 모양
    items = list(insp["items"].values())
    defects = [i for i in items if i["status"] == "부적합"]
    return {
        "inspectionId": insp["inspectionId"], "inspectedAt": insp.get("submittedAt"), "inspector": insp["inspector"],  # 점검 번호, 시각, 점검자
        "inspectionType": insp.get("inspectionType", "regular"),  # 자체점검 / 정기검사
        "durationMinutes": insp.get("durationMinutes", 0), "status": "CAUTION" if defects else "NORMAL",  # 부적합이 하나라도 있으면 CAUTION
        "checkedItems": [i["itemCode"] for i in items if i["status"] != "미확인"],  # 이번에 점검한 항목 (다음 주기 계산용)
        "cautionItems": [i["itemCode"] for i in defects],  # 부적합 항목
        "note": "; ".join(f"{i['itemCode']} {i.get('note') or ''}" for i in defects) or None,  # 부적합 특기사항을 이어 붙인 메모
    }


def restore():  # 서버가 켜질 때: 보드에 저장해 둔 설비와 제출한 점검을 다시 불러옴
    CUSTOM.update(S.load_escalators())
    ESCALATORS.update(CUSTOM)
    n = 0
    for insp in S.load_inspections():  # 오래된 것부터
        iid = insp["inspectionId"]
        if iid in INSPECTIONS:
            continue
        insp.update(seen={}, skipped=set(), pending=None, log=[], lastSpeech=None, submitted=True)  # 진행용 값 채워 넣기
        INSPECTIONS[iid] = insp
        HISTORIES.setdefault(insp["escalatorId"], []).insert(0, history_entry(insp))  # 이력 맨 앞에 (최신순)
        n += 1
    print(f"저장된 설비 {len(CUSTOM)}개, 지난 점검 {n}개를 불러왔습니다. (저장 위치: {S.DATA_DIR.resolve()})")


def ok(data):  # 성공 응답 모양을 만드는 함수
    return {"success": True, "data": data, "error": None}  # 앱은 {"data": ...} 를 풀어서 읽음


def fail(status, code, message):  # 오류 응답을 내는 함수
    raise HTTPException(status_code=status, detail={"code": code, "message": message})  # 예: 404 + {"code": "ESCALATOR_NOT_FOUND", "message": "..."}


def get_insp(iid):  # 점검 번호로 점검 상태를 꺼내는 함수
    insp = INSPECTIONS.get(iid)  # 저장소에서 찾기
    if not insp:  # 없으면
        fail(404, "INSPECTION_NOT_FOUND", "점검 정보를 찾을 수 없습니다.")  # 404 오류
    return insp  # 찾은 점검 상태 반환


def progress(insp):  # 진행률(기록한 개수 / 전체 개수)을 계산하는 함수
    done = sum(1 for it in insp["items"].values() if it["status"] != "미확인")  # '미확인'이 아닌 항목 수 = 기록한 수
    return {"recorded": done, "total": len(insp["items"])}  # {"recorded": 2, "total": 5} 같은 모양


def speak(text):  # 안내 문장을 응답 모양으로 만드는 함수
    return {"text": text, "audioId": P.tts(text)}  # 글자 + 보드 TTS 음성 파일 id (지금은 None)


# ───────────────────────── A. 연결 ─────────────────────────
@app.get("/api/health")  # GET /api/health 요청이 오면 아래 함수 실행
def health():  # 서버가 살아 있는지 확인
    return ok({"status": "ok", **P.STATUS, "serverTime": datetime.now().isoformat()})  # stt·llm·tts 상태 (stub = 아직 실제 모델이 아님)


@app.get("/api/audio/{audio_id}")  # 보드 TTS 안내 음성 파일 (앱이 speech.audioId 로 요청)
def get_audio(audio_id: str):
    p = P.audio_path(audio_id)  # 파일 찾기
    if p is None:
        fail(404, "AUDIO_NOT_FOUND", "음성 파일이 없습니다.")
    return FileResponse(p, media_type="audio/wav")  # WAV 그대로 보냄


# ───────────────────────── B. 설비 (앱이 이미 호출하는 2개) ─────────────────────────
@app.get("/api/escalators")  # 설비 전체 목록
def list_escalators():
    return ok(list(ESCALATORS.values()))


@app.post("/api/escalators")  # 새 설비 등록 (모르는 QR 을 찍고 '새 설비로 등록')
def register_escalator(body: dict):  # body 예: {"escalatorId": "esc-12", "name": "12호기 에스컬레이터", "location": "D동 1층", "spec": {...}}
    eid = str(body.get("escalatorId") or body.get("id") or "").strip().lower()  # 설비 번호 (QR 값, 소문자)
    name = str(body.get("name") or "").strip()  # 이름
    if not eid or not name:  # 둘 다 있어야 함
        fail(400, "INVALID_REQUEST", "escalatorId 와 name 이 필요합니다.")
    if eid in ESCALATORS:  # 이미 있으면
        fail(409, "ESCALATOR_ALREADY_EXISTS", "이미 등록된 설비입니다.")
    e = {"escalatorId": eid, "name": name, "location": str(body.get("location") or "")}
    if isinstance(body.get("spec"), dict):  # 기본제원이 있으면 같이
        e["spec"] = body["spec"]
    ESCALATORS[eid] = CUSTOM[eid] = e  # 등록
    S.save_escalators(CUSTOM)  # 보드에 저장 (서버를 껐다 켜도 남게)
    return ok(e)


@app.get("/api/escalators/{escalator_id}")  # QR 스캔, 설비 정보
def get_escalator(escalator_id: str):  # 주소의 {escalator_id} 가 이 변수로 들어옴
    e = ESCALATORS.get(escalator_id.lower())  # 소문자로 바꿔서 설비 찾기
    if not e:  # 없으면
        fail(404, "ESCALATOR_NOT_FOUND", "등록되지 않은 설비입니다.")  # 앱: ApiNotFound -> 새 설비 등록 안내
    return ok(e)  # 설비 정보 반환


@app.get("/api/escalators/{escalator_id}/histories")  # 최근 점검 이력 / 점검 기록 / 설비 이력
def get_histories(escalator_id: str, limit: int = 20):  # limit = 최대 몇 건까지 (주소 뒤 ?limit=1 처럼)
    if escalator_id.lower() not in ESCALATORS:  # 없는 설비면
        fail(404, "ESCALATOR_NOT_FOUND", "등록되지 않은 설비입니다.")  # 404
    return ok(HISTORIES.get(escalator_id.lower(), [])[:limit])  # 이력 목록을 limit 개까지 반환 (없으면 빈 목록)


TYPE_LABEL = {"self": "자체점검", "regular": "정기검사"}  # 점검 종류 이름


def session_items(escalator_id, inspection_type):  # 점검 시작 때 깔 항목들
    if inspection_type == "regular":  # 정기검사: 점검표 70개 전부
        return {c: {"itemCode": c, "itemName": v["name"], "status": "미확인"} for c, v in P.ITEMS.items()}
    due = P.due_items(HISTORIES.get(escalator_id, []))  # 자체점검: 주기가 돌아온 항목만 (기한 지난 것 먼저)
    return {c: {"itemCode": c, "itemName": P.ALL_ITEMS[c]["name"], "status": "미확인",
                "due": d, "lastChecked": last, "cycleMonths": P.ALL_ITEMS[c]["cycleMonths"]} for c, d, last in due}


@app.get("/api/escalators/{escalator_id}/items")  # 점검 항목 목록
def get_items(escalator_id: str, type: str = "all"):  # type=all: 70개 전부 / type=self: 이번 달 자체점검 대상 (앱 '이번 달 필수 점검')
    if type == "self":
        if escalator_id.lower() not in ESCALATORS:
            fail(404, "ESCALATOR_NOT_FOUND", "등록되지 않은 설비입니다.")
        return ok(list(session_items(escalator_id.lower(), "self").values()))
    return ok([{"itemCode": c, **v} for c, v in P.ALL_ITEMS.items()])  # 모델이 아는 검사기준 항목 전체


# ───────────────────────── C. 점검 세션 ─────────────────────────
@app.post("/api/inspections")  # 점검 시작
def start_inspection(escalatorId: str = Form(...), inspector: str = Form("점검자"), inspectionType: str = Form("self")):  # 설비 번호(필수), 점검자, 점검 종류
    """inspectionType: self = 월간 자체점검 (주기가 돌아온 항목만) / regular = 정기검사 (점검표 70개 전부)"""
    if escalatorId.lower() not in ESCALATORS:  # 없는 설비면
        fail(404, "ESCALATOR_NOT_FOUND", "등록되지 않은 설비입니다.")  # 404
    if inspectionType not in TYPE_LABEL:  # 모르는 종류면
        fail(400, "INVALID_REQUEST", "inspectionType 은 self 또는 regular 입니다.")
    iid = f"insp-{datetime.now():%Y%m%d-%H%M%S}-{next(_seq):02d}"  # 점검 번호. 예: insp-20261112-140312-01 (서버를 다시 켜도 겹치지 않게 시각 포함)
    while iid in INSPECTIONS:  # 그래도 겹치면 (다시 켠 직후 같은 초) 번호를 하나 더 올림
        iid = f"insp-{datetime.now():%Y%m%d-%H%M%S}-{next(_seq):02d}"
    INSPECTIONS[iid] = {  # 새 점검 상태를 저장소에 만듦
        "inspectionId": iid, "escalatorId": escalatorId.lower(), "inspector": inspector,  # 점검 번호, 설비, 점검자
        "startedAt": datetime.now().isoformat(), "submitted": False,  # 시작 시각, 제출 여부
        "inspectionType": inspectionType,  # self(자체점검) / regular(정기검사)
        "items": session_items(escalatorId.lower(), inspectionType),  # 이번 점검 대상 항목 (전부 '미확인'으로 시작)
        "log": [],          # 기록 순서 (취소용)
        "pending": None,    # 대기 중인 질문 {"itemCode", "expect"}
        "seen": {},         # clientSeq -> 응답 (재전송 중복 방지)
        "lastSpeech": None,  # 마지막 안내 문장 ("다시 읽어 줘"용)
        "skipped": set(),   # "건너뛰기"로 미점검 처리한 항목 (점검 끝에서 다시 묻지 않음)
    }
    return ok({"inspectionId": iid, "inspectionType": inspectionType, "items": list(INSPECTIONS[iid]["items"].values())})  # 점검 번호, 종류, 항목 목록


@app.get("/api/inspections/{iid}")  # 지금 점검 상태 조회
def inspection_state(iid: str):
    insp = get_insp(iid)  # 점검 상태 꺼내기
    return ok({"progress": progress(insp), "pending": insp["pending"], "lastRecord": insp["log"][-1] if insp["log"] else None})  # 진행률, 대기 중인 질문, 마지막 기록


# ───────────────────────── D. 음성 (핸즈프리 핵심) ─────────────────────────
@app.post("/api/inspections/{iid}/utterances")  # 말 한 덩어리 처리
async def utterance(iid: str, clientSeq: int = Form(...), audio: UploadFile | None = File(None), text: str | None = Form(None)):  # 발화 순번(필수), 음성 파일 또는 텍스트
    """VAD로 자른 말 한 덩어리. 테스트할 때는 audio 대신 text 를 보내면 STT를 건너뜀."""
    insp = get_insp(iid)  # 점검 상태 꺼내기
    if text is None and audio is None:  # 음성도 글자도 없으면
        fail(400, "INVALID_REQUEST", "audio 또는 text 가 필요합니다.")  # 잘못된 요청
    wav = await audio.read() if text is None else None  # 업로드된 음성 파일 읽기
    return ok(await run_in_threadpool(process_utterance, insp, clientSeq, wav, text))  # 모델 실행은 별도 스레드에서


def process_utterance(insp, seq, wav, text):  # 발화 하나 처리 (스레드에서 실행)
    with _proc_lock:  # 한 번에 하나씩
        if insp["submitted"]:  # 이미 제출한 점검이면
            fail(409, "INSPECTION_ALREADY_SUBMITTED", "이미 제출된 점검입니다.")  # 더 기록하지 않음
        if seq in insp["seen"]:  # 같은 순번이 또 왔으면 (연결이 끊겨서 앱이 다시 보낸 경우)
            return insp["seen"][seq]  # 다시 처리하지 않고 지난번 결과를 그대로 돌려줌
        t0 = time.perf_counter()  # 시작 시각
        if text is None:  # 글자가 없으면 음성을 받아쓰기
            try:
                text = P.stt(wav)  # STT
            except NotImplementedError as e:  # STT가 아직 연결 안 됐으면
                fail(503, "MODEL_NOT_READY", str(e))  # 503: 모델 준비 안 됨
            text = (text or "").strip()
            if len(re.sub(r"[^가-힣A-Za-z0-9]", "", text)) < 2:  # 말소리가 아니었거나 한 글자짜리 잡음 ("음", "아")
                res = {"kind": "ignored", "reason": "말소리 아님", "transcript": text, "progress": progress(insp)}  # 조용히 무시 (안내도 안 띄움)
                insp["seen"][seq] = res
                print(f"[무시] 말소리 아님: \"{text}\"")
                return res
        t1 = time.perf_counter()  # STT 끝난 시각
        res = handle_text(insp, P.normalize(text))  # 정규화한 문장으로 실제 처리 (아래 함수)
        t2 = time.perf_counter()  # LLM·판정 끝난 시각
        res["timing"] = {"sttMs": round((t1 - t0) * 1000), "llmMs": round((t2 - t1) * 1000)}  # 단계별 걸린 시간
        print(f"[처리] STT {t1 - t0:.1f}초 · LLM {t2 - t1:.1f}초 · {res['kind']} · \"{text}\" → {res.get('llmRaw', '')}")  # 서버 창에 기록
        res["transcript"] = text  # 받아쓴 원래 문장도 응답에 넣음 (앱 화면 '방금 들은 말')
        res["progress"] = progress(insp)  # 진행률도 넣음
        insp["seen"][seq] = res  # 이 순번의 결과를 기억 (중복 방지)
        if res.get("speech"):  # 안내 문장이 있으면
            insp["lastSpeech"] = res["speech"]  # "다시 읽어 줘"를 위해 기억
        return res  # 결과 반환


def handle_text(insp, t):  # 문장 하나를 보고 무엇을 할지 정하는 함수 (명령 → 대답 → LLM 순서)
    # 1) 명령 — LLM을 거치지 않음
    cmd = P.match_command(t)  # 음성 명령인지 확인
    if cmd == "undo":  # "취소"
        return {"kind": "command", "command": "undo", **undo_last(insp)}  # 마지막 기록을 되돌림
    if cmd == "progress":  # "몇 개 남았어"
        p = progress(insp)  # 진행률 계산
        left = [it["itemName"] for it in insp["items"].values() if it["status"] == "미확인"]  # 아직 안 한 항목 이름들
        return {"kind": "command", "command": "progress", "speech": speak(f"{p['total']}개 중 {p['recorded']}개 기록했습니다. 남은 항목은 {', '.join(left) or '없습니다'}.")}  # 남은 항목을 읽어 줌
    if cmd == "repeat":  # "다시 읽어 줘"
        return {"kind": "command", "command": "repeat", "speech": insp["lastSpeech"]}  # 마지막 안내를 다시
    if cmd == "finish":  # "점검 끝"
        return {"kind": "command", "command": "finish", **finish_check(insp)}  # 빠진 항목 확인 (아래 함수)
    if cmd == "skip_all":  # "나머지 건너뛰기": 남은 항목 전부 미점검으로 두고 검토 화면으로
        left = [it for it in insp["items"].values() if it["status"] == "미확인"]  # 아직 안 한 항목
        insp["skipped"].update(it["itemCode"] for it in left)  # 다시 묻지 않게
        insp["pending"] = None  # 묻던 질문 끝
        p = progress(insp)
        msg = f"남은 {len(left)}개 항목을 미점검으로 남깁니다. 화면에서 확인하고 제출해 주세요." if left else "모든 항목을 기록했습니다. 화면에서 확인하고 제출해 주세요."
        return {"kind": "command", "command": "finish", "missing": [], "speech": speak(msg)}  # 앱은 finish + 빈 missing 이면 검토 화면을 엶
    if cmd == "skip" and not insp["pending"]:  # 묻는 항목이 없는데 "건너뛰기" → LLM에 넘기지 않고 안내
        return {"kind": "command", "command": "skip", "speech": speak("지금 묻고 있는 항목이 없습니다. 남은 항목을 모두 넘기려면 나머지 건너뛰기라고 하세요.")}
    if cmd == "skip" and insp["pending"]:  # "건너뛰기" (묻고 있는 항목이 있을 때만)
        code = insp["pending"]["itemCode"]  # 묻고 있던 항목
        insp["pending"] = None  # 질문 끝
        insp["skipped"].add(code)  # 다시 묻지 않도록 기억
        return {"kind": "command", "command": "skip", "speech": speak(f"{P.ALL_ITEMS[code]['name']} 항목은 미점검으로 남깁니다.")}  # 미점검으로 두고 안내

    # 2) 대기 중인 질문의 대답 — 규칙 엔진이 빈칸만 채움
    if insp["pending"]:  # "상부, 하부 중 어디인가요?" 같은 질문을 한 상태면
        q = insp["pending"]  # 질문 정보 (어느 항목의, 무엇을 묻는지)
        ans = P.match_answer(t, q["expect"])  # 문장에서 대답 찾기 (상부/하부/양쪽 또는 적합/부적합)
        if ans:  # 대답을 찾았으면
            item = insp["items"][q["itemCode"]]  # 그 항목을 꺼내
            item["location" if q["expect"] == "location" else "status"] = ans  # 위치 질문이면 위치 칸, 판정 질문이면 판정 칸을 채움
            insp["pending"] = None  # 질문 끝
            return {"kind": "answered", "item": item, "speech": speak(read_back(item))}  # 채운 결과를 다시 읽어 줌
        # 대답이 아니면 아래 LLM으로 넘어감 (다른 항목을 말한 경우)

    # 3) 짧은 말인데 명령도 대답도 아니면 LLM에 넣지 않음 (넣으면 없는 항목을 지어낼 수 있음)
    if len(P.compact(t)) <= P.SHORT_LEN:
        return {"kind": "retry", "reason": "짧은 말", "speech": speak("잘 못 들었습니다. 다시 말씀해 주세요.")}

    # 4) LLM
    raw = P.llm(t)  # 문장을 LLM에 넣어 함수 호출 문자열을 받음
    try:
        func, kw = P.parse_call(raw)  # 함수 이름과 인자로 나눔
        if func is None:  # <esc_end> 만 나왔으면 (잡담)
            return {"kind": "ignored", "llmRaw": raw}  # 아무것도 기록하지 않음
        P.validate(func, kw)  # 허용값인지 검사
    except ValueError as e:  # 형식이 깨졌거나 허용값 밖이면
        return {"kind": "retry", "llmRaw": raw, "reason": str(e), "speech": speak("다시 말씀해 주세요.")}  # 기록하지 않고 다시 말하게 함

    code = P.FUNC_TO_CODE[func]  # 함수 이름 → 검사기준 번호 (esc_10 → 3.10)
    verdict, criteria = P.judge(code, kw)  # 규칙 엔진으로 판정 (말한 판정 또는 측정값으로 계산, 선택형은 선택값 기록)
    added = code not in insp["items"]  # 점검 목록에 없던 항목인지 (취소하면 목록에서도 뺌)
    item = insp["items"].setdefault(code, {"itemCode": code, "itemName": P.ALL_ITEMS[code]["name"], "status": "미확인"})  # 이번 점검의 그 항목 (없으면 추가)
    before = dict(item)  # 바꾸기 전 상태를 복사해 둠 (취소용)
    item.update({"function": func, "status": verdict or "미확인", "judgedBy": "speech" if kw.get("판정") else ("selection" if P.ALL_ITEMS[code].get("selection") else "rule"),  # 판정과 판정 출처(말/규칙)
                 "detail": kw.get("세부"), "location": kw.get("위치"), "note": kw.get("특기사항"),  # 세부 항목, 위치, 특기사항
                 "action": kw.get("조치"), "criteria": criteria,  # 조치, 판정에 쓴 기준
                 "measure": {k: v for k, v in kw.items() if k.startswith("측정")} or None,  # 측정값들 (측정_좌, 측정_우, 측정값)
                 "updatedAt": datetime.now().isoformat()})  # 기록 시각
    insp["log"].append({"itemCode": code, "before": before, "added": added})  # 기록 순서에 추가 (취소할 때 이걸로 되돌림)

    # 5) 빈칸이 있으면 추가 질문
    if verdict is None:  # 판정을 정할 수 없으면
        insp["pending"] = {"itemCode": code, "expect": "verdict"}  # 판정 질문 대기 상태로
        return {"kind": "question", "item": item, "llmRaw": raw, "speech": speak("적합인가요, 부적합인가요?")}  # 질문
    if P.ALL_ITEMS[code]["hasLocation"] and item["location"] is None:  # 상·하부 구분 항목인데 위치를 안 말했으면
        insp["pending"] = {"itemCode": code, "expect": "location"}  # 위치 질문 대기 상태로
        return {"kind": "question", "item": item, "llmRaw": raw, "speech": speak("상부, 하부 중 어디인가요?")}  # 질문
    return {"kind": "recorded", "item": item, "llmRaw": raw, "speech": speak(read_back(item))}  # 다 채워졌으면 기록 완료 + 다시 읽어 주기


def read_back(item):  # 기록한 내용을 읽어 줄 문장을 만드는 함수
    if item.get("judgedBy") == "selection" and item.get("criteria"):  # 선택형 (구동방식 등): "3.1.5 구동방식, 체인식으로 기록했습니다."
        return f"{item['itemCode']} {item['itemName']}, {item['criteria'].split('= ')[-1]}으로 기록했습니다."
    loc = f" {item['location']}" if item.get("location") else ""  # 위치가 있으면 " 하부" 처럼 붙임
    note = f", {item['note']}" if item.get("note") else ""  # 특기사항이 있으면 뒤에 붙임
    return f"{item['itemCode']} {item['itemName']}{loc}, {item['status']}으로 기록했습니다{note}."  # 예: "3.6.2 비상정지장치 하부, 적합으로 기록했습니다."


# ───────────────────────── E. 기록 조작 ─────────────────────────
def undo_last(insp):  # 마지막 기록을 되돌리는 함수
    insp["pending"] = None  # 묻고 있던 질문도 취소
    if not insp["log"]:  # 되돌릴 기록이 없으면
        return {"speech": speak("취소할 기록이 없습니다.")}  # 안내만
    last = insp["log"].pop()  # 마지막 기록을 꺼냄
    if last.get("added"):  # 이번에 새로 추가됐던 항목이면
        insp["items"].pop(last["itemCode"], None)  # 목록에서 뺌
    else:
        insp["items"][last["itemCode"]] = last["before"]  # 그 항목을 기록 전 상태로 되돌림
    return {"item": last["before"], "speech": speak(f"{last['before']['itemName']} 기록을 취소했습니다.")}  # 되돌린 항목과 안내


@app.post("/api/inspections/{iid}/undo")  # 화면 버튼으로 취소할 때
def undo(iid: str):
    return ok(undo_last(get_insp(iid)))  # 위 함수와 똑같이 처리


@app.patch("/api/inspections/{iid}/items/{item_code}")  # 화면에서 손으로 고치기
def edit_item(iid: str, item_code: str, body: dict):  # body = 앱이 보낸 JSON (바꿀 값들)
    """화면 손 수정 (노션의 체크박스 + 결과 수정을 하나로). body 예: {"status": "부적합", "note": "..."}"""
    insp = get_insp(iid)  # 점검 상태 꺼내기
    item = insp["items"].get(item_code)  # 고칠 항목 찾기
    if not item:  # 없는 항목이면
        fail(404, "ITEM_NOT_FOUND", "이 점검에 없는 항목입니다.")  # 404
    if body.get("status") == "부적합" and not (body.get("note") or item.get("note")):  # 부적합인데 특기사항이 없으면
        fail(422, "NOTE_REQUIRED", "부적합 항목에 특기사항을 입력하세요.")  # 특기사항을 쓰라고 거절
    item.update({k: v for k, v in body.items() if k in {"status", "location", "note", "action", "detail"}}, judgedBy="manual")  # 허용된 칸만 바꾸고, 판정 출처를 '손 수정'으로
    return ok(item)  # 고친 항목 반환


def finish_check(insp):  # "점검 끝"일 때 빠진 항목을 확인하는 함수
    missing = [it for it in insp["items"].values() if it["status"] == "미확인" and it["itemCode"] not in insp["skipped"]]  # 아직 '미확인'이고 건너뛰지 않은 항목들
    p = progress(insp)  # 진행률
    if missing:  # 빠진 게 있으면
        insp["pending"] = {"itemCode": missing[0]["itemCode"], "expect": "verdict"}  # 첫 번째 빠진 항목의 판정을 묻는 상태로 (대답하거나 "건너뛰기")
        msg = f"{p['total']}개 중 {p['recorded']}개 기록했습니다. {missing[0]['itemCode']} {missing[0]['itemName']} 결과가 없습니다. 말씀하시거나 건너뛰기라고 해 주세요."  # 음성으로 물을 문장
        if len(missing) > 3:  # 많이 남았으면 한 번에 넘기는 방법도 안내
            msg += f" 남은 {len(missing)}개를 모두 미점검으로 두려면 나머지 건너뛰기라고 하세요."
    else:  # 다 했으면
        msg = "모든 항목을 기록했습니다. 화면에서 확인하고 제출해 주세요."  # 검토 화면으로 안내
    return {"missing": missing, "speech": speak(msg)}  # 빠진 항목 목록과 안내 문장


@app.post("/api/inspections/{iid}/finish")  # 화면 버튼으로 "점검 끝"
def finish(iid: str):
    return ok(finish_check(get_insp(iid)))  # 위 함수와 똑같이 처리


@app.get("/api/inspections/{iid}/result")  # 검토 화면용 전체 결과
def result(iid: str):
    insp = get_insp(iid)  # 점검 상태 꺼내기
    items = list(insp["items"].values())  # 항목 목록
    summary = {s: sum(1 for i in items if i["status"] == s) for s in ["적합", "부적합", "해당없음", "미확인"]}  # 판정별 개수 세기
    return ok({"inspectionId": iid, "escalatorId": insp["escalatorId"], "summary": summary, "items": items,
               "defects": [i for i in items if i["status"] == "부적합"], "submitted": insp["submitted"],  # 요약, 전체 항목, 부적합 목록, 제출 여부
               "inspector": insp["inspector"], "startedAt": insp.get("startedAt"),  # 점검자, 시작 시각
               "inspectionType": insp.get("inspectionType", "regular"),  # 점검 종류
               "submittedAt": insp.get("submittedAt"), "durationMinutes": insp.get("durationMinutes"),  # 제출 시각, 소요 시간 (제출 전이면 None)
               "reportUrl": f"/api/inspections/{iid}/report" if insp["submitted"] else None})  # 보고서 주소 (제출한 점검만)


@app.get("/api/inspections/{iid}/report")  # 점검 보고서 (HTML. 폰 브라우저에서 열고 인쇄 / PDF 저장)
def get_report(iid: str):
    p = S.report_path(iid)  # 제출 때 보드에 저장한 보고서
    if p is not None:
        return FileResponse(p, media_type="text/html; charset=utf-8")
    insp = get_insp(iid)  # 아직 제출 전이면 지금 상태로 미리보기
    return HTMLResponse(RPT.build(insp, ESCALATORS.get(insp["escalatorId"])))


# ───────────────────────── F. 제출 ─────────────────────────
@app.post("/api/inspections/{iid}/submit")  # 최종 제출
def submit(iid: str, acknowledgeMissing: bool = False):  # acknowledgeMissing = 미점검이 있어도 제출할지 (주소 뒤 ?acknowledgeMissing=true)
    insp = get_insp(iid)  # 점검 상태 꺼내기
    if insp["submitted"]:  # 이미 제출했으면
        fail(409, "INSPECTION_ALREADY_SUBMITTED", "이미 제출된 점검입니다.")  # 두 번 제출 막기
    items = list(insp["items"].values())  # 항목 목록
    missing = [i for i in items if i["status"] == "미확인"]  # 미점검 항목
    if missing and not acknowledgeMissing:  # 미점검이 있는데 확인을 안 받았으면
        fail(409, "MISSING_REQUIRED_ITEMS", f"확인하지 않은 항목 {len(missing)}개가 있습니다.")  # 경고 → 앱이 확인 창을 띄우고 다시 요청
    now = datetime.now()
    insp["submitted"] = True  # 제출 완료로 표시
    insp["submittedAt"] = now.isoformat(timespec="seconds")  # 제출 시각
    started = datetime.fromisoformat(insp["startedAt"])
    insp["durationMinutes"] = max(1, round((now - started).total_seconds() / 60))  # 소요 시간 (분)
    defects = [i for i in items if i["status"] == "부적합"]  # 부적합 항목들
    HISTORIES.setdefault(insp["escalatorId"], []).insert(0, history_entry(insp))  # 앱 이력 화면이 읽는 모양으로 이력 맨 앞에 추가 (최신순)
    S.save_inspection(insp)  # 보드에 저장 (지난 점검 상세 보기용, 서버를 껐다 켜도 남게)
    S.save_report(iid, RPT.build(insp, ESCALATORS.get(insp["escalatorId"])))  # 보고서 HTML 도 보드에 저장
    return ok({"inspectionId": iid, "submittedAt": insp["submittedAt"], "defects": defects,  # 제출 결과
               "reportUrl": f"/api/inspections/{iid}/report"})  # 보고서 주소 (앱이 '보고서 보기'로 엶)
