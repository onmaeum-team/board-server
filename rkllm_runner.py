"""보드(Orange Pi 5 Plus, RK3588 NPU)에서 .rkllm 모델을 돌리는 파일.

pipeline.load_models() 가 ONMAEUM_LLM_PATH 가 있을 때 이 파일의 load() 를 부른다.
마음AI가 준 rkllm-class-dist 예시 코드의 '실행(추론)' 부분을 아래 TODO 자리에 옮기면 된다.
  1) load(): 예시 코드의 모델 초기화 (librkllmrt.so 불러오기, 파라미터 설정, init)
  2) _generate(): 문장 하나 넣고 결과 글자를 모아서 돌려주기 (예시 코드의 run + 콜백)
  3) release(): 예시 코드의 종료(destroy)

주의: 프롬프트 모양은 학습 때와 '글자 하나까지' 같아야 한다 → build_prompt() 는 pipeline.build_prompt 를 그대로 씀.
      rkllm 이 프롬프트 템플릿을 따로 붙이는 옵션이 있으면 끄거나, 위 템플릿과 같게 맞출 것.
"""

_handle = None  # 불러온 모델 (예시 코드가 돌려주는 핸들)


def build_prompt(text: str) -> str:
    """학습 때 쓴 프롬프트 그대로 (pipeline.build_prompt = 데모 app.py 의 to_call 과 같음)."""
    from pipeline import build_prompt as _bp
    return _bp(text)


def load(model_path: str):
    """모델을 한 번 불러오고, '문장 -> <esc_..>(...)<esc_end>' 함수를 돌려준다."""
    global _handle
    # TODO: 마음AI 예시 코드의 초기화 부분을 여기로 옮기기
    #   - librkllmrt.so 불러오기
    #   - 파라미터: model_path, max_new_tokens(함수 호출 한 줄이면 128 정도), top_k=1 (항상 같은 답이 나오게)
    #   - init 호출 결과를 _handle 에 저장
    raise NotImplementedError("rkllm_runner.load() 를 마음AI 예시 코드로 채워 주세요")

    def generate(text: str) -> str:  # noqa: unreachable — 위 TODO 를 채우면 이 함수가 쓰임
        return _generate(build_prompt(text))

    return generate


def _generate(prompt: str) -> str:
    """프롬프트 하나를 넣고 모델이 낸 글자를 다 모아서 돌려준다.
    TODO: 예시 코드의 run 호출 + 콜백(글자가 나올 때마다 모으기)을 여기로."""
    raise NotImplementedError


def release():
    """서버 종료 때 NPU 자원 해제. TODO: 예시 코드의 destroy 호출."""
    global _handle
    _handle = None
