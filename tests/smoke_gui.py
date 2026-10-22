"""Run with xvfb-run -a python tests/smoke_gui.py on Linux CI."""
import os, sys, threading, time, tkinter as tk
from tkinter import ttk
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__),'..')))
from support import WorkspaceTemp
from mcrs import store, vph
from mcrs.genres import REVIEW
from mcrs.gui import Studio, ProfileDialog
with WorkspaceTemp() as temp:
    os.environ['ZISU_DATA_DIR']=str(temp)
    first = store.create_profile("GUI 스모크 조사", "JP", "MORNING_CHILL_RAP")
    second = store.create_profile("GUI 전환 한국", "KR", "CHILL_RAP")
    third = store.create_profile("GUI 같은 계정 두 번째 채널", "JP", "RNB_HIPHOP")
    store.bind_google_account_channel(first,"UCgui001","JP main","shared@example.test")
    store.bind_google_account_channel(third,"UCgui002","JP second","SHARED@example.test")
    store.bind_google_account_channel(second,"UCgui003","KR channel","kr@example.test")
    start_research=[]
    with patch.object(Studio,"start_vph_scheduled_research",lambda self:start_research.append(self)):
        app=Studio()
        app.vph_start_button.configure(state="normal")
        app.vph_start_button.invoke()
        assert start_research==[app]
    app.update_idletasks()
    assert len(app.profiles)>=1
    assert app.active_id
    assert len(app.frames)==9
    assert len(app.tabs.tabs())==10
    assert app.tabs.tab(app.tabs.tabs()[0],"text")=="쉬운 시작"
    assert hasattr(app,"dashboard_channels")
    assert hasattr(app,"vph_tree") and hasattr(app,"vph_profile_combo")
    assert app.vph_start_button.cget("text")=="자동 연구 시작"
    assert app.vph_start_button.master is app.vph_buttons
    assert app.vph_buttons.grid_info().get("row")==2
    assert app.vph_tree.master.grid_info().get("row")==5
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    first_video="vphGUI12345"
    second_video="vphGUI54321"
    missing_video="vphGUI00000"
    store.save_candidates(first,[{"video_id":first_video,"video_title":"Morning Chill Rap instrumental","channel_title":"JP Radar","published_at":"2025-12-01T00:00:00Z","views":1200}],)
    store.save_candidates(second,[{"video_id":second_video,"video_title":"Chill Rap study","channel_title":"KR Radar","views":50},
                                  {"video_id":missing_video,"video_title":"Chill Rap unknown","channel_title":"KR Radar"}],)
    with store.connect() as database:
        database.execute("UPDATE candidates SET genre_decision=? WHERE profile_id=? AND video_id=?",(REVIEW,first,first_video))
    vph.record_observation(first,first_video,1000,start,video_format="longform",duration_seconds=240,path=None)
    vph.record_observation(first,first_video,1600,start+timedelta(hours=1),video_format="longform",duration_seconds=240,path=None)
    vph.record_observation(first,first_video,1700,start+timedelta(hours=2),video_format="longform",duration_seconds=240,path=None)
    vph.record_observation(second,second_video,50,start,path=None)
    vph.record_observation(second,second_video,40,start+timedelta(hours=1),path=None)
    vph.record_observation(second,missing_video,None,start,sample_status="missing",uncertainty_reason="view_count_missing",path=None)
    app.reload_profiles(first)
    app.tabs.select(app.frames[8]);app.update_idletasks()
    app._vph_research_error_profile=first
    app._vph_research_error_text="Timeout · checkpoint kept"
    app._refresh_vph_research_status(None)
    assert "마지막 오류: Timeout · checkpoint kept" in app.vph_status.get()
    app._vph_research_error_profile=None;app._vph_research_error_text=""
    assert len(app.vph_tree.get_children())==1
    current=store.get_profile(first)
    assert app.vph_tree.item(app.vph_tree.get_children()[0],"values")[0]=="JP"
    assert "REVIEW" in app.vph_tree.item(app.vph_tree.get_children()[0],"values")[2]
    assert app.vph_tree.item(app.vph_tree.get_children()[0],"values")[9]=="100.0"
    app.vph_tier_a_var.set("150");app.vph_tier_b_var.set("75");app.save_vph_thresholds()
    assert (store.get_profile(first)["vph_tier_a"],store.get_profile(first)["vph_tier_b"])==(150,75)
    assert len(vph.list_history(first,first_video))==3
    app.vph_tree.selection_set(app.vph_tree.get_children()[0])
    with patch("mcrs.gui.vph.list_radar",wraps=vph.list_radar) as radar_query:
        app.open_vph_history()
        assert radar_query.call_count==0
    history_windows=[child for child in app.winfo_children() if isinstance(child,tk.Toplevel) and "표본 이력" in child.title()]
    assert len(history_windows)==1
    history_tree=next(widget for widget in history_windows[0].winfo_children() if isinstance(widget,ttk.Treeview))
    assert len(history_tree.get_children())==3
    history_windows[0].destroy()
    selected_eval=app.vph_tree.selection()[0]
    app.reload_vph_radar()
    assert app.vph_tree.selection()==(selected_eval,)
    with patch("mcrs.gui.vph.list_radar",wraps=vph.list_radar) as radar_query:
        app.open_vph_manual_review()
        assert radar_query.call_count==0
    assert app.tabs.select()==str(app.frames[2])
    assert app.selected_video==first_video and app.candidates.selection()==(first_video,)
    app.tabs.select(app.frames[8])
    app.autopilot_schedule_running=True
    app.vph_profile_combo.current([p["id"] for p in app.vph_profiles].index(second))
    with patch("mcrs.gui.messagebox.showinfo"):
        app._vph_profile_selected()
    assert app.active_id==first and app.vph_profile_combo.current()==[p["id"] for p in app.vph_profiles].index(first)
    app.autopilot_schedule_running=False
    with patch("mcrs.gui.vph.list_radar",return_value=[]):
        app.reload_vph_radar()
        assert len(app.vph_tree.get_children())==0 and "0건" in app.vph_status.get()
    stress_rows=[{"id":index+1000,"video_id":f"x{index:010d}","state":"VALID","tier":"A",
        "view_count":1000,"vph":100.0,"video_format":"longform","published_at":"2026-01-01",
        "duration_seconds":600,"video_title":f"Stress video {index}","channel_title":"Mock channel"}
        for index in range(1001)]
    with patch("mcrs.gui.vph.list_radar",return_value=stress_rows):
        render_started=time.perf_counter()
        app.reload_vph_radar()
        render_seconds=time.perf_counter()-render_started
        assert len(app.vph_tree.get_children())==1001
        assert render_seconds<5.0, render_seconds
        print(f"VPH_GUI_1001_RENDER_SECONDS {render_seconds:.3f}")
        keep_iid=app.vph_tree.get_children()[800]
        app.vph_tree.selection_set(keep_iid);app.vph_tree.yview_moveto(0.45);app.vph_tree.xview_moveto(0.25)
        old_y,old_x=app.vph_tree.yview()[0],app.vph_tree.xview()[0]
        app.reload_vph_radar()
        assert app.vph_tree.selection()==(keep_iid,)
        assert abs(app.vph_tree.yview()[0]-old_y)<0.02 and abs(app.vph_tree.xview()[0]-old_x)<0.02
    app.vph_profile_combo.current([p["id"] for p in app.vph_profiles].index(second));app._vph_profile_selected()
    assert app.active_id==second and len(app.vph_tree.get_children())==2
    second_rows=[app.vph_tree.item(iid,"values") for iid in app.vph_tree.get_children()]
    assert all(row[0]=="KR" for row in second_rows)
    assert {row[10] for row in second_rows}=={"REVIEW","UNKNOWN"}
    assert any("조회수 항목 없음" in row[11] for row in second_rows)
    app.vph_profile_combo.current([p["id"] for p in app.vph_profiles].index(first));app._vph_profile_selected()
    assert app.active_id==first and app.vph_tier_a_var.get()=="150.0"
    assert hasattr(app,"dashboard_selected_text")
    assert callable(app._dashboard_refresh)
    assert callable(app._dashboard_start_research)
    app.tabs.select(app.tabs.tabs()[0])
    with patch.object(app,"_autopilot_start") as scheduled_start:
        app._dashboard_start_research()
        assert scheduled_start.call_count==1
        request_id=scheduled_start.call_args.kwargs["auto_request_id"]
        assert app.tabs.select()==str(app.tabs.tabs()[0])
        intent=store.get_vph_auto_start_intent(request_id)
        assert intent["status"]=="claimed" and intent["profile_id"]==app.active_id
        assert scheduled_start.call_args.kwargs["vph_one_click"] is True
        assert app.tabs.select()==str(app.tabs.tabs()[0])
    store.update_vph_auto_start_intent(request_id,"cancelled")
    with patch.object(app,"_autopilot_start") as scheduled_start:
        app.start_vph_scheduled_research()
        assert scheduled_start.call_count==1
        assert scheduled_start.call_args.kwargs["vph_one_click"] is True
        assert scheduled_start.call_args.kwargs["auto_request_id"].startswith("gui_")
        assert scheduled_start.call_args.kwargs["auto_settings"]["paid_ai"] is False
        assert app.tabs.select()==str(app.frames[7])
    store.update_vph_auto_start_intent(scheduled_start.call_args.kwargs["auto_request_id"],"cancelled")
    assert hasattr(app,"research_dashboard_frame")
    assert app.research_dashboard_frame.master is app.tabs.nametowidget(app.tabs.tabs()[0])
    assert int(app.research_dashboard_frame.grid_info()["row"])==0
    assert len(app.research_stage_cards)==9
    assert "최종 연구 완료" not in app.research_dashboard_results.get()
    assert "결과 측정 전" in app.research_dashboard_results.get()
    assert app.dashboard_start_button.cget("text")=="연구 시작"
    assert app.dashboard_pause_button.cget("text")=="일시정지"
    assert app.dashboard_resume_button.cget("text")=="재개"
    assert app.dashboard_stop_button.cget("text")=="안전 중단"
    synthetic={"state":"ERROR","error_stage":"search","stages":{key:False for key in app.research_stage_cards},
               "can_start":False,"can_pause":False,"can_resume":False,"can_stop":False}
    app._set_research_stage_cards(synthetic)
    assert "!" in app.research_stage_cards["search"].get() and "오류" in app.research_stage_cards["search"].get()
    synthetic["state"]="BLOCKED"
    app._set_research_stage_cards(synthetic)
    assert "차단" in app.research_stage_cards["search"].get()
    app._refresh_research_dashboard(app.active_id)
    assert app.research_dashboard_summary.get().split(" · ",1)[0] in {
        "READY","RUNNING","BREAK","WAITING_SAMPLE_2","BLOCKED","ERROR","COMPLETE"}
    assert {str(value) for value in app.dashboard_channels.cget("show")}=={"tree","headings"}
    assert len([row for row in store.list_google_accounts() if row["email"].casefold()=="shared@example.test"])==1
    assert app.dashboard_channels.exists("account:"+store.get_profile(first)["google_account_id"])
    assert callable(app.open_google_wizard)
    assert callable(app._offer_recovery)
    assert hasattr(app,"autopilot_start_button")
    assert hasattr(app,"autopilot_exception_tree")
    assert hasattr(app,"autopilot_controller")
    assert app.autopilot_start_button.cget("text")=="연구 시작"
    assert app.schedule_hours.get()=="8"
    assert app.schedule_research_minutes.get()=="20"
    assert app.schedule_break_minutes.get()=="20"
    assert app.vph_observation_slots.get()=="10"
    assert "20" in app.vph_slots_combo.cget("values")
    assert app.schedule_mode.get()=="fixed"
    assert callable(app.save_research_schedule_preset)
    assert callable(app.apply_schedule_settings_next_cycle)
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
    easy_tab=app.tabs.tabs()[0]
    app.tabs.select(easy_tab);app.update()
    easy_widget=app.tabs.nametowidget(easy_tab)
    target_height=max(650,app.winfo_height()+(605-easy_widget.winfo_height()))
    app.geometry(f"1280x{target_height}");app.update()
    target_height=app.winfo_height()+(605-easy_widget.winfo_height())
    app.geometry(f"1280x{target_height}");app.update()
    measured_viewport=min(605,easy_widget.winfo_height())
    assert measured_viewport>0, easy_widget.winfo_height()
    assert app.research_dashboard_frame.winfo_ismapped()
    assert app.research_dashboard_frame.winfo_rooty()>=easy_widget.winfo_rooty()
    assert app.research_dashboard_frame.winfo_rooty()+app.research_dashboard_frame.winfo_height() <= app._easy_canvas.winfo_rooty()+app._easy_canvas.winfo_height()
    assert app.research_dashboard_frame.winfo_rooty()+app.research_dashboard_frame.winfo_height() <= easy_widget.winfo_rooty()+measured_viewport
    assert app.dashboard_start_button.winfo_ismapped()
    initial_scroll=app._easy_canvas.yview()
    wheel_event=SimpleNamespace(widget=app.easy_dashboard,num=None,delta=-120)
    assert app._easy_mousewheel(wheel_event)=="break"
    app.update_idletasks()
    assert app._easy_canvas.yview()[0]>=initial_scroll[0]
    nested_event=SimpleNamespace(widget=app.dashboard_channels,num=None,delta=-120)
    assert app._easy_mousewheel(nested_event) is None
    linux_wheel=SimpleNamespace(widget=app.easy_dashboard,num=5,delta=0)
    assert app._easy_mousewheel(linux_wheel)=="break"
    app.tabs.select(app.frames[1]);app.update_idletasks()
    assert app._easy_mousewheel(wheel_event) is None
    app.tabs.select(easy_tab);app.update_idletasks()
    for scaling in (1.0,1.25,1.5,2.0):
        app.tk.call("tk","scaling",scaling)
        app.update_idletasks()
        canvas=app._easy_canvas
        assert canvas.winfo_width()>0 and canvas.winfo_height()>0
        bounds=canvas.bbox("all")
        assert len(bounds)==4 and bounds[2]<=canvas.winfo_width()+2, (scaling,bounds,canvas.winfo_width(),canvas.itemcget(app._easy_canvas_window,"width"))
        if bounds[3]>canvas.winfo_height():
            canvas.yview_moveto(1.0);app.update_idletasks()
            assert canvas.yview()[1]>=0.99
            canvas.yview_moveto(0.0);app.update_idletasks()
        pending=[app.easy_dashboard]
        while pending:
            parent=pending.pop()
            children=parent.winfo_children();pending.extend(children)
            for child in children:
                if child.winfo_class() == "TButton":
                    assert child.winfo_width()>0 and child.winfo_height()>0
                    assert child.winfo_height()+2>=child.winfo_reqheight()
        assert app.easy_next.cget("text")
        easy_widget=app.tabs.nametowidget(easy_tab)
        assert app.research_dashboard_frame.winfo_rooty()+app.research_dashboard_frame.winfo_height() <= easy_widget.winfo_rooty()+easy_widget.winfo_height()
        for card in app.research_stage_card_labels.values():
            assert card.winfo_width()>0 and card.winfo_height()+2>=card.winfo_reqheight()
        for button in (app.dashboard_start_button,app.dashboard_pause_button,
                       app.dashboard_resume_button,app.dashboard_stop_button,app.dashboard_results_button):
            assert button.winfo_width()>0 and button.winfo_height()+2>=button.winfo_reqheight()
        app.dashboard_start_button.focus_set();app.update_idletasks()
        assert app.focus_get() is app.dashboard_start_button
    vph_tab=app.tabs.tabs()[9]
    for scaling in (1.0,1.25,1.5,2.0):
        app.tk.call("tk","scaling",scaling);app.tabs.select(vph_tab);app.update()
        assert app.vph_tree.winfo_width()>0 and app.vph_tree.winfo_height()>0
        assert app.vph_tree.cget("xscrollcommand") and app.vph_tree.cget("yscrollcommand")
        viewport=app.frames[8]
        left,top=viewport.winfo_rootx(),viewport.winfo_rooty()
        right,bottom=left+viewport.winfo_width(),top+viewport.winfo_height()
        for widget in (app.vph_controls,app.vph_buttons,*app.vph_controls.winfo_children(),*app.vph_buttons.winfo_children()):
            assert widget.winfo_rootx()>=left-1 and widget.winfo_rootx()+widget.winfo_width()<=right+2, (scaling,widget,widget.winfo_rootx(),widget.winfo_width(),right)
            assert widget.winfo_rooty()>=top-1 and widget.winfo_rooty()+widget.winfo_height()<=bottom+2, (scaling,widget,widget.winfo_rooty(),widget.winfo_height(),bottom)
        assert app.vph_buttons.winfo_rooty()<app.vph_tree.winfo_rooty()
        for widget in (app.vph_start_button,app.vph_pause_button,app.vph_resume_button,app.vph_stop_button):
            assert widget.winfo_ismapped() and widget.winfo_height()>=widget.winfo_reqheight()
    # Verify actual Tk key bindings and focus navigation on the beginner screen.
    app.tabs.select(easy_tab);app.update()
    app.dashboard_channels.selection_set("profile:"+third)
    app.dashboard_channels.focus_set();app.event_generate("<Return>");app.update()
    assert app.active_id==third
    app.event_generate("<Escape>");app.update_idletasks()
    assert app.focus_get() is app.profile_combo
    assert app.easy_next.tk_focusNext() is not None
    app.profile_combo.current([profile["id"] for profile in app.profiles].index(first))
    app.select_profile();app.update_idletasks()
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
