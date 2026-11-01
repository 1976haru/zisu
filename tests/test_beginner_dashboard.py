import json
import os
import unittest
from types import SimpleNamespace
from datetime import timedelta, timezone
from unittest.mock import patch

from mcrs import research_dashboard_presenter, store
from mcrs.gui import Studio
from tests.support import WorkspaceTemp


class BeginnerDashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp();self.root=self.temp.__enter__()
        self.previous=os.environ.get("ZISU_DATA_DIR")
        os.environ["ZISU_DATA_DIR"]=str(self.root/"isolated-data")
        self.jp=store.create_profile("JP morning", "JP", "MORNING_CHILL_RAP")
        self.jp2=store.create_profile("JP second", "JP", "CHILL_RAP")
        self.kr=store.create_profile("KR lounge", "KR", "JAZZ_LOUNGE")
        store.bind_google_account_channel(self.jp,"UCdashboard1","Morning JP","same@example.test")
        store.bind_google_account_channel(self.jp2,"UCdashboard2","Second JP","SAME@example.test")
        store.bind_google_account_channel(self.kr,"UCdashboard3","KR Lounge","other@example.test")

    def tearDown(self):
        if self.previous is None:os.environ.pop("ZISU_DATA_DIR",None)
        else:os.environ["ZISU_DATA_DIR"]=self.previous
        self.temp.__exit__(None,None,None)

    def test_dashboard_groups_multiple_channels_by_account_without_credentials(self):
        accounts=store.list_google_accounts()
        self.assertEqual(2,len(accounts))
        same=next(row for row in accounts if row["email"].casefold()=="same@example.test")
        self.assertEqual({"UCdashboard1","UCdashboard2"},{row["channel_id"] for row in same["channels"]})
        self.assertEqual({self.jp,self.jp2},{row["id"] for row in same["profiles"]})
        self.assertNotIn("token",json.dumps(accounts).casefold())

    def test_dashboard_summary_is_scoped_to_profile_and_counts_only(self):
        row={"video_id":"dashboard01","video_title":"Morning chill rap with mellow vocals"}
        other={"video_id":"dashboard01","video_title":"Jazz lounge smooth trumpet jazz"}
        store.save_candidates(self.jp,[row]);store.save_candidates(self.kr,[other])
        store.update_candidate(self.jp,"dashboard01","검토 중","JP memo","JP draft")
        first=store.dashboard_summary(self.jp)
        second=store.dashboard_summary(self.kr)
        self.assertEqual(1,first["candidates"])
        self.assertEqual(1,second["candidates"])
        self.assertEqual("same@example.test",first["account_email"].casefold())
        self.assertEqual("other@example.test",second["account_email"])
        self.assertEqual("JP memo",store.get_candidate(self.jp,"dashboard01")["notes"])
        self.assertEqual("",store.get_candidate(self.kr,"dashboard01")["notes"])

    def test_dashboard_summary_missing_profile_is_empty(self):
        self.assertIsNone(store.dashboard_summary("not-a-profile"))

    def test_beginner_status_translates_technical_state_without_relaxing_controls(self):
        model={"state":"BLOCKED","next_action":"기존 연구 일정을 확인하세요.",
               "error_code":"ScheduleBusy","can_start":False}
        text=research_dashboard_presenter.beginner_status(model)
        self.assertEqual("현재 연구를 시작할 수 없습니다",text["headline"])
        self.assertEqual("BLOCKED",text["technical_state"])
        self.assertEqual("ScheduleBusy",text["technical_error"])
        self.assertFalse(model["can_start"])

    def test_result_status_does_not_label_blocked_work_as_running(self):
        self.assertIn("시작 차단",research_dashboard_presenter.result_status_label("BLOCKED",True))
        self.assertIn("진행 중",research_dashboard_presenter.result_status_label("RUNNING",True))
        self.assertIn("완료",research_dashboard_presenter.result_status_label("COMPLETE"))

    def test_elapsed_format_keeps_subminute_seconds_visible(self):
        self.assertEqual("8초",research_dashboard_presenter.format_elapsed(8))
        self.assertEqual("00:01:00",research_dashboard_presenter.format_elapsed(60))
        self.assertEqual("01:00:08",research_dashboard_presenter.format_elapsed(3608))

    def test_schedule_preview_is_read_only_and_visible_in_preview_variables(self):
        class Var:
            def __init__(self,value):self.value=value
            def get(self):return self.value
            def set(self,value):self.value=value
        app=SimpleNamespace(
            schedule_hours=Var("2"),schedule_research_minutes=Var("20"),schedule_break_minutes=Var("10"),
            schedule_start_time=Var(""),schedule_mode=Var("fixed"),schedule_research_min=Var("20"),
            schedule_research_max=Var("30"),schedule_break_min=Var("10"),schedule_break_max=Var("15"),
            ai_max_calls=Var("3"),schedule_preview_text=Var(""),schedule_progress_text=Var(""),
            current_profile=lambda:{"id":"fixture-profile"})
        with patch("mcrs.gui.store.initialize",side_effect=AssertionError("preview initialized storage")), \
             patch("mcrs.gui.store.connect",side_effect=AssertionError("preview opened DB")), \
             patch("mcrs.gui.research_scheduler.create",side_effect=AssertionError("preview created schedule")):
            Studio._autopilot_schedule_preview(app)
        self.assertIn("저장 전 예상 일정",app.schedule_preview_text.get())
        self.assertIn("아직 저장·예약되지 않았습니다",app.schedule_preview_text.get())
        self.assertIn("전체 2시간",app.schedule_preview_text.get())
        self.assertIn("검색 호출 상한 3회",app.schedule_preview_text.get())
        self.assertEqual(app.schedule_preview_text.get(),app.schedule_progress_text.get())

    def test_schedule_preview_input_error_is_explained_in_current_screen(self):
        class Var:
            def __init__(self,value):self.value=value
            def get(self):return self.value
            def set(self,value):self.value=value
        app=SimpleNamespace(
            schedule_hours=Var("25"),schedule_research_minutes=Var("20"),schedule_break_minutes=Var("10"),
            schedule_start_time=Var(""),schedule_mode=Var("fixed"),schedule_research_min=Var("20"),
            schedule_research_max=Var("20"),schedule_break_min=Var("10"),schedule_break_max=Var("10"),
            ai_max_calls=Var("3"),schedule_preview_text=Var(""),schedule_progress_text=Var(""),
            current_profile=lambda:{"id":"fixture-profile"})
        Studio._autopilot_schedule_preview(app)
        self.assertIn("표시할 수 없습니다",app.schedule_preview_text.get())
        self.assertIn("수정 방법",app.schedule_preview_text.get())

    def test_local_time_label_keeps_explicit_timezone_conversion(self):
        kst=timezone(timedelta(hours=9),name="KST")
        label=research_dashboard_presenter.local_time_label(
            "2026-11-01T00:30:00+00:00",local_timezone=kst)
        self.assertEqual("2026-11-01 09:30 KST",label)
        with self.assertRaises(ValueError):
            research_dashboard_presenter.local_time_label("2026-11-01T00:30:00")


if __name__=="__main__":
    unittest.main()
