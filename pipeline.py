"""모시응 보드 파이프라인 (FastAPI와 분리된 순수 로직).

음성 1건 처리 순서:
  STT -> 정규화 -> [명령?] -> [대기 중인 질문의 대답?] -> LLM -> 파싱·검증 -> 규칙 엔진

보드가 오기 전에는 노트북에서 그대로 돌리고, STT·LLM·TTS 자리만 실제 모델로 바꾸면 된다.
"""
import ast  # LLM 출력의 인자 부분(판정='부적합', ...)을 안전하게 파이썬 값으로 읽는 데 씀
import difflib  # 비슷한 말 찾기 (STT가 '취소'를 '치소'로 받아쓴 경우)
import io  # 받은 WAV 바이트를 파일처럼 읽기
import os  # 환경변수 (어떤 모델을 쓸지)
import re  # 정규식: 함수 호출 모양 찾기, 문장부호 지우기
import threading  # 잠금 (NPU·CPU 를 한 번에 한 요청만 쓰게)
import uuid  # TTS 음성 파일 이름
import wave  # WAV 파일 읽고 쓰기
from pathlib import Path  # 파일 경로

# ───────────────────────── 1. 모델 연결 자리 ─────────────────────────
# 서버가 켜질 때 load_models() 가 환경변수를 보고 모델을 한 번만 불러온다.
#   ONMAEUM_STT_DIR  = SenseVoice 모델 폴더 (model.int8.onnx, tokens.txt 가 든 곳)  → 없으면 STT 미연결
#   ONMAEUM_STT_WHISPER = 파인튜닝한 Whisper small 폴더 (whisper-ct2, CTranslate2)    → SenseVoice 대신 비교용
#   ONMAEUM_LLM_CT2  = CTranslate2 모델 폴더 (노트북·PC, Llama-3.2-1B-Escalator-ct2)  → 없으면 아래 .rkllm 또는 가짜 LLM
#   ONMAEUM_LLM_PATH = .rkllm 파일 경로 (보드)                                       → 없으면 가짜 LLM
#   ONMAEUM_TTS      = 1 이면 tts_runner.py 사용 (보드 VITS)                          → 없으면 음성 없음
# 아무것도 안 정하면 지금처럼 '글자로 보내기' 테스트용으로 돈다.

STATUS = {"stt": "stub", "llm": "stub", "tts": "stub"}  # /api/health 에 보여 줄 모델 상태
_stt_fn = None  # 진짜 STT 함수 (WAV 바이트 -> 문장). None 이면 미연결
_llm_fn = None  # 진짜 LLM 함수 (문장 -> '<esc_..>(...)<esc_end>'). None 이면 가짜 LLM
_tts_fn = None  # 진짜 TTS 함수 (문장 -> WAV 바이트). None 이면 음성 없음
_model_lock = threading.Lock()  # 모델은 한 번에 한 요청만 (보드 NPU 는 동시에 못 돌림)
AUDIO_DIR = Path(os.environ.get("ONMAEUM_AUDIO_DIR", "audio_cache"))  # TTS 음성 파일을 저장할 폴더


