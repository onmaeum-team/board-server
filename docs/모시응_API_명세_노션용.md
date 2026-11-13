# 모시응 보드 API 명세 (FastAPI)

> 기준: 2026-11-12 (70개 점검표, 월간 자체점검 주기 반영) · 서버 코드 `onmaeum_server` (main.py, pipeline.py, storage.py, report.py)
> 앱(Flutter)은 녹음만 하고, STT·LLM·판정·저장은 전부 보드 서버가 한다.

## 1. 기본 정보

| 항목 | 값 |
| --- | --- |
| 서버 | FastAPI + uvicorn, 포트 8080 |
| 주소 (에뮬레이터) | `http://10.0.2.2:8080` |
| 주소 (실제 폰 + USB) | `http://localhost:8080` + `adb reverse tcp:8080 tcp:8080` |
| 주소 (같은 Wi-Fi) | `http://<노트북·보드 IP>:8080` |
| 주소 (현장, 보드 핫스팟) | `http://10.42.0.1:8080` (보드에서 `ip addr`로 확인) |
| 앱에서 주소 바꾸기 | 핸즈프리 화면 아래 "주소 바꾸기" (폰에 저장됨) |
| 자동 API 문서 | `http://<서버>:8080/docs` (FastAPI Swagger, 직접 눌러 볼 수 있음) |

## 2. 공통 응답 형식

| 경우 | HTTP | 본문 |
| --- | --- | --- |
| 성공 | 200 | `{"success": true, "data": {...}, "error": null}` → 앱은 `data`만 사용 |
| 실패 | 4xx / 5xx | `{"detail": {"code": "에러코드", "message": "사용자 문구"}}` → 앱 `ApiError(status, code, message)` |
| 예외 | - | `/api/audio/{id}` 는 WAV 파일, `/api/inspections/{id}/report` 는 HTML 페이지를 그대로 돌려줌 |

## 3. 엔드포인트 전체 (16개)

| # | 메서드 | 경로 | 요청 | 응답 data | 앱에서 부르는 곳 |
| --- | --- | --- | --- | --- | --- |
| 1 | GET | `/api/health` | 없음 | `status`, `stt`, `llm`, `tts`, `serverTime` | 연결 확인, 끊겼을 때 5초마다 재확인 |
| 2 | GET | `/api/audio/{audioId}` | 경로: 음성 id | WAV 파일 (`audio/wav`) | 보드 TTS 안내 음성 재생 (보드 TTS 연결 후) |
| 3 | GET | `/api/escalators` | 없음 | 설비 목록 `[{escalatorId, name, location, spec}]` | (관리용) |
| 4 | POST | `/api/escalators` | JSON: `escalatorId`(필수), `name`(필수), `location`, `spec` | 등록된 설비 | 모르는 QR → "새 설비로 등록" |
| 5 | GET | `/api/escalators/{id}` | 경로: 설비 번호 (예: `esc-7`) | `escalatorId`, `name`, `location`, `spec` | QR 스캔, 설비 정보 |
| 6 | GET | `/api/escalators/{id}/histories?limit=20` | `limit` 선택 | 이력 목록 (아래 5-1) | 점검 기록 탭, 설비 이력 |
| 7 | GET | `/api/escalators/{id}/items?type=all` | `type`: `all`(기본) / `self` | `all`: 점검표 70개 `itemCode`, `function`, `name`, `hasLocation`, `selection`, `cycleMonths` · `self`: 이번 달 자체점검 대상 (+`due`, `lastChecked`) | (이력 화면 '이번 달 필수 점검'에 쓸 수 있음) |
| 8 | POST | `/api/inspections` | form: `escalatorId`(필수), `inspector`, `inspectionType`(`self` 기본 / `regular`) | `inspectionId`, `inspectionType`, `items[]` (자체점검: 이번 달 대상 / 정기검사: 70개, 전부 미확인) | 점검 시작 (종류 선택 후) |
| 9 | GET | `/api/inspections/{iid}` | 없음 | `progress{recorded,total}`, `pending`, `lastRecord` | (재접속 때 쓸 수 있음) |
| 10 | POST | `/api/inspections/{iid}/utterances` | multipart: `clientSeq`(필수), `audio`(WAV) 또는 `text` | 발화 응답 (아래 4) | 말 한 덩어리마다 (핸즈프리) |
| 11 | POST | `/api/inspections/{iid}/undo` | 없음 | 되돌린 `item`, `speech` | 취소 |
| 12 | PATCH | `/api/inspections/{iid}/items/{itemCode}` | JSON: `status`, `location`, `note`, `action`, `detail` 중 바꿀 것 | 바뀐 `item` | 검토 화면 손 수정 |
| 13 | POST | `/api/inspections/{iid}/finish` | 없음 | `missing[]`, `speech` | "점검 끝내고 검토하기" 버튼 |
| 14 | GET | `/api/inspections/{iid}/result` | 없음 | 결과 전체 (아래 5-2) | 검토 화면, **지난 점검 상세** |
| 15 | POST | `/api/inspections/{iid}/submit?acknowledgeMissing=false` | 쿼리 선택 | `inspectionId`, `submittedAt`, `defects[]`, `reportUrl` | 제출 (보고서 자동 생성·저장) |
| 16 | GET | `/api/inspections/{iid}/report` | 없음 | 보고서 HTML 페이지 | "보고서 보기" (폰 브라우저, 인쇄·PDF 저장) |

