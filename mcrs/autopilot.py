"""One-click orchestration over the existing bounded research pipeline.

This module coordinates existing search, genre review, AI budget, cache and
session components. It never plays videos or posts comments.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import inspect
import sqlite3
import threading
from pathlib import Path
from typing import Callable

from . import ai_research, comments, store
from .youtube_api import APIError


class AutopilotBusy(RuntimeError):
    """An Autopilot worker is already running in this application instance."""


class FailClosedProvider:
    """Stop making provider requests after the first uncertain provider failure."""

    stop_after_failure = True

    def __init__(self, provider):
        self._provider = provider
        self.name = provider.name
        self.model = provider.model
        self.circuit_open = False

    def review(self, candidate, genre_id, related_genres=()):
        if self.circuit_open:
            raise ai_research.ProviderFailure("unknown")
        try:
            supports_related = "related_genres" in inspect.signature(self._provider.review).parameters
        except (TypeError, ValueError):
            supports_related = False
        try:
            if supports_related:
                return self._provider.review(candidate, genre_id, related_genres=related_genres)
            return self._provider.review(candidate, genre_id)
        except Exception:
            self.circuit_open = True
            raise


@dataclass(frozen=True)
class AutopilotConfig:
    profile_id: str
    target: int = 10
    max_search_calls: int = 3
    provider_id: str = "disabled"
    allow_paid_external: bool = False
    cost_estimate_usd: float | None = None
    cost_limit_usd: float | None = None
    session_id: str | None = None

    def validate(self):
        if self.target not in (5, 10, 20) or not 1 <= self.max_search_calls <= 12:
            raise ValueError("Autopilot 목표 또는 검색 한도가 허용 범위를 벗어났습니다.")
        if self.provider_id not in {"disabled", "ollama", "gemini", "openai"}:
            raise ValueError("선택한 AI 공급자를 사용할 수 없습니다.")
        if self.provider_id in {"gemini", "openai"}:
            if not self.allow_paid_external:
                raise PermissionError("유료 AI 요청은 작업 시작 시 명시 승인해야 합니다.")
            ai_research.validate_cost_budget(self.cost_estimate_usd, self.cost_limit_usd)


@dataclass(frozen=True)
class AutopilotResult:
    profile_id: str
    session_id: str
    state: str
    report: dict
    exceptions: tuple[dict, ...]
    run: ai_research.AutoResearchRun | None = None


def _exception(code: str, impact: str, action: str, retry: bool) -> dict:
    return {"type": code, "impact": impact, "recommended_action": action,
            "retry_allowed": bool(retry), "status": "pending"}


def _safe_error(exc: Exception) -> dict:
    if isinstance(exc, store.ActiveResearchSessionError):
        return _exception("active_job", "이 프로필의 조사가 이미 진행 중이어서 중복 실행을 막았습니다.",
                          "진행 중인 창에서 일시 정지하거나 완료된 뒤 다시 확인하세요.", False)
    if isinstance(exc, APIError):
        message = str(exc).casefold()
        if "할당량" in message or "quota" in message:
            return _exception("youtube_quota", "검색이 안전하게 중단됐습니다.",
                              "YouTube 프로젝트 할당량을 확인하고 갱신 후 새 한도를 승인하세요.", False)
        if "만료" in message or "취소" in message or "401" in message:
            return _exception("google_auth", "읽기 검색을 계속할 수 없습니다.",
                              "해당 프로필의 Google 읽기 전용 연결 상태를 확인하세요.", True)
        if "인터넷" in message or "시간" in message:
            return _exception("network", "현재 검색 페이지를 완료하지 못했습니다.",
                              "연결을 확인한 뒤 체크포인트에서 이어가세요.", True)
        return _exception("youtube_api", "읽기 검색 요청이 실패했습니다.",
                          "YouTube API 설정과 읽기 권한을 확인한 뒤 이어가세요.", True)
    if isinstance(exc, sqlite3.Error):
        return _exception("storage", "결과 저장에 실패했을 수 있습니다.",
                          "DB 파일을 보존하고 쓰기 권한·디스크 공간을 확인하세요. 중복 방지를 위해 상태부터 확인하세요.", False)
    if isinstance(exc, PermissionError):
        return _exception("approval_required", "승인 범위 밖의 AI 요청은 실행하지 않았습니다.",
                          "예산 상세 설정을 확인하고 명시적으로 승인하거나 규칙 전용 모드를 사용하세요.", False)
    if isinstance(exc, (ValueError, KeyError)):
        return _exception("settings_changed", "프로필 또는 저장된 설정이 일치하지 않습니다.",
                          "프로필 장르·시장·검색어 설정을 확인하고 필요하면 새 조사를 시작하세요.", False)
    if isinstance(exc, (TimeoutError, OSError)):
        return _exception("network_or_io", "요청 또는 저장 작업을 안전하게 중단했습니다.",
                          "네트워크와 저장 경로를 확인한 뒤 저장된 작업 상태를 확인하세요.", True)
    return _exception("unexpected", "조사를 안전하게 중단했습니다.",
                      "개인정보가 없는 진단 정보를 확인하고, 기존 세션을 확인한 뒤 이어가세요.", False)


class AutopilotController:
    """Runs one profile-scoped job while the UI remains responsive."""

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self._running = False
        self._profile_id = ""
        self._session_id = ""
        self._pause_event: threading.Event | None = None
        self._cancel_event: threading.Event | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def pause(self) -> bool:
        event = self._pause_event
        if not self.running or event is None:
            return False
        event.set()
        return True

    def cancel(self) -> bool:
        event = self._cancel_event
        if not self.running or event is None:
            return False
        event.set()
        return True

    def run(self, config: AutopilotConfig, collector: Callable, *, provider=None,
            on_event: Callable[[dict], None] | None = None) -> AutopilotResult:
        config.validate()
        profile = store.get_profile(config.profile_id, self.path)
        if not profile:
            raise ValueError("선택한 Autopilot 프로필을 찾을 수 없습니다.")
        with self._lock:
            if self._running:
                raise AutopilotBusy("이미 다른 Autopilot 작업이 실행 중입니다.")
            self._running = True
            self._profile_id = config.profile_id
            self._session_id = config.session_id or ""
            self._pause_event = threading.Event()
            self._cancel_event = threading.Event()
            pause_event, cancel_event = self._pause_event, self._cancel_event

        def emit(stage, **values):
            if on_event:
                on_event({"stage": stage, "profile_id": config.profile_id, **values})

        actual_queries: list[dict] = []
        session_id = config.session_id or ""
        try:
            if config.session_id:
                saved = store.get_autopilot_settings(config.profile_id, config.session_id, self.path)
                if not saved:
                    raise ValueError("이 세션에는 Autopilot 예산 승인 기록이 없어 안전하게 재개할 수 없습니다.")
                expected = self._settings(config, provider)
                if saved != expected:
                    raise ValueError("저장된 Autopilot 프로필·모델·예산 범위가 변경되어 재개할 수 없습니다.")
            if provider is not None and not isinstance(provider, FailClosedProvider):
                provider = FailClosedProvider(provider)
            emit("validate", message="프로필, 연결, 승인된 예산 확인")

            def collect(term, market, limit, page_token=None):
                actual_queries.append({"keyword": term, "market": market,
                                       "page": len(actual_queries) + 1,
                                       "continued_page": bool(page_token)})
                emit("search", keyword=term, market=market,
                     search_call=len(actual_queries), search_limit=config.max_search_calls)
                try:
                    parameters = inspect.signature(collector).parameters
                    accepts_page = "page_token" in parameters or len(parameters) >= 4
                except (TypeError, ValueError):
                    accepts_page = True
                return collector(term, market, limit, page_token) if accepts_page else collector(term, market, limit)

            def session_ready(new_session_id, resumed):
                nonlocal session_id
                session_id = new_session_id
                store.save_autopilot_settings(config.profile_id, session_id,
                                              self._settings(config, provider), self.path)
                with self._lock:
                    self._session_id = session_id
                emit("resume" if resumed else "session", session_id=session_id,
                     message="저장된 체크포인트에서 이어갑니다." if resumed else "체크포인트 생성 완료")

            emit("search", message="읽기 전용 영상 검색 및 규칙 판정 시작")
            run = ai_research.run_auto_recommendations(
                config.profile_id, collect, target=config.target,
                max_search_calls=config.max_search_calls, provider=provider,
                path=self.path, cancel_event=cancel_event, pause_event=pause_event,
                progress=lambda item: emit(
                    "progress",
                    engine_stage=item.get("stage"),
                    **{key: value for key, value in item.items() if key != "stage"},
                ), session_id=config.session_id,
                allow_paid_external=config.allow_paid_external,
                cost_estimate_usd=config.cost_estimate_usd,
                cost_limit_usd=config.cost_limit_usd, session_ready=session_ready)
            emit("drafts", message="사람의 실제 감상 메모가 있는 추천 후보에만 초안 준비")
            report, exceptions = self._build_report(config, profile, run, actual_queries)
            store.save_autopilot_report(config.profile_id, run.session_id, report, exceptions, self.path)
            emit("report", report=report, exceptions=list(exceptions), session_id=run.session_id)
            emit("complete", state=run.state, report=report, exceptions=list(exceptions),
                 session_id=run.session_id)
            return AutopilotResult(config.profile_id, run.session_id, run.state, report,
                                   tuple(exceptions), run)
        except Exception as exc:
            item = _safe_error(exc)
            exceptions = [item]
            if session_id:
                try:
                    store.checkpoint_research_session(session_id, config.profile_id,
                        state="paused", in_flight=False, path=self.path)
                except Exception:
                    pass
            report = {"schema": 1, "profile_id": config.profile_id,
                      "profile_name": profile["name"], "state": "paused",
                      "generated_at": datetime.now(timezone.utc).isoformat(),
                      "error_type": item["type"], "exception_class": type(exc).__name__,
                      "search_calls": len(actual_queries),
                      "queries": actual_queries, "message": item["impact"]}
            if session_id:
                try:
                    store.save_autopilot_report(config.profile_id, session_id, report, exceptions, self.path)
                except Exception:
                    pass
            emit("exception", report=report, exceptions=exceptions,
                 session_id=session_id, state="paused")
            return AutopilotResult(config.profile_id, session_id, "paused", report,
                                   tuple(exceptions), None)
        finally:
            with self._lock:
                self._running = False
                self._profile_id = ""
                self._pause_event = None
                self._cancel_event = None

    @staticmethod
    def _settings(config, provider):
        return {"provider_id": config.provider_id,
                "model": getattr(provider, "model", "none") if provider else "none",
                "target": config.target,
                "max_search_calls": config.max_search_calls,
                "cost_estimate_usd": config.cost_estimate_usd,
                "cost_limit_usd": config.cost_limit_usd,
                "paid_external_approved": bool(config.allow_paid_external),
                "report_version": 1}

    def _build_report(self, config, profile, run, queries):
        related = store.normalize_related_genres(profile["genre"], profile.get("related_genres_json", "[]"))
        candidate_by_id = {str(row.get("video_id")): row for row in run.candidates}
        proposals = {}
        evidence = []
        for review in run.reviews:
            candidate = candidate_by_id.get(review.video_id, {})
            verdict = ai_research.proposed_verdict(profile, review, candidate, related)
            proposals[review.video_id] = verdict
            if review.rule_reason or (review.ai_opinion and review.ai_opinion.evidence_from_metadata):
                evidence.append({"video_id": review.video_id,
                                 "title": candidate.get("video_title", review.video_id),
                                 "proposal": verdict,
                                 "rule_reason": review.rule_reason,
                                 "ai_evidence": list(review.ai_opinion.evidence_from_metadata) if review.ai_opinion else [],
                                 "uncertainty": review.ai_opinion.uncertainty if review.ai_opinion else review.warning})

        prepared = 0
        pending_notes = 0
        for video_id, verdict in proposals.items():
            if verdict != "auto_fit":
                continue
            saved = store.get_candidate(config.profile_id, video_id, self.path)
            observation = (saved or {}).get("notes", "")
            if len(observation.strip()) < 10:
                pending_notes += 1
                continue
            drafts = comments.suggest_drafts(profile, (saved or {}).get("video_title", ""),
                                              profile["genre"], observation, 3)
            store.save_autopilot_comment_drafts(config.profile_id, video_id, run.session_id,
                                                drafts, self.path)
            prepared += 1

        audit = {row["video_id"]: row for row in store.list_genre_audit(config.profile_id, labeled=True, path=self.path)}
        session_ids = set(candidate_by_id)
        human_count = sum(1 for video_id in session_ids if video_id in audit and audit[video_id].get("human_label"))
        counts = {
            "ai_recommended_fit": sum(v == "auto_fit" for v in proposals.values()),
            "review_needed": sum(v in {"cross_genre_review", "insufficient_evidence"} for v in proposals.values()),
            "excluded": sum(v == "exclude" for v in proposals.values()),
            "related_reference": sum(v == "related_reference" for v in proposals.values()),
            "human_verified": human_count,
        }
        channels = sorted({str(row.get("channel_title", "")).strip()
                           for video_id, row in candidate_by_id.items()
                           if proposals.get(video_id) == "auto_fit" and str(row.get("channel_title", "")).strip()})
        exceptions = []
        if run.state == "paused" and "검색 요청이 실패했습니다" in run.warning:
            exceptions.append(_exception("search_failed", "검색 요청 실패 후 체크포인트에서 일시 정지했습니다.",
                                         "연결 또는 할당량을 확인한 뒤 저장된 작업을 이어가세요.", True))
        elif "중단 시 응답 여부가 불확실한 검색 페이지" in run.warning:
            exceptions.append({"type": "inflight_page_skipped",
                               "impact": "중단 당시 불확실했던 페이지를 재호출하지 않고 건너뛰었습니다.",
                               "recommended_action": "현재 결과는 중복 요청 없이 이어졌습니다.",
                               "retry_allowed": False, "status": "handled"})
        if run.shortfall:
            exceptions.append(_exception("goal_not_met", f"목표보다 적합 후보가 {run.shortfall}개 부족합니다.",
                                         "검색 한도 안에서 후보만 더 조사하거나, 새 승인 범위를 설정하세요. 부적합 후보는 승격하지 않았습니다.", False))
        if any(review.ai_opinion and review.ai_opinion.uncertainty.startswith("AI 검토 실패")
               for review in run.reviews):
            exceptions.append(_exception("ai_provider_failure", "첫 AI 오류 후 추가 공급자 요청을 차단했습니다.",
                                         "키·모델·연결 상태를 확인하세요. 이미 진행된 규칙 판정은 유지됩니다.", False))
        if counts["review_needed"] or pending_notes:
            exceptions.append(_exception("human_review", f"판정 검토 {counts['review_needed']}건, 감상 메모 대기 {pending_notes}건입니다.",
                                         "후보를 직접 열어 실제로 감상한 뒤 사람 라벨과 메모를 남기세요.", False))
        if config.provider_id in {"gemini", "openai", "ollama"}:
            ai_usage = store.ai_usage_for_session(config.profile_id, run.session_id, self.path)
        else:
            ai_usage = {"requests": 0, "estimated_cost_usd": 0.0}
        report = {
            "schema": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": config.profile_id, "profile_name": profile["name"],
            "market": profile["market"], "core_genre": profile["genre"], "related_genres": related,
            "session_id": run.session_id, "state": run.state,
            "search_keywords": list(dict.fromkeys(item["keyword"] for item in queries)),
            "query_pages": queries, "candidate_total": len(run.candidates),
            "status_note": run.warning,
            "duplicate_removed": run.duplicate_count, "counts": counts,
            "recommended_channels": channels[:20], "evidence": evidence[:30],
            "target_fits": run.target_fits, "target_met": run.shortfall == 0,
            "shortfall": run.shortfall, "search_calls_used": run.calls_used,
            "search_calls_remaining": max(0, config.max_search_calls - run.calls_used),
            "search_calls_approved": config.max_search_calls,
            "ai_reviewed_candidates": run.ai_review_count,
            "ai_requests_used": int(ai_usage["requests"]),
            "ai_request_limit": 10,
            "ai_requests_remaining": max(0, 10 - int(ai_usage["requests"])),
            "ai_estimated_cost_usd": ai_usage.get("estimated_cost_usd"),
            "ai_cost_limit_usd": config.cost_limit_usd,
            "paid_external_approved": bool(config.allow_paid_external),
            "comment_draft_videos_prepared": prepared,
            "comment_drafts_per_video": 3,
            "comment_draft_waiting_for_real_listening_note": pending_notes,
            "human_approval_required": True,
            "actual_audio_listening_claimed": False,
            "exceptions": exceptions,
        }
        return report, exceptions


def export_report(report: dict, target: str | Path) -> None:
    Path(target).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