def load_models():  # main.py 가 서버 시작 때 한 번 부름
    global _stt_fn, _llm_fn, _tts_fn
    stt_dir = os.environ.get("ONMAEUM_STT_DIR")  # SenseVoice 폴더
    whisper_dir = os.environ.get("ONMAEUM_STT_WHISPER")  # Whisper small 폴더
    if whisper_dir:
        try:
            _stt_fn = _load_whisper(whisper_dir)
            STATUS["stt"] = "whisper-small"
        except Exception as e:
            STATUS["stt"] = f"error: {e}"
    elif stt_dir:
        try:
            _stt_fn = _load_sensevoice(stt_dir)  # 불러오기 (몇 초 걸림)
            STATUS["stt"] = "sensevoice"
        except Exception as e:  # 실패해도 서버는 켜지게 (글자 테스트는 됨)
            STATUS["stt"] = f"error: {e}"
    ct2_dir = os.environ.get("ONMAEUM_LLM_CT2")  # PC 용 CTranslate2 모델 폴더
    llm_path = os.environ.get("ONMAEUM_LLM_PATH")  # 보드용 .rkllm 파일
    if ct2_dir:
        try:
            _llm_fn = _load_ct2(ct2_dir)  # 불러오기 (몇 초)
            STATUS["llm"] = "ct2"
        except Exception as e:
            STATUS["llm"] = f"error: {e}"  # 실패하면 가짜 LLM 그대로
    elif llm_path:
        try:
            import rkllm_runner  # 마음AI 예시 코드로 채우는 파일 (같은 폴더)
            _llm_fn = rkllm_runner.load(llm_path)  # 모델 불러오기
            STATUS["llm"] = "rkllm"
        except Exception as e:
            STATUS["llm"] = f"error: {e}"  # 실패하면 가짜 LLM 그대로
    if os.environ.get("ONMAEUM_TTS") == "1":
        try:
            import tts_runner  # 보드 VITS 연결 파일 (같은 폴더)
            _tts_fn = tts_runner.load()
            STATUS["tts"] = "vits"
        except Exception as e:
            STATUS["tts"] = f"error: {e}"
    _warm_up()  # 첫 요청이 느리지 않게 미리 한 번 돌려 둠
    print("모델 상태:", STATUS)  # 서버 창에 표시


def _silence_wav(sec: float = 1.0) -> bytes:  # 조용한 WAV (워밍업용)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * sec))
    return buf.getvalue()


def _warm_up():  # 모델을 한 번씩 돌려서 첫 요청 지연을 없앰 (몇 초 걸림)
    try:
        if _stt_fn is not None:
            _stt_fn(_silence_wav())
        if _llm_fn is not None:
            _llm_fn("비상정지 스위치 정상입니다")
    except Exception as e:
        print("워밍업 실패 (무시):", e)


def unload_models():  # 서버가 꺼질 때 (보드 NPU 자원 해제)
    try:
        import rkllm_runner
        if _llm_fn is not None:
            rkllm_runner.release()
    except Exception:
        pass


def _load_whisper(model_dir: str):  # 팀이 파인튜닝한 Whisper small (데모 app.py 와 같은 설정)
    from faster_whisper import WhisperModel  # pip install faster-whisper

    threads = int(os.environ.get("ONMAEUM_STT_THREADS", os.cpu_count() or 4))  # 기본: CPU 코어 전부
    model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=threads)

    def run(audio_bytes: bytes) -> str:
        segs, _ = model.transcribe(io.BytesIO(audio_bytes), language="ko", beam_size=1, without_timestamps=True,
                                   suppress_blank=False, condition_on_previous_text=False, vad_filter=True)
        return "".join(s.text for s in segs).strip()  # 말소리가 없으면 빈 문장

    return run


def _load_sensevoice(model_dir: str):  # sherpa-onnx 로 SenseVoice 불러오기 (노트북·보드 둘 다 CPU 로 됨)
    import numpy as np  # 소리 데이터를 숫자 배열로
    import sherpa_onnx  # pip install sherpa-onnx

    d = Path(model_dir)  # 모델 폴더
    onnx = d / "model.int8.onnx"  # 가벼운 int8 모델 (없으면 model.onnx)
    if not onnx.exists():
        onnx = d / "model.onnx"
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(  # 인식기 만들기
        model=str(onnx),
        tokens=str(d / "tokens.txt"),
        language="ko",  # 한국어로 고정 (자동 감지보다 덜 틀림)
        use_itn=True,  # 숫자를 '4'처럼 숫자로 (normalize 에서 학습 표기로 다시 맞춤)
        num_threads=int(os.environ.get("ONMAEUM_STT_THREADS", os.cpu_count() or 4)),  # CPU 코어 수 (기본: 전부)
    )

    def run(audio_bytes: bytes) -> str:  # WAV 바이트 -> 문장
        with wave.open(io.BytesIO(audio_bytes)) as w:  # 앱이 보낸 WAV (16kHz 모노 16bit)
            sr = w.getframerate()  # 1초에 몇 번 쟀는지
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)  # 소리 숫자들
        samples = pcm.astype(np.float32) / 32768.0  # -1.0 ~ 1.0 으로
        s = rec.create_stream()  # 인식 한 번
        s.accept_waveform(sr, samples)  # 소리 넣기
        rec.decode_stream(s)  # 인식
        return s.result.text.strip()  # 받아쓴 문장

    return run


