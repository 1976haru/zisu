"""Profile-scoped comment drafting aids. Nothing here posts or plays media."""
from __future__ import annotations

PERSONAS = {
    "jp_morning_chill": {"name": "일본 Morning Chill", "language": "ja", "tone": "친근하고 차분한", "emoji_level": "light", "length": "short"},
    "senior_old_pop": {"name": "시니어 Old Pop", "language": "ko", "tone": "차분하고 정중한", "emoji_level": "none", "length": "medium"},
}

# Twenty-five editable seeds per persona; these are generic and never assert
# unheard musical details. The operator's listening note is the only specific
# listening claim inserted into a suggested draft.
_JP = (
    "選曲の流れが心地よくて、最後までゆっくり楽しめました。",
    "朝の時間に合う雰囲気で、素敵なプレイリストですね。",
    "この一曲が入っていることで、全体の流れが印象に残りました。",
    "聴いていて気持ちが落ち着きました。素敵な選曲をありがとうございます。",
    "今日の気分にぴったりの音楽に出会えてうれしいです。",
    "リズムの心地よさが印象に残りました。",
    "ボーカルの表情がすてきで、もう一度聴きたくなりました。",
    "やさしい雰囲気に包まれて、ほっとする時間になりました。",
    "通勤前に聴くのにちょうどよく、気持ちよく一日を始められそうです。",
    "カフェで過ごす時間に合いそうな選曲ですね。",
    "懐かしい気持ちになりながら、楽しく聴かせていただきました。",
    "季節に合った空気感が感じられて、とても素敵でした。",
    "落ち着いた時間を過ごせました。ありがとうございます。",
    "曲のつながりが自然で、心地よく聴けました。",
    "このプレイリストを見つけられてよかったです。",
    "一日の始まりに聴きたい、やわらかな雰囲気ですね。",
    "作業の合間に気分を整える時間になりました。",
    "選曲から丁寧さが伝わってきました。",
    "音楽と一緒に穏やかな時間を楽しめました。",
    "またゆっくり聴きに来たいと思います。",
    "聴いたときの印象を大切にしたくなる音楽でした。",
    "この曲順が好きです。気持ちよく聴けました。",
    "朝のドライブにも合いそうな雰囲気ですね。",
    "心地よい時間をありがとうございます。",
    "今日のプレイリストに加えたいと思いました。",
)
_KO = (
    "선곡의 흐름이 편안해서 차분히 감상했습니다. 좋은 음악 감사합니다.",
    "아침에 듣기 좋은 분위기라 하루를 기분 좋게 시작할 수 있겠네요.",
    "이 곡이 더해져 플레이리스트의 흐름이 인상 깊었습니다.",
    "편안한 시간을 보내게 해 주셔서 고맙습니다.",
    "오늘 기분과 잘 어울리는 음악을 만났습니다.",
    "리듬이 참 인상적이어서 다시 듣고 싶어졌습니다.",
    "보컬의 느낌이 좋았습니다. 좋은 선곡 감사합니다.",
    "차분한 분위기 덕분에 잠시 여유를 느꼈습니다.",
    "출근길에 들으면 기분 전환이 될 것 같습니다.",
    "카페에서 천천히 듣기 좋은 선곡이네요.",
    "오랜만에 추억을 떠올리며 감상했습니다.",
    "계절과 잘 어울리는 분위기가 참 좋았습니다.",
    "편안하게 들을 수 있는 시간을 만들어 주셔서 감사합니다.",
    "곡이 자연스럽게 이어져서 즐겁게 감상했습니다.",
    "이 플레이리스트를 알게 되어 반갑습니다.",
    "하루를 시작하며 듣기 좋은 차분한 구성입니다.",
    "잠깐 쉬어 가는 시간에 잘 어울렸습니다.",
    "정성스러운 선곡이 느껴져 좋았습니다.",
    "음악과 함께 여유로운 시간을 보냈습니다.",
    "다음에도 천천히 감상하러 오겠습니다.",
    "감상한 뒤에도 여운이 남는 음악이었습니다.",
    "곡 순서가 마음에 들었습니다. 편하게 들었습니다.",
    "아침 드라이브에 잘 어울릴 것 같은 분위기네요.",
    "좋은 음악으로 편안한 시간을 보내게 해 주셔서 고맙습니다.",
    "오늘의 플레이리스트에 담아 두고 싶습니다.",
)
_EN = (
    "I enjoyed the flow of this playlist; thank you for sharing it.",
    "This was a lovely soundtrack for a quiet start to the day.",
    "The song order made for a relaxed listening session.",
    "Thanks for putting together a thoughtful selection.",
    "This fit the mood I was looking for today.",
    "The rhythm stood out to me during my listen.",
    "I appreciated the vocal moment I noted while listening.",
    "This made a calm little break in my day.",
    "I can imagine enjoying this on a commute.",
    "This would be pleasant background music for a cafe visit.",
    "It brought back a warm memory for me.",
    "This felt well suited to the season.",
    "Thank you for sharing music that made this a restful moment.",
    "The transitions felt smooth to me.",
    "I am glad I found this playlist.",
    "A gentle way to begin the morning.",
    "This was a nice companion for a short break.",
    "I enjoyed the care in the selection.",
    "I spent a peaceful moment listening to this.",
    "I would be happy to listen again.",
    "The impression from my listening stayed with me.",
    "I liked the sequence of the tracks.",
    "This could be a nice fit for a morning drive.",
    "Thanks for making time for a relaxing listen.",
    "I would add this to today's playlist.",
)

