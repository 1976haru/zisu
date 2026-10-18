"""Conservative, explainable KR/JP music-genre classification."""
from dataclasses import dataclass
import hashlib
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
    aliases: tuple[str, ...] = ()
    en_queries: tuple[str, ...] = ()
    is_custom: bool = False
    localized_queries: tuple[tuple[str, tuple[str, ...]], ...] = ()

COMMON_NON_MUSIC = ("asmr", "먹방", "朗読", "環境音", "백색소음", "white noise")
SLEEP_STUDY = ("수면", "숙면", "공부", "집중", "作業用", "睡眠用", "勉強用", "lofi beats to study")

GENRES = {
 "MORNING_CHILL_RAP": Genre("Morning Chill Rap",
  ("morning chill rap", "chill rap", "mellow rap", "melodic rap", "감성 힙합", "멜로딕 랩", "칠 랩", "모닝 칠랩", "아침 칠랩", "チルラップ", "メロウラップ", "朝ラップ", "メロディックラップ"),
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

# Existing V2/V3 IDs remain a stable compatibility boundary.
LEGACY_GENRE_IDS = tuple(GENRES)

# Additional conservative built-in presets. Existing six IDs and definitions
# above remain stable for persisted V2/V3 profiles and audit rows.
_PRESETS = {
 "JAZZ_SMOOTH": ("Jazz / Smooth Jazz", ("smooth jazz", "jazz", "스무스 재즈", "재즈", "スムースジャズ", "ジャズ"),
   ("asmr", "sleep music", "no vocals", "ambient only"), ("スムースジャズ", "ジャズラウンジ", "朝 ジャズ"), ("스무스 재즈", "재즈 라운지", "아침 재즈"), ("smooth jazz playlist", "jazz lounge")),
 "INDIE_POP": ("Indie / Indie Pop", ("indie pop", "indie", "인디 팝", "인디 음악", "インディーポップ", "インディーズ"),
   ("asmr", "karaoke", "instrumental only"), ("インディーポップ", "日本 インディー", "インディーズ 音楽"), ("인디 팝", "한국 인디 음악", "인디 플레이리스트"), ("indie pop playlist", "independent music")),
 "LOFI": ("Lo-fi", ("lo-fi", "lofi", "lo fi"), ("asmr", "white noise", "rain sounds only"),
   ("lofi hip hop", "ローファイ 音楽", "作業用 lo-fi"), ("로파이 음악", "lofi 힙합", "공부 로파이"), ("lo-fi playlist", "lofi beats")),
 "MELODIC_HIPHOP": ("Melodic Hip-Hop", ("melodic hip-hop", "melodic hip hop", "멜로딕 힙합", "멜로딕 랩", "メロディックヒップホップ", "メロディックラップ"),
   ("asmr", "sleep music", "no vocals"), ("メロディックラップ", "メロディックヒップホップ"), ("멜로딕 랩", "멜로딕 힙합"), ("melodic hip-hop playlist", "melodic rap")),
 "CAFE_POP": ("Cafe Pop", ("cafe pop", "카페 팝", "카페 음악", "カフェポップ", "カフェミュージック"),
   ("asmr", "sleep music", "no music"), ("カフェポップ", "カフェミュージック", "喫茶店 音楽"), ("카페 팝", "카페 플레이리스트", "카페 음악"), ("cafe pop playlist", "cafe music")),
 "CAFE_JAZZ": ("Cafe Jazz", ("cafe jazz", "카페 재즈", "카페 재즈 음악", "カフェジャズ", "カフェ ボサノバ"),
   ("asmr", "sleep music", "no music"), ("カフェジャズ", "喫茶店 ジャズ"), ("카페 재즈", "카페 재즈 플레이리스트"), ("cafe jazz playlist", "cafe bossa nova")),
 "SENIOR_OLDIES": ("Senior Oldies", ("senior oldies", "oldies", "시니어 올드팝", "추억의 팝송", "懐メロ", "オールディーズ"),
   ("asmr", "karaoke only"), ("懐メロ 洋楽", "オールディーズ", "シニア ポップス"), ("시니어 올드팝", "추억의 팝송", "7080 올드팝"), ("senior oldies", "classic pop hits")),
 "CHANSON": ("Chanson", ("chanson", "샹송", "シャンソン", "프랑스 샹송"),
   ("asmr", "instrumental only"), ("フレンチシャンソン", "シャンソン 名曲"), ("샹송 명곡", "프랑스 샹송"), ("chanson playlist", "french chanson")),
 "LATIN": ("Latin", ("latin music", "라틴 음악", "라틴 팝", "ラテン音楽", "ラテンポップ"),
   ("asmr", "instrumental only"), ("ラテンポップ", "ラテン音楽"), ("라틴 음악", "라틴 팝 플레이리스트"), ("latin pop playlist", "latin music")),
 "BOSSA_NOVA": ("Bossa Nova", ("bossa nova", "보사노바", "보사 노바", "ボサノバ", "ボサ・ノヴァ"),
   ("asmr", "sleep sounds only"), ("ボサノバ", "ボサノバ カフェ"), ("보사노바", "카페 보사노바"), ("bossa nova playlist", "bossa nova cafe")),
 "ACOUSTIC_BALLAD": ("Acoustic / Ballad", ("acoustic ballad", "acoustic", "어쿠스틱 발라드", "발라드", "アコースティックバラード", "バラード"),
   ("asmr", "white noise", "instrumental only"), ("アコースティックバラード", "バラード名曲"), ("어쿠스틱 발라드", "발라드 플레이리스트"), ("acoustic ballad playlist", "acoustic songs")),
}
for _genre_id, (_title, _positive, _negative, _jp, _kr, _en) in _PRESETS.items():
    GENRES[_genre_id] = Genre(_title, _positive, _negative, _jp, _kr,
                              aliases=_positive, en_queries=_en)


def custom_genre_id(name: str) -> str:
    """Return a stable opaque ID for a user-defined genre name."""
    normalized = " ".join(str(name or "").split()).casefold()
    if not 2 <= len(normalized) <= 80:
        raise ValueError("장르 이름은 2~80자로 입력하세요.")
    return "CUSTOM_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16].upper()


def validate_custom_genres(definitions) -> list[dict]:
    if definitions is None:
        return []
    if not isinstance(definitions, (list, tuple)) or len(definitions) > 50:
        raise ValueError("사용자 장르는 프로필마다 최대 50개까지 등록할 수 있습니다.")
    normalized = []
    seen = set()
    fields = {"aliases": 24, "include_terms": 24, "exclude_terms": 24,
              "jp_queries": 24, "kr_queries": 24, "en_queries": 24, "conflicts": 12}
    for item in definitions:
        if not isinstance(item, dict):
            raise ValueError("사용자 장르 설정 형식이 올바르지 않습니다.")
        allowed = {"id", "name", *fields, "localized_queries"}
        if set(item) - allowed:
            raise ValueError("지원하지 않는 사용자 장르 설정 항목이 있습니다.")
        name = " ".join(str(item.get("name", "")).split())
        genre_id = str(item.get("id") or custom_genre_id(name))
        if not re.fullmatch(r"CUSTOM_[A-F0-9]{16}", genre_id) or genre_id in seen or genre_id in GENRES:
            raise ValueError("사용자 장르 ID가 중복되었거나 잘못되었습니다.")
        if not 2 <= len(name) <= 80:
            raise ValueError("사용자 장르 이름은 2~80자로 입력하세요.")
        seen.add(genre_id)
        record = {"id": genre_id, "name": name}
        for field, limit in fields.items():
            raw = item.get(field, [])
            if isinstance(raw, str):
                raw = raw.splitlines() if field == "conflicts" else raw.split(",")
            if not isinstance(raw, (list, tuple)) or len(raw) > limit:
                raise ValueError(f"{field} 목록이 올바르지 않습니다.")
            values = []
            for value in raw:
                text = " ".join(str(value).split())
                if len(text) > 100:
                    raise ValueError(f"{field} 항목은 100자 이하여야 합니다.")
                if text and text not in values:
                    values.append(text)
            record[field] = values
        if not record["include_terms"]:
            raise ValueError("사용자 장르에는 적어도 하나의 포함 신호가 필요합니다.")
        localized = item.get("localized_queries", {})
        if not isinstance(localized, dict) or len(localized) > 8:
            raise ValueError("추가 언어 검색어 설정이 올바르지 않습니다.")
        localized_clean = {}
        for language, raw_terms in localized.items():
            code = str(language).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{2,12}", code) or code in {"JP", "KR", "EN"}:
                raise ValueError("추가 검색 언어 코드는 JP/KR/EN을 제외한 영문·숫자 2~12자여야 합니다.")
            if isinstance(raw_terms, str):
                raw_terms = raw_terms.split(",")
            if not isinstance(raw_terms, (list, tuple)) or len(raw_terms) > 24:
                raise ValueError("언어별 검색어는 최대 24개까지 가능합니다.")
            terms = [" ".join(str(term).split()) for term in raw_terms if str(term).strip()]
            if any(len(term) > 100 for term in terms):
                raise ValueError("검색어는 100자 이하여야 합니다.")
            localized_clean[code] = list(dict.fromkeys(terms))
        record["localized_queries"] = localized_clean
        if any(value not in GENRES and value not in seen for value in record["conflicts"]):
            # Cross-references may target another custom definition declared later;
            # full validation is completed after all IDs have been collected below.
            pass
        normalized.append(record)
    valid_ids = set(GENRES) | seen
    if any(value not in valid_ids for item in normalized for value in item["conflicts"]):
        raise ValueError("충돌 장르는 등록된 프리셋 또는 사용자 장르여야 합니다.")
    return normalized


def custom_catalog(definitions=None) -> dict[str, Genre]:
    result = {}
    for item in validate_custom_genres(definitions):
        result[item["id"]] = Genre(item["name"], tuple(item["include_terms"]),
            tuple(item["exclude_terms"]), tuple(item["jp_queries"]), tuple(item["kr_queries"]),
            conflicts=tuple(item["conflicts"]), aliases=tuple(item["aliases"]),
            en_queries=tuple(item["en_queries"]), is_custom=True,
            localized_queries=tuple((code, tuple(terms)) for code, terms in item["localized_queries"].items()))
    return result


def genre_title(genre_id: str, custom_genres=None) -> str:
    genre = GENRES.get(genre_id) or custom_catalog(custom_genres).get(genre_id)
    return genre.title if genre else f"미등록 장르 ({genre_id})"

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
    return [term for term in (*genre.positives, *genre.aliases) if _contains(text, term)]


# A genre word alone is insufficient. For these rap genres, corroborating
# metadata in the description must independently mention both vocal presence
# and a beat/rhythm before the rule score can cross the recommendation threshold.
_RAP_SUPPORT = {
    "vocal": ("vocal", "vocals", "vocal performance", "보컬", "보컬 중심", "ボーカル", "歌声"),
    "rhythm": ("beat", "beats", "rhythm", "비트", "리듬", "ビート", "リズム"),
}
_RAP_SUPPORT_GENRES = {"MORNING_CHILL_RAP", "CHILL_RAP"}
_RAP_ABSENCE_TERMS = (
    "no vocals", "without vocals", "no rap", "without rap", "rap-free",
    "보컬 없음", "보컬이 없는", "랩 없음", "랩이 없는",
    "ボーカルなし", "歌声なし", "ラップなし", "ラップのない",
)


def _rap_support(genre_id: str, description: str) -> tuple[str, ...]:
    if genre_id not in _RAP_SUPPORT_GENRES:
        return ()
    evidence = []
    for group, terms in _RAP_SUPPORT.items():
        match = next((term for term in terms if _contains(description, term)), None)
        if match:
            evidence.append(f"{group}: {match}")
    return tuple(evidence)

def assess(genre_id: str, title: str, description: str = "", custom_genres=None) -> Verdict:
    """Classify from metadata only; search keywords never influence the result."""
    custom = custom_catalog(custom_genres)
    catalog = {**GENRES, **custom}
    genre = catalog.get(genre_id)
    if not genre:
        return Verdict(REVIEW, 0, "선택한 장르 규칙을 찾을 수 없습니다. 장르 설정을 확인하세요.",
                       uncertainty="미지원 또는 미등록 장르")
    title_text = (title or "").casefold()
    description_text = (description or "")[:1500].casefold()
    combined = title_text + "\n" + description_text
    negative = [term for term in genre.negatives if _contains(combined, term)]
    if genre_id in _RAP_SUPPORT_GENRES:
        negative.extend(term for term in _RAP_ABSENCE_TERMS if _contains(combined, term))
    if negative:
        return Verdict(EXCLUDE, 0, "배제 신호: " + ", ".join(negative[:3]),
                       conflict_signals=tuple(negative), uncertainty="명시적 배제 조건")
    title_hits = _signals(genre, title_text)
    description_hits = [x for x in _signals(genre, description_text) if x not in title_hits]
    support_evidence = _rap_support(genre_id, description_text)
    context_hits = [x for x in genre.required_context if _contains(combined, x)]
    conflicting = []
    morning_rap_compatibility = bool(
        genre_id == "MORNING_CHILL_RAP" and context_hits and title_hits
        and {"vocal", "rhythm"}.issubset({item.split(":", 1)[0] for item in support_evidence})
    )
    for other_id in genre.conflicts:
        # Chill Rap and R&B/Neo Soul are constituent sounds of Morning Chill
        # Rap when morning context, a direct core cue, and vocal+rhythm evidence
        # all agree. Keep Cafe/Jazz and other unrelated genre conflicts intact.
        if morning_rap_compatibility and other_id in {"CHILL_RAP", "RNB_HIPHOP"}:
            continue
        other = catalog.get(other_id)
        if other:
            conflicting.extend(_signals(other, combined))
    if genre.is_custom:
        # User rules are never treated as audio truth. A direct title match and
        # two distinct description signals are required before custom FIT.
        title_hits = _signals(genre, title_text)
        description_hits = _signals(genre, description_text)
        score = min(95, 55 * bool(title_hits) + 10 * min(len(set(description_hits)), 4))
        conflicting = []
        for other_id in genre.conflicts:
            other = catalog.get(other_id)
            if other:
                conflicting.extend(_signals(other, combined))
        positives = tuple(dict.fromkeys(title_hits + description_hits))
        if conflicting:
            conflicts = tuple(dict.fromkeys(conflicting))
            return Verdict(REVIEW, min(score, 69), "충돌하는 장르 신호가 있어 직접 확인이 필요합니다.",
                           positives, conflicts, "사용자 장르 규칙 간 충돌")
        if title_hits and len(set(description_hits)) >= 2 and score >= 75:
            return Verdict(FIT, score, "사용자 설정 장르 신호가 제목과 설명에서 함께 확인됨 (실제 음원 청취 확인 아님)", positives)
        return Verdict(REVIEW, score, "사용자 장르 설정만으로 음원 장르를 확정할 근거가 부족합니다.",
                       positives, uncertainty="메타데이터 근거 부족 또는 직접 청취 필요")
    score = min(100, 55 * bool(title_hits) + 15 * min(max(len(title_hits)-1, 0), 1)
                + 10 * min(len(description_hits), 2) + 8 * len(support_evidence))
    positives = tuple(dict.fromkeys(title_hits + description_hits + list(support_evidence)))
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
        return Verdict(FIT, score,
                       "메타데이터 장르 신호와 보강 근거: " + ", ".join((title_hits + list(support_evidence))[:4])
                       + " (실제 음원 청취 확인 아님)", positives)
    if title_hits or description_hits:
        return Verdict(REVIEW, score, "장르 신호는 있으나 제목·설명만으로 확정 불가",
                       positives, uncertainty="메타데이터 근거 부족")
    return Verdict(REVIEW, 0, "장르 근거 부족: 직접 감상 후 검토",
                   uncertainty="긍정 신호 없음")

def search_terms(genre_id: str, market: str, custom_genres=None, language: str | None = None) -> tuple[str, ...]:
    """Return locale-specific queries; locale controls search text, not identity."""
    locale = (language or market or "").upper()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", locale):
        raise ValueError("검색 언어 코드는 영문/숫자 2~12자여야 합니다.")
    genre = GENRES.get(genre_id) or custom_catalog(custom_genres).get(genre_id)
    if not genre:
        return ()
    if locale == "JP":
        return genre.jp_queries
    if locale == "KR":
        return genre.kr_queries
    if locale == "EN":
        return genre.en_queries
    localized = dict(genre.localized_queries)
    if locale not in localized:
        raise ValueError("해당 장르에 등록된 검색어 언어가 아닙니다.")
    return localized[locale]

def related_classification(core_id: str, related_ids, title: str, description: str = "", custom_genres=None) -> dict:
    """Keep core verdict independent; related signals are evidence, never promotion."""
    core = assess(core_id, title, description, custom_genres)
    custom = custom_catalog(custom_genres)
    catalog = {**GENRES, **custom}
    signals = {}
    for genre_id in related_ids or ():
        if genre_id not in catalog or genre_id == core_id:
            continue
        result = assess(genre_id, title, description, custom_genres)
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