# 학습 때 쓴 프롬프트 (임창수 데모 app.py 의 to_call 과 글자 하나까지 같아야 함)
SYSTEM = "에스컬레이터 정기검사 기록기. 발화를 검사기준 함수 호출로 바꾼다."


def build_prompt(text: str) -> str:  # Llama 3.2 대화 형식으로 감싸기 (보드 rkllm_runner 도 이걸 씀)
    return (f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{SYSTEM}<|eot_id|>"
            f"<|start_header_id|>user<|end_header_id|>\n\n{text}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n")


def _load_ct2(model_dir: str):  # PC·노트북용 LLM (CTranslate2 int8, CPU) — 데모 app.py 와 같은 방식
    import ctranslate2  # pip install ctranslate2
    from tokenizers import Tokenizer  # pip install tokenizers

    threads = int(os.environ.get("ONMAEUM_LLM_THREADS", os.cpu_count() or 2))  # 쓸 CPU 코어 수
    gen = ctranslate2.Generator(model_dir, device="cpu", compute_type="int8", intra_threads=threads)  # 모델
    tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))  # 글자 <-> 토큰
    special = tok.token_to_id("<esc_end>") is not None  # <esc_end> 가 특수 토큰인 모델인지

    def run(text: str) -> str:  # 문장 -> '<esc_..>(...)<esc_end>'
        r = gen.generate_batch([tok.encode(build_prompt(text), add_special_tokens=False).tokens],
                               max_length=160, sampling_topk=1,  # 항상 같은 답 (가장 확률 높은 것만)
                               end_token=["<esc_end>", "<|eot_id|>"] if special else "<|eot_id|>",
                               include_prompt_in_result=False, return_end_token=True)[0]
        out = tok.decode(r.sequences_ids[0], skip_special_tokens=False).replace("<|eot_id|>", "").strip()
        return out or "<esc_end>"  # 아무것도 안 나오면 '무관한 말'

    return run


def stt(audio_bytes: bytes) -> str:  # 음성 파일 바이트를 받아 텍스트를 돌려주는 함수
    """음성(WAV 16kHz 모노) -> 텍스트. 모델이 없으면 NotImplementedError (main.py 가 503 으로 바꿈)"""
    if _stt_fn is None:
        raise NotImplementedError("STT 미연결: ONMAEUM_STT_DIR 을 정하거나, 요청에 text 필드를 넣어 테스트하세요")
    with _model_lock:  # 한 번에 하나만
        return _stt_fn(audio_bytes)


try:
    import textnorm as _textnorm  # 임창수 데모의 textnorm.py (있으면)
except Exception:
    _textnorm = None


def normalize(text: str) -> str:  # STT 결과를 LLM이 학습한 표기로 맞추는 함수
    """숫자·단위·문장부호를 학습 표기로. 같은 폴더에 textnorm.py(데모 코드)가 있으면 그 stt_to_llm 을 먼저 씀."""
    if _textnorm is not None:
        text = _textnorm.stt_to_llm(text)  # 숫자는 한글 발음으로 ('4' -> '사'), 학습 데이터 표기
    t = text.replace("mm", "밀리").replace("미리", "밀리")  # 단위 표기를 '밀리'로 통일
    t = re.sub(r"[.,!?]", "", t)  # 마침표·쉼표·느낌표·물음표 지우기
    for wrong, right in CORRECTIONS.items():  # 교정 사전을 하나씩 돌면서
        t = t.replace(wrong, right)  # 자주 틀리는 말을 올바른 검사 용어로 바꿈
    return t.strip()  # 앞뒤 공백을 지우고 돌려줌


CORRECTIONS = {"콩 ": "콤 ", "콧 ": "콤 ", "매가": "메가", "디딘판": "디딤판"}  # 교정 사전 (STT가 잘못 듣는 말 → 올바른 말)


