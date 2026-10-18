"""Run with xvfb-run -a python tests/smoke_gui.py on Linux CI."""
import os, sys, threading, time
from unittest.mock import patch
sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__),'..')))
from support import WorkspaceTemp
from mcrs import store
from mcrs.gui import Studio, ProfileDialog
with WorkspaceTemp() as temp:
    os.environ['ZISU_DATA_DIR']=str(temp)
    first = store.create_profile("GUI 스모크 조사", "JP", "MORNING_CHILL_RAP")
    second = store.create_profile("GUI 전환 한국", "KR", "CHILL_RAP")
    third = store.create_profile("GUI 같은 계정 두 번째 채널", "JP", "RNB_HIPHOP")
    store.bind_google_account_channel(first,"UCgui001","JP main","shared@example.test")
    store.bind_google_account_channel(third,"UCgui002","JP second","SHARED@example.test")
    store.bind_google_account_channel(second,"UCgui003","KR channel","kr@example.test")
    app=Studio()
    app.update_idletasks()
    assert len(app.profiles)>=1
    assert app.active_id
    assert len(app.frames)==8
    assert len(app.tabs.tabs())==9
    assert app.tabs.tab(app.tabs.tabs()[0],"text")=="쉬운 시작"
    assert hasattr(app,"dashboard_channels")
    assert hasattr(app,"dashboard_selected_text")
    assert callable(app._dashboard_refresh)
    assert callable(app._dashboard_start_research)
    assert {str(value) for value in app.dashboard_channels.cget("show")}=={"tree","headings"}
    assert len([row for row in store.list_google_accounts() if row["email"].casefold()=="shared@example.test"])==1
    assert app.dashboard_channels.exists("account:"+store.get_profile(first)["google_account_id"])
    assert callable(app.open_google_wizard)
    assert callable(app._offer_recovery)
    assert hasattr(app,"autopilot_start_button")
    assert hasattr(app,"autopilot_exception_tree")
    assert hasattr(app,"autopilot_controller")
    assert app.autopilot_start_button.cget("text")=="연구 시작"
    assert app.schedule_hours.get()=="6"
    assert app.schedule_research_minutes.get()=="30"
    assert app.schedule_break_minutes.get()=="30"
    assert app.resource_low_load_var.get() is True
    assert callable(app._autopilot_schedule_worker)
    assert callable(app._autopilot_schedule_preview)
    assert len(app.ai_tree.get_children())==0
    assert hasattr(app,"ai_credential_entry") and hasattr(app,"ai_pause_button")
    assert hasattr(app,"ai_gemini_test_button")
    assert hasattr(app,"ai_gemini_models_button")
    assert hasattr(app,"ai_gemini_model_combo")
    assert hasattr(app,"oauth_client_combo")
    assert hasattr(app,"oauth_accounts_summary")
    assert callable(app.register_oauth_client)
    assert callable(app._save_youtube_key)
    assert "readonly" in app.oauth_client_combo.state()
    with patch.object(ProfileDialog,"wait_window",lambda self: None):
        dialog=ProfileDialog(app)
        assert {key for key,var in dialog.related_vars.items() if var.get()} == {"CHILL_RAP","RNB_HIPHOP","CAFE_CHILL"}
        dialog.genre_display.set("Chill Rap")
        dialog._core_genre_changed()
        assert not dialog.related_vars["CHILL_RAP"].get()
        assert "Chill Rap" not in dialog.related_summary.get()
        dialog.destroy()
    assert app.ai_provider_choice.get()=="규칙만"
    app.profile_combo.current([profile["id"] for profile in app.profiles].index(first))
    app.select_profile()
    app.update_idletasks()
    assert app.dashboard_selected_text.get().startswith("현재 선택: GUI 스모크 조사")
    assert "shared@example.test" in app.dashboard_selected_text.get().casefold()
    app.dashboard_channels.selection_set("profile:"+third)
    app._dashboard_open_profile()
    assert app.active_id==third
    app.autopilot_schedule_running=True
    app.dashboard_channels.selection_set("profile:"+second)
    with patch("mcrs.gui.messagebox.showinfo"):
        app._dashboard_open_profile()
    assert app.active_id==third
    app.autopilot_schedule_running=False
    app.dashboard_channels.selection_set("profile:"+first)
    app._dashboard_open_profile()
    assert app.active_id==first
    # Mechanically exercise Tk scaling and scrollable layout; this is not a visual screenshot check.
    for scaling in (1.0,1.25,1.5,2.0):
        app.tk.call("tk","scaling",scaling)
        app.update_idletasks()
        assert app._easy_canvas.winfo_width()>0 and app._easy_canvas.winfo_height()>0
        assert len(app._easy_canvas.bbox("all"))==4
    easy_tab=app.tabs.tabs()[0]
    assert app.bind_all("<Escape>")
    assert app.bind_all("<Return>")
    app._ai_preview_plan()
    assert app.ai_confirmed_plan["profile_id"] == first
    with store.connect() as database:
        ai_rows_before = database.execute("SELECT COUNT(*) FROM ai_reviews").fetchone()[0]
    with patch("mcrs.gui.messagebox.askyesno", return_value=True):
        app._ai_start_demo()
    demo_deadline = time.monotonic() + 3
    while not app.ai_tree.get_children() and time.monotonic() < demo_deadline:
        app.update()
        time.sleep(0.02)
    assert len(app.ai_tree.get_children()) == 3
    with store.connect() as database:
        assert database.execute("SELECT COUNT(*) FROM ai_reviews").fetchone()[0] == ai_rows_before
    app.api_key.set("offline-mock-key")
    fake_search_row={"video_id":"aBcDeF12345","video_title":"朝のプレイリスト","description":"落ち着く音楽",
                     "video_url":"https://www.youtube.com/watch?v=aBcDeF12345"}
    app._ai_preview_plan()
    with patch("mcrs.gui.messagebox.askyesno", return_value=True), patch("mcrs.gui.lookup_videos", return_value=[fake_search_row]):
        app._ai_start_live()
        live_deadline=time.monotonic()+5
        while app.ai_running and time.monotonic()<live_deadline:
            app.update()
            time.sleep(0.02)
    assert not app.ai_running
    assert store.get_candidate(first,"aBcDeF12345") is not None
    assert len(app.ai_tree.get_children()) == 1
    assert "미연결" in app.oauth_status.get()
    second_index = [profile["id"] for profile in app.profiles].index(second)
    app.profile_combo.current(second_index)
    app.select_profile()
    assert app.active_id == second and app.active_id != first
    assert "KR" in app.active_account_banner.get() and "Chill Rap" in app.active_account_banner.get()
    app.tabs.select(app.frames[1])
    app.update()
    app.tabs.select(app.frames[5])
    app.update()
    entered = threading.Event()
    release = threading.Event()
    def mock_search(*_args, **_kwargs):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("GUI smoke search release timed out")
        return [{"video_id":"aBcDeF12345","video_title":"朝 morning chill rap"}]
    app.profile_combo.current([profile["id"] for profile in app.profiles].index(first))
    app.select_profile()
    app.api_key.set("offline-mock-key")
    result = []
    deadline = time.monotonic() + 5
    def switch_while_searching():
        if not entered.is_set():
            app.after(20, switch_while_searching)
            return
        app.profile_combo.current(second_index)
        app.select_profile()
        release.set()
        app.after(20, check_search_completion)
    def check_search_completion():
        if app.search_inflight and time.monotonic() < deadline:
            app.after(20, check_search_completion)
            return
        result.append(not app.search_inflight)
        result.append(store.get_candidate(first, "aBcDeF12345") is not None)
        result.append(store.get_candidate(second, "aBcDeF12345") is None)
        result.append(app.active_id == second)
        app.destroy()
    with patch("mcrs.gui.lookup_videos", side_effect=mock_search):
        app.api_search()
        app.after(20, switch_while_searching)
        app.mainloop()
    assert result == [True, True, True, True], f"profile switch search isolation failed: {result}"
    print('GUI_SMOKE_PASS')
