"""Conservative, explainable KR/JP music-genre classification."""
from dataclasses import dataclass
import re

FIT, REVIEW, EXCLUDE, MANUAL_APPROVAL = "적합", "검토", "제외", "수동 승인"

@dataclass(frozen=True)
class Genre:
    title: str
    positives: tuple[str, ...]
    negatives: tuple[str, ...]
    jp_queries: tuple[str, ...]
    kr_queries: tuple[str, ...]
    required_context: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

COMMON_NON_MUSIC = ("asmr", "먹방", "朗読", "環境音", "백색소음", "white noise")
SLEEP_STUDY = ("수면", "숙면", "공부", "집중", "作業用", "睡眠用", "勉強用", "lofi beats to study")

GENRES = {
 "MORNING_CHILL_RAP": Genre("Morning Chill Rap",
  ("morning chill rap", "chill rap", "mellow rap", "melodic rap", "감성 힙합", "멜로딕 랩", "칠 랩", "チルラップ", "メロウラップ", "朝ラップ", "メロディックラップ"),
  COMMON_NON_MUSIC + SLEEP_STUDY + ("클래식", "classic bgm", "solo piano", "jazz instrumental", "카페 재즈", "ジャズ", "ハードコアラップ", "무보컬", "보컬 없음", "instrumental only", "ambient", "앰비언트", "インストゥルメンタル", "アンビエント"),
  ("朝 チルラップ", "朝 メロウラップ", "モーニング チルラップ", "朝 ドライブ チルラップ", "朝の感性ヒップホップ", "朝 R&B ヒップホップ", "Morning Chill Rap Japan", "朝 通勤 ラップ", "朝 カフェ チルラップ", "爽やか チルラップ", "朝 プレイリスト ヒップホップ", "朝 日本語ラップ"),
  ("아침 칠랩", "모닝 칠랩", "아침 감성 힙합", "출근길 칠랩", "아침 드라이브 힙합", "Morning Chill Rap Korea", "아침 멜로우 랩", "모닝 감성랩", "아침 R&B 힙합", "상쾌한 칠랩", "아침 플레이리스트 힙합", "출근 감성랩"),
  ("morning", "아침", "모닝", "출근", "朝", "モーニング", "通勤", "爽やか"),
  ("CHILL_RAP", "RNB_HIPHOP", "CAFE_CHILL", "JAZZ_LOUNGE")),
 "CHILL_RAP": Genre("Chill Rap",
  ("chill rap", "mellow rap", "칠랩", "칠 랩", "감성 랩", "감성 힙합", "チルラップ", "メロウラップ"),
  COMMON_NON_MUSIC + SLEEP_STUDY + ("클래식", "solo piano", "jazz instrumental", "カフェジャズ"),
  ("チルラップ プレイリスト", "メロウラップ 日本", "日本語 チルラップ", "落ち着く ラップ", "チルヒップホップ", "夜 チルラップ", "ドライブ チルラップ", "Japanese Chill Rap Playlist", "感性 ヒップホップ", "ゆったり ラップ", "チルラップ ミックス", "メロウ ヒップホップ"),
  ("칠랩 플레이리스트", "감성 랩 모음", "멜로우 랩", "감성 힙합", "한국 칠랩", "잔잔한 힙합", "드라이브 칠랩", "밤 감성 힙합", "Chill Rap Korea", "칠랩 믹스", "부드러운 랩", "감성 힙합 플레이리스트"), conflicts=("MORNING_CHILL_RAP", "RNB_HIPHOP")),
 "RNB_HIPHOP": Genre("R&B Hip-Hop",
  ("r&b hip-hop", "r&b hiphop", "알앤비 힙합", "감성 알앤비", "neo soul", "R&B ヒップホップ", "ネオソウル"),
  COMMON_NON_MUSIC + ("클래식", "jazz instrumental", "ハードコア", "heavy metal"),
  ("R&B ヒップホップ 日本", "感性 R&B ヒップホップ", "ネオソウル 日本", "Japanese R&B Hip-Hop", "メロウ R&B ヒップホップ", "夜 R&B ヒップホップ", "R&B ラップ プレイリスト", "日本語 R&B ラップ", "チル R&B ヒップホップ", "都会 R&B ヒップホップ", "R&B Hip-Hop Mix Japan", "エモーショナル R&B"),
  ("감성 R&B 힙합", "알앤비 힙합 플레이리스트", "한국 R&B 힙합", "네오소울 힙합", "멜로우 R&B 힙합", "밤 R&B 힙합", "드라이브 알앤비 힙합", "R&B Hip-Hop Korea", "감성 알앤비 랩", "한국어 R&B 랩", "칠 R&B 힙합", "알앤비 힙합 믹스"), conflicts=("CHILL_RAP",)),
 "CAFE_CHILL": Genre("Cafe Chill",
  ("cafe chill", "café chill", "카페 칠", "카페 팝", "acoustic pop", "カフェチル", "カフェポップ", "アコースティックポップ"),
  COMMON_NON_MUSIC + ("hardcore rap", "heavy metal", "수면 음악", "睡眠用"),
  ("カフェチル プレイリスト", "カフェポップ 日本", "朝 カフェチル", "アコースティック カフェ", "カフェ BGM チル", "Japanese Cafe Chill", "カフェ 洋楽 チル", "カフェ リラックス ポップ", "昼 カフェチル", "おしゃれ カフェポップ", "カフェチル ミックス", "作業 カフェポップ"),
  ("카페 칠 플레이리스트", "카페 팝 모음", "아침 카페 칠", "어쿠스틱 카페 팝", "카페 BGM 칠", "Cafe Chill Korea", "브런치 카페 팝", "감성 카페 칠", "낮 카페 플레이리스트", "카페 칠 믹스", "잔잔한 카페 팝", "드라이브 카페 칠"), conflicts=("MORNING_CHILL_RAP", "JAZZ_LOUNGE")),
 "OLD_POP": Genre("Old Pop",
  ("old pop", "oldies", "올드팝", "7080 팝", "추억의 팝송", "オールディーズ", "懐かしい洋楽", "60年代", "70年代", "80年代"),
  COMMON_NON_MUSIC + ("chill rap", "힙합", "ヒップホップ", "hardcore", "metal", "edm"),
  ("オールディーズ プレイリスト", "懐かしい洋楽 70年代", "80年代 洋楽", "60年代 洋楽", "Old Pop Japan", "昭和 洋楽", "70年代 ヒット曲", "80年代 ヒット曲", "懐かしい ポップス", "オールディーズ 名曲", "70s 80s Japan", "昔の洋楽"),
  ("올드팝 플레이리스트", "7080 팝송", "추억의 팝송", "60년대 팝송", "70년대 팝송", "80년대 팝송", "Old Pop Korea", "올드팝 명곡", "7080 올드팝", "옛날 팝송", "추억의 올드팝", "올드팝 모음")),
 "JAZZ_LOUNGE": Genre("Jazz Lounge",
  ("jazz lounge", "재즈 라운지", "스무스 재즈", "ジャズラウンジ", "スムースジャズ", "lounge jazz"),
  COMMON_NON_MUSIC + ("chill rap", "감성 힙합", "ヒップホップ", "hardcore", "metal", "edm"),
  ("ジャズラウンジ プレイリスト", "スムースジャズ 日本", "ラウンジジャズ", "Jazz Lounge Japan", "夜 ジャズラウンジ", "ホテル ラウンジジャズ", "大人 ジャズラウンジ", "カフェ ジャズラウンジ", "ボサノバ ジャズラウンジ", "都会 ジャズラウンジ", "ジャズラウンジ ミックス", "リラックス ジャズラウンジ"),
  ("재즈 라운지 플레이리스트", "스무스 재즈", "라운지 재즈", "Jazz Lounge Korea", "밤 재즈 라운지", "호텔 라운지 재즈", "성인 재즈 라운지", "카페 재즈 라운지", "보사노바 재즈 라운지", "도시 재즈 라운지", "재즈 라운지 믹스", "편안한 재즈 라운지"), conflicts=("CAFE_CHILL",)),
}