def llm(text: str) -> str:  # 정규화된 문장을 받아 함수 호출 문자열을 돌려주는 함수
    """정규화된 발화 -> '<esc_X_Y>(...)<esc_end>' 문자열. 진짜 모델이 없으면 가짜 LLM."""
    if _llm_fn is None:
        return _mock_llm(text)  # 아래 가짜 LLM
    with _model_lock:  # NPU 는 한 번에 한 요청만
        return _llm_fn(text).strip()


def _mock_llm(text: str) -> str:
    """연결 확인용 가짜 LLM. 스텝 롤러·비상정지 문장만 흉내 낸다."""
    if "스텝 롤러" in text and ("벗겨" in text or "박리" in text):  # 예시 1: 스텝 롤러 고무가 벗겨졌다는 말이면
        return "<esc_10>(판정='부적합', 세부=['스텝 롤러'], 위치=None, 특기사항='스텝 롤러 고무 박리', 조치=None)<esc_end>"  # 3.10 부품 상태 부적합으로 기록
    if "비상정지" in text:  # 예시 2: 비상정지 얘기면
        loc = "'상부'" if "상부" in text else "'하부'" if "하부" in text else "None"  # 위치를 말했으면 넣고, 안 했으면 None(빈칸)
        verdict = "'부적합'" if "안 " in text else "'적합'"  # '안 돼요' 같은 부정 표현이 있으면 부적합, 아니면 적합
        return f"<esc_6_2>(판정={verdict}, 위치={loc}, 특기사항=None, 조치=None)<esc_end>"  # 3.6.2 비상정지장치 함수 호출
    return "<esc_end>"  # 그 밖의 말은 점검과 무관한 말로 보고 끝 토큰만 냄


def tts(text: str):  # 안내 문장을 음성 파일로 만드는 함수
    """안내 문구 -> 음성 파일 id (앱이 GET /api/audio/{id} 로 받아 재생). TTS 가 없으면 None (앱은 글자만 표시)."""
    if _tts_fn is None or not text:
        return None
    with _model_lock:
        wav_bytes = _tts_fn(text)  # VITS 로 WAV 만들기
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)  # 저장 폴더
    audio_id = uuid.uuid4().hex  # 파일 이름 (추측할 수 없는 32글자)
    (AUDIO_DIR / f"{audio_id}.wav").write_bytes(wav_bytes)  # 저장
    return audio_id


def audio_path(audio_id: str):  # 음성 파일 id -> 파일 경로 (없거나 이상한 id 면 None)
    if not re.fullmatch(r"[0-9a-f]{32}", audio_id):  # 우리가 만든 모양의 id 만 (../ 같은 경로 공격 막기)
        return None
    p = AUDIO_DIR / f"{audio_id}.wav"
    return p if p.exists() else None


