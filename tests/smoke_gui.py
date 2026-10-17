"""Run with xvfb-run -a python tests/smoke_gui.py on Linux CI."""
import os, sys
sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__),'..')))
from support import WorkspaceTemp
from mcrs import store
from mcrs.gui import Studio
with WorkspaceTemp() as temp:
    os.environ['ZISU_DATA_DIR']=str(temp)
    first = store.create_profile("GUI 스모크 조사", "JP", "MORNING_CHILL_RAP")
    second = store.create_profile("GUI 전환 한국", "KR", "CHILL_RAP")
    app=Studio()
    app.update_idletasks()
    assert len(app.profiles)>=1
    assert app.active_id
    assert len(app.frames)==6
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
    app.destroy()
    print('GUI_SMOKE_PASS')