## 4. 발화 응답 (#10)

### 4-1. 응답 칸

| 칸 | 뜻 | 예 |
| --- | --- | --- |
| `kind` | 처리 결과 종류 (아래 4-2) | `recorded` |
| `command` | 음성 명령일 때 명령 이름 | `undo`, `progress`, `finish`, `repeat`, `skip` |
| `transcript` | STT가 받아쓴 문장 | `하부 콤 빗살 두 개 부러졌어요 부적합이에요` |
| `item` | 기록·변경된 항목 (아래 5-3) | |
| `llmRaw` | LLM 원래 출력 | `<esc_4_2>(판정='부적합', 위치='하부', ...)<esc_end>` |
| `speech` | 안내 `{text, audioId}` (audioId는 보드 TTS 음성, 지금은 null) | `3.10 승강기부품의 상태, 부적합으로 기록했습니다.` |
| `progress` | `{recorded, total}` 기록 수 / 전체 수 | `{1, 5}` |
| `missing` | `command=finish`일 때 빠진 항목 | `[]`이면 앱이 검토 화면을 엶 |
| `timing` | 단계별 처리 시간 `{sttMs, llmMs}` | `{2300, 1800}` |
| `reason` | `retry`·`ignored` 이유 | `말소리 아님` |

### 4-2. kind 종류

| kind | 언제 | 기록 | 앱 화면 |
| --- | --- | --- | --- |
| `recorded` | 항목·판정이 다 채워짐 | O | 안내 + 기록 카드 |
| `question` | 판정이나 위치가 비어서 되물음 | 일부 | 질문을 크게 (주황) |
| `answered` | 질문의 대답 (상부/하부, 적합/부적합) | O | 안내 + 기록 카드 |
| `command` | 음성 명령 | 명령마다 | 안내 |
| `ignored` | 점검과 상관없는 말, 또는 말소리 아님(빈 결과·한 글자) | X | 그대로 (아무것도 안 바뀜) |
| `retry` | LLM 출력 형식 오류·허용값 밖 | X | "다시 말씀해 주세요" |

### 4-3. 처리 순서

| 순서 | 단계 | 걸러지면 |
| --- | --- | --- |
| 1 | 앱 VAD: 주변 소음의 2.5배 이상, 0.3초 이상인 소리만 잘라서 전송 (1초 조용하면 문장 끝) | 전송 안 함 |
| 2 | STT (SenseVoice 또는 Whisper small) | 빈 결과·한 글자 → `ignored` |
| 3 | 교정 사전 + 숫자 표기 맞추기 (textnorm) | - |
| 4 | 음성 명령인지 (단어 포함 여부, LLM 안 거침) | 명령 처리 → `command` |
| 5 | 대기 중인 질문의 대답인지 | 빈칸 채움 → `answered` |
| 6 | LLM (Llama-3.2-1B LoRA) → `<esc_X_Y>(...)<esc_end>` | `<esc_end>`만 → `ignored` |
| 7 | 파싱·검증 (함수 이름, 허용값) | 실패 → `retry` |
| 8 | 규칙 엔진 판정 (측정값 → 기준표) | - |
| 9 | 빈칸이 있으면 되묻기 | `question` |

같은 `clientSeq`를 다시 보내면 다시 처리하지 않고 지난 응답을 그대로 돌려준다 (연결이 끊겨 재전송해도 두 번 기록되지 않음).

## 5. 데이터 모양

### 5-1. 이력 (#6)