# ───────────────────────── 2. 검사기준 항목 ─────────────────────────
# TODO(조해리): 01_기능정의 엑셀의 70개 항목으로 채우기. hasLocation = 상부/하부 구분 항목.
# ───── 점검표: 승강기 정기검사기준 [별표 2] 3. 에스컬레이터, 70개 항목 (PDF 원문 이름) ─────
# 정기검사는 매번 전 항목을 확인하므로 주기 필터 없이 70개를 전부 깐다.
# 위치(상부/하부)는 학습 데이터에서 '위치' 칸을 쓰는 항목만 되묻는다. 선택형(3.1.2~3.1.6)은 판정 대신 선택값을 기록.
_CHECKLIST = [  # (번호, 이름)
    ("3.1.1", "일반사항 (정보망 일치)"), ("3.1.2", "구동기 설치 위치"), ("3.1.3", "제어반 설치 위치"),
    ("3.1.4", "디딤판 형식"), ("3.1.5", "구동방식"), ("3.1.6", "구동 운전방식"),
    ("3.1.7", "디딤판 체인 윤활"), ("3.1.8", "손잡이 체인 윤활"), ("3.1.9", "구동체인 윤활"), ("3.1.10", "기타"),
    ("3.2.1", "안전표시"), ("3.2.2", "수동핸들 지침"), ("3.2.3", "기계류 접근 출입문 안내"),
    ("3.2.4", "비상정지장치 색상"), ("3.2.5", "유지보수 및 점검 중 접근방지 수단"), ("3.2.6", "자동 기동 운행방향 표시장치"),
    ("3.3.1", "접근금지장치"), ("3.3.2", "미끄럼방지장치"), ("3.3.3", "장애물로부터의 보호"),
    ("3.3.4", "방화셔터 인근의 에스컬레이터"), ("3.3.5", "연속되는 에스컬레이터 사이 공간"),
    ("3.3.6", "손잡이 바깥쪽 건물난간"), ("3.3.7", "조명 및 콘센트"),
    ("3.4.1", "조명 절연저항"), ("3.4.2", "접지, 전동기 절연저항 및 배선"),
    ("3.5.1", "디딤판 주행안내"), ("3.5.2", "스텝/팔레트 사이의 틈새"), ("3.5.3", "디딤판과 스커트 사이 틈새"),
    ("3.5.4", "트레드 홈에 맞물리는 콤의 깊이"), ("3.5.5", "손잡이 외형 및 가이드/덮개"), ("3.5.6", "손잡이 틈새"),
    ("3.6.1", "유지점검 및 보수용 정지스위치"), ("3.6.2", "비상정지장치"), ("3.6.3", "과부하(전류 증가)"),
    ("3.6.4", "과부하(온도 증가)"), ("3.6.5", "과속감지"), ("3.6.6", "의도되지 않은 운행 방향의 역전"),
    ("3.6.7", "보조브레이크의 미-작동 감지"), ("3.6.8", "인장장치의 과도한 움직임"),
    ("3.6.9", "디딤판체인의 파단과 과도한 늘어짐"), ("3.6.10", "콤 안전장치"),
    ("3.6.11", "연속 에스컬레이터 및 무빙워크의 추가 정지장치"), ("3.6.12", "손잡이 인입구 장치"),
    ("3.6.13", "스텝/팔레트 처짐 감지 장치"), ("3.6.14", "스텝/팔레트 누락 감지 장치"),
    ("3.6.15", "브레이크의 미-작동 감지장치"), ("3.6.16", "손잡이의 속도 편차 감지"), ("3.6.17", "점검용 덮개 열림"),
    ("3.6.18", "과도한 정지거리"), ("3.6.19", "접지 결함"), ("3.6.20", "수동핸들의 설치 감지"),
    ("3.6.21", "점검운전 제어장치의 정지장치 작동 감지"), ("3.6.22", "쇼핑 카트 및 수하물 카트 진입방지대 감지"),
    ("3.7.1", "점검운전 제어장치"), ("3.7.2", "수동 기동운전"), ("3.7.3", "자동 기동 운전"),
    ("3.8.1", "디딤판 주행"), ("3.8.2", "손잡이 주행"), ("3.8.3", "스커트 및 스커트 디플렉터"),
    ("3.8.4", "기어오름방지장치"), ("3.8.5", "접근금지장치"), ("3.8.6", "미끄럼방지장치"),
    ("3.8.7", "안전 보호판"), ("3.8.8", "쇼핑카트"), ("3.8.9", "옥외용 에스컬레이터의 추가요건"),
    ("3.9.1", "속도, 전류 및 정지거리"), ("3.9.2", "보조브레이크"),
    ("3.10", "에스컬레이터를 구성하는 승강기부품의 상태"), ("3.11", "그 밖의 표시 등"), ("3.12", "자체점검의 확인"),
]
_HAS_LOCATION = {  # 학습 데이터에서 '위치'(상부/하부/양쪽)를 쓰는 항목
    "3.1.10", "3.2.1", "3.2.3", "3.2.4", "3.2.6", "3.3.1", "3.3.4", "3.3.6", "3.3.7", "3.4.1", "3.5.3", "3.5.4",
    "3.6.1", "3.6.2", "3.6.10", "3.6.12", "3.6.14", "3.6.17", "3.6.22", "3.7.1", "3.7.3", "3.8.8", "3.10",
}
# 자체점검 주기 (개월). [별표 3] 자체점검기준 3. 에스컬레이터의 '점검주기(회/월)'를 정기검사 70개 항목에 연결.
#   1/1 → 1, 1/3 → 3, 1/6 → 6. 한 항목에 주기가 여러 개면 가장 짧은 것. 없는 항목(None)은 자체점검 대상이 아님 (정기검사 때 확인).
SELF_CYCLE = {
    "3.2.1": 3, "3.2.2": 3, "3.2.3": 3, "3.2.4": 3, "3.2.5": 3, "3.2.6": 1,
    "3.3.1": 3, "3.3.2": 3, "3.3.3": 1, "3.3.4": 6, "3.3.5": 3, "3.3.6": 1, "3.3.7": 1,
    "3.4.1": 1, "3.4.2": 3,
    "3.5.1": 1, "3.5.2": 1, "3.5.3": 1, "3.5.4": 1, "3.5.5": 3, "3.5.6": 3,
    "3.6.1": 1, "3.6.2": 1, "3.6.3": 1, "3.6.4": 1, "3.6.5": 1, "3.6.6": 1, "3.6.7": 1, "3.6.8": 1,
    "3.6.9": 1, "3.6.10": 1, "3.6.11": 1, "3.6.12": 1, "3.6.13": 1, "3.6.14": 1, "3.6.15": 1,
    "3.6.16": 1, "3.6.17": 1, "3.6.18": 1, "3.6.20": 1, "3.6.21": 1, "3.6.22": 1,
    "3.7.1": 3, "3.7.2": 3, "3.7.3": 1,
    "3.8.1": 1, "3.8.2": 1, "3.8.3": 3, "3.8.4": 1, "3.8.5": 1, "3.8.6": 1, "3.8.7": 1, "3.8.8": 1, "3.8.9": 6,
    "3.9.1": 1, "3.9.2": 1,
}