LIBRARY = {"jp_morning_chill": _JP, "senior_old_pop": _KO, "en_generic": _EN}
assert all(len(items) == 25 for items in LIBRARY.values())


def persona_for_profile(profile):
    if profile.get("comment_persona_name"):
        return {"name": profile["comment_persona_name"], "language": profile.get("comment_language") or "ja",
                "tone": profile.get("comment_tone") or "friendly", "emoji_level": profile.get("comment_emoji_level") or "light",
                "length": profile.get("comment_length") or "short"}
    if profile.get("market") == "JP" and profile.get("genre") == "MORNING_CHILL_RAP":
        return dict(PERSONAS["jp_morning_chill"])
    return dict(PERSONAS["senior_old_pop"] if profile.get("genre") == "OLD_POP" else {
        "name": "기본 채널 페르소나", "language": "ko" if profile.get("market") == "KR" else "ja",
        "tone": "차분하고 친근한", "emoji_level": "light", "length": "short"})


def suggest_drafts(profile, title: str, genre: str, observation: str, count: int = 3):
    """Return three human-editable drafts anchored to a real listening note."""
    note = (observation or "").strip()
    if len(note) < 10:
        raise ValueError("실제로 감상한 뒤 10자 이상의 메모를 먼저 입력하세요.")
    persona = persona_for_profile(profile)
    key = {"ja":"jp_morning_chill","ko":"senior_old_pop","en":"en_generic"}[persona["language"]]
    seeds = LIBRARY[key]
    # Stable title/genre ordering helps, but never infers musical facts from metadata.
    start = sum(ord(ch) for ch in (title + genre)) % len(seeds)
    note_limit={"short":60,"medium":100,"long":140}.get(persona["length"],60)
    result = []
    safe_title=(title or "").strip()[:60]
    for offset in range(min(3, max(1, count))):
        base = seeds[(start + offset * 7) % len(seeds)]
        if persona["language"] == "ja":
            draft=f"「{safe_title}」を聴いて、「{note[:note_limit]}」という印象が残りました。{base}"
        elif persona["language"] == "ko":
            draft=f"‘{safe_title}’을 감상하며 ‘{note[:note_limit]}’라는 느낌이 남았습니다. {base}"
        else:
            draft=f'While listening to "{safe_title}", I noticed: "{note[:note_limit]}". {base}'
        if persona["emoji_level"] == "light":
            draft += " ☕" if persona["language"] == "ja" else " 🌿" if persona["language"] == "ko" else " ☕"
        elif persona["emoji_level"] == "moderate":
            draft += " ☕🎧" if persona["language"] == "ja" else " 🌿🎧"
        result.append(draft[:400])
    return result