| 칸 | 뜻 |
| --- | --- |
| `inspectionId` | 점검 번호 (#14 상세 보기에 사용) |
| `inspectedAt` | 제출 시각 |
| `inspector` | 점검자 |
| `durationMinutes` | 소요 시간 (분) |
| `status` | `NORMAL` / `CAUTION` (부적합이 하나라도 있으면 CAUTION) |
| `checkedItems` | 점검한 항목 번호 (다음 주기 계산용) |
| `cautionItems` | 부적합 항목 번호 |
| `note` | 부적합 특기사항 모음 |
| `inspectionType` | `self` / `regular` |

### 5-2. 결과 (#14)

| 칸 | 뜻 |
| --- | --- |
| `inspectionId`, `escalatorId` | 점검 번호, 설비 번호 |
| `summary` | `{적합, 부적합, 해당없음, 미확인}` 개수 |
| `items[]` | 전체 항목 (5-3) |
| `defects[]` | 부적합 항목만 |
| `submitted` | 제출했는지 |
| `inspectionType` | `self`(자체점검) / `regular`(정기검사) |
| `inspector`, `startedAt`, `submittedAt`, `durationMinutes` | 점검자, 시작·제출 시각, 소요 시간 |
| `reportUrl` | 보고서 주소 (제출한 점검만, 아니면 null) |

### 5-3. 항목 (item)

| 칸 | 값 |
| --- | --- |
| `itemCode` | 검사기준 번호 (예: `3.6.2`) |
| `itemName` | 항목 이름 (예: 비상정지장치) |
| `function` | LLM 함수 이름 (예: `esc_6_2`) |
| `status` | `적합` / `부적합` / `해당없음` / `미확인` |
| `location` | `상부` / `하부` / `양쪽` / null |
| `note` | 특기사항 |
| `action` | `현장조치완료` / `조치필요` / null |
| `detail` | 세부 항목 목록 |
| `measure` | 측정값 `{측정_좌, 측정_우, 측정값}` |
| `criteria` | 규칙 엔진이 판정에 쓴 기준 (예: 각 측 4mm 이하, 합 7mm 이하) |
| `judgedBy` | `speech`(말로 판정) / `rule`(규칙 엔진) / `selection`(선택형 항목, 선택값 기록) / `manual`(손 수정) |
| `updatedAt` | 기록 시각 |

## 6. 음성 명령

| 명령 | 알아듣는 말 | 하는 일 |
| --- | --- | --- |
| 취소 | 취소, 방금 거 지워, 되돌려 | 마지막 기록을 되돌림 (새로 추가된 항목이면 목록에서도 뺌) |
| 남은 개수 | 몇 개 남았, 얼마나 남았 | 기록 수와 남은 항목 안내 |
| 점검 끝 | 점검 끝, 점검 종료, 다 했어 | 빠진 항목을 하나씩 물어봄 (4개 이상 남으면 '나머지 건너뛰기' 안내). 다 끝나면 검토 화면 |
| 다시 읽기 | 다시 읽어, 뭐라고 | 마지막 안내 다시 |
| 건너뛰기 | 건너뛰기, 건너뛰어, 넘어가 | 묻고 있는 항목을 미점검으로 (다시 안 물음) |
| 나머지 건너뛰기 | 나머지 건너뛰기, 전부·모두·다 건너뛰기, 나머지 넘어가 | 남은 항목 전부 미점검 → 바로 검토 화면 |

명령은 단어 포함 여부로 찾는다. 점검 문장 안에 "취소"가 들어가도 명령으로 잡히니 주의.

## 7. 에러 코드

| HTTP | code | 언제 | 앱 처리 |
| --- | --- | --- | --- |
| 400 | `INVALID_REQUEST` | 발화에 audio·text 둘 다 없음 / 설비 등록에 번호·이름 없음 | 오류 표시 |
| 404 | `ESCALATOR_NOT_FOUND` | 없는 설비 | 새 설비 등록 안내 |
| 404 | `INSPECTION_NOT_FOUND` | 없는 점검 번호 | 오류 화면 / 상세: "상세 결과가 없어요" |
| 404 | `ITEM_NOT_FOUND` | 점검에 없는 항목 수정 | 오류 표시 |
| 404 | `AUDIO_NOT_FOUND` | 없는 음성 파일 | 재생 안 함 |
| 409 | `ESCALATOR_ALREADY_EXISTS` | 이미 있는 설비 등록 | 그대로 진행 (폰에 등록) |
| 409 | `INSPECTION_ALREADY_SUBMITTED` | 제출한 점검에 다시 기록·제출 | 오류 표시 |
| 409 | `MISSING_REQUIRED_ITEMS` | 미점검 항목이 있는데 `acknowledgeMissing=false` | 확인 창 → `true`로 다시 제출 |
| 422 | `NOTE_REQUIRED` | 부적합으로 고치는데 특기사항 없음 | 특기사항 입력 안내 |
| 503 | `MODEL_NOT_READY` | STT가 연결 안 됨 (음성을 보냈을 때) | 오류 표시 |

## 8. 보드 저장 (storage.py)

| 무엇 | 위치 | 언제 |
| --- | --- | --- |
| 등록한 설비 | `data/escalators.json` | #4 등록할 때 |
| 제출한 점검 | `data/inspections/<점검번호>.json` | #15 제출할 때 |
| 점검 보고서 | `data/reports/<점검번호>.html` | #15 제출할 때 |

서버를 다시 켜면 이 파일들을 읽어 설비·이력·지난 점검을 되살린다. 진행 중(제출 전) 점검은 저장되지 않는다. 저장 폴더는 `ONMAEUM_DATA_DIR`로 바꿀 수 있다. SQLite로 바꿀 때는 storage.py 함수 안만 바꾸면 된다.

## 9. 점검 종류와 항목

| 항목 | 내용 |
| --- | --- |
| 항목 기준 | 승강기 정기검사기준 [별표 2] 3. 에스컬레이터 (3.1.1 ~ 3.12, 70개). LLM 학습도 이 70개 |
| 정기검사 (`regular`) | 70개 전부 미확인으로 깔림 (매번 전 항목 확인) |
| 월간 자체점검 (`self`, 기본) | [별표 3] 자체점검기준 3. 에스컬레이터의 점검주기(1/1=매달, 1/3=3개월, 1/6=6개월)를 70개 항목에 연결. 지난 점검 기록(checkedItems)을 보고 주기가 돌아온 항목만 깔림 (기한 지난 것 먼저). 주기가 있는 항목 56개, 없는 14개(3.1.x 기본제원, 3.6.19, 3.10~3.12)는 정기검사 때 확인 |
| 대상 밖 항목 | 말하면 그 자리에서 목록에 추가되어 기록됨 |
| 위치 되묻기 | 학습 데이터에서 위치(상부/하부/양쪽)를 쓰는 23개 항목만 |
| 선택형 | 3.1.2~3.1.6 (설치위치·디딤판형식·구동방식·운전방식): 판정 대신 선택값을 기록하고 '적합(확인)'으로 표시 |

## 10. 보고서 (#16)

| 항목 | 내용 |
| --- | --- |
| 형식 | HTML 한 장 (보드에 PDF 라이브러리 불필요, 인터넷 없이 열림) |
| 내용 | 설비·위치·승강기번호, 점검 번호·점검자·시작·제출·소요, 판정별 개수, 부적합 항목, 전체 항목 표(번호·항목·위치·판정·특기사항·조치·판정 기준), 서명란 |
| 저장 | 제출할 때 보드 `data/reports/`에 저장. 제출 전에 열면 현재 상태로 미리보기 |
| 앱 | 제출 직후 알림의 "보고서 보기", 지난 점검 상세의 "보고서 보기" → 폰 브라우저 → 인쇄 / PDF로 저장 |

## 11. 서버 설정 (환경변수)

| 이름 | 뜻 | 없으면 |
| --- | --- | --- |
| `ONMAEUM_STT_DIR` | SenseVoice 모델 폴더 (model.int8.onnx, tokens.txt) | 아래 Whisper |
| `ONMAEUM_STT_WHISPER` | 팀 Whisper small 폴더 (whisper-ct2). 있으면 SenseVoice보다 먼저 씀 | 음성 요청 시 503 |
| `ONMAEUM_STT_THREADS` | STT CPU 코어 수 | 전부 |
| `ONMAEUM_LLM_CT2` | LLM CTranslate2 폴더 (PC·노트북·보드 CPU) | 아래 .rkllm |
| `ONMAEUM_LLM_PATH` | `.rkllm` 파일 (보드 NPU, rkllm_runner.py 필요) | 가짜 LLM (스텝 롤러·비상정지만) |
| `ONMAEUM_LLM_THREADS` | LLM CPU 코어 수 | 전부 |
| `ONMAEUM_TTS` | `1`이면 tts_runner.py (보드 VITS) | 안내는 글자로만 |
| `ONMAEUM_FUNCTIONS` | 점검표에 없는 함수를 더할 파일 (선택) | main.py 옆 functions_v3.json |
| `ONMAEUM_SESSION_ITEMS` | `demo`면 점검 시작 때 예전 5개만 깔기 | 70개 전부 |
| `ONMAEUM_DATA_DIR` | 설비·점검·보고서 저장 폴더 | `data` |
| `ONMAEUM_AUDIO_DIR` | TTS 음성 저장 폴더 | `audio_cache` |

## 12. 남은 일

| 할 일 | 담당 |
| --- | --- |
| 측정값 판정 규칙 추가 (지금은 3.5.3 스커트 틈새 1개) | 조해리 |
| 학습 데이터에 '상관없는 말' 예시(<esc_end>) 추가 후 재학습 검토 | 임창수 |
| 보드에 서버 올리기 (CPU) → 속도 측정 | 임창수 |
| `.rkllm` 변환 + rkllm_runner.py | 임창수 |
| 보드 TTS (tts_runner.py) + 앱 음성 재생 | 임창수 |
| 핫스팟, 자동 실행, SQLite | 정래원 |
