"""보드에서 VITS 로 안내 음성을 만드는 파일.

pipeline.load_models() 가 ONMAEUM_TTS=1 일 때 이 파일의 load() 를 부른다.
load() 는 '문장 -> WAV 바이트' 함수를 돌려주면 된다. 만든 음성은 pipeline.tts() 가 저장하고,
앱은 GET /api/audio/{id} 로 받아 재생한다 (앱 voice_guide.dart 의 speak() 에서).
"""


def load():
    # TODO(임창수): VITS 모델 불러오기 (마음AI 자료 또는 onnx 모델). 한 번만 불러오고 아래 함수를 돌려줌
    raise NotImplementedError("tts_runner.load() 를 VITS 모델로 채워 주세요")

    def synth(text: str) -> bytes:  # noqa: unreachable
        # TODO: text -> 16bit PCM WAV 바이트 (wave 모듈로 헤더 붙이기)
        raise NotImplementedError

    return synth