SELECTION = {  # 선택형 항목: 판정 대신 선택값을 기록 (함수 이름 → 인자 이름)
    "esc_1_2": "설치위치", "esc_1_3": "설치위치", "esc_1_4": "디딤판형식", "esc_1_5": "구동방식", "esc_1_6": "운전방식",
}


def _func_of(code: str) -> str:  # 3.6.2 -> esc_6_2, 3.10 -> esc_10
    return "esc_" + code[2:].replace(".", "_")


def _code_of(func: str) -> str:  # esc_6_2 -> 3.6.2, esc_10 -> 3.10
    return "3." + func[len("esc_"):].replace("_", ".")


ALL_ITEMS = {  # 모델이 알아듣는 전체 70개 (점검표 순서)
    code: {"function": _func_of(code), "name": name, "hasLocation": code in _HAS_LOCATION,
           "selection": _func_of(code) in SELECTION, "cycleMonths": SELF_CYCLE.get(code)}
    for code, name in _CHECKLIST
}
DEMO_CODES = ["3.5.3", "3.6.2", "3.6.12", "3.8.3", "3.10"]  # 예전 시연용 5개

# ITEMS = 점검을 시작할 때 깔아 두는 항목 ('점검 끝'에서 빠진 걸 묻는 대상).
#   기본: 70개 전부 (정기검사는 전 항목 확인). ONMAEUM_SESSION_ITEMS=demo 면 예전 5개만.
ITEMS = ({c: ALL_ITEMS[c] for c in DEMO_CODES} if os.environ.get("ONMAEUM_SESSION_ITEMS") == "demo" else dict(ALL_ITEMS))


def _load_functions():  # functions_v3.json 에 점검표에 없는 함수가 있으면 추가 (이름은 점검표 우선)
    path = Path(os.environ.get("ONMAEUM_FUNCTIONS", Path(__file__).with_name("functions_v3.json")))
    if not path.exists():
        return
    import json
    for f in json.loads(path.read_text(encoding="utf-8")):
        name = f.get("name", "")
        if not name.startswith("esc_") or _code_of(name) in ALL_ITEMS:
            continue
        params = f.get("parameters") or {}
        props = params.get("properties", params) if isinstance(params, dict) else {}
        ALL_ITEMS[_code_of(name)] = {"function": name, "name": f.get("desc") or f.get("description") or name,
                                     "hasLocation": "위치" in props, "selection": False, "cycleMonths": None}


_load_functions()


