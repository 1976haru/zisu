"""Offline V4.6.0 acceptance matrix. Never a substitute for real Windows QA.

Runs focused isolated tests and writes a machine-readable result. In particular,
a Mock Chrome cycle is *not* evidence that a real browser opened correctly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
# Some Python installations have an unrelated third-party "tests" package.
# Bind the local test namespace explicitly to prevent testing the wrong code.
local_tests = types.ModuleType("tests")
local_tests.__path__ = [str(ROOT / "tests")]
sys.modules["tests"] = local_tests

GATES = {
    "scheduler_checkpoints": (
        "tests.test_research_scheduler.ResearchSchedulerTests.test_six_hour_schedule_switches_research_and_break_and_completes",
        "tests.test_research_scheduler.ResearchSchedulerTests.test_search_allowance_claim_is_atomic_break_aware_and_crash_safe",
        "tests.test_research_scheduler.ResearchSchedulerTests.test_resource_recovery_never_resumes_uncertain_or_crash_request",
    ),
    "account_channel_isolation": (
        "tests.test_oauth_management.OAuthManagementTests.test_google_account_and_channel_registry_supports_many_channels_and_accounts",
        "tests.test_oauth_management.OAuthManagementTests.test_account_switch_without_explicit_confirmation_preserves_token_and_research",
        "tests.test_oauth_management.OAuthManagementTests.test_active_profiled_session_blocks_account_channel_rebind",
    ),
    "browser_mock_lifecycle": (
        "tests.test_browser_verifier.BrowserVerifierTests.test_full_automatic_pipeline_mock_runs_and_saves_report",
        "tests.test_browser_verifier.BrowserVerifierTests.test_unverified_profile_ownership_stops_automatic_run_immediately",
        "tests.test_browser_verifier.BrowserVerifierTests.test_late_close_request_does_not_replace_completed_auto_report_with_cancelled_state",
    ),
    "resource_admission": (
        "tests.test_resource_lifecycle.ResourceLifecycleTests.test_resource_threshold_edges_and_cpu_sustain_boundary",
        "tests.test_resource_lifecycle.ResourceLifecycleTests.test_process_wide_search_slot_prevents_overlapping_search_adapters",
        "tests.test_resource_lifecycle.ResourceLifecycleTests.test_resource_monitor_survives_sampler_failure_and_repeated_start_stop",
    ),
    "candidate_quality_rules": (
        "tests.test_candidate_diversity.CandidateDiversityTests.test_page_diversification_is_deterministic_genre_first_and_channel_fair",
        "tests.test_candidate_diversity.CandidateDiversityTests.test_autopilot_deduplicates_previous_video_and_records_discovery_provenance_per_profile",
        "tests.test_candidate_diversity.CandidateDiversityTests.test_scheduled_autopilot_uses_relevance_then_bounded_recent_order",
    ),
    "network_and_quota_safety": (
        "tests.test_network_quota_safety.NetworkQuotaSafetyTests.test_uncertain_timeout_is_not_retried_and_reservation_survives",
        "tests.test_network_quota_safety.NetworkQuotaSafetyTests.test_shared_project_and_profile_budgets_are_separate_and_restart_safe",
        "tests.test_network_quota_safety.NetworkQuotaSafetyTests.test_scheduler_pauses_only_own_profile_and_requires_auth_recovery_confirmation",
    ),
    "analytics_mock_readonly": (
        "tests.test_analytics.AnalyticsTests.test_missing_scope_and_wrong_account_never_send_request",
        "tests.test_analytics.AnalyticsTests.test_shared_project_call_limit_applies_across_profiles",
        "tests.test_analytics.AnalyticsTests.test_empty_data_is_not_fabricated_as_zero_and_dashboard_stays_uncollected",
    ),
    "dashboard_interaction": (
        "tests.test_dashboard_tree_state.DashboardTreeStateTests.test_refresh_restores_expansion_selection_focus_and_scroll",
        "tests.test_dashboard_tree_state.DashboardTreeStateTests.test_deleted_selection_falls_back_to_active_profile_and_ignores_stale_open_nodes",
    ),
}

# CI cannot certify any of these by running mocks, headless geometry, or fake clock.
EXTERNAL_GATES = (
    "five_real_chrome_cycles_on_user_desktop",
    "visual_dpi_100_125_150_200_percent",
    "live_google_oauth_and_profile_binding",
    "live_youtube_discovery_human_label_precision",
    "actual_google_project_quota",
    "live_youtube_analytics_consent_and_data",
    "six_hour_real_windows_exe_resource_soak",
    "final_cross_version_consistency_security_and_stress",
)


def run_gate(methods: tuple[str, ...]) -> dict:
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for method in methods:
        loaded = loader.loadTestsFromName(method)
        if loaded.countTestCases() != 1:
            raise ValueError("Offline acceptance target must resolve to one test: " + method)
        suite.addTests(loaded)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    failures = [test.id() for test, _ in (*result.errors, *result.failures)]
    skipped = [test.id() for test, _ in result.skipped]
    passed = result.testsRun - len(failures) - len(skipped) - len(result.expectedFailures)
    return {
        "status": "PASS" if result.wasSuccessful() and not skipped and passed == len(methods) else "FAIL",
        "tests_run": result.testsRun,
        "passed": passed,
        "failures": failures,
        "skipped": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="dist/v46-offline-acceptance.json")
    args = parser.parse_args()
    outcomes = {name: run_gate(methods) for name, methods in GATES.items()}
    passed = all(info["status"] == "PASS" for info in outcomes.values())
    report = {
        "schema_version": 1,
        "project": "ZISU SIGNAL LAB",
        "scope": "isolated_mock_and_fake_clock_only",
        "commit": os.environ.get("GITHUB_SHA", "local_unidentified"),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "result": "PASS" if passed else "FAIL",
        "verified_offline_gates": outcomes,
        "external_validation_not_claimed": {item: "PENDING" for item in EXTERNAL_GATES},
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("V46 offline acceptance:", report["result"], "-",
          sum(x["tests_run"] for x in outcomes.values()), "tests")
    for name, item in outcomes.items():
        print(" ", name, item["status"], f'({item["passed"]}/{item["tests_run"]})')
    print("Real browser/OAuth/DPI/6-hour wall-clock and final QA are PENDING.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
