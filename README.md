# 모시응 보드 서버 (escalator-inspection-server)

에스컬레이터 점검자가 **말로** 점검 결과를 남기면, 보드가 받아써서(STT) 점검표 항목으로 바꾸고(LLM) 기록·판정·보고서까지 만드는 **온디바이스 FastAPI 서버**입니다.
한성대학교 모바일시스템응용프로젝트 N분반 2팀 **온마음** · 프론트엔드(앱): [onmaeum-team/frontend](https://github.com/onmaeum-team/frontend)

```
[휴대폰 앱]  말소리만 잘라 WAV 전송  ──▶  [보드 서버 (이 저장소)]
  버튼 없이 듣기 (VAD)                      STT → 교정 → 음성 명령 → LLM → 검증 → 규칙 판정
  결과 확인 · 검토 · 제출        ◀──        기록 · 되묻기 · 보고서 · 저장
```

- 인터넷 없이 현장에서 동작 (노트북 · RunPod · Orange Pi 5 Plus 모두 같은 코드, 환경변수만 다름)
- 점검표: 승강기 정기검사기준 [별표 2] 3. 에스컬레이터 **70개 항목**
- 월간 자체점검: [별표 3] 자체점검기준의 **점검주기**로 이번 달 대상 항목만 선택
- 보고서: 제출하면 HTML 보고서를 보드에 저장 (폰 브라우저에서 인쇄 / PDF 저장)

## 파일 구성

| 파일 | 하는 일 | 담당 |
| --- | --- | --- |
| `main.py` | API 16개, 점검 세션, 음성 명령, 제출 | 정래원 |
| `pipeline.py` | 모델 불러오기, STT·교정·LLM·파싱·검증·규칙 판정, 점검표 70개, 자체점검 주기 | 조해리 · 임창수 |
| `storage.py` | 보드에 파일로 저장 (설비, 제출한 점검, 보고서) → SQLite 로 교체 예정 | 정래원 |
| `report.py` | 점검 보고서 HTML | - |
| `rkllm_runner.py` | 보드 NPU 에서 `.rkllm` 실행 (빈칸, 마음AI 예시 코드로 채우기) | 임창수 |
| `tts_runner.py` | 보드 VITS 안내 음성 (빈칸) | 임창수 |
| `deploy/hotspot.sh` | 보드 Wi-Fi 핫스팟 | 정래원 |
| `deploy/onmaeum.service` | 보드 전원을 켜면 서버 자동 실행 (systemd) | 정래원 |
| `docs/` | API 명세, 자체점검 주기 연결표 | - |

## 빠른 시작

### 1. 노트북 (Windows, 모델 없이 '글자로 보내기' 테스트)
```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8080
```
`http://localhost:8080/docs` 에서 API 를 직접 눌러 볼 수 있습니다. 모델이 없으면 가짜 LLM(스텝 롤러·비상정지 문장만 이해)으로 동작합니다.

### 2. 진짜 STT·LLM 켜기
모델 파일은 Git 에 없습니다. RunPod `/workspace/mosieung/space/models_small/` 에서 받거나 팀 공유 위치에서 받으세요.
```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-llm.txt -r requirements-stt.txt
$env:ONMAEUM_STT_DIR="C:\models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"   # SenseVoice
$env:ONMAEUM_LLM_CT2="C:\models\models_small\Llama-3.2-1B-Escalator-ct2"              # Llama-3.2-1B LoRA (CTranslate2)
.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8080
```
서버 창에 `모델 상태: {'stt': 'sensevoice', 'llm': 'ct2', ...}` 가 나오면 성공입니다.
SenseVoice 모델: [sherpa-onnx asr-models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models) 의 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`.

### 3. 보드 (Orange Pi 5 Plus, Linux)
```bash
sudo apt install python3-venv          # Python 3.10 이상 필요
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-llm.txt -r requirements-stt.txt
export ONMAEUM_STT_DIR=/home/orangepi/models/sensevoice
export ONMAEUM_LLM_CT2=/home/orangepi/models/Llama-3.2-1B-Escalator-ct2
./venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080
```
현장 배포: `sudo bash deploy/hotspot.sh` (핫스팟) → `sudo cp deploy/onmaeum.service /etc/systemd/system/ && sudo systemctl enable --now onmaeum` (자동 실행).
확인: `curl http://localhost:8080/api/health`

### 4. 앱 연결
| 상황 | 앱 '주소 바꾸기'에 넣을 주소 |
| --- | --- |
| 에뮬레이터 | `10.0.2.2` |
| 실제 폰 + USB | `localhost` (노트북에서 `adb reverse tcp:8080 tcp:8080`) |
| 같은 Wi-Fi / 폰 핫스팟 | 서버 IP (예: `192.168.0.10`) |
| 보드 핫스팟 | `10.42.0.1` |

## API 요약 (16개)

| 메서드 | 경로 | 하는 일 |
| --- | --- | --- |
| GET | `/api/health` | 연결 확인, 모델 상태 |
| GET | `/api/audio/{audioId}` | 보드 TTS 안내 음성 (WAV) |
| GET · POST | `/api/escalators` | 설비 목록 / 새 설비 등록 |
| GET | `/api/escalators/{id}` | 설비 정보 (QR 스캔) |
| GET | `/api/escalators/{id}/histories` | 점검 이력 |
| GET | `/api/escalators/{id}/items?type=all\|self` | 점검표 70개 / 이번 달 자체점검 대상 |
| POST | `/api/inspections` | 점검 시작 (`inspectionType`: `self` 월간 자체점검 / `regular` 정기검사) |
| GET | `/api/inspections/{iid}` | 진행 상태 |
| POST | `/api/inspections/{iid}/utterances` | 말 한 덩어리 (WAV 또는 text) 처리 |
| POST | `/api/inspections/{iid}/undo` | 마지막 기록 취소 |
| PATCH | `/api/inspections/{iid}/items/{itemCode}` | 손 수정 |
| POST | `/api/inspections/{iid}/finish` | 점검 끝 (빠진 항목 확인) |
| GET | `/api/inspections/{iid}/result` | 결과 (검토 화면, 지난 점검 상세) |
| POST | `/api/inspections/{iid}/submit` | 제출 (이력 추가, 보고서 저장) |
| GET | `/api/inspections/{iid}/report` | 점검 보고서 HTML |

자세한 요청·응답·에러 코드: [`docs/모시응_API_명세_노션용.md`](docs/모시응_API_명세_노션용.md)

### 음성 명령
| 명령 | 말 | 하는 일 |
| --- | --- | --- |
| 취소 | 취소, 되돌려 | 마지막 기록 되돌리기 |
| 남은 개수 | 몇 개 남았어 | 남은 항목 안내 |
| 점검 끝 | 점검 끝, 다 했어 | 빠진 항목 하나씩 확인 |
| 다시 읽기 | 다시 읽어 줘 | 마지막 안내 반복 |
| 건너뛰기 | 건너뛰기, 넘어가 | 묻는 항목을 미점검으로 |
| 나머지 건너뛰기 | 나머지 건너뛰기, 전부 건너뛰기 | 남은 항목 전부 미점검 → 검토 화면 |

## 환경변수

| 이름 | 뜻 | 없으면 |
| --- | --- | --- |
| `ONMAEUM_STT_DIR` | SenseVoice 모델 폴더 | 아래 Whisper |
| `ONMAEUM_STT_WHISPER` | 팀 Whisper small 폴더 (있으면 SenseVoice 보다 먼저) | 음성 요청 시 503 |
| `ONMAEUM_LLM_CT2` | LLM CTranslate2 폴더 (CPU) | 아래 `.rkllm` |
| `ONMAEUM_LLM_PATH` | `.rkllm` 파일 (보드 NPU) | 가짜 LLM |
| `ONMAEUM_TTS` | `1` 이면 `tts_runner.py` 사용 | 안내는 글자로만 |
| `ONMAEUM_STT_THREADS` · `ONMAEUM_LLM_THREADS` | 사용할 CPU 코어 수 | 전부 |
| `ONMAEUM_DATA_DIR` | 설비·점검·보고서 저장 폴더 | `data` |
| `ONMAEUM_AUDIO_DIR` | TTS 음성 저장 폴더 | `audio_cache` |
| `ONMAEUM_SESSION_ITEMS` | `demo` 면 정기검사 때 예전 시연용 5개만 | 70개 |
| `ONMAEUM_FUNCTIONS` | 점검표에 없는 함수를 더할 JSON (선택) | `functions_v3.json` |

## 저장되는 것 (`data/`, Git 제외)
| 파일 | 내용 |
| --- | --- |
| `data/escalators.json` | 앱에서 새로 등록한 설비 |
| `data/inspections/<점검번호>.json` | 제출한 점검 (지난 점검 상세) |
| `data/reports/<점검번호>.html` | 점검 보고서 |

서버를 다시 켜면 이 파일들을 읽어 이력과 지난 점검을 되살립니다.

## 남은 일
- [ ] 보드 CPU 에서 실행·속도 측정 
- [ ] `.rkllm` 변환 + `rkllm_runner.py` 
- [ ] 보드 TTS (`tts_runner.py`) + 앱 음성 재생 
- [ ] 핫스팟, 자동 실행, SQLite
- [ ] 교정 사전(`CORRECTIONS`), 측정값 판정 규칙 추가 
- [ ] 학습 데이터에 '점검과 상관없는 말' 예시 추가 검사
- [ ] 자체점검 주기 연결표 팀 확인 ([`docs/자체점검_주기_연결표.md`](docs/자체점검_주기_연결표.md))

## 라이선스 · 모델
Built with Llama. Llama 3.2 is licensed under the Llama 3.2 Community License, Copyright © Meta Platforms, Inc. All Rights Reserved.
모델 파일과 학습 데이터는 이 저장소에 포함하지 않습니다.
