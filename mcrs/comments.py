"""Draft helper: never posts comments; requires the user's own observation."""
import re


def from_observation(observation):
    observation = observation.strip()
    if len(observation) < 10:
        raise ValueError("실제로 감상한 뒤 구체적인 일본어 감상을 10자 이상 입력하세요.")
    if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", observation):
        raise ValueError("일본어 감상 포인트를 입력해주세요. 자동번역 기능은 아직 없습니다.")
    if "チャンネルに来て" in observation or "登録返し" in observation:
        raise ValueError("채널 방문 유도·맞구독 문구는 댓글 초안에 사용하지 않습니다.")
    if len(observation) > 400:
        raise ValueError("댓글 초안은 400자 이내로 작성하세요.")
    return observation