def due_items(histories, today=None):
    """자체점검: 이번 달에 해야 할 항목. histories = 그 설비의 이력 (최신순, checkedItems·inspectedAt).
    돌려주는 값: [(검사기준 번호, 'overdue' | 'thisMonth', 마지막 점검일 또는 None)] (기한 지난 것 먼저)"""
    from datetime import date, datetime
    today = today or date.today()
    last = {}  # 항목 → 마지막으로 점검한 날
    for h in histories:
        try:
            d = datetime.fromisoformat(str(h.get("inspectedAt"))).date()
        except ValueError:
            continue
        for c in h.get("checkedItems") or []:
            if c not in last or d > last[c]:
                last[c] = d
    out = []
    for code, it in ALL_ITEMS.items():
        cyc = it.get("cycleMonths")
        if not cyc:
            continue  # 자체점검 대상 아님
        d = last.get(code)
        if d is None:
            out.append((code, "overdue", None))  # 한 번도 안 했으면 바로 대상
            continue
        months = (today.year - d.year) * 12 + (today.month - d.month)  # 몇 달 지났는지
        if months > cyc:
            out.append((code, "overdue", d.isoformat()))  # 기한 지남
        elif months == cyc:
            out.append((code, "thisMonth", d.isoformat()))  # 이번 달이 기한
    out.sort(key=lambda x: 0 if x[1] == "overdue" else 1)
    return out


FUNC_TO_CODE = {v["function"]: k for k, v in ALL_ITEMS.items()}  # 거꾸로 찾기용: 함수 이름(esc_10) → 검사기준 번호(3.10)
VERDICTS = {"적합", "부적합", "해당없음", None}  # 판정에 허용되는 값 (None = 말하지 않음)
LOCATIONS = {"상부", "하부", "양쪽", None}  # 위치에 허용되는 값

# ───────────────────────── 3. 파싱 · 검증 ─────────────────────────
CALL_RE = re.compile(r"^\s*<(esc_\d+(?:_\d+)*)>\((.*)\)<esc_end>\s*$", re.S)  # '<함수>(인자)<esc_end>' 모양을 찾는 정규식. 괄호 1번 = 함수 이름, 2번 = 인자


def parse_call(raw: str):  # LLM 출력 문자열을 (함수 이름, 인자 사전)으로 나누는 함수
    """'<esc_10>(판정='부적합', ...)<esc_end>' -> ('esc_10', {...}). 무관한 말이면 (None, {}).
    형식이 깨졌으면 ValueError."""
    if raw.strip() == "<esc_end>":  # 끝 토큰만 있으면
        return None, {}  # 기록할 게 없다는 뜻
    m = CALL_RE.match(raw)  # 정해진 모양인지 검사
    if not m:  # 모양이 다르면
        raise ValueError(f"형식 오류: {raw!r}")  # 형식 오류로 처리 (기록하지 않고 다시 말하게 함)
    func, args = m.group(1), m.group(2)  # 함수 이름과 인자 부분을 꺼냄
    call = ast.parse(f"f({args})", mode="eval").body  # 인자 부분을 'f(...)' 함수 호출 문장으로 감싸 파이썬 문법으로 해석
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}  # '판정='부적합'' 같은 인자를 하나씩 값으로 바꿔 사전으로 만듦 (코드 실행은 안 함)
    return func, kwargs  # (함수 이름, 인자 사전) 반환


def validate(func: str, kw: dict):  # 파싱한 결과가 허용되는 값인지 검사하는 함수
    if func not in FUNC_TO_CODE:  # 모르는 함수 이름이면
        raise ValueError(f"모르는 함수: {func}")  # 오류
    if kw.get("판정") not in VERDICTS:  # 판정 값이 적합·부적합·해당없음·None이 아니면
        raise ValueError(f"허용값 밖 판정: {kw.get('판정')}")  # 오류
    if kw.get("위치") not in LOCATIONS:  # 위치 값이 상부·하부·양쪽·None이 아니면
        raise ValueError(f"허용값 밖 위치: {kw.get('위치')}")  # 오류


