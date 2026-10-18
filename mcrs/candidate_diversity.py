"""Deterministic, genre-first ordering for research candidates only.

This module changes which result is reviewed first; it does not claim quality,
change a genre verdict, or perform viewer actions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import random

from . import genres, store

CORE = "core"
USER_CORE = "user_core"
CORE_RECENT = "core_recent"
RELATED = "related"


def classify_search_category(term: str, profile: dict, path=None) -> str:
    """Identify query provenance without treating Japanese text as genre evidence."""
    saved = set()
    try:
        saved = set(store.list_profile_search_terms(profile["id"], profile["market"], path))
    except (KeyError, OSError):
        pass
    if term in saved:
        return USER_CORE
    if term in genres.search_terms(profile["genre"], profile["market"]):
        return CORE
    related = set()
    try:
        for genre_id in store.normalize_related_genres(profile["genre"], profile.get("related_genres_json", "[]")):
            related.update(genres.search_terms(genre_id, profile["market"]))
    except (KeyError, ValueError):
        pass
    return RELATED if term in related else CORE


def _published_timestamp(value) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def diversify_page(rows, *, genre_id: str, exposure_counts=None, seed=""):
    """Deduplicate and fairly order one search page; returns (rows, page_duplicates).

    Core genre rule results remain the primary tier. Within each tier, the
    least-exposed channels are selected first, then a recent-upload tie break;
    a stable seed resolves exact ties for reproducible fixture/rerun behavior.
    """
    exposures = dict(exposure_counts or {})
    unique = []
    seen = set()
    duplicates = 0
    for row in rows or ():
        video_id = str(row.get("video_id", ""))
        if len(video_id) != 11 or not all(char.isalnum() or char in "-_" for char in video_id):
            continue
        if video_id in seen:
            duplicates += 1
            continue
        seen.add(video_id)
        unique.append(row)

    rng_seed = int.from_bytes(hashlib.sha256(str(seed).encode("utf-8")).digest()[:8], "big")
    rng = random.Random(rng_seed)
    tie = {str(row["video_id"]): rng.random() for row in unique}
    tiers = {genres.FIT: 2, genres.REVIEW: 1, genres.EXCLUDE: 0}
    verdict_tier = {str(row["video_id"]):tiers.get(
        genres.assess(genre_id,row.get("video_title",""),row.get("description","")).decision,0)
        for row in unique}
    local_channel_exposures = {}
    ordered = []
    remaining = list(unique)
    while remaining:
        # Strict genre tier first; candidates from a less suitable class never
        # jump ahead merely because their channel is new or lightly exposed.
        tier = max(verdict_tier[str(row["video_id"])] for row in remaining)
        pool = [row for row in remaining if verdict_tier[str(row["video_id"])] == tier]
        def rank(row):
            channel = str(row.get("channel_id", ""))
            prior = int(exposures.get(channel, 0)) if channel else 0
            within_page = local_channel_exposures.get(channel, 0) if channel else 0
            # Views are a weak channel-scale proxy only; keep it behind genre,
            # channel novelty and recency and never display it as quality.
            try:views=max(0,int(row.get("views",0) or 0))
            except (TypeError,ValueError,OverflowError):views=0
            scale_penalty = min(6, math.log10(views + 1))
            return (prior + within_page, -_published_timestamp(row.get("published_at")),
                    scale_penalty, tie[str(row["video_id"])])
        picked = min(pool, key=rank)
        ordered.append(picked)
        channel = str(picked.get("channel_id", ""))
        if channel:
            local_channel_exposures[channel] = local_channel_exposures.get(channel, 0) + 1
        remaining.remove(picked)
    return ordered, duplicates