@dataclass(frozen=True)
class Verdict:
    decision: str
    score: int
    reason: str
    positive_signals: tuple[str, ...] = ()
    conflict_signals: tuple[str, ...] = ()
    uncertainty: str = ""

def _contains(text: str, term: str) -> bool:
    folded = term.casefold()
    if folded.isascii() and len(folded) <= 4:
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(folded) + r"(?![a-z0-9])", text))
    return folded in text

def _signals(genre: Genre, text: str) -> list[str]:
    return [term for term in genre.positives if _contains(text, term)]

def assess(genre_id: str, title: str, description: str = "") -> Verdict:
    """Classify from metadata only; search keywords never influence the result."""
    genre = GENRES[genre_id]
    title_text = (title or "").casefold()
    description_text = (description or "")[:1500].casefold()
    combined = title_text + "\n" + description_text
    negative = [term for term in genre.negatives if _contains(combined, term)]
    if negative:
        return Verdict(EXCLUDE, 0, "배제 신호: " + ", ".join(negative[:3]),
                       conflict_signals=tuple(negative), uncertainty="명시적 배제 조건")
    title_hits = _signals(genre, title_text)
    description_hits = [x for x in _signals(genre, description_text) if x not in title_hits]
    context_hits = [x for x in genre.required_context if _contains(combined, x)]
    conflicting = []
    for other_id in genre.conflicts:
        conflicting.extend(_signals(GENRES[other_id], combined))
    score = min(100, 55 * bool(title_hits) + 15 * min(max(len(title_hits)-1, 0), 1) + 10 * min(len(description_hits), 2))
    positives = tuple(dict.fromkeys(title_hits + description_hits))
    if genre_id == "MORNING_CHILL_RAP":
        rap_terms = ("rap", "랩", "ラップ", "힙합", "hip-hop", "hiphop", "ヒップホップ")
        if not any(_contains(combined, term) for term in rap_terms):
            return Verdict(REVIEW, min(score, 39), "랩/힙합 리듬 또는 보컬 근거가 부족함",
                           positives, uncertainty="음악을 직접 들어야 함")
    if genre.required_context:
        if not context_hits:
            return Verdict(REVIEW, min(score, 49), "아침/모닝 맥락이 없어 자동 적합 불가",
                           positives, uncertainty="시간대 맥락 부족")
        score = min(100, score + 20)
    own_hits = set(title_hits + description_hits)
    conflicting = [x for x in conflicting if x not in own_hits]
    if conflicting:
        conflicts = tuple(dict.fromkeys(conflicting))
        return Verdict(REVIEW, min(score, 69), "인접 장르 신호가 함께 있음: " + ", ".join(conflicts[:3]),
                       positives, conflicts, "교차 장르 가능성")
    if title_hits and score >= 70:
        return Verdict(FIT, score, "제목 장르 신호: " + ", ".join(title_hits[:3]), positives)
    if title_hits or description_hits:
        return Verdict(REVIEW, score, "장르 신호는 있으나 제목·설명만으로 확정 불가",
                       positives, uncertainty="메타데이터 근거 부족")
    return Verdict(REVIEW, 0, "장르 근거 부족: 직접 감상 후 검토",
                   uncertainty="긍정 신호 없음")

def search_terms(genre_id: str, market: str) -> tuple[str, ...]:
    if market not in ("JP", "KR"):
        raise ValueError("시장 코드는 JP 또는 KR이어야 합니다.")
    genre = GENRES[genre_id]
    return genre.jp_queries if market == "JP" else genre.kr_queries

def related_classification(core_id: str, related_ids, title: str, description: str = "") -> dict:
    """Keep core verdict independent; related signals are evidence, never promotion."""
    core = assess(core_id, title, description)
    signals = {}
    for genre_id in related_ids or ():
        if genre_id not in GENRES or genre_id == core_id:
            continue
        result = assess(genre_id, title, description)
        if result.positive_signals:
            signals[genre_id] = {"decision": result.decision, "signals": result.positive_signals}
    if core.decision == EXCLUDE:
        category = "exclude"
    elif core.decision == FIT:
        category = "core_fit"
    elif signals:
        category = "cross_genre_review" if any(v["decision"] == FIT for v in signals.values()) else "related_reference"
    else:
        category = "exclude" if core.decision == EXCLUDE else "review"
    return {"category": category, "core": core, "related_evidence": signals,
            "human_listening_required": core.decision == REVIEW}