def judge(code: str, kw: dict):  # 규칙 엔진: 판정을 정하는 함수
    """규칙 엔진: 판정이 비었고 측정값이 있으면 기준표로 판정. 판정 근거 문자열도 돌려줌."""
    if kw.get("판정") is not None:  # 기사가 판정을 직접 말했으면
        return kw["판정"], None  # 그대로 씀 (기준 문자열 없음)
    func = ALL_ITEMS.get(code, {}).get("function")
    if func in SELECTION and kw.get(SELECTION[func]):  # 선택형 항목 (구동방식 등): 선택값을 말했으면 '확인함'으로 기록
        return "적합", f"선택값 기록: {SELECTION[func]} = {kw[SELECTION[func]]}"
    if code == "3.5.3" and kw.get("측정_좌") is not None and kw.get("측정_우") is not None:  # 스커트 틈새이고 좌·우 측정값이 있으면
        l, r = kw["측정_좌"], kw["측정_우"]  # 좌측, 우측 값을 꺼냄
        ok = l <= 4 and r <= 4 and l + r <= 7  # 기준: 각 측 4mm 이하, 합 7mm 이하
        return ("적합" if ok else "부적합"), "각 측 4mm 이하, 합 7mm 이하"  # 판정과 사용한 기준을 돌려줌
    return None, None  # 판단 불가 -> 추가 질문 대상


# ───────────────────────── 4. 명령 · 대답 ─────────────────────────
COMMANDS = {  # 음성 명령 이름 → 그 명령으로 알아들을 단어들 (띄어쓰기는 무시하고 비교)
    # 치소·취서·추소 = Whisper 가 '취소'를 자주 이렇게 받아씀 (11/13 실제 로그)
    "undo": ["취소", "치소", "취서", "추소", "방금 거 지워", "되돌려", "지워 줘", "잘못 말했"],  # 마지막 기록 취소
    "progress": ["몇 개 남았", "얼마나 남았"],  # 남은 항목 안내
    "finish": ["점검 끝", "점검 종료", "다 했어"],  # 점검 끝 → 누락 확인
    "repeat": ["다시 읽어", "뭐라고"],  # 마지막 안내 다시 듣기
    # skip_all 은 skip 보다 먼저 검사해야 함 ("나머지 건너뛰기" 안에 "건너뛰기"가 들어 있어서)
    # '머지 건' = 나머지/내머지/아머지 + 건너뛰/건너띠/건노 (11/13 실제 로그)
    "skip_all": ["머지 건", "나머지 넘어가", "나머지 미점검", "전부 건너", "모두 건너", "다 건너"],  # 남은 항목 전부 미점검 → 검토 화면
    "skip": ["건너뛰", "건너띠", "건너 뛰", "넘어가", "패스"],  # 묻고 있는 항목을 미점검으로 남김
}


SHORT_LEN = 5  # 띄어쓰기·문장부호를 빼고 이 글자 수 이하면 '짧은 말' (점검 내용은 보통 이보다 김)


def compact(text: str) -> str:  # 한글·영문·숫자만 남김 ("취소 해." → "취소해")
    return re.sub(r"[^가-힣A-Za-z0-9]", "", text)


def match_command(text: str):  # 문장이 음성 명령인지 찾는 함수
    c = compact(text)
    for cmd, words in COMMANDS.items():  # 1) 명령 단어가 문장 안에 그대로 있으면
        if any(compact(w) in c for w in words):
            return cmd
    if len(c) <= SHORT_LEN + 2:  # 2) 짧은 말이면 STT가 조금 틀려도 비슷한 명령 단어로 인정 ("치소" → 취소, "건너뛰기요" → 건너뛰기)
        best, score = None, 0.0
        for cmd, words in COMMANDS.items():
            for w in words:
                s = difflib.SequenceMatcher(None, c, compact(w)).ratio()
                if s > score:
                    best, score = cmd, s
        if score >= 0.6:
            return best
    return None  # 명령이 아니면 None


def match_answer(text: str, expect: str):  # 추가 질문의 대답을 찾는 함수
    """대기 중인 질문의 대답 찾기. expect = 'location' | 'verdict'."""
    choices = {"location": ["상부", "하부", "양쪽"], "verdict": ["부적합", "적합", "해당없음"]}[expect]  # 질문 종류에 맞는 보기 목록
    for c in choices:  # '부적합'을 '적합'보다 먼저 검사 ('부적합' 안에 '적합'이 들어 있어서)
        if c in text:  # 문장에 보기가 들어 있으면
            return c  # 그 보기가 대답
    return None  # 대답을 못 찾으면 None
