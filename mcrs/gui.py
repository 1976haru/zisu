"""Beginners-first KR/JP multi-channel GUI with read-only account connection."""
from __future__ import annotations
import os
import platform
import json
import queue
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from datetime import datetime, date
from urllib.parse import urlencode, urlparse, parse_qs
from . import store
from .genres import GENRES, search_terms, assess, related_classification
from . import genres as genre_engine
from .youtube_api import lookup_videos, APIError, quota_project_identity
from . import oauth
from . import quota_ledger
from . import ai_research
from . import comments
from . import timers
from . import autopilot
from . import vph
from . import research_scheduler
from .resource_guard import ResourceGuard, ResourceLimits, sample_resources
from .research_browser import (ResearchBrowserManager, BrowserError, BrowserResourceDeferred,
                               BrowserOwnershipUncertain, open_manual_research_url)
from .credential_store import CredentialStore, CredentialError
from .oauth_clients import OAuthClientRegistry, OAuthClientRegistryError
from .analytics import (AnalyticsClient, AnalyticsCredentialManager, AnalyticsError,
                        AnalyticsRepository, dashboard_view, project_scope_hash,
                        begin_analytics_consent)
from .branding import DISPLAY_NAME, KOREAN_NAME, SLOGAN, apply_window_branding
from .version import APP_VERSION, DISPLAY_VERSION
from .dashboard_tree_state import snapshot as snapshot_dashboard_tree, restore as restore_dashboard_tree
from .startup_probe import write_stage as _write_smoke_stage

BG = "#F5F7FA"
ACCENT = "#20635A"


def _genre_title_for_profile(profile, genre_id=None):
    config = store.profile_genre_config(profile or {})
    return genre_engine.genre_title(genre_id or (profile or {}).get("genre", ""), config["custom_genres"])


def extract_video_id(value: str):
    try:
        value = value.strip()
        url = urlparse(value if "://" in value else "https://"+value)
        host = (url.hostname or "").lower()
        if host in ("youtu.be", "www.youtu.be"):
            candidate = url.path.strip("/").split("/")[0]
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            candidate = parse_qs(url.query).get("v",[""])[0]
            if not candidate and url.path.startswith(("/shorts/", "/live/")):
                candidate = url.path.split("/")[2]
        else:
            return ""
        return candidate if re.fullmatch(r"[\w-]{11}",candidate) else ""
    except (ValueError,IndexError):
        return ""


class CustomGenreDialog(tk.Toplevel):
    """Small editor for channel-local user genre rules and localized queries."""
    def __init__(self, root, initial=None):
        super().__init__(root)
        self.title("사용자 장르 등록 / 수정")
        self.result = None
        self.transient(root); self.grab_set(); self.geometry("640x610"); self.resizable(True, True)
        initial = initial or {}
        self._id = initial.get("id") or ""
        fields = (
            ("name", "장르 이름 (예: Dream Pop)"), ("aliases", "별칭"),
            ("include_terms", "포함 신호 (장르 판정에 사용할 핵심 표현)"),
            ("exclude_terms", "제외 신호"), ("jp_queries", "일본어 검색어"),
            ("kr_queries", "한국어 검색어"), ("en_queries", "영어 검색어"),
            ("localized_queries", "추가 언어 검색어 (예: FR: chanson, chanson douce)"),
        )
        self.widgets = {}
        body = ttk.Frame(self, padding=16); body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, highlightthickness=0)
        bar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        self.form.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.form, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True); bar.pack(side="right", fill="y")
        for row, (key, label) in enumerate(fields):
            ttk.Label(self.form, text=label).grid(row=row*2, column=0, sticky="w", pady=(7, 2))
            value = initial.get(key, "")
            if isinstance(value, (list, tuple)):
                value = ", ".join(value)
            elif key == "localized_queries" and isinstance(value, dict):
                value = "\n".join(f"{lang}: {', '.join(terms)}" for lang, terms in value.items())
            widget = tk.Text(self.form, height=2 if key not in ("include_terms", "exclude_terms") else 3,
                             wrap="word", font=("Malgun Gothic", 10))
            widget.insert("1.0", str(value)); widget.grid(row=row*2+1, column=0, sticky="ew")
            self.widgets[key] = widget
        self.form.columnconfigure(0, weight=1)
        buttons = ttk.Frame(self, padding=(16, 0, 16, 14)); buttons.pack(fill="x")
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="장르 저장", command=self._save).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.wait_window()

    def _save(self):
        def values(key):
            raw = self.widgets[key].get("1.0", "end-1c").strip()
            return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]
        localized = {}
        for line in self.widgets["localized_queries"].get("1.0", "end-1c").splitlines():
            if not line.strip():
                continue
            language, separator, terms = line.partition(":")
            if not separator:
                messagebox.showwarning("추가 언어 형식", "언어별 검색어는 FR: terme, terme 형식으로 적어 주세요.", parent=self)
                return
            localized[language.strip().upper()] = [item.strip() for item in terms.split(",") if item.strip()]
        definition = {"id": self._id, "name": self.widgets["name"].get("1.0", "end-1c").strip(),
            "aliases": values("aliases"), "include_terms": values("include_terms"),
            "exclude_terms": values("exclude_terms"), "jp_queries": values("jp_queries"),
            "kr_queries": values("kr_queries"), "en_queries": values("en_queries"),
            "localized_queries": localized}
        try:
            self.result = genre_engine.validate_custom_genres([definition])[0]
        except ValueError as exc:
            messagebox.showwarning("장르 설정 확인", str(exc), parent=self)
            return
        self.destroy()


class ProfileDialog(tk.Toplevel):
    def __init__(self,root,initial=None):
        super().__init__(root)
        self.title("작업 채널 또는 조사 계정 추가")
        self.result = None
        self.transient(root); self.grab_set(); self.resizable(True,True); self.geometry("720x720")
        initial=initial or {}
        config = store.profile_genre_config(initial) if initial else _empty_genre_config()
        self.custom_genres = list(config["custom_genres"])
        self.subgenres = list(config["subgenres"])
        self.facets = dict(config["facets"])
        self.search_languages = list(config["search_languages"])
        self.name=tk.StringVar(value=initial.get("name",""))
        self.market=tk.StringVar(value=initial.get("market","JP"))
        self.genre=tk.StringVar(value=initial.get("genre","MORNING_CHILL_RAP"))
        body=ttk.Frame(self,padding=18); body.pack(fill="both",expand=True)
        ttk.Label(body,text="1단계 · 알아보기 쉬운 작업 프로필 이름").pack(anchor="w",pady=(0,5))
        ttk.Entry(body,textvariable=self.name,width=56).pack(anchor="w",fill="x")
        ttk.Label(body,text="2단계 · 조사 국가 / 검색 시장").pack(anchor="w",pady=(12,3))
        ttk.Combobox(body,textvariable=self.market,values=["JP","KR"],state="readonly",width=12).pack(anchor="w")
        ttk.Label(body,text="3단계 · 핵심 장르 (새 프리셋 또는 이 프로필의 사용자 장르)").pack(anchor="w",pady=(12,3))
        self.genre_keys=[]; self.genre_names=[]
        self.genre_display=tk.StringVar()
        self.genre_combo=ttk.Combobox(body,textvariable=self.genre_display,state="readonly",width=55)
        self.genre_combo.pack(anchor="w")
        self.genre_combo.bind("<<ComboboxSelected>>",self._core_genre_changed)
        ttk.Label(body,text="4단계 · 유사 장르 (선택 사항, 최대 5개)").pack(anchor="w",pady=(12,3))
        self.related_frame=ttk.Frame(body); self.related_frame.pack(anchor="w",fill="x")
        self.related_summary=tk.StringVar()
        ttk.Label(body,textvariable=self.related_summary,wraplength=660).pack(anchor="w",pady=5)
        ttk.Button(body,text="사용자 장르 추가 / 수정",command=self._manage_custom_genre).pack(anchor="w",pady=(0,8))
        ttk.Label(body,text="5단계 · 세부 장르 (최대 12개, 쉼표로 구분)").pack(anchor="w",pady=(4,2))
        self.subgenre_var=tk.StringVar(value=", ".join(self.subgenres))
        ttk.Entry(body,textvariable=self.subgenre_var).pack(fill="x")
        ttk.Label(body,text="장르와 별도로 지정하는 연구 문맥 (쉼표로 구분)").pack(anchor="w",pady=(8,2))
        self.facet_vars={key:tk.StringVar(value=", ".join(self.facets.get(key,[]))) for key in ("moods","eras","audiences","languages")}
        facet_labels={"moods":"분위기","eras":"시대","audiences":"청취 대상","languages":"콘텐츠 언어"}
        for key,label in facet_labels.items():
            row=ttk.Frame(body); row.pack(fill="x",pady=2)
            ttk.Label(row,text=label,width=16).pack(side="left")
            ttk.Entry(row,textvariable=self.facet_vars[key]).pack(side="left",fill="x",expand=True)
        ttk.Label(body,text="검색어에 사용할 언어 코드 (예: JP, EN, FR)").pack(anchor="w",pady=(8,2))
        self.search_languages_var=tk.StringVar(value=", ".join(self.search_languages or [self.market.get()]))
        ttk.Entry(body,textvariable=self.search_languages_var).pack(fill="x")
        ttk.Button(body,text="저장하고 Google 연결로 이동",command=self._save).pack(anchor="e",pady=14)
        self._refresh_genre_controls(initial)
        self.bind("<Escape>",lambda _e:self.destroy())
        self.wait_window()

    def _catalog(self):
        return {**GENRES, **genre_engine.custom_catalog(self.custom_genres)}

    def _refresh_genre_controls(self, initial=None):
        catalog=self._catalog(); old_id=self.genre.get()
        self.genre_keys=list(catalog); self.genre_names=[catalog[key].title for key in self.genre_keys]
        if old_id not in catalog:
            old_id="MORNING_CHILL_RAP"
        self.genre.set(old_id); self.genre_combo.configure(values=self.genre_names)
        self.genre_display.set(catalog[old_id].title)
        for widget in self.related_frame.winfo_children(): widget.destroy()
        current=store.normalize_related_genres(old_id, (initial or {}).get("related_genres_json","[]"), self.custom_genres) if initial else []
        if not initial and self.market.get()=="JP" and old_id=="MORNING_CHILL_RAP":
            current=[key for key in ("CHILL_RAP","RNB_HIPHOP","CAFE_CHILL") if key in catalog]
        self.related_vars={key:tk.BooleanVar(value=key in current) for key in self.genre_keys}
        for index,key in enumerate(self.genre_keys):
            ttk.Checkbutton(self.related_frame,text=catalog[key].title,variable=self.related_vars[key],
                command=self._update_related_summary).grid(row=index//3,column=index%3,sticky="w",padx=(0,14),pady=2)
        self._update_related_summary()

    def _manage_custom_genre(self):
        if self.custom_genres:
            choice=simpledialog.askstring("사용자 장르", "수정할 장르 이름을 입력하세요. 비우면 새 장르를 등록합니다.\n\n"+
                "\n".join(item["name"] for item in self.custom_genres), parent=self)
            if choice is None: return
            existing=next((item for item in self.custom_genres if item["name"].casefold()==choice.strip().casefold()),None)
        else:
            existing=None
        dialog=CustomGenreDialog(self,existing)
        if dialog.result:
            if existing:
                self.custom_genres=[dialog.result if item["id"]==existing["id"] else item for item in self.custom_genres]
                if dialog.result["id"] != existing["id"]:
                    self.custom_genres=[item for item in self.custom_genres if item["id"]!=existing["id"]]
            else:
                self.custom_genres.append(dialog.result)
            current=self.genre.get()
            self._refresh_genre_controls({"genre":current,"related_genres_json":json.dumps(
                [key for key,var in getattr(self,"related_vars",{}).items() if var.get()])})

    def _core_genre_changed(self, _event=None):
        selected=self.genre_names.index(self.genre_display.get())
        self.genre.set(self.genre_keys[selected])
        self.related_vars[self.genre.get()].set(False)
        self._update_related_summary()

    def _update_related_summary(self):
        catalog=self._catalog(); key=self.genre.get()
        if key not in catalog:
            key=self.genre_keys[self.genre_names.index(self.genre_display.get())]
            self.genre.set(key)
        selected=[item for item in self.genre_keys if item!=key and self.related_vars[item].get()]
        if len(selected)>5:
            self.related_vars[selected[-1]].set(False); selected=selected[:-1]
        self.related_summary.set("선택한 유사 장르: "+(", ".join(catalog[item].title for item in selected) if selected else "없음"))

    def _save(self):
        name=self.name.get().strip()
        if not 2<=len(name)<=70:
            messagebox.showwarning("이름 확인","채널 이름을 2~70자로 입력해 주세요.",parent=self); return
        genre=self.genre_keys[self.genre_names.index(self.genre_display.get())]
        related=[key for key in self.genre_keys if self.related_vars[key].get() and key!=genre]
        facets={key:[item.strip() for item in value.get().split(",") if item.strip()]
                for key,value in self.facet_vars.items()}
        languages=[item.strip().upper() for item in self.search_languages_var.get().split(",") if item.strip()]
        config={"custom_genres":self.custom_genres,"subgenres":[item.strip() for item in self.subgenre_var.get().split(",") if item.strip()],
                "facets":facets,"search_languages":languages}
        try: config=store._normalize_genre_config(config)
        except ValueError as exc:
            messagebox.showwarning("장르 설정 확인",str(exc),parent=self); return
        self.result=(name,self.market.get(),genre,related,config)
        self.destroy()


def _empty_genre_config():
    return {"custom_genres":[],"subgenres":[],"facets":{"moods":[],"eras":[],"audiences":[],"languages":[]},"search_languages":[]}


class Studio(tk.Tk):
    def __init__(self):
        super().__init__()
        _write_smoke_stage("tk_root_created")
        _write_smoke_stage("database_initializing")
        store.initialize()
        self.analytics_credentials=AnalyticsCredentialManager()
        self.analytics_repository=AnalyticsRepository()
        self.analytics_busy=False
        self.analytics_days=tk.IntVar(value=28)
        _write_smoke_stage("database_initialized")
        first_run = not store.list_profiles()
        smoke_report = os.environ.get("ZISU_EXE_SMOKE_REPORT", "").strip()
        apply_window_branding(self)
        screen_w=max(800,self.winfo_screenwidth()-80)
        screen_h=max(560,self.winfo_screenheight()-120)
        initial_w=min(1320,screen_w);initial_h=min(840,screen_h)
        self.geometry(f"{initial_w}x{initial_h}")
        self.minsize(min(900,initial_w),min(560,initial_h))
        self.configure(background=BG)
        style=ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame",background=BG)
        style.configure("TLabel",background=BG,foreground="#253B45",font=("Malgun Gothic",10))
        style.configure("TButton",font=("Malgun Gothic",10),padding=(10,8))
        style.configure("TNotebook.Tab",font=("Malgun Gothic",10,"bold"),padding=(16,10))
        style.configure("Treeview",font=("Malgun Gothic",9),rowheight=30)
        style.configure("Treeview.Heading",font=("Malgun Gothic",9,"bold"))
        style.configure("Accent.TButton",background=ACCENT,foreground="white")
        style.map("Accent.TButton",background=[("active","#16483F")])
        self.profiles=[]
        self.active_id=None
        self.profile_choice=tk.StringVar()
        self.credential_store=CredentialStore()
        try:
            saved_youtube_key=self.credential_store.load("youtube-data") or ""
        except CredentialError:
            saved_youtube_key=""
        self.api_key=tk.StringVar(value=os.environ.get("YOUTUBE_API_KEY","") or saved_youtube_key)
        self.search_status=tk.StringVar(value="장르를 선택한 후 검색을 시작하세요.")
        self.active_account_banner=tk.StringVar(value="현재 작업 계정을 확인하세요.")
        self.filter=tk.StringVar()
        self.decision_filter=tk.StringVar(value="전체")
        self.selected_video=None
        self.comment_video=None
        self.timer_end=0
        self.timer_text=tk.StringVar(value="감상 타이머 대기")
        self.footer=tk.StringVar(value="Google OAuth 조회 전용 · 비밀번호 수집 안 함 · 연결 토큰은 로컬 PC에만 저장")
        self.oauth_client_file=tk.StringVar(value="")
        self.oauth_registry=OAuthClientRegistry()
        self.oauth_client_choice=tk.StringVar(value="")
        self.oauth_client_ids={}
        self.oauth_accounts_summary=tk.StringVar(value="연결된 Google 계정 없음")
        self.oauth_status=tk.StringVar(value="계정 상태 확인 중...")
        self.oauth_busy=False
        self.search_inflight=False
        self._ui_state=(store.load_ui_state() or {}).get("state",{})
        self._ui_save_after=None
        self._restoring_ui=False
        self.scheduler_enabled=tk.BooleanVar(value=False)
        self.scheduler_fired=set()
        self.terms_seen=0
        self._header()
        self.tabs=ttk.Notebook(self)
        self.tabs.pack(fill="both",expand=True,padx=15,pady=8)
        self.frames=[]
        for label in ("① 계정 연결","② 장르별 채널 검색","③ 후보·장르 검토","④ 댓글 초안","⑤ 국가별 성과","⑥ 예약·도움말","⑦ AI 연구 도우미","⑧ AI 자동 조사 · Autopilot"):
            tab=ttk.Frame(self.tabs,padding=14)
            self.tabs.add(tab,text=label)
            self.frames.append(tab)
        vph_frame=ttk.Frame(self.tabs,padding=14)
        self.tabs.add(vph_frame,text="VPH 연구 레이더")
        self.frames.append(vph_frame)
        self._account_ui(self.frames[0])
        self._search_ui(self.frames[1])
        self._candidate_ui(self.frames[2])
        self._comment_ui(self.frames[3])
        self._metrics_ui(self.frames[4])
        self._help_ui(self.frames[5])
        self._ai_research_ui(self.frames[6])
        self._autopilot_ui(self.frames[7])
        self._vph_ui(self.frames[8])
        self.filter.trace_add("write",lambda *_:self._save_ui_state())
        self.decision_filter.trace_add("write",lambda *_:self._save_ui_state())
        self.ai_target.trace_add("write",lambda *_:self._save_ui_state())
        self.ai_max_calls.trace_add("write",lambda *_:self._save_ui_state())
        easy = ttk.Frame(self.tabs, padding=22)
        self.tabs.insert(0, easy, text="쉬운 시작")
        self._easy_ui(easy)
        ttk.Label(self,textvariable=self.footer).pack(anchor="w",padx=24,pady=(0,9))
        self.reload_profiles()
        self.tabs.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.report_callback_exception = self._report_ui_exception
        self._install_thread_exception_handler()
        self.bind_all("<Control-s>", lambda _e: self._save_ui_state())
        self._restore_ui_state()
        if first_run and not smoke_report:
            self.after(150, self.start_onboarding)
        self._check_research_browser_schedule()
        now=time.monotonic()
        if now-getattr(self,"_browser_metrics_checked_at",0)>=15 and getattr(self,"research_browser",None):
            self._browser_metrics_checked_at=now
            if not getattr(self,"_browser_metrics_running",False):
                self._browser_metrics_running=True
                threading.Thread(target=self._research_browser_metrics_probe,
                    name="zisu-browser-metrics",daemon=True).start()
        self.after(1000,self._tick)
        self.after(14000,self._scheduled_tick)
        if smoke_report:
            _write_smoke_stage("gui_widgets_ready")
            self.after(250, lambda: self._complete_exe_smoke(smoke_report))
        self.protocol("WM_DELETE_WINDOW", self._autopilot_window_close)

    def _complete_exe_smoke(self, report_path):
        """CI-only startup probe. It never performs OAuth or network requests."""
        self.update_idletasks()
        payload = {
            "app_version": APP_VERSION,
            "gui_initialized": len(self.frames) == 9 and len(self.tabs.tabs()) == 10 and bool(self.winfo_exists()),
            "profile_count": len(self.profiles),
            "frozen_executable": bool(getattr(sys, "frozen", False)),
            "python_executable": Path(sys.executable).name,
            "windows_release": platform.release(),
            "windows_version": platform.version(),
            "data_directory_created": store.data_dir().is_dir(),
            "oauth_connected": False,
            "network_used": False,
        }
        _write_smoke_stage("gui_ready", **payload)
        profile_test = None
        if os.environ.get("ZISU_EXE_SMOKE_PROFILE_TEST") == "1":
            jp_id = store.create_profile("ZISU smoke JP", "JP", "MORNING_CHILL_RAP")
            kr_id = store.create_profile("ZISU smoke KR", "KR", "CHILL_RAP")
            store.set_profile_search_terms(jp_id, "JP", ["smoke JP isolated term"])
            store.set_profile_search_terms(kr_id, "KR", ["smoke KR isolated term"])
            self.reload_profiles(jp_id)
            jp_selected = self.active_id == jp_id and store.list_profile_search_terms(jp_id, "JP") == ["smoke JP isolated term"]
            kr_index = [profile["id"] for profile in self.profiles].index(kr_id)
            self.profile_combo.current(kr_index)
            self.select_profile()
            kr_selected = self.active_id == kr_id and store.list_profile_search_terms(kr_id, "KR") == ["smoke KR isolated term"]
            no_cross_terms = (
                not store.list_profile_search_terms(jp_id, "KR")
                and not store.list_profile_search_terms(kr_id, "JP")
            )
            profile_test = {
                "passed": bool(jp_selected and kr_selected and no_cross_terms),
                "profiles_created": 2,
                "profile_switch_verified": bool(jp_selected and kr_selected),
                "search_terms_isolated": bool(no_cross_terms),
            }
        with store.connect() as database:
            database_integrity = database.execute("PRAGMA quick_check").fetchone()[0]
        payload["database_integrity"] = database_integrity
        if profile_test is not None:
            payload["profile_test"] = profile_test
        payload["autopilot_ui"] = bool(hasattr(self, "autopilot_start_button") and
                                       hasattr(self, "autopilot_exception_tree") and
                                       hasattr(self, "autopilot_controller"))
        _write_smoke_stage("offline_smoke_complete", **payload)
        self.destroy()

    def _header(self):
        top=ttk.Frame(self,padding=(18,14))
        top.pack(fill="x")
        identity=ttk.Frame(top)
        identity.pack(side="left",anchor="w")
        brand_icon=getattr(self,"_zisu_signal_lab_icon",None)
        if brand_icon is not None:
            self._brand_header_icon=brand_icon
            ttk.Label(identity,image=brand_icon).pack(side="left",padx=(0,10))
        brand_text=ttk.Frame(identity)
        brand_text.pack(side="left",anchor="w")
        ttk.Label(brand_text,text=f"{DISPLAY_NAME} · {KOREAN_NAME} · {DISPLAY_VERSION}",
                  font=("Malgun Gothic",17,"bold")).pack(anchor="w")
        ttk.Label(brand_text,text=SLOGAN,font=("Malgun Gothic",9)).pack(anchor="w")
        bar=ttk.Frame(self,padding=(18,0))
        bar.pack(fill="x",pady=(0,9))
        ttk.Label(bar,text="작업 채널").pack(side="left")
        self.profile_combo=ttk.Combobox(bar,textvariable=self.profile_choice,state="readonly",width=49)
        self.profile_combo.pack(side="left",padx=10)
        self.profile_combo.bind("<<ComboboxSelected>>",lambda _e:self.select_profile())
        ttk.Button(bar,text="+ 채널 추가",command=self.add_profile,style="Accent.TButton").pack(side="left",padx=4)
        ttk.Button(bar,text="채널 수정",command=self.edit_profile).pack(side="left",padx=4)
        self.profile_description=tk.StringVar()
        ttk.Label(bar,textvariable=self.profile_description).pack(side="left",padx=12)

    def _easy_ui(self, tab):
        tab.columnconfigure(0, weight=1); tab.rowconfigure(0, weight=1)
        canvas=tk.Canvas(tab,highlightthickness=0,background=BG)
        scrollbar=ttk.Scrollbar(tab,orient="vertical",command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0,column=0,sticky="nsew"); scrollbar.grid(row=0,column=1,sticky="ns")
        content=ttk.Frame(canvas,padding=(12,10)); self.easy_dashboard=content
        self._easy_canvas=canvas
        self._easy_canvas_window=canvas.create_window((0,0),window=content,anchor="nw")
        content.bind("<Configure>",lambda _e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",lambda event:canvas.itemconfigure(self._easy_canvas_window,width=event.width))
        content.columnconfigure(0,weight=1); content.columnconfigure(1,weight=1)
        ttk.Label(content,text=f"{DISPLAY_NAME} · {KOREAN_NAME} · {DISPLAY_VERSION}",font=("Malgun Gothic",20,"bold")).grid(row=0,column=0,sticky="w",pady=(3,4))
        ttk.Label(content,text=f"{SLOGAN}  Google 계정과 채널은 구분되며, 채널별 조사 기록과 인증은 서로 섞이지 않습니다.",
                  wraplength=1000).grid(row=1,column=0,columnspan=2,sticky="w",pady=(0,8))
        self.easy_progress=tk.StringVar(value="먼저 조사 채널을 만들거나 기존 채널을 선택하세요.")
        ttk.Label(content,textvariable=self.easy_progress,wraplength=1000,font=("Malgun Gothic",13,"bold")).grid(row=2,column=0,columnspan=2,sticky="ew",pady=5)
        account_panel=ttk.LabelFrame(content,text="Google 계정 → YouTube 채널 → 조사 프로필",padding=8)
        account_panel.grid(row=3,column=0,columnspan=2,sticky="nsew",pady=5)
        account_panel.columnconfigure(0,weight=1);account_panel.rowconfigure(0,weight=1)
        self.dashboard_channels=ttk.Treeview(account_panel,columns=("market","genre","state"),show="tree headings",height=5)
        self.dashboard_channels.heading("#0",text="계정 / 채널 / 조사 프로필")
        for col,label,width in (("market","국가",65),("genre","핵심 장르",180),("state","연결 상태",115)):
            self.dashboard_channels.heading(col,text=label);self.dashboard_channels.column(col,width=width,stretch=col=="genre")
        self.dashboard_channels.grid(row=0,column=0,sticky="nsew")
        tree_scroll=ttk.Scrollbar(account_panel,orient="vertical",command=self.dashboard_channels.yview)
        tree_scroll.grid(row=0,column=1,sticky="ns");self.dashboard_channels.configure(yscrollcommand=tree_scroll.set)
        self._dashboard_tree_profiles={}
        self.dashboard_channels.bind("<Double-1>",self._dashboard_open_profile)
        self.dashboard_channels.bind("<Return>",self._dashboard_open_profile)
        self.dashboard_selected_text=tk.StringVar(value="현재 선택: 아직 프로필이 없습니다")
        ttk.Label(content,textvariable=self.dashboard_selected_text,wraplength=1000,font=("Malgun Gothic",12,"bold")).grid(row=4,column=0,columnspan=2,sticky="w",pady=5)
        cards=ttk.LabelFrame(content,text="현재 채널 요약",padding=8);cards.grid(row=5,column=0,columnspan=2,sticky="ew",pady=5)
        for col in range(3):cards.columnconfigure(col,weight=1)
        self.dashboard_today_text=tk.StringVar(value="오늘 조사한 후보: 0")
        self.dashboard_ai_text=tk.StringVar(value="AI 분석: 아직 없음")
        self.dashboard_review_text=tk.StringVar(value="사람 확인 필요: 0 · 사람 검증: 0")
        for index,var in enumerate((self.dashboard_today_text,self.dashboard_ai_text,self.dashboard_review_text)):
            ttk.Label(cards,textvariable=var,wraplength=1000).grid(row=index,column=0,sticky="ew",padx=5,pady=4)
        cards.columnconfigure(0,weight=1)
        self.dashboard_schedule_text=tk.StringVar(value="연구 일정: 아직 설정되지 않았습니다")
        self.dashboard_discovery_text=tk.StringVar(value="오늘 새 채널: 0 · 누적 중복 제외: 0 · 규칙 적합 후보: 0")
        self.dashboard_ai_queue_text=tk.StringVar(value="AI 검토 대기: 0 · 조사 검색 호출 누계: 0회")
        self.dashboard_resource_text=tk.StringVar(value="컴퓨터 여유 있음 · 저부하 모드 권장")
        self.dashboard_browser_text=tk.StringVar(value="연구용 창: 전용 모드 또는 직접 여는 수동 모드를 선택할 수 있어요")
        ttk.Label(content,textvariable=self.dashboard_schedule_text,wraplength=1000).grid(row=6,column=0,columnspan=2,sticky="w",pady=3)
        ttk.Label(content,textvariable=self.dashboard_discovery_text,wraplength=1000).grid(row=7,column=0,columnspan=2,sticky="w",pady=3)
        ttk.Label(content,textvariable=self.dashboard_ai_queue_text,wraplength=1000).grid(row=8,column=0,columnspan=2,sticky="w",pady=3)
        ttk.Label(content,textvariable=self.dashboard_resource_text,wraplength=1000).grid(row=9,column=0,columnspan=2,sticky="w",pady=3)
        ttk.Label(content,textvariable=self.dashboard_browser_text,wraplength=1000).grid(row=10,column=0,columnspan=2,sticky="w",pady=3)
        self.easy_next=ttk.Button(content,text="내 조사 채널 만들기",command=self.add_profile,style="Accent.TButton")
        self.easy_next.grid(row=11,column=0,columnspan=2,sticky="ew",ipady=10,pady=8)
        actions=ttk.Frame(content);actions.grid(row=12,column=0,columnspan=2,sticky="ew",pady=4)
        actions.columnconfigure(0,weight=1)
        for index,(label,command,style_name) in enumerate((
            ("Google 계정 연결",lambda:self.tabs.select(self.frames[0]),"TButton"),
            ("장르·채널 설정",self.edit_profile,"TButton"),
            ("연구 시간 설정",lambda:self.tabs.select(self.frames[7]),"TButton"),
            ("연구 시작",self._dashboard_start_research,"Accent.TButton"),
            ("결과 확인",lambda:self.tabs.select(self.frames[2]),"TButton"),
            ("Google 연결 도우미",self.open_google_wizard,"TButton"))):
            ttk.Button(actions,text=label,command=command,style=style_name).grid(row=index,column=0,sticky="ew",padx=3,pady=3)
        ttk.Button(actions,text="VPH 연구 레이더",command=lambda:self.tabs.select(self.frames[8])).grid(row=6,column=0,sticky="ew",padx=3,pady=3)
        browser_row=ttk.Frame(content);browser_row.grid(row=13,column=0,columnspan=2,sticky="ew",pady=4)
        browser_row.columnconfigure(0,weight=1)
        for index,(label,command) in enumerate((
            ("연구용 인터넷 창 열기",self._research_browser_open),
            ("일반 브라우저로 직접 열기",self._research_browser_manual_open),
            ("이전 작업 복원",self._offer_recovery),
            ("도움말",lambda:self.tabs.select(self.frames[5])))):
            ttk.Button(browser_row,text=label,command=command).grid(row=index,column=0,sticky="ew",padx=3,pady=2)
        ttk.Label(content,text="수동 모드로 연 일반 브라우저는 ZISU가 자동으로 닫지 않습니다. 영상 재생·댓글·좋아요·구독은 직접 수행합니다.",
                  wraplength=1000).grid(row=14,column=0,columnspan=2,sticky="w",pady=(5,12))
        performance=ttk.LabelFrame(content,text="실제 YouTube 채널 성과 · 읽기 전용",padding=10)
        performance.grid(row=15,column=0,columnspan=2,sticky="ew",pady=(8,4))
        performance.columnconfigure(0,weight=1)
        ttk.Label(performance,text="연구 후보·메모 수와 별개의 YouTube Analytics 측정값입니다. 가져오지 못한 값은 0이 아니라 미수집으로 표시합니다.",wraplength=1000).grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,4))
        ttk.Label(performance,text="기간:").grid(row=1,column=0,sticky="w")
        ttk.Combobox(performance,textvariable=self.analytics_days,values=(7,28,90),state="readonly",width=6).grid(row=1,column=1,sticky="w",padx=4)
        ttk.Button(performance,text="성과 읽기 권한 연결",command=self._analytics_consent).grid(row=1,column=2,sticky="e",padx=3)
        ttk.Button(performance,text="성과 가져오기 (Google 조회)",command=self._analytics_fetch,style="Accent.TButton").grid(row=2,column=2,sticky="e",padx=3,pady=4)
        self.analytics_dashboard_text=tk.StringVar(value="아직 성과를 가져오지 않았습니다.")
        ttk.Label(performance,textvariable=self.analytics_dashboard_text,wraplength=1000,justify="left").grid(row=2,column=0,columnspan=2,sticky="w",pady=4)
        ttk.Label(performance,text="노출수·썸네일 클릭률은 reports.query 미지원 지표입니다. 별도 Reporting API Reach 보고서는 아직 연결되지 않았습니다.",wraplength=1000,foreground="#8a4b08").grid(row=3,column=0,columnspan=3,sticky="w",pady=4)
        self._easy_enter_button=None
        self.bind_all("<Return>",self._easy_enter,add="+")
        self.bind_all("<Escape>",self._easy_escape,add="+")

    def _easy_enter(self, _event=None):
        if self.tabs.select() == str(self.tabs.tabs()[0]):
            focus=self.focus_get()
            if focus is self.dashboard_channels:
                self._dashboard_open_profile()
                return "break"
        return None

    def _easy_escape(self,_event=None):
        if self.tabs.select()==str(self.tabs.tabs()[0]):
            self.profile_combo.focus_set()
            return "break"
        return None

    def _dashboard_open_profile(self,_event=None):
        selection=self.dashboard_channels.selection()
        if not selection:return "break"
        profile_id=self._dashboard_tree_profiles.get(selection[0])
        if not profile_id:return "break"
        if profile_id!=self.active_id and (self.search_inflight or getattr(self,"ai_running",False) or
                getattr(self,"autopilot_schedule_running",False)):
            messagebox.showinfo("조사 중인 채널","현재 채널의 작업을 안전하게 마친 뒤 채널을 바꿔 주세요. 진행 중인 조사는 다른 채널로 옮겨지지 않습니다.",parent=self)
            return "break"
        ids=[p["id"] for p in self.profiles]
        if profile_id in ids:
            self.profile_combo.current(ids.index(profile_id));self.select_profile()
        return "break"

    def _dashboard_start_research(self):
        if not self.active_id:
            self.add_profile();return
        self.tabs.select(self.frames[7])
        # Keep the existing Autopilot approval, budget and confirmation path.
        self._autopilot_start()

    def _dashboard_refresh(self):
        if not hasattr(self,"dashboard_channels"):return
        tree_state=snapshot_dashboard_tree(self.dashboard_channels)
        self.dashboard_channels.delete(*self.dashboard_channels.get_children())
        self._dashboard_tree_profiles={}
        accounts=store.list_google_accounts()
        seen=set()
        for account in accounts:
            account_id=account["id"]
            account_node="account:"+account_id
            email=account.get("email") or "이메일 정보 없음"
            self.dashboard_channels.insert("","end",iid=account_node,text="Google · "+email,values=("","","계정"),open=True)
            channels=account.get("channels",[])
            if not channels:channels=[{"channel_id":"","channel_title":"채널 미연결"}]
            for index,channel in enumerate(channels):
                channel_id=channel.get("channel_id") or ""
                node=f"{account_node}:channel:{index}"
                title=channel.get("channel_title") or ("YouTube 채널 없음" if not channel_id else "이름 없는 채널")
                self.dashboard_channels.insert(account_node,"end",iid=node,text=title,values=("","",("채널 없음" if not channel_id else "등록됨")),open=True)
                for profile in account.get("profiles",[]):
                    if channel_id and profile.get("youtube_channel_id")!=channel_id:continue
                    if not channel_id and profile.get("youtube_channel_id"):continue
                    if profile["id"] in seen:continue
                    seen.add(profile["id"]);self._dashboard_insert_profile(node,profile)
            for profile in account.get("profiles",[]):
                if profile["id"] not in seen:
                    seen.add(profile["id"]);self._dashboard_insert_profile(account_node,profile)
        unlinked=[profile for profile in self.profiles if profile["id"] not in seen]
        if unlinked:
            self.dashboard_channels.insert("","end",iid="unlinked",text="Google 미연결 프로필",values=("","","연결 전"),open=True)
            for profile in unlinked:self._dashboard_insert_profile("unlinked",profile)
        fallback="profile:"+self.active_id if self.active_id else ""
        restore_dashboard_tree(self.dashboard_channels,tree_state,fallback)
        profile=store.dashboard_summary(self.active_id) if self.active_id else None
        if not profile:
            self._refresh_analytics_panel(None)
            self.dashboard_selected_text.set("현재 선택: 조사 채널을 추가하거나 위 목록에서 선택하세요")
            self.dashboard_today_text.set("오늘 저장·갱신한 후보: 0");self.dashboard_ai_text.set("AI 분석: 아직 없음")
            self.dashboard_review_text.set("사람 확인 필요: 0 · 사람 검증: 0")
            self.dashboard_schedule_text.set("연구 일정: 아직 설정되지 않았습니다")
            self.dashboard_discovery_text.set("오늘 새 채널: 0 · 누적 중복 제외: 0 · 규칙 적합 후보: 0")
            self.dashboard_ai_queue_text.set("AI 검토 대기: 0 · 조사 검색 호출 누계: 0회")
            self.dashboard_resource_text.set("컴퓨터 상태: 조사 채널을 선택하세요")
            self.dashboard_browser_text.set("연구용 창: 전용 모드 또는 직접 여는 수동 모드를 선택할 수 있어요")
            return
        connected="연결됨" if oauth.is_connected(profile["id"]) else "연결 전"
        self.dashboard_selected_text.set(f"현재 선택: {profile['name']} · {profile['market']} · {_genre_title_for_profile(profile)} · {profile['account_email'] or 'Google 미연결'} · {profile['youtube_channel_title'] or 'YouTube 채널 정보 없음'} ({connected})")
        schedule=profile.get("schedule_state")
        if schedule and schedule.get("state") in {"running","scheduled"}:
            try: profile["schedule_state"]=research_scheduler.get(schedule["id"],profile["id"])
            except (sqlite3.Error,ValueError): pass
        self.dashboard_today_text.set(f"오늘 저장·갱신한 후보: {profile['today_candidates']} · 저장 후보 전체: {profile['candidates']}")
        self.dashboard_ai_text.set(f"AI 분석 결과: 전체 {profile['ai_reviews']} · 오늘 {profile['today_ai_reviews']} · 분석은 사람의 청취 정답과 별도")
        self.dashboard_review_text.set(f"사람 확인 필요: {profile['needs_review']} · 사람 검증 완료: {profile['human_labels']}")
        self.dashboard_schedule_text.set(self._dashboard_schedule_label(profile))
        self.dashboard_discovery_text.set(f"오늘 새로 발견한 채널: {profile['new_channels_today']} · 누적 중복 제외: {profile['duplicate_candidates_total']} · 규칙 적합 후보: {profile['genre_fit_candidates']}")
        self.dashboard_ai_queue_text.set(f"AI 검토 대기 후보: {profile['ai_pending']} · 조사 검색 호출 누계: {profile['search_calls_total']}회")
        self.dashboard_resource_text.set("컴퓨터 상태: "+getattr(self,"resource_status_text",tk.StringVar(value="컴퓨터 여유 있음")).get())
        browser=getattr(self,"research_browser_status",None)
        dedicated=browser.get() if browser else "전용 창 상태 확인 전"
        self.dashboard_browser_text.set(dedicated+" · 일반 브라우저 수동 모드는 ZISU가 자동 종료하지 않습니다")

        self._refresh_analytics_panel(profile)

    def _dashboard_insert_profile(self,parent,profile):
        profile_id=profile["id"];node="profile:"+profile_id
        genre_title=_genre_title_for_profile(profile)
        linked=oauth.is_connected(profile_id)
        self.dashboard_channels.insert(parent,"end",iid=node,text=profile.get("name",profile_id),
            values=(profile.get("market",""),genre_title,"연결됨" if linked else "연결 전"))
        self._dashboard_tree_profiles[node]=profile_id

    def _dashboard_schedule_label(self,profile):
        schedule=profile.get("schedule_state")
        if schedule:
            state=schedule.get("state");phase=schedule.get("phase")
            elapsed=max(0,int(schedule.get("elapsed_seconds",0)));total=int(schedule.get("total_minutes",0))*60
            remaining=max(0,total-elapsed)
            phase_text={"research":"연구 중","break":"휴식 중","waiting":"시작 대기","paused":"일시 정지"}.get(phase,phase or "예약됨")
            return f"연구 상태: {phase_text} · 남은 시간 {remaining//3600}시간 {(remaining%3600)//60}분 · 검색 {schedule.get('search_calls_used',0)}/{schedule.get('approved_search_calls',0)}회"
        times=sorted(x for x in (profile.get("schedule") or "").split(",") if re.fullmatch(r"\d{2}:\d{2}",x))
        if times:
            now=datetime.now().strftime("%H:%M")
            next_time=next((x for x in times if x>now),times[0])
            return f"연구 상태: 예약 일정 없음 · 다음 저장된 검색 시각 {next_time} · Autopilot 예약은 필요할 때 설정하세요"
        return "연구 상태: 예약된 연구가 없습니다. ‘연구 시간 설정’에서 일정을 만들 수 있어요."

    def _refresh_analytics_panel(self, profile=None):
        if not hasattr(self,"analytics_dashboard_text"):
            return
        profile=profile or self.current_profile()
        if not profile or not profile.get("youtube_channel_id"):
            self.analytics_dashboard_text.set("YouTube 채널 연결 후 성과 데이터를 가져올 수 있습니다. 채널 없는 조사 프로필의 성과는 미수집입니다.")
            return
        try:
            view=dashboard_view(profile["id"],profile["youtube_channel_id"],int(self.analytics_days.get()),self.analytics_repository)
        except Exception:
            self.analytics_dashboard_text.set("성과 저장 결과를 읽지 못했습니다. 기존 조사 자료는 별도로 유지됩니다.")
            return
        if view["state"]=="not_collected":
            self.analytics_dashboard_text.set("이 채널의 실제 성과는 아직 미수집입니다. [성과 가져오기]를 눌러 Google에 읽기 전용으로 요청할 수 있습니다.")
            return
        metrics=view.get("metrics",{})
        def show(key, suffix=""):
            value=metrics.get(key)
            return "미수집" if value is None else (f"{value:,.1f}{suffix}" if isinstance(value,float) else f"{value:,}{suffix}")
        top=" · ".join(str(row.get("video", "")) for row in view.get("top_videos",[])[:3]) or "미수집"
        countries=" · ".join(f"{r.get('country')}: {r.get('views')}" for r in view.get("countries",[])[:3]) or "미수집"
        ideas="\n".join(("측정 사실: " if row.get("kind")=="measured_fact" else
                            "검토 가설: " if row.get("kind")=="hypothesis" else "미지원: ")+row.get("text","")
                           for row in view.get("suggestions",[])[:4]) or "개선 제안은 실제 자료가 더 모이면 표시됩니다."
        self.analytics_dashboard_text.set(
            f"최근 {view['window_days']}일 실제 성과 · 조회수 {show('views')} · 시청시간 {show('watch_minutes','분')} · "
            f"직전 같은 기간 대비 조회수 {show('views_change')} ({show('views_change_percent','%')}) · 시청시간 변화 {show('watch_minutes_change','분')} · "
            f"평균 지속시간 {show('avg_view_duration_seconds','초')} · 평균 시청비율 {show('avg_view_percentage','%')} · "
            f"구독자 순증감 {show('subscriber_change')}\n성과 상위 영상 ID: {top}\n주요 시청 국가: {countries}\n{ideas}\n"
            "YouTube Analytics 성과는 연구 활동 수치와 별도이며, 개선안은 원인 확정이나 성장 보장이 아닙니다.")

    def _analytics_client_config(self):
        selected=self.oauth_client_ids.get(self.oauth_client_choice.get())
        if selected:
            return self.oauth_registry.load_config(selected)
        path=self.oauth_client_file.get().strip()
        if path and Path(path).is_file():
            try:
                with open(path,"r",encoding="utf-8") as stream:
                    config=json.load(stream)
                if config.get("installed",{}).get("client_id"):
                    return config
            except (OSError,ValueError,TypeError):
                pass
        raise AnalyticsError("consent_required")

    def _analytics_consent(self):
        profile=store.get_profile(self.active_id) if self.active_id else None
        if not profile or not profile.get("youtube_channel_id") or not profile.get("connected_email"):
            messagebox.showinfo("Google 연결 필요","먼저 이 프로필에 Google 계정과 YouTube 채널을 연결해 주세요.",parent=self);return
        if self.analytics_busy:return
        try: config=self._analytics_client_config()
        except (AnalyticsError,OAuthClientRegistryError):
            messagebox.showinfo("OAuth 설정 필요","먼저 OAuth 클라이언트를 등록하거나 선택해 주세요.",parent=self);return
        if not messagebox.askyesno("읽기 전용 성과 권한",
                "Google 로그인 창에서 YouTube Analytics 보고서 읽기 권한만 추가합니다. 기존 YouTube 연결 토큰은 덮어쓰지 않습니다. 계속할까요?",parent=self):return
        self.analytics_busy=True
        self.analytics_dashboard_text.set("Google 읽기 전용 권한을 기다리는 중입니다. 브라우저에서 연결할 계정을 확인해 주세요.")
        def worker():
            try:
                result=begin_analytics_consent(profile,config,approved=True,credential_manager=self.analytics_credentials)
                error="" if result.get("state")=="connected" else "동의가 취소되었습니다. 기존 연결은 유지됩니다."
            except AnalyticsError as exc:error=str(exc)
            except Exception:error="권한을 연결하지 못했습니다. 기존 계정 연결은 그대로 유지됩니다."
            self.after(0,lambda:self._analytics_consent_finished(profile["id"],error))
        threading.Thread(target=worker,name="zisu-analytics-consent",daemon=True).start()

    def _analytics_consent_finished(self,profile_id,error):
        self.analytics_busy=False
        if error:self.analytics_dashboard_text.set(error)
        else:self.analytics_dashboard_text.set("성과 읽기 권한이 이 채널 프로필에 안전하게 연결되었습니다. 다음으로 [성과 가져오기]를 선택하세요.")
        if self.active_id==profile_id:self._refresh_analytics_panel()

    def _analytics_fetch(self):
        profile=store.get_profile(self.active_id) if self.active_id else None
        if not profile or not profile.get("youtube_channel_id"):
            messagebox.showinfo("채널 연결 필요","성과를 확인할 YouTube 채널을 먼저 연결해 주세요.",parent=self);return
        if self.analytics_busy:return
        if not self.analytics_credentials.configured(profile["id"]):
            messagebox.showinfo("추가 동의 필요","먼저 [성과 읽기 권한 연결]을 눌러 Google의 추가 동의를 완료해 주세요. 기존 Google 연결은 변경되지 않습니다.",parent=self);return
        days=int(self.analytics_days.get())
        if not messagebox.askyesno("YouTube 성과 읽기",
                f"{profile['name']} 프로필의 최근 {days}일과 직전 같은 기간 성과를 Google에 읽기 전용으로 요청합니다. 최대 5개 보고서 요청이며 저장된 결과는 캐시에서 재사용합니다. 진행할까요?",parent=self):return
        profile_id=profile["id"];self.analytics_busy=True
        self.analytics_dashboard_text.set("성과를 읽고 있습니다. 이 작업은 다른 프로필과 분리 저장됩니다.")
        def worker():
            try:
                identity=oauth.quota_project_identity(profile_id) or "unknown-google-project"
                client=AnalyticsClient(lambda _pid:self.analytics_credentials.token_data(profile),
                    repository=self.analytics_repository,resource_guard=getattr(self,"resource_guard",None),
                    project_hash=project_scope_hash(identity))
                result=client.fetch(profile,days,use_cache=True)
                error=result.get("message","")
                if not error and result.get("state") not in {"data"}:error="성과를 가져오지 못했습니다."
            except AnalyticsError as exc:error=str(exc)
            except Exception:error="성과를 안전하게 가져오지 못했습니다. 기존 자료는 보존했습니다."
            self.after(0,lambda:self._analytics_fetch_finished(profile_id,error))
        threading.Thread(target=worker,name="zisu-analytics-fetch",daemon=True).start()

    def _analytics_fetch_finished(self,profile_id,error):
        self.analytics_busy=False
        if self.active_id==profile_id:
            self._refresh_analytics_panel()
            if error:self.analytics_dashboard_text.set(error)

    def _refresh_easy_step(self):
        if not hasattr(self,"easy_progress"):
            return
        profile=self.current_profile()
        self._dashboard_refresh()
        if not profile:
            self.easy_progress.set("1단계: 조사 채널을 만들거나 기존 채널을 선택하세요.")
            self.easy_next.configure(text="내 조사 채널 만들기",command=self.add_profile)
            return
        try:
            count=len(store.list_candidates(profile["id"]))
        except Exception:
            count=0
        if not oauth.is_connected(profile["id"]):
            self.easy_progress.set(f"현재 채널: {profile['name']}\n2단계: 공개 영상 검색을 위해 API 키를 등록하거나 Google 읽기 전용 연결을 진행하세요.")
            self.easy_next.configure(text="Google 연결하기",command=lambda:self.tabs.select(self.frames[0]))
        elif count <= 0:
            self.easy_progress.set(f"현재 채널: {profile['name']}\n3단계: 연결이 준비됐어요. 장르별 영상 검색을 시작하세요.")
            self.easy_next.configure(text="영상 검색 시작",command=lambda:self.tabs.select(self.frames[1]))
        else:
            self.easy_progress.set(f"현재 채널: {profile['name']} · 저장된 후보 {count}개\n다음 단계: AI 추천을 확인하거나 자동 조사를 시작하세요. 실제 청취 정답은 별도로 기록됩니다.")
            self.easy_next.configure(text="AI 자동 조사 열기",command=lambda:self.tabs.select(self.frames[7]))

    def _safe_diagnostic(self, code):
        try:
            target=store.data_dir()/"logs"/"ui_diagnostics.log"
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open("a",encoding="utf-8") as handle:
                handle.write(f"{datetime.now().astimezone().isoformat()} {code[:60]}\n")
        except OSError:
            pass

    def _report_ui_exception(self, exc_type, _value, _tb):
        # Deliberately omit exception text and traceback: providers may include secrets.
        self._safe_diagnostic(f"ui_callback_exception:{getattr(exc_type,'__name__','unknown')}")
        try:
            messagebox.showerror("화면에서 문제가 생겼어요", "작업 내용은 자동 저장했어요. 프로그램을 다시 열고 이전 작업 복원을 선택해 주세요. 자세한 진단은 로그에 안전하게 기록했습니다.", parent=self)
        except tk.TclError:
            pass

    def _install_thread_exception_handler(self):
        if not hasattr(threading,"excepthook"):
            return
        def safe_hook(args):
            self._safe_diagnostic(f"background_exception:{getattr(args.exc_type,'__name__','unknown')}")
        threading.excepthook=safe_hook

    def _ui_state_snapshot(self):
        try: tab_index=self.tabs.index(self.tabs.select())
        except (tk.TclError,ValueError): tab_index=0
        selected=()
        if hasattr(self,"candidates"):
            selected=self.candidates.selection()
        search_term=""
        if hasattr(self,"keyword_list"):
            search_term="\n".join(self.keyword_list.get(index) for index in self.keyword_list.curselection())
        report=getattr(self,"autopilot_saved_report",None) or {}
        return {"profile_id":self.active_id,"tab_index":tab_index,
                "selected_video_id":selected[0] if selected else None,
                "search_term":search_term,
                "filter":self.filter.get(),"decision_filter":self.decision_filter.get(),
                "sort":json.dumps(getattr(self,"_sort_state",{}).get("candidate"),ensure_ascii=False),
                "ai_target":self.ai_target.get() if hasattr(self,"ai_target") else "10",
                "ai_max_calls":self.ai_max_calls.get() if hasattr(self,"ai_max_calls") else "3",
                "ai_provider":self.ai_provider_choice.get() if hasattr(self,"ai_provider_choice") else "",
                "ai_cost_estimate":self.ai_cost_estimate.get() if hasattr(self,"ai_cost_estimate") else "",
                "ai_cost_limit":self.ai_cost_limit.get() if hasattr(self,"ai_cost_limit") else "",
                "last_report_session":report.get("session_id")}

    def _save_ui_state(self):
        if getattr(self,"_restoring_ui",False): return
        if self._ui_save_after:
            try:self.after_cancel(self._ui_save_after)
            except tk.TclError:pass
        self._ui_save_after=self.after(350,self._write_ui_state)

    def _write_ui_state(self):
        self._ui_save_after=None
        try:store.save_ui_state(self._ui_state_snapshot())
        except (OSError,sqlite3.Error,ValueError):self._safe_diagnostic("ui_state_save_failed")

    def _on_tab_changed(self,_event=None):
        self._save_ui_state(); self._refresh_easy_step()
        try: index=self.tabs.index(self.tabs.select())
        except (tk.TclError,ValueError): return
        if index==3:self.reload_candidates()
        elif index==7:self._ai_refresh_saved_results()
        elif index==8:self._autopilot_refresh_profile()
        elif index==9:self.reload_vph_radar()

    def _restore_ui_state(self):
        state=getattr(self,"_ui_state",{})
        if not state:return
        self._restoring_ui=True
        try:
            profile_id=state.get("profile_id")
            if profile_id and any(p["id"]==profile_id for p in self.profiles):
                self.reload_profiles(profile_id)
            self.filter.set(str(state.get("filter", "")))
            self.decision_filter.set(str(state.get("decision_filter", "전체")))
            if hasattr(self,"ai_target"):
                self.ai_target.set(str(state.get("ai_target","10")))
                self.ai_max_calls.set(str(state.get("ai_max_calls","3")))
                self.ai_cost_estimate.set(str(state.get("ai_cost_estimate",self.ai_cost_estimate.get())))
                self.ai_cost_limit.set(str(state.get("ai_cost_limit",self.ai_cost_limit.get())))
                provider=state.get("ai_provider")
                if provider in self.ai_provider_ids:self.ai_provider_choice.set(provider)
            if state.get("search_term") and hasattr(self,"keyword_list"):
                wanted=set(str(state["search_term"]).splitlines())
                for index in range(self.keyword_list.size()):
                    if self.keyword_list.get(index) in wanted:self.keyword_list.selection_set(index)
            try:
                index=max(0,min(int(state.get("tab_index",0)),len(self.tabs.tabs())-1))
                self.tabs.select(self.tabs.tabs()[index])
            except (ValueError,tk.TclError):pass
            self.reload_candidates()
            video_id=state.get("selected_video_id")
            if video_id and hasattr(self,"candidates") and self.candidates.exists(video_id):
                self.candidates.selection_set(video_id); self.candidates.see(video_id)
        finally:self._restoring_ui=False
        self._refresh_easy_step()
        self.after(500,self._offer_recovery)

    def _offer_recovery(self):
        profile=self.current_profile()
        if not profile:return
        sessions=store.list_resumable_sessions(profile["id"])
        if not sessions:return
        session=sessions[0]
        details=(f"저장 시각: {session.get('updated_at','확인 불가')}\n프로필: {profile['name']}\n"
                 f"조사 단계: {session.get('state','보류')} · 검색 {session.get('calls_used',0)}회\n"
                 "외부 검색은 자동으로 재시작하지 않으며, 이어가기를 누를 때만 다시 시작합니다.")
        win=tk.Toplevel(self);win.title("이전 작업이 발견되었습니다");win.transient(self);win.grab_set()
        win.geometry("520x290")
        ttk.Label(win,text="이전 작업이 발견되었습니다",font=("Malgun Gothic",16,"bold")).pack(anchor="w",padx=20,pady=(18,8))
        ttk.Label(win,text=details,wraplength=480,justify="left").pack(anchor="w",padx=20,pady=8)
        actions=ttk.Frame(win);actions.pack(fill="x",padx=14,pady=16)
        def continue_work():
            win.destroy();self.tabs.select(self.frames[7])
            if store.get_autopilot_settings(profile["id"],session["id"]):self._autopilot_resume()
            else:self._ai_resume_latest()
        def view_only():
            win.destroy();self.reload_candidates();self.tabs.select(self.frames[2])
        def new_search():
            win.destroy();self.tabs.select(self.frames[7])
            self.autopilot_stage_text.set("새 조사는 준비 화면만 열었습니다. 시작 버튼을 눌러야 외부 검색이 시작됩니다.")
        ttk.Button(actions,text="이어서 작업하기",command=continue_work,style="Accent.TButton").pack(side="left",padx=3)
        ttk.Button(actions,text="저장된 결과만 보기",command=view_only).pack(side="left",padx=3)
        ttk.Button(actions,text="새 조사 시작",command=new_search).pack(side="left",padx=3)

    def _sort_tree(self, tree, col, reverse, key_name):
        rows=[]
        for iid in tree.get_children(""):
            values=tree.item(iid,"values")
            try:index=list(tree["columns"]).index(col); value=values[index]
            except (ValueError,IndexError):value=""
            rows.append((value,iid))
        def sort_key(pair):
            value=pair[0]
            try:return (0,float(value))
            except (TypeError,ValueError):return (1,str(value).casefold())
        for pos,(_,iid) in enumerate(sorted(rows,key=sort_key,reverse=reverse)):
            tree.move(iid,"",pos)
        self._sort_state=getattr(self,"_sort_state",{})
        self._sort_state[key_name]=(col,reverse)
        for column in tree["columns"]:
            text=tree.heading(column,"text").rstrip(" ↑↓")
            tree.heading(column,text=text+(" ↓" if column==col and reverse else " ↑" if column==col else ""))
        self._save_ui_state()

    def _sort_tree_click(self, tree, col, key_name):
        previous=getattr(self,"_sort_state",{}).get(key_name)
        reverse=(not previous[1]) if previous and previous[0]==col else False
        self._sort_tree(tree,col,reverse,key_name)

    def open_google_wizard(self):
        steps=[("Google 계정 준비","연결할 Google 계정으로 로그인 준비를 합니다."),
               ("Cloud 프로젝트 선택/생성","보통 기존 ZISU 프로젝트 하나를 여러 Google 계정에서 재사용할 수 있어요."),
               ("YouTube Data API v3 활성화","Cloud Console에서 API 및 서비스 > 라이브러리에서 YouTube Data API v3를 사용 설정합니다."),
               ("인증 플랫폼 설정","Google Auth Platform에서 앱 이름과 사용자 유형을 확인합니다."),
               ("테스트 사용자 등록","앱이 테스트 중이면 Audience > Test users에 연결 계정을 추가해야 할 수 있어요."),
               ("데스크톱 OAuth 클라이언트 확인/생성","OAuth 클라이언트 유형은 데스크톱 앱이어야 합니다."),
               ("OAuth JSON 다운로드","클라이언트 JSON은 비밀정보예요. 다른 사람과 공유하지 마세요."),
               ("ZISU에 JSON 등록","파일 선택으로 등록합니다. 이미 등록된 클라이언트는 다시 추가할 필요가 없습니다."),
               ("브라우저에서 Google 로그인","직접 계정을 선택하고 읽기 전용 권한을 승인합니다."),
               ("계정/채널 확인","표시된 이메일과 채널이 연결하려던 계정인지 직접 확인합니다. 채널 없는 조사 계정도 가능합니다.")]
        links=["https://accounts.google.com/","https://console.cloud.google.com/","https://console.cloud.google.com/apis/library/youtube.googleapis.com",
               "https://console.cloud.google.com/auth/overview","https://console.cloud.google.com/auth/audience",
               "https://console.cloud.google.com/auth/clients","https://console.cloud.google.com/auth/clients"]
        win=tk.Toplevel(self);win.title("Google 연결 도우미");win.geometry("650x340");win.transient(self)
        index=tk.IntVar(value=0); title=tk.StringVar(); body=tk.StringVar()
        ttk.Label(win,textvariable=title,font=("Malgun Gothic",16,"bold")).pack(anchor="w",padx=20,pady=(20,8))
        ttk.Label(win,textvariable=body,wraplength=590).pack(anchor="w",padx=20,pady=8)
        ttk.Label(win,text="직접 완료했을 때만 다음 단계로 이동하세요. ZISU는 Google Cloud를 대신 설정하지 않습니다.",wraplength=590).pack(anchor="w",padx=20,pady=8)
        controls=ttk.Frame(win);controls.pack(fill="x",side="bottom",padx=16,pady=16)
        def show():
            i=index.get();title.set(f"단계 {i+1}/10 · {steps[i][0]}");body.set(steps[i][1])
        def prev():index.set(max(0,index.get()-1));show()
        def nxt():
            if index.get()<7:index.set(index.get()+1);show()
            elif index.get()==7:
                self.choose_client_file();index.set(8);show()
            elif index.get()==8:
                if messagebox.askyesno("Google 연결", "브라우저에서 계정을 선택하고 읽기 전용 승인을 진행할까요?",parent=win):self.connect_google()
                index.set(9);show()
            else:
                self.refresh_account_status();win.destroy()
        ttk.Button(controls,text="이전",command=prev).pack(side="left")
        if links:
            ttk.Button(controls,text="공식 설정 페이지 열기",command=lambda:webbrowser.open(links[min(index.get(),len(links)-1)])).pack(side="left",padx=8)
        ttk.Button(controls,text="다음",command=nxt,style="Accent.TButton").pack(side="right")
        win.bind("<Escape>",lambda _e:win.destroy())
        win.bind("<Return>",lambda _e:nxt())
        show()

    def reload_profiles(self,select_id=None):
        self.profiles=store.list_profiles()
        values=[f'{x["name"]}  |  {x["market"]}  |  {_genre_title_for_profile(x)}' for x in self.profiles]
        self.profile_combo["values"]=values
        ids=[x["id"] for x in self.profiles]
        if not ids:
            self.active_id=None
            self.profile_choice.set("먼저 작업 프로필을 추가하세요")
            self.profile_description.set("국가 → 계정 → 읽기 전용 연결 → 장르 → 검색")
            return
        chosen=select_id if select_id in ids else (self.active_id if self.active_id in ids else ids[0])
        self.profile_combo.current(ids.index(chosen))
        self.select_profile()

    def current_profile(self):
        return store.get_profile(self.active_id) if self.active_id else None

    def select_profile(self):
        i=self.profile_combo.current()
        if i<0 or i>=len(self.profiles): return
        profile=self.profiles[i]
        if getattr(self,"comment_video",None):self._autosave_comment()
        self.active_id=profile["id"]
        if hasattr(self,"ai_tree") and getattr(self,"ai_expected_profile",None) != self.active_id:
            self.ai_tree.delete(*self.ai_tree.get_children())
            self.ai_reviews={}
            self.ai_candidate_rows={}
            self.ai_confirmed_plan=None
            self.ai_status.set("프로필이 바뀌었습니다. 현재 프로필 기준으로 새 조사 계획을 미리보세요.")
        self.selected_video=None
        self.comment_video=None
        self.comment_label.set("영상 선택 없음")
        state = "Google 연결됨" if oauth.is_connected(self.active_id) else "연결 전"
        config=store.profile_genre_config(profile)
        related=store.normalize_related_genres(profile["genre"],profile.get("related_genres_json","[]"),config["custom_genres"])
        related_text=", ".join(genre_engine.genre_title(key,config["custom_genres"]) for key in related) if related else "없음"
        core_title=_genre_title_for_profile(profile)
        config=store.profile_genre_config(profile)
        subgenre_text=", ".join(config["subgenres"]) if config["subgenres"] else "없음"
        self.profile_description.set(f'{profile["market"]} · 핵심 {core_title} · 세부 {subgenre_text} · 유사 {related_text} · {state}')
        account = profile.get("connected_email") or "Google 미연결"
        channel = profile.get("youtube_channel_title") or "채널 정보 없음"
        self.active_account_banner.set(
            f"현재 작업: {profile['name']} | {profile['market']} | 핵심 {core_title} | 유사 {related_text} | {account} | {channel}")
        self.refresh_account_status()
        self._reload_terms()
        self.reload_candidates()
        self.reload_metrics()
        self.schedule_text.set(profile["schedule"])
        self._autopilot_refresh_profile()
        if hasattr(self,"vph_profile_combo"):
            self._sync_vph_profile_choice()
            self.reload_vph_radar()
        self._save_ui_state()
        self._refresh_easy_step()
        self.footer.set("채널을 전환했습니다. 다른 채널의 검색 후보와 성과는 분리됩니다.")

    def add_profile(self):
        dialog=ProfileDialog(self)
        if dialog.result:
            try:
                name,market,genre,related,genre_config=dialog.result
                ident=store.create_profile(name,market,genre,related_genres=related,genre_config=genre_config)
                self.reload_profiles(ident)
                self.tabs.select(self.frames[0])
            except Exception as exc: messagebox.showerror("채널 추가 실패",str(exc))

    def start_onboarding(self):
        """First-screen, beginner-safe path into profile and OAuth setup."""
        messagebox.showinfo(
            "ZISU V4 시작 마법사",
            "다섯 단계로 시작합니다.\n\n"
            "1. 조사 국가 선택\n2. 작업 채널 또는 조사 계정 추가\n"
            "3. Google 읽기 전용 연결\n4. 핵심 장르 선택\n5. KR/JP 검색 시작\n\n"
            "비밀번호는 입력받지 않으며 YouTube 채널이 없는 조사 계정도 연결할 수 있습니다.",
            parent=self,
        )
        self.add_profile()
        if self.active_id:
            self.tabs.select(self.frames[0])
            self.footer.set("다음: OAuth JSON을 선택하고 Google 읽기 전용 연결을 완료하세요.")

    def edit_profile(self):
        old=self.current_profile()
        dialog=ProfileDialog(self,old)
        if dialog.result:
            try:
                name,market,genre,related,genre_config=dialog.result
                store.update_profile(old["id"],name=name,market=market,genre=genre,related_genres=related,genre_config=genre_config)
                self.reload_profiles(old["id"])
            except Exception as exc: messagebox.showerror("채널 수정 실패",str(exc))

    def _account_ui(self, tab):
        tab.columnconfigure(0,weight=1)
        ttk.Button(tab,text="Google 연결 도우미 · 10단계",command=self.open_google_wizard,style="Accent.TButton").grid(row=0,column=1,sticky="e",pady=8)
        ttk.Label(tab,text="첫 단계: 내 YouTube 채널을 안전하게 연결하세요.",
                  font=("Malgun Gothic",16,"bold")).grid(row=0,column=0,sticky="w",pady=(6,10))
        ttk.Label(tab,text="Google OAuth 로그인 · 조회 전용 권한 · 비밀번호 입력/댓글 게시/구독 실행 권한 없음").grid(row=1,column=0,sticky="w",pady=4)
        panel=ttk.LabelFrame(tab,text="연결할 채널 (위의 작업 프로필별 독립 연결)",padding=20)
        panel.grid(row=2,column=0,sticky="ew",pady=14)
        panel.columnconfigure(0,weight=1)
        ttk.Label(panel,textvariable=self.oauth_status,font=("Malgun Gothic",12,"bold"),wraplength=950).grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,13))
        ttk.Button(panel,text="OAuth 클라이언트 등록 (최초 1회)",command=self.register_oauth_client).grid(row=1,column=0,sticky="w",pady=6)
        ttk.Button(panel,text="기존 JSON 파일 경로 사용",command=self.choose_client_file).grid(row=1,column=1,sticky="w",pady=6,padx=8)
        self.oauth_client_combo=ttk.Combobox(panel,textvariable=self.oauth_client_choice,state="readonly",width=38)
        self.oauth_client_combo.grid(row=1,column=2,sticky="w",pady=6)
        ttk.Button(panel,text="2. Google 계정 추가 / 연결",command=self.connect_google,
                   style="Accent.TButton").grid(row=2,column=0,sticky="w",pady=6)
        ttk.Button(panel,text="연결 상태 새로고침",command=self.refresh_account_status).grid(row=3,column=0,sticky="w",pady=8)
        ttk.Button(panel,text="선택 프로필 연결 해제",command=self.disconnect_google).grid(row=3,column=1,sticky="w",pady=8,padx=15)
        ttk.Label(panel,textvariable=self.oauth_client_file,wraplength=800,foreground="#697A88").grid(row=4,column=0,columnspan=3,sticky="w",pady=7)
        ttk.Label(panel,text="Google 계정 → 인증된 YouTube 채널 → 연결 프로필 (각 프로필 토큰은 별도 저장)").grid(row=5,column=0,columnspan=3,sticky="w",pady=(8,2))
        ttk.Label(panel,textvariable=self.oauth_accounts_summary,wraplength=950,justify="left").grid(row=6,column=0,columnspan=3,sticky="w",pady=3)
        ttk.Button(panel,text="Google Cloud 없이 먼저 유튜브 열기 (브라우저 로그인만)",
                   command=lambda:webbrowser.open("https://www.youtube.com/",new=2)).grid(row=7,column=0,columnspan=3,sticky="w",pady=7)
        ttk.Label(tab,text="연결을 위한 준비 (최초 1회)",font=("Malgun Gothic",12,"bold")).grid(row=3,column=0,sticky="w",pady=(14,7))
        instructions=("① 'OAuth_모듈_설치하기.bat' 더블클릭 (최초 1회)\n"
                      "② Google Cloud에서 YouTube Data API v3 사용 설정\n"
                      "③ 보통 Google Cloud 프로젝트 하나와 Desktop OAuth JSON 하나로 여러 Google 계정을 연결할 수 있습니다.\n"
                      "   JSON은 [OAuth 클라이언트 등록]으로 한 번 등록해 Windows 사용자 DPAPI에 보관하고 재사용할 수 있습니다.\n"
                      "④ 테스트 모드라면 연결할 구글 계정을 테스트 사용자에 추가\n"
                      "⑤ 등록된 OAuth 클라이언트를 선택하고 [Google 계정 추가 / 연결]을 클릭\n"
                      "⑥ 브라우저에서 연결할 Google 계정을 선택 → 이메일·채널 확인 후 승인\n"
                      "    (유튜브 채널을 아직 만들지 않은 조사 전용 구글 계정도 연결 가능)\n\n"
                      "간편 체험: 위 '유튜브 열기'로 새 계정에 로그인하고 웹 검색만 먼저 써도 됩니다.\n"
                      "(브라우저 로그인은 앱의 OAuth 계정 연결과 별개입니다.)\n"
                      "한 Google 계정에 접근 가능한 여러 채널은 각각 별도 프로필에 연결할 수 있습니다. 같은 계정의 각 프로필도 다시 승인해야 하며 토큰은 프로필별로 따로 저장됩니다. 토큰을 자동 복사하지 않습니다.\n"
                      "중요: JP/KR 지역 검색은 해당 지역에서 볼 수 있는 영상을 찾는 것으로, 현지 운영자 인증이 아닙니다.\n"
                      "계정 연결은 유튜브 홈 추천, 접속 IP, 시청자 지역을 변경하지 않습니다.\n"
                      "댓글·좋아요·구독·영상 자동재생은 실행되지 않습니다.")
        info=tk.Text(tab,height=15,wrap="word",font=("Malgun Gothic",10),padx=14,pady=12,background="white")
        info.grid(row=4,column=0,sticky="nsew",pady=4)
        info.insert("1.0",instructions)
        info.configure(state="disabled")
        row=ttk.Frame(tab)
        row.grid(row=5,column=0,sticky="w",pady=12)
        ttk.Button(row,text="Google Cloud 설정 열기",command=lambda:webbrowser.open("https://console.cloud.google.com/apis/credentials")).pack(side="left",padx=(0,10))
        ttk.Button(row,text="공식 OAuth 안내 열기",command=lambda:webbrowser.open("https://developers.google.com/youtube/v3/guides/auth/installed-apps")).pack(side="left")
        self._refresh_oauth_clients()
        tab.rowconfigure(4,weight=1)

    def choose_client_file(self):
        path=filedialog.askopenfilename(title="Google Cloud '데스크톱 앱' OAuth JSON 선택",filetypes=[("Google OAuth JSON","*.json")])
        if path:
            self.oauth_client_file.set(path)

    def _refresh_oauth_clients(self):
        if not hasattr(self,"oauth_client_combo"):
            return
        try:
            rows=self.oauth_registry.list_clients()
        except OAuthClientRegistryError:
            rows=[]
        self.oauth_client_ids={row["label"]+" · "+row["id"]:row["id"] for row in rows}
        values=list(self.oauth_client_ids)
        self.oauth_client_combo.configure(values=values)
        if values and self.oauth_client_choice.get() not in values:
            self.oauth_client_choice.set(values[0])
        elif not values:
            self.oauth_client_choice.set("")

    def register_oauth_client(self):
        path=filedialog.askopenfilename(title="등록할 Desktop OAuth JSON 선택",filetypes=[("Google OAuth JSON","*.json")])
        if not path:
            return
        if not messagebox.askyesno("OAuth 클라이언트 저장 승인",
                "선택한 Desktop OAuth 클라이언트 JSON을 현재 Windows 사용자 전용 DPAPI 저장소에 암호화해 보관합니다.\n"
                "JSON은 소스 폴더나 Git에 복사되지 않습니다. 등록하시겠습니까?",parent=self):
            return
        try:
            registered=self.oauth_registry.register_file(path,approved=True)
            self._refresh_oauth_clients()
            for label,ident in self.oauth_client_ids.items():
                if ident==registered["id"]:
                    self.oauth_client_choice.set(label)
            self.oauth_client_file.set("보안 저장된 공통 OAuth 클라이언트: "+registered["label"])
            messagebox.showinfo("등록 완료","이 OAuth 클라이언트를 여러 Google 계정 연결에 재사용할 수 있습니다. 계정별 토큰은 프로필마다 따로 저장됩니다.",parent=self)
        except OAuthClientRegistryError as exc:
            messagebox.showerror("등록 실패",str(exc),parent=self)

    def refresh_account_status(self):
        if not self.active_id or not hasattr(self,'oauth_status'):return
        profile=store.get_profile(self.active_id)
        if not profile:return
        if oauth.is_connected(self.active_id):
            self.oauth_status.set("연결됨: " + (profile.get('youtube_channel_title') or '(채널명 불명)')
                + "  |  계정: " + (profile.get('connected_email') or '이메일 확인 안 됨')
                + "  |  채널 ID: " + (profile.get('youtube_channel_id') or '조사 전용')
                + "  |  대상: " + profile['market'] + "  |  조회 전용")
        else:
            self.oauth_status.set("미연결: " + profile['name'] + " · " + profile['market']
                + " · " + _genre_title_for_profile(profile))
        accounts=[]
        for account_row in store.list_google_accounts():
            connected_profiles=[row for row in account_row["profiles"] if oauth.is_connected(row["id"])]
            if not connected_profiles:
                continue
            channel_rows=account_row["channels"]
            channel_text="; ".join(
                f"{item.get('channel_title') or '이름 없음'} [{item.get('channel_id') or '채널 없음'}]"
                for item in channel_rows) or "채널 정보 없음"
            profile_text=", ".join(
                f"{item['name']} ({item['market']})" for item in connected_profiles)
            accounts.append(f"{account_row.get('email') or '이메일 확인 안 됨'}\n  채널: {channel_text}\n  연결 프로필: {profile_text}")
        self.oauth_accounts_summary.set("\n".join(accounts) if accounts else "연결된 Google 계정 없음 · 프로필을 선택하고 Google 계정 추가 / 연결을 누르세요.")

    def connect_google(self):
        if self.oauth_busy:return
        profile=store.get_profile(self.active_id)
        if not profile:return
        client_file=self.oauth_client_file.get().strip()
        selected_label=self.oauth_client_choice.get()
        selected_id=self.oauth_client_ids.get(selected_label)
        client_config=None
        if selected_id:
            try:
                client_config=self.oauth_registry.load_config(selected_id)
            except OAuthClientRegistryError as exc:
                messagebox.showerror("OAuth 클라이언트 확인",str(exc),parent=self)
                return
        if not client_config and (not client_file or not Path(client_file).is_file()):
            messagebox.showinfo("준비 필요","먼저 Google Cloud 데스크톱 앱 OAuth JSON을 선택하세요.")
            return
        profile_id=profile['id']
        self.oauth_busy=True
        self.oauth_status.set("브라우저에서 Google 로그인 중... 새 계정을 직접 선택하세요.")
        def worker():
            try:
                channels,token_json=oauth.begin_connect(client_file or None,client_config=client_config)
                error=None
            except oauth.OAuthError as exc:
                channels,token_json,error=[],None,f"[{exc.code} / {exc.stage}] {exc}"
            except Exception as exc:
                channels,token_json,error=[],None,"[unknown / oauth_callback] Google 인증을 완료하지 못했습니다. 민감한 진단 정보는 표시하지 않았습니다."
            def finish():
                self.oauth_busy=False
                if error:
                    self.refresh_account_status()
                    messagebox.showerror("Google 계정 연결 실패",error)
                    return
                if len(channels)==1:
                    chosen=channels[0]
                else:
                    lines="\n".join(str(i+1)+". "+c['channel_title'] for i,c in enumerate(channels))
                    idx=simpledialog.askinteger("채널 선택","인증된 채널이 여러 개입니다. 연결할 번호를 입력하세요:\n"+lines,
                                                 parent=self,minvalue=1,maxvalue=len(channels))
                    if idx is None:return
                    chosen=channels[idx-1]
                if not messagebox.askyesno("계정 확인",
                    "이 프로필에 아래 실제 유튜브 채널을 연결할까요?\n\n"
                    +chosen['channel_title']+"\n"+(chosen['email'] or '(이메일 확인 안 됨)')+"\n"+(chosen['channel_id'] or '(조사용 Google 계정)')
                    +"\n\n다른 계정이면 취소 후 새 계정으로 다시 로그인하세요."):
                    return
                existing=store.get_profile(profile_id) or {}
                identity_changed=(
                    (existing.get("connected_email") and chosen.get("email") and
                     existing["connected_email"].casefold()!=chosen["email"].casefold()) or
                    (existing.get("youtube_channel_id") and
                     existing.get("youtube_channel_id")!=chosen.get("channel_id",""))
                )
                allow_account_change=False
                if identity_changed:
                    allow_account_change=messagebox.askyesno(
                        "연결 계정 변경 확인",
                        "이 프로필은 이미 다른 Google 계정 또는 YouTube 채널에 연결되어 있습니다.\n"
                        "기존 후보·메모·사람 검토·성과 데이터는 유지하고, 이 프로필의 인증 연결만 새 계정으로 바꿀까요?",
                        parent=self)
                    if not allow_account_change:
                        return
                try:
                    oauth.save_connection(profile_id,chosen,token_json,allow_account_change=allow_account_change)
                    self.reload_profiles(profile_id)
                    self.tabs.select(self.frames[1])
                    messagebox.showinfo("연결 성공","읽기 전용으로 계정이 연결되었습니다. 이제 장르별 검색을 시작하세요.")
                except (oauth.OAuthError,ValueError,OSError) as exc:
                    messagebox.showerror("연결 정보 저장 실패",str(exc))
            self.after(0,finish)
        threading.Thread(target=worker,daemon=True).start()

    def disconnect_google(self):
        if not self.active_id:return
        if not messagebox.askyesno("연결 해제","선택한 채널의 Google 인증을 해제할까요?\n후보·메모·성과는 삭제되지 않습니다."):
            return
        try:
            oauth.disconnect(self.active_id)
            self.reload_profiles(self.active_id)
            self.footer.set("Google 연결 토큰만 삭제했습니다. 기존 후보와 메모는 유지됩니다.")
        except (OSError,ValueError) as exc:
            messagebox.showerror("연결 해제 실패",str(exc))

    def _save_youtube_key(self):
        key=self.api_key.get().strip()
        try:
            self.credential_store.save("youtube-data",key)
            messagebox.showinfo("저장 완료","공개 YouTube Data API 키를 현재 Windows 사용자 DPAPI로 암호화해 저장했습니다. 키는 화면에 다시 표시하지 않습니다.",parent=self)
        except CredentialError as exc:
            self.api_key.set("")
            messagebox.showwarning("안전 저장 실패",str(exc),parent=self)

    def _delete_youtube_key(self):
        try:
            self.credential_store.delete("youtube-data")
            self.api_key.set("")
            messagebox.showinfo("삭제 완료","저장된 공개 YouTube API 키를 삭제했습니다. Google OAuth 토큰은 변경하지 않았습니다.",parent=self)
        except CredentialError as exc:
            messagebox.showwarning("삭제 실패",str(exc),parent=self)

    def _search_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(3,weight=1)
        panel=ttk.Frame(tab)
        panel.grid(row=0,column=0,sticky="ew",pady=8)
        ttk.Label(panel,text="공개 영상 검색용 YouTube API 키 (OAuth 계정 연결과 별도)").pack(side="left")
        ttk.Entry(panel,textvariable=self.api_key,width=40,show="●").pack(side="left",padx=10)
        ttk.Button(panel,text="현재 키 DPAPI 저장",command=self._save_youtube_key).pack(side="left",padx=3)
        ttk.Button(panel,text="저장 키 삭제",command=self._delete_youtube_key).pack(side="left",padx=3)
        ttk.Label(panel,text="연결 계정은 읽기 전용 · 계정이 없으면 웹 검색 가능").pack(side="left")
        ttk.Label(tab,textvariable=self.active_account_banner,font=("Malgun Gothic",11,"bold"),
                  foreground="#9A3412",wraplength=1100).grid(row=1,column=0,sticky="w",pady=(4,1))
        ttk.Label(tab,text="선택한 채널의 국가/장르만 검색합니다. 일본어 검색 = 일본인 운영 채널 인증은 아닙니다.").grid(row=2,column=0,sticky="w",pady=6)
        pane=ttk.Panedwindow(tab,orient="horizontal")
        pane.grid(row=3,column=0,sticky="nsew")
        left=ttk.Frame(pane)
        right=ttk.Frame(pane)
        pane.add(left,weight=2)
        pane.add(right,weight=4)
        ttk.Label(left,text="전용 키워드 (1~3개 선택)").pack(anchor="w",pady=8)
        self.keyword_list=tk.Listbox(left,selectmode=tk.EXTENDED,exportselection=False,
                                     activestyle="dotbox",font=("Malgun Gothic",11),bg="white",height=18)
        self.keyword_list.pack(fill="both",expand=True,padx=(0,12))
        self.keyword_list.bind("<<ListboxSelect>>",lambda _e:self._save_ui_state())
        ttk.Button(left,text="1. 유튜브 검색 열기",command=self.open_youtube).pack(fill="x",pady=6,padx=(0,12))
        ttk.Button(left,text="2. API로 후보 자동 수집",command=self.api_search,style="Accent.TButton").pack(fill="x",padx=(0,12))
        ttk.Label(right,textvariable=self.search_status,wraplength=650).pack(anchor="w",pady=8)
        self.search_tree=ttk.Treeview(right,columns=("name","title","score","country"),show="headings",height=16)
        for col,heading,w in (("name","채널",170),("title","영상",375),("score","장르",95),("country","국가*",70)):
            self.search_tree.heading(col,text=heading)
            self.search_tree.column(col,width=w,stretch=col=="title")
        self.search_tree.pack(fill="both",expand=True)
        self.search_tree.bind("<Double-1>",lambda e:self._open_search_video())
        ttk.Button(right,text="결과 영상 열기",command=self._open_search_video).pack(anchor="e",pady=9)
        ttk.Label(right,text="* 공개 채널 국가 정보이며 실제 운영자의 국적·거주지를 증명하지 않습니다.").pack(anchor="w")

    def _reload_terms(self):
        if not hasattr(self,"keyword_list") or not self.active_id:return
        profile=self.current_profile()
        terms,core_count,related_count=ai_research._profile_query_plan(profile,12)
        self.keyword_list.delete(0,tk.END)
        for item in terms:self.keyword_list.insert(tk.END,item)
        self.keyword_list.select_set(0)
        self.search_tree.delete(*self.search_tree.get_children())
        self.search_status.set(f"{profile['market']} / 핵심 {_genre_title_for_profile(profile)} 검색어 {core_count}개 + 유사 장르 검색어 {related_count}개 준비됨")

    def selected_terms(self):
        ids=self.keyword_list.curselection()
        return [self.keyword_list.get(i) for i in ids[:3]] or [self.keyword_list.get(0)]

    def open_youtube(self):
        for term in self.selected_terms()[:3]:
            webbrowser.open("https://www.youtube.com/results?"+urlencode({"search_query":term}),new=2)
        self.footer.set("브라우저에서 실제로 음악을 확인한 뒤 영상 URL을 수동 등록할 수 있습니다.")

    def api_search(self,scheduled=False,profile_id=None,keywords=None):
        if self.search_inflight:return
        profile=store.get_profile(profile_id or self.active_id)
        if not profile:return
        key=self.api_key.get().strip()
        if not key and not oauth.is_connected(profile["id"]):
            if not scheduled:messagebox.showinfo("연결 필요","① 계정 연결에서 본인의 구글 계정을 연결하거나 API 키를 입력해 주세요.")
            return
        words=(keywords if keywords is not None else self.selected_terms())[:3]
        self.search_inflight=True
        self.search_status.set(f"{profile['name']} 검색 진행 중... (장르 필터 적용)")
        def worker():
            collected=[]
            error=""
            try:
                token = oauth.get_access_token(profile["id"]) if not key else None
                for term in words:
                    collected.extend(lookup_videos(key,term,profile["market"],limit=12,access_token=token,
                        quota_context={"profile_id":profile["id"],"path":store.db_path(),
                            "project_identity":quota_project_identity(key,
                                (oauth.quota_project_identity(profile["id"]) or "oauth-project:unknown") if not key else "")}))
                unique={r["video_id"]:r for r in collected}
                counts=store.save_candidates(profile["id"],list(unique.values()))
            except (APIError,oauth.OAuthError,ValueError,OSError) as exc:
                error=str(exc)
                counts={"적합":0,"검토":0,"제외":0}
                unique={}
            def complete():
                self.search_inflight=False
                if self.active_id==profile["id"]:
                    self.search_tree.delete(*self.search_tree.get_children())
                    for row in unique.values():
                        config=store.profile_genre_config(profile)
                        decision=assess(profile["genre"],row.get("video_title",""),row.get("description",""),config["custom_genres"])
                        if decision.decision=="제외":continue
                        self.search_tree.insert("","end",iid=row["video_id"],values=(row.get("channel_title",""),row.get("video_title",""),
                            f"{decision.decision} {decision.score}",row.get("country") or "미공개"))
                    self.reload_candidates()
                    self.search_status.set(f"장르 적합 {counts['적합']} · 검토 보류 {counts['검토']} · 충돌 제외 {counts['제외']}")
                if error and not scheduled:messagebox.showwarning("검색 실패",error)
            self.after(0,complete)
        threading.Thread(target=worker,daemon=True).start()

    def _open_search_video(self):
        ids=self.search_tree.selection()
        if ids:webbrowser.open("https://www.youtube.com/watch?v="+ids[0],new=2)

    def _candidate_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(1,weight=2)
        head=ttk.Frame(tab)
        head.grid(row=0,column=0,sticky="ew")
        ttk.Label(head,text="검색").pack(side="left")
        ttk.Entry(head,textvariable=self.filter,width=25).pack(side="left",padx=7)
        ttk.Label(head,text="장르 판정").pack(side="left",padx=(12,5))
        ttk.Combobox(head,textvariable=self.decision_filter,values=["전체","적합","검토","수동 승인"],width=12,state="readonly").pack(side="left")
        ttk.Button(head,text="조회",command=self.reload_candidates).pack(side="left",padx=7)
        ttk.Button(head,text="장르 정확도 검증",command=self.open_genre_audit).pack(side="left",padx=7)
        ttk.Button(head,text="+ 영상 수동 등록",command=self.manual_add).pack(side="right")
        self.candidates=ttk.Treeview(tab,columns=("channel","title","verdict","score","status"),show="headings")
        for col,title,w in (("channel","채널",190),("title","영상 제목",410),("verdict","장르",100),("score","점수",75),("status","메모상태",90)):
            self.candidates.heading(col,text=title,command=lambda c=col:self._sort_tree_click(self.candidates,c,"candidate"))
            self.candidates.column(col,width=w,stretch=col=="title")
        self.candidates.grid(row=1,column=0,sticky="nsew",pady=10)
        self.candidates.bind("<<TreeviewSelect>>",lambda e:self.selected_candidate())
        detail=ttk.LabelFrame(tab,text="실제 감상 후 기록 · 장르 검토",padding=12)
        detail.grid(row=2,column=0,sticky="ew")
        detail.columnconfigure(0,weight=1)
        self.genre_reason=tk.StringVar(value="후보를 선택하면 장르 판정 근거가 표시됩니다.")
        ttk.Label(detail,textvariable=self.genre_reason).grid(row=0,column=0,columnspan=2,sticky="w",pady=(0,6))
        self.notes=tk.Text(detail,height=4,wrap="word",font=("Malgun Gothic",10))
        self.notes.grid(row=1,column=0,columnspan=2,sticky="ew",pady=4)
        self.notes.bind("<KeyRelease>",lambda _e:self._schedule_note_save())
        actions=ttk.Frame(detail)
        actions.grid(row=2,column=0,columnspan=2,sticky="ew",pady=6)
        self.item_status=tk.StringVar(value="미확인")
        ttk.Combobox(actions,textvariable=self.item_status,values=["미확인","검토 중","보류","완료"],state="readonly",width=10).pack(side="left")
        for text,method in (("영상 열기",self.open_candidate),("메모 저장",self.save_candidate),("장르 수동 승인",self.approve_candidate),("댓글 초안",self.go_comment)):
            ttk.Button(actions,text=text,command=method).pack(side="left",padx=4)
        ttk.Label(actions,text="분석 알림").pack(side="left",padx=(12,4))
        for m in (10,15,20):ttk.Button(actions,text=f"{m}분",command=lambda mins=m:self.start_timer(mins)).pack(side="left",padx=2)
        ttk.Button(actions,text="랜덤 분석 알림: 10~20분",command=self.start_random_timer).pack(side="left",padx=4)
        ttk.Label(actions,textvariable=self.timer_text).pack(side="right")

    def reload_candidates(self):
        if not hasattr(self,"candidates") or not self.active_id:return
        selected=self.candidates.selection()
        selected_id=selected[0] if selected else getattr(self,"selected_video",None)
        rows=store.list_candidates(self.active_id,self.filter.get(),self.decision_filter.get())
        self.candidates.delete(*self.candidates.get_children())
        for item in rows:
            self.candidates.insert("","end",iid=item["video_id"],values=(item["channel_title"],item["video_title"],item["genre_decision"],item["genre_score"],item["status"]))
        if selected_id and self.candidates.exists(selected_id):
            self.candidates.selection_set(selected_id)
        self.genre_reason.set(f"현재 채널 후보 {len(rows)}개 · 제외 판정은 정확도 검증 화면에 보존")

    def open_genre_audit(self):
        if not self.active_id:return
        window=tk.Toplevel(self)
        window.title("장르 정확도 검증 · 사람 정답 라벨")
        window.geometry("1120x680")
        window.transient(self)
        top=ttk.Frame(window,padding=12); top.pack(fill="x")
        quality_var=tk.StringVar()
        ttk.Label(top,textvariable=quality_var,font=("Malgun Gothic",11,"bold")).pack(side="left")
        tree=ttk.Treeview(window,columns=("title","predicted","score","ai","proposal","human","reason"),show="headings")
        for col,title,width in (("title","영상 제목",360),("predicted","자동 판정",90),("score","점수",60),
                                ("human","사람 정답",90),("reason","판정 이유",400)):
            tree.heading(col,text=title,command=lambda c=col,t=tree:self._sort_tree_click(t,c,"audit")); tree.column(col,width=width,stretch=col in ("title","reason"))
        for col,title,width in (("ai","AI 의견",80),("proposal","추천 구분",120)):
            tree.heading(col,text=title,command=lambda c=col,t=tree:self._sort_tree_click(t,c,"audit")); tree.column(col,width=width,stretch=False)
        tree.pack(fill="both",expand=True,padx=12,pady=8)
        notes=tk.Text(window,height=4,wrap="word",font=("Malgun Gothic",10)); notes.pack(fill="x",padx=12,pady=6)
        def reload_audit():
            tree.delete(*tree.get_children())
            for row in store.list_genre_audit(self.active_id):
                tree.insert("","end",iid=row["video_id"],values=(row["video_title"],row["predicted_decision"],
                    row["predicted_score"],row["human_label"] or "미라벨",row["reason"]))
            for row in store.list_genre_audit(self.active_id):
                tree.item(row["video_id"],values=(row["video_title"],row["rule_verdict"] or row["predicted_decision"],
                    row["predicted_score"],row["ai_verdict"],ai_research.PROPOSED_VERDICT_LABELS.get(row["proposed_verdict"],row["proposed_verdict"]),
                    row["human_label"] or ("확인 필요" if row["review_status"]=="needs_review" else row["review_status"]),
                    (row["evidence"] or row["reason"])+" · "+(row["uncertainty"] or "")))
            q=store.genre_quality(self.active_id)
            precision="측정 전" if q["precision"] is None else f"{q['precision']*100:.1f}%"
            exclusion="측정 전" if q["exclusion_error_rate"] is None else f"{q['exclusion_error_rate']*100:.1f}%"
            quality_var.set(f"사람 라벨 {q['labeled']}개 · 추천 정밀도 {precision} · 제외 오류율 {exclusion}")
        def label(value):
            selected=tree.selection()
            if not selected:
                messagebox.showinfo("표본 선택","먼저 영상을 선택하세요.",parent=window); return
            try:
                store.label_genre_audit(self.active_id,selected[0],value,notes.get("1.0",tk.END).strip())
                notes.delete("1.0",tk.END); reload_audit()
            except ValueError as exc: messagebox.showwarning("라벨 확인",str(exc),parent=window)
        buttons=ttk.Frame(window,padding=12); buttons.pack(fill="x")
        for label_text,value in (("사람 판정: 적합","적합"),("사람 판정: 제외","제외"),("아직 불확실","불확실")):
            ttk.Button(buttons,text=label_text,command=lambda v=value:label(v)).pack(side="left",padx=5)
        ttk.Label(buttons,text="실제 감상 후 근거를 입력하세요. 불확실 표본은 정밀도 계산에서 제외됩니다.").pack(side="left",padx=15)
        reload_audit()

    def selected_candidate(self):
        ids=self.candidates.selection()
        if not ids:return
        self.selected_video=ids[0]
        item=store.get_candidate(self.active_id,self.selected_video)
        if not item:return
        profile=self.current_profile()
        try:
            candidate_config=store._normalize_genre_config(item.get("genre_config_snapshot_json") or profile.get("genre_config_json", "{}"))
        except ValueError:
            candidate_config=store.profile_genre_config(profile)
        custom_genres=candidate_config["custom_genres"]
        related_info=related_classification(
            item.get("classified_genre") or profile["genre"],
            store.normalize_related_genres(item.get("classified_genre") or profile["genre"],item.get("related_genres_json","[]"),custom_genres),
            item["video_title"],item.get("description",""),custom_genres)
        labels={"core_fit":"핵심 적합","cross_genre_review":"교차 장르 검토",
                "related_reference":"유사 장르 참고 후보","review":"검토","exclude":"제외"}
        related_signals=[genre_engine.genre_title(key,custom_genres) for key in related_info["related_evidence"]]
        self.item_status.set(item["status"])
        self.notes.delete("1.0",tk.END)
        self.notes.insert("1.0",item["notes"])
        self.genre_reason.set(f"{labels[related_info['category']]} · 핵심 판정 {item['genre_decision']} · {item['genre_score']}점 · {item['genre_reason']}"
            +(f" · 유사 신호: {', '.join(related_signals)}" if related_signals else "")
            +( " · 제목/설명만으로 불명확: 직접 청취 확인 필요" if related_info["human_listening_required"] else ""))

        self._save_ui_state()

    def manual_add(self):
        url=simpledialog.askstring("영상 URL","YouTube 영상 주소를 붙여넣으세요.",parent=self)
        if not url:return
        vid=extract_video_id(url)
        if not vid:
            messagebox.showwarning("주소 확인","YouTube watch, Shorts 또는 youtu.be 주소만 등록할 수 있습니다.")
            return
        title=simpledialog.askstring("영상 제목","영상 제목을 그대로 입력하세요.",parent=self)
        if not title:return
        channel=simpledialog.askstring("채널 이름","채널명을 입력하세요.",parent=self) or "수동 등록"
        row={"video_id":vid,"video_title":title,"channel_title":channel,"keyword":"직접 등록"}
        result=store.save_candidates(self.active_id,[row])
        if result["제외"]:
            messagebox.showwarning("장르 충돌","현재 채널 장르와 충돌해 저장하지 않았습니다. 별도 장르 프로필을 사용하세요.")
            return
        self.reload_candidates()
        self.candidates.selection_set(vid)
        self.candidates.see(vid)
        self.selected_candidate()

    def open_candidate(self):
        if self.selected_video:
            item=store.get_candidate(self.active_id,self.selected_video)
            if item:webbrowser.open(item.get("video_url") or f"https://www.youtube.com/watch?v={self.selected_video}",new=2)

    def save_candidate(self):
        if not self.selected_video:return
        item=store.get_candidate(self.active_id,self.selected_video)
        store.update_candidate(self.active_id,self.selected_video,self.item_status.get(),self.notes.get("1.0",tk.END).strip(),item["draft"])
        self.reload_candidates()
        self.candidates.selection_set(self.selected_video)
        self.footer.set("선택 채널의 메모만 저장했습니다.")

    def _schedule_note_save(self):
        if not getattr(self,"selected_video",None):return
        if getattr(self,"_note_save_after",None):
            try:self.after_cancel(self._note_save_after)
            except tk.TclError:pass
        self._note_save_after=self.after(900,self._autosave_note)

    def _autosave_note(self):
        self._note_save_after=None
        if getattr(self,"selected_video",None):
            try:self.save_candidate()
            except (sqlite3.Error,OSError):self._safe_diagnostic("candidate_note_autosave_failed")

    def approve_candidate(self):
        if not self.selected_video:return
        reason=self.notes.get("1.0",tk.END).strip()
        try:
            store.approve_review(self.active_id,self.selected_video,reason)
            self.reload_candidates()
            self.candidates.selection_set(self.selected_video)
            self.selected_candidate()
        except ValueError as e:messagebox.showwarning("장르 검토",str(e))

    def start_timer(self,minutes):
        if not self.selected_video:
            messagebox.showinfo("영상 선택","먼저 후보 영상을 선택하세요.")
            return
        self.timer_end=time.monotonic()+minutes*60
        self.timer_text.set(f"{minutes}분 분석 알림")

    def start_random_timer(self):
        if not self.selected_video:
            messagebox.showinfo("영상 선택","먼저 직접 감상할 영상을 선택하세요.",parent=self); return
        minutes=timers.random_analysis_minutes()
        self.start_timer(minutes)

    def _tick(self):
        if self.timer_end:
            remain=max(0,int(self.timer_end-time.monotonic()))
            self.timer_text.set(f"{remain//60:02d}:{remain%60:02d}")
            if remain==0:
                self.timer_end=0
                self.timer_text.set("알림 완료")
                messagebox.showinfo("분석 시간","실제 감상한 점을 메모하세요. 프로그램은 영상을 자동 재생하지 않습니다.")
        now=time.monotonic()
        if (hasattr(self,"tabs") and self.tabs.select()==str(self.tabs.tabs()[0]) and
                now-getattr(self,"_dashboard_last_refresh",0)>=15):
            self._dashboard_last_refresh=now
            self._dashboard_refresh()
        self.after(1000,self._tick)

    def _comment_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        ttk.Label(tab,text="실제로 감상한 특정 영상의 고유한 감상을 작성합니다. 자동 게시·반복 댓글은 제공하지 않습니다.").grid(row=0,column=0,sticky="w",pady=10)
        self.comment_label=tk.StringVar(value="영상 선택 없음")
        ttk.Label(tab,textvariable=self.comment_label,font=("Malgun Gothic",11,"bold")).grid(row=1,column=0,sticky="w",pady=12)
        ttk.Label(tab,text="구체적인 감상 · 일본어/한국어 자유 입력").grid(row=2,column=0,sticky="w")
        self.observation=tk.Text(tab,height=6,font=("Malgun Gothic",11),wrap="word")
        self.observation.grid(row=3,column=0,sticky="ew",pady=7)
        buttons=ttk.Frame(tab)
        buttons.grid(row=4,column=0,sticky="w",pady=7)
        ttk.Button(buttons,text="내용 검토·저장",command=self.make_draft,style="Accent.TButton").pack(side="left",padx=4)
        ttk.Button(buttons,text="초안 복사",command=self.copy_draft).pack(side="left",padx=4)
        ttk.Button(buttons,text="영상 열기",command=self.open_comment_video).pack(side="left",padx=4)
        ttk.Label(tab,text="수정 가능한 댓글 초안").grid(row=5,column=0,sticky="w",pady=(15,5))
        self.draft=tk.Text(tab,height=7,font=("Malgun Gothic",11),wrap="word")
        self.draft.grid(row=6,column=0,sticky="ew")
        self.observation.bind("<KeyRelease>",lambda _e:self._schedule_comment_save())
        self.draft.bind("<KeyRelease>",lambda _e:self._schedule_comment_save())
        persona_frame=ttk.Frame(tab); persona_frame.grid(row=7,column=0,sticky="w",pady=5)
        ttk.Label(persona_frame,text="채널별 댓글 페르소나").pack(side="left")
        self.comment_persona_choice=tk.StringVar(value=comments.PERSONAS["jp_morning_chill"]["name"])
        self.comment_persona_combo=ttk.Combobox(persona_frame,textvariable=self.comment_persona_choice,
            values=tuple(item["name"] for item in comments.PERSONAS.values()),state="readonly",width=24)
        self.comment_persona_combo.pack(side="left",padx=6)
        self.comment_language=tk.StringVar(value="ja")
        self.comment_tone=tk.StringVar(value="친근하고 차분한")
        self.comment_emoji_level=tk.StringVar(value="light")
        self.comment_length=tk.StringVar(value="short")
        ttk.Combobox(persona_frame,textvariable=self.comment_language,values=("ja","ko","en"),state="readonly",width=5).pack(side="left",padx=2)
        ttk.Combobox(persona_frame,textvariable=self.comment_tone,values=("친근하고 차분한","차분하고 정중한","밝고 간결한"),width=13).pack(side="left",padx=2)
        ttk.Combobox(persona_frame,textvariable=self.comment_emoji_level,values=("none","light","moderate"),state="readonly",width=9).pack(side="left",padx=2)
        ttk.Combobox(persona_frame,textvariable=self.comment_length,values=("short","medium","long"),state="readonly",width=8).pack(side="left",padx=2)
        ttk.Button(persona_frame,text="프로필에 저장",command=self._save_comment_persona).pack(side="left",padx=4)
        ttk.Button(persona_frame,text="감상 기반 초안 3개 추천",command=self._suggest_comment_drafts).pack(side="left",padx=4)
        self.comment_suggestion=tk.StringVar(value="")
        self.comment_suggestion_combo=ttk.Combobox(tab,textvariable=self.comment_suggestion,
            values=(),state="readonly",width=100)
        self.comment_suggestion_combo.grid(row=8,column=0,sticky="ew",pady=4)
        self.comment_suggestion_combo.bind("<<ComboboxSelected>>",self._use_comment_suggestion)

    def go_comment(self):
        if not self.selected_video:return
        if getattr(self,"comment_video",None):self._autosave_comment()
        self.comment_video=(self.active_id,self.selected_video)
        item=store.get_candidate(*self.comment_video)
        if hasattr(self,"comment_persona_combo"):
            persona=comments.persona_for_profile(store.get_profile(self.comment_video[0]))
            known={value["name"] for value in comments.PERSONAS.values()}
            chosen=persona["name"] if persona["name"] in known else (
                comments.PERSONAS["jp_morning_chill"]["name"] if persona["language"]=="ja"
                else comments.PERSONAS["senior_old_pop"]["name"])
            self.comment_persona_choice.set(chosen)
            self.comment_language.set(persona["language"])
            self.comment_tone.set(persona["tone"])
            self.comment_emoji_level.set(persona["emoji_level"])
            self.comment_length.set(persona["length"])
        self.comment_label.set(item["channel_title"]+" · "+item["video_title"][:70])
        self.observation.delete("1.0",tk.END)
        self.observation.insert("1.0",item.get("notes", ""))
        self.draft.delete("1.0",tk.END)
        self.draft.insert("1.0",item["draft"])
        self.tabs.select(self.frames[3])

    def _schedule_comment_save(self):
        if not getattr(self,"comment_video",None):return
        if getattr(self,"_comment_save_after",None):
            try:self.after_cancel(self._comment_save_after)
            except tk.TclError:pass
        self._comment_save_after=self.after(900,self._autosave_comment)

    def _autosave_comment(self):
        if getattr(self,"_comment_save_after",None):
            try:self.after_cancel(self._comment_save_after)
            except tk.TclError:pass
            self._comment_save_after=None
        target=getattr(self,"comment_video",None)
        if not target or not hasattr(self,"observation") or not hasattr(self,"draft"):return
        try:
            item=store.get_candidate(*target)
            if item:
                store.update_candidate(*target,item["status"],self.observation.get("1.0",tk.END).strip(),
                                       self.draft.get("1.0",tk.END).strip())
        except (sqlite3.Error,OSError):self._safe_diagnostic("comment_draft_autosave_failed")

    def _save_comment_persona(self):
        if not self.comment_video:
            messagebox.showinfo("프로필 선택","먼저 댓글을 작성할 채널 후보를 선택하세요.",parent=self); return
        try:
            store.set_comment_persona(self.comment_video[0],name=self.comment_persona_choice.get(),
                language=self.comment_language.get(),tone=self.comment_tone.get(),
                emoji_level=self.comment_emoji_level.get(),length=self.comment_length.get())
            self.footer.set("댓글 페르소나를 현재 프로필에 저장했습니다.")
        except ValueError as exc:
            messagebox.showwarning("저장 실패",str(exc),parent=self)

    def _suggest_comment_drafts(self):
        if not self.comment_video:
            messagebox.showinfo("후보 선택","먼저 후보 영상을 선택하세요.",parent=self); return
        item=store.get_candidate(*self.comment_video)
        profile=store.get_profile(self.comment_video[0])
        try:
            suggestions=comments.suggest_drafts(profile,item["video_title"],profile["genre"],
                self.observation.get("1.0",tk.END).strip(),3)
        except ValueError as exc:
            messagebox.showinfo("실제 감상 메모 필요",str(exc),parent=self); return
        self.comment_suggestion_combo.configure(values=suggestions)
        self.comment_suggestion.set(suggestions[0])
        self._use_comment_suggestion()

    def _use_comment_suggestion(self,_event=None):
        value=self.comment_suggestion.get()
        if value:
            self.draft.delete("1.0",tk.END); self.draft.insert("1.0",value)

    def make_draft(self):
        if not self.comment_video:
            messagebox.showinfo("대상 선택","먼저 후보에서 댓글 대상 영상을 선택하세요.")
            return
        content=self.observation.get("1.0",tk.END).strip()
        if len(content)<10 or len(content)>400:
            messagebox.showwarning("내용 확인","실제 감상을 10~400자로 작성해 주세요.")
            return
        if re.search(r"(구독해 주세요|맞구독|登録返し|チャンネルに来て|相互登録)",content,re.I):
            messagebox.showwarning("홍보 문구","반복 홍보·맞구독 문구는 제외해 주세요.")
            return
        profile=store.get_profile(self.comment_video[0])
        if profile["market"]=="JP" and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]",content):
            messagebox.showwarning("언어 확인","일본 채널에는 일본어 감상 내용을 작성하세요.")
            return
        self.draft.delete("1.0",tk.END)
        self.draft.insert("1.0",content)
        item=store.get_candidate(*self.comment_video)
        store.update_candidate(*self.comment_video,item["status"],item["notes"],content)
        self.footer.set("초안을 저장했습니다. 실제 댓글은 브라우저에서 직접 게시하세요.")

    def copy_draft(self):
        if not self.comment_video:return
        draft=self.draft.get("1.0",tk.END).strip()
        if len(draft)<10:
            messagebox.showwarning("초안 확인","10자 이상 작성해 주세요.")
            return
        self.clipboard_clear()
        self.clipboard_append(draft)
        item=store.get_candidate(*self.comment_video)
        store.update_candidate(*self.comment_video,item["status"],item["notes"],draft)
        self.footer.set("초안만 복사했습니다. 실제 게시 전 내용·상대 채널을 확인하세요.")

    def open_comment_video(self):
        if self.comment_video:
            item=store.get_candidate(*self.comment_video)
            if item:webbrowser.open(item["video_url"],new=2)

    def _sync_vph_profile_choice(self):
        if not hasattr(self,"vph_profile_combo"):
            return
        self.vph_profiles=list(self.profiles)
        self.vph_profile_combo.configure(values=[
            f'{p["name"]} | {p["market"]} | {_genre_title_for_profile(p)}' for p in self.vph_profiles])
        index=next((i for i,p in enumerate(self.vph_profiles) if p["id"]==self.active_id),-1)
        if index>=0:
            self.vph_profile_combo.current(index)

    def _vph_profile_selected(self,_event=None):
        index=self.vph_profile_combo.current()
        if 0<=index<len(self.vph_profiles) and self.vph_profiles[index]["id"]!=self.active_id:
            if self.search_inflight or getattr(self,"ai_running",False) or getattr(self,"autopilot_schedule_running",False):
                self._sync_vph_profile_choice()
                messagebox.showinfo("연구 진행 중","현재 프로필의 진행 작업이 안전하게 끝난 뒤 프로필을 전환하세요.",parent=self)
                return
            self.reload_profiles(self.vph_profiles[index]["id"])
        self.reload_vph_radar()

    def _vph_ui(self,tab):
        tab.columnconfigure(0,weight=1);tab.rowconfigure(2,weight=1)
        ttk.Label(tab,text="VPH 연구 레이더",font=("Malgun Gothic",17,"bold")).grid(row=0,column=0,sticky="w",pady=(2,6))
        ttk.Label(tab,text="누적 조회수 표본의 실제 변화 속도입니다. 조회수·구독·좋아요·댓글을 자동으로 늘리거나 영상 자동재생을 하지 않습니다. 영상은 사용자가 하나씩 직접 열어 듣고 10~20분 연구 메모를 남깁니다.",wraplength=1150).grid(row=1,column=0,sticky="w",pady=(0,8))
        controls=ttk.Frame(tab);self.vph_controls=controls;controls.grid(row=2,column=0,sticky="new")
        ttk.Label(controls,text="조사 프로필 / 채널").pack(side="left")
        self.vph_profiles=[]
        self.vph_profile_combo=ttk.Combobox(controls,state="readonly",width=42)
        self.vph_profile_combo.pack(side="left",padx=6)
        self.vph_profile_combo.bind("<<ComboboxSelected>>",self._vph_profile_selected)
        ttk.Label(controls,text="Tier A ≥").pack(side="left",padx=(10,3))
        self.vph_tier_a_var=tk.StringVar(value="100")
        ttk.Entry(controls,textvariable=self.vph_tier_a_var,width=8).pack(side="left")
        ttk.Label(controls,text="Tier B ≥").pack(side="left",padx=(7,3))
        self.vph_tier_b_var=tk.StringVar(value="50")
        ttk.Entry(controls,textvariable=self.vph_tier_b_var,width=8).pack(side="left")
        ttk.Button(controls,text="기준 저장",command=self.save_vph_thresholds).pack(side="left",padx=6)
        ttk.Button(controls,text="새로고침",command=self.reload_vph_radar).pack(side="left",padx=3)
        columns=("market","genre","title","channel","published","duration","format","views","vph","tier","reason")
        self.vph_tree=ttk.Treeview(tab,columns=columns,show="headings",height=14)
        self.vph_tree.tag_configure("unknown",foreground="#795500")
        self.vph_tree.tag_configure("review",foreground="#9B2C2C")
        labels={"market":"시장","genre":"장르","title":"영상 제목","channel":"채널","published":"게시일","duration":"길이","format":"형식","views":"누적 조회수","vph":"시간당 조회","tier":"등급","reason":"판정 사유"}
        widths={"market":58,"genre":140,"title":230,"channel":160,"published":95,"duration":65,"format":82,"views":100,"vph":95,"tier":115,"reason":200}
        for col in columns:
            self.vph_tree.heading(col,text=labels[col])
            self.vph_tree.column(col,width=widths[col],stretch=col in {"title","reason"},anchor="w")
        self.vph_tree.grid(row=3,column=0,sticky="nsew",pady=7)
        tab.rowconfigure(3,weight=1)
        scroll=ttk.Scrollbar(tab,orient="vertical",command=self.vph_tree.yview)
        scroll.grid(row=3,column=1,sticky="ns");self.vph_tree.configure(yscrollcommand=scroll.set)
        horizontal=ttk.Scrollbar(tab,orient="horizontal",command=self.vph_tree.xview)
        horizontal.grid(row=4,column=0,sticky="ew");self.vph_tree.configure(xscrollcommand=horizontal.set)
        self.vph_status=tk.StringVar(value="표본이 없습니다. 오프라인 연구 데이터가 있으면 여기에 표시됩니다.")
        ttk.Label(tab,textvariable=self.vph_status,wraplength=1100).grid(row=5,column=0,sticky="w",pady=3)
        buttons=ttk.Frame(tab);self.vph_buttons=buttons;buttons.grid(row=6,column=0,sticky="w",pady=5)
        ttk.Button(buttons,text="선택 영상의 표본 이력",command=self.open_vph_history).pack(side="left")
        ttk.Button(buttons,text="선택 영상 후보·수동 메모",command=self.open_vph_manual_review).pack(side="left",padx=5)
        ttk.Label(buttons,text="VPH는 장르 적합도 다음의 보조 정렬 기준이며, 60분 미만·결측·감소 등은 수치 대신 검토 상태로 표시합니다.").pack(side="left",padx=9)
        self._sync_vph_profile_choice()
        self.reload_vph_radar()

    @staticmethod
    def _vph_reason_ko(row):
        if row.get("sample_status")=="missing" or row.get("view_count") is None:
            details={"view_count_missing":"조회수 항목 없음","video_metadata_missing":"영상 정보를 찾지 못함"}
            detail=details.get(row.get("uncertainty_reason"),"")
            suffix="" if not detail else f" · {detail}"
            return "누적 조회수 결측: VPH 산출 안 함"+suffix
        if row.get("state")=="REVIEW":
            reasons={"uncertain_or_missing_sample":"표본 결측 또는 불확실","stale_sample_pair":"표본 간격이 너무 오래됨","cumulative_view_count_decreased":"누적 조회수 감소: 확인 필요","duplicate_observation_timestamp":"동일 시각 표본 중복: 확인 필요","out_of_order_observation":"관측 순서 역전: 확인 필요"}
        else:
            reasons={"first_sample":"첫 표본: 비교 표본 없음","interval_under_60_minutes":"표본 부족: 60분 미만","stale_sample_pair":"오래된 표본: 재측정 필요","uncertain_or_missing_sample":"표본 결측 또는 불확실"}
        if row.get("state")=="VALID":
            return "유효한 60분 이상 표본" if row.get("tier") in {"A","B"} else "유효 표본: Tier B 기준 미만"
        return reasons.get(row.get("reason"),"불확실한 표본: 사람 확인 필요")

    def reload_vph_radar(self):
        if not hasattr(self,"vph_tree"):
            return
        profile=self.current_profile()
        profile_id=profile["id"] if profile else None
        same_profile=profile_id==getattr(self,"_vph_tree_profile_id",None)
        selected=self.vph_tree.selection()[0] if same_profile and self.vph_tree.selection() else None
        try: vertical=self.vph_tree.yview()[0]; horizontal=self.vph_tree.xview()[0]
        except tk.TclError: vertical=horizontal=0.0
        self.vph_tree.delete(*self.vph_tree.get_children())
        self._sync_vph_profile_choice()
        if not profile:
            self._vph_tree_profile_id=None
            self.vph_tier_a_var.set("100");self.vph_tier_b_var.set("50")
            self.vph_status.set("조사 프로필이 없습니다. 먼저 KR/JP 프로필을 추가하세요.")
            return
        self._vph_tree_profile_id=profile_id
        self.vph_tier_a_var.set(str(profile.get("vph_tier_a",100)))
        self.vph_tier_b_var.set(str(profile.get("vph_tier_b",50)))
        genre=_genre_title_for_profile(profile)
        rows=vph.list_radar(profile["id"])
        self._vph_video_by_eval_id={}
        for row in rows:
            tier=(f"Tier {row['tier']}" if row["state"]=="VALID" and row["tier"] in {"A","B"}
                  else "기준 미만" if row["state"]=="VALID" else row["state"])
            views=row.get("view_count")
            row_tag="review" if row.get("state")=="REVIEW" else "unknown" if row.get("state")=="UNKNOWN" else ""
            self.vph_tree.insert("","end",iid=str(row["id"]),tags=(row_tag,) if row_tag else (),values=(profile["market"],genre,
                row.get("video_title") or "제목 정보 없음",row.get("channel_title") or "채널 정보 없음",
                str(row.get("published_at") or "")[:10] or "미상",self._format_vph_duration(row.get("duration_seconds")),
                self._vph_format_ko(row.get("video_format")),f"{views:,}" if views is not None else "미상",
                f"{row['vph']:.1f}" if row.get("vph") is not None else "—",tier,self._vph_reason_ko(row)))
            self._vph_video_by_eval_id[str(row["id"])]=row["video_id"]
        if selected and self.vph_tree.exists(selected):
            self.vph_tree.selection_set(selected);self.vph_tree.focus(selected)
        self.vph_tree.yview_moveto(vertical);self.vph_tree.xview_moveto(horizontal)
        self.vph_status.set(f"{profile['name']} · {profile['market']} · {genre} · 최신 영상 평가 {len(rows)}건 · 원본 표본은 수정 없이 보존")

    @staticmethod
    def _vph_format_ko(value):
        return {"longform":"롱폼","live":"라이브","short_candidate":"쇼츠 후보","unknown":"미상"}.get(value,"미상")

    @staticmethod
    def _format_vph_duration(seconds):
        if seconds is None:return "미상"
        seconds=int(seconds)
        return f"{seconds//3600}:{(seconds%3600)//60:02d}:{seconds%60:02d}" if seconds>=3600 else f"{seconds//60}:{seconds%60:02d}"

    def save_vph_thresholds(self):
        if not self.active_id:
            self.vph_status.set("프로필이 없어 기준을 저장할 수 없습니다.");return
        try:
            tier_a=float(self.vph_tier_a_var.get());tier_b=float(self.vph_tier_b_var.get())
            vph.set_thresholds(self.active_id,tier_a,tier_b)
        except (ValueError,OverflowError) as exc:
            messagebox.showwarning("기준 확인","Tier A는 Tier B보다 크고 Tier B는 0 이상이어야 합니다. 숫자를 확인하세요.\n"+str(exc),parent=self);return
        self.reload_profiles(self.active_id)
        self.vph_status.set("이 프로필의 Tier 기준을 저장했습니다. 기존 평가에는 당시 기준이 보존됩니다.")

    def open_vph_history(self):
        if not self.active_id:
            self.vph_status.set("표본 이력을 볼 프로필을 선택하세요.");return
        selected=self.vph_tree.selection()
        if not selected:
            self.vph_status.set("먼저 레이더에서 영상을 선택하세요.");return
        video_id=getattr(self,"_vph_video_by_eval_id",{}).get(selected[0])
        if not video_id:return
        current_title=self.vph_tree.item(selected[0],"values")[2]
        history=vph.list_history(self.active_id,video_id)
        window=tk.Toplevel(self);window.title("VPH 표본 이력 · "+current_title)
        window.geometry("920x390");window.transient(self)
        ttk.Label(window,text="각 행은 독립 관측 표본에 대한 평가입니다. 이 기록은 프로필별로 분리되어 보존됩니다.",wraplength=870).pack(anchor="w",padx=12,pady=8)
        tree=ttk.Treeview(window,columns=("time","views","interval","delta","vph","tier","state","reason"),show="headings")
        for col,label,width in (("time","관측 시각 (UTC)",170),("views","누적 조회수",100),("interval","간격(분)",85),("delta","조회수 차이",95),("vph","VPH",80),("tier","등급",75),("state","상태",90),("reason","사유",200)):
            tree.heading(col,text=label);tree.column(col,width=width,stretch=col=="reason")
        for row in history:
            tree.insert("","end",values=(row["observed_at_utc"],f"{row['view_count']:,}" if row.get("view_count") is not None else "미상",
                f"{row['elapsed_seconds']/60:.1f}" if row.get("elapsed_seconds") is not None else "—",
                row.get("view_delta") if row.get("view_delta") is not None else "—",
                f"{row['vph']:.1f}" if row.get("vph") is not None else "—",row.get("tier"),row.get("state"),self._vph_reason_ko(row)))
        tree.pack(fill="both",expand=True,padx=12,pady=8)
        ttk.Button(window,text="닫기",command=window.destroy).pack(anchor="e",padx=12,pady=8)

    def open_vph_manual_review(self):
        if not self.active_id:
            self.vph_status.set("수동 연구를 진행할 프로필을 선택하세요.");return
        selected=self.vph_tree.selection()
        if not selected:
            self.vph_status.set("먼저 레이더에서 영상을 선택하세요.");return
        video_id=getattr(self,"_vph_video_by_eval_id",{}).get(selected[0])
        if not video_id:return
        candidate=store.get_candidate(self.active_id,video_id)
        if not candidate:
            self.vph_status.set("선택 영상의 후보 메타데이터가 없습니다. 후보에 등록된 뒤 수동 메모를 연결할 수 있습니다.");return
        self.filter.set("");self.decision_filter.set("전체")
        self.reload_candidates()
        if not self.candidates.exists(video_id):
            self.vph_status.set("선택 영상이 현재 프로필 후보 목록에 없습니다.");return
        self.candidates.selection_set(video_id);self.candidates.focus(video_id);self.candidates.see(video_id)
        self.selected_candidate()
        self.tabs.select(self.frames[2])
        self.footer.set("선택한 VPH 영상이 후보 화면에 연결되었습니다. 직접 영상 열기를 눌러 한 편씩 듣고 메모를 작성하세요.")

    def _metrics_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(3,weight=1)
        ttk.Label(tab,text="YouTube Studio 실측값만 채널별·국가별로 보관합니다. 국가 비율은 자동 계산됩니다.").grid(row=0,column=0,sticky="w",pady=8)
        frame=ttk.Frame(tab)
        frame.grid(row=1,column=0,sticky="ew")
        self.metric_vars={}
        specs=[("날짜 YYYY-MM-DD","day",date.today().isoformat()),("전체 조회수","all","0"),
          ("대상 국가 조회수","target","0"),("노출수","impressions","0"),
          ("CTR (%)","ctr","0"),("평균 시청분","avg","0"),("구독자 수","subs","0")]
        for n,(label,key,default) in enumerate(specs):
            col=n%4
            row=n//4
            ttk.Label(frame,text=label).grid(row=row*2,column=col,sticky="w",padx=9,pady=(5,2))
            self.metric_vars[key]=tk.StringVar(value=default)
            ttk.Entry(frame,textvariable=self.metric_vars[key],width=22).grid(row=row*2+1,column=col,sticky="w",padx=9,pady=(2,11))
        ttk.Button(tab,text="실측 성과 저장",command=self.save_metric,style="Accent.TButton").grid(row=2,column=0,sticky="w",pady=10)
        self.metrics_tree=ttk.Treeview(tab,columns=("day","all","target","ratio","ctr","avg","subs"),show="headings")
        for col,title in (("day","날짜"),("all","전체 조회"),("target","대상 국가 조회"),("ratio","대상 국가 비중"),
                         ("ctr","CTR"),("avg","평균 시청(분)"),("subs","구독자")):
            self.metrics_tree.heading(col,text=title)
            self.metrics_tree.column(col,width=130,stretch=True)
        self.metrics_tree.grid(row=3,column=0,sticky="nsew",pady=8)
        bar=ttk.Frame(tab)
        bar.grid(row=4,column=0,sticky="w",pady=8)
        ttk.Button(bar,text="후보 CSV 내보내기",command=lambda:self.export("candidates")).pack(side="left",padx=5)
        ttk.Button(bar,text="성과 CSV 내보내기",command=lambda:self.export("metrics")).pack(side="left",padx=5)
        ttk.Button(bar,text="DB 전체 백업",command=self.backup).pack(side="left",padx=5)

    def save_metric(self):
        if not self.active_id:return
        v=self.metric_vars
        try:
            store.upsert_metric(self.active_id,v["day"].get(),int(v["all"].get()),int(v["target"].get()),
                int(v["impressions"].get()),float(v["ctr"].get()),float(v["avg"].get()),int(v["subs"].get()))
        except (ValueError,OverflowError) as exc:
            messagebox.showwarning("입력 확인",str(exc))
            return
        self.reload_metrics()
        self.footer.set("현재 채널 실측 성과 저장 완료")

    def reload_metrics(self):
        if not hasattr(self,"metrics_tree") or not self.active_id:return
        self.metrics_tree.delete(*self.metrics_tree.get_children())
        for item in store.list_metrics(self.active_id):
            pct=100*item["target_views"]/item["all_views"] if item["all_views"] else 0
            self.metrics_tree.insert("","end",values=(item["day"],f"{item['all_views']:,}",f"{item['target_views']:,}",
                f"{pct:.1f}%",f"{item['ctr']:.1f}%",f"{item['avg_minutes']:.1f}",f"{item['subscribers']:,}"))

    def export(self,kind):
        path=filedialog.asksaveasfilename(defaultextension=".csv",initialfile=f"zisu_{kind}_{self.current_profile()['market']}.csv",filetypes=[("CSV","*.csv")])
        if path:
            n=store.export_csv(self.active_id,kind,path)
            messagebox.showinfo("내보내기",f"{n}개 행을 저장했습니다.\n{path}")

    def backup(self):
        path=filedialog.asksaveasfilename(defaultextension=".db",initialfile="zisu_backup.db",filetypes=[("SQLite","*.db")])
        if path:
            store.backup(path)
            messagebox.showinfo("백업 완료",f"전체 채널을 백업했습니다.\n{path}")

    def _help_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(3,weight=1)
        identity=ttk.LabelFrame(tab,text="제품 정보",padding=8)
        identity.grid(row=0,column=0,sticky="ew",pady=(0,8))
        ttk.Label(identity,text=f"{DISPLAY_NAME} · {KOREAN_NAME} · {DISPLAY_VERSION}",
                  font=("Malgun Gothic",12,"bold")).pack(anchor="w")
        ttk.Label(identity,text=SLOGAN).pack(anchor="w")
        ttk.Button(identity,text="정보 보기",command=self._show_about).pack(anchor="e",pady=(3,0))
        ttk.Label(tab,text="각 채널별 검색 예약 · 프로그램이 켜져 있을 때만 동작").grid(row=1,column=0,sticky="w",pady=8)
        bar=ttk.Frame(tab)
        bar.grid(row=2,column=0,sticky="ew",pady=6)
        ttk.Label(bar,text="현재 채널 시간 (쉼표 구분)").pack(side="left")
        self.schedule_text=tk.StringVar(value="07:00,20:00")
        ttk.Entry(bar,textvariable=self.schedule_text,width=30).pack(side="left",padx=8)
        ttk.Button(bar,text="채널별 시간 저장",command=self.save_schedule).pack(side="left")
        ttk.Checkbutton(bar,text="예약 검색 켜기 (채널 연결 또는 API 키 필요)",variable=self.scheduler_enabled).pack(side="left",padx=16)
        guide=("처음 사용법 ① 채널을 선택하고 첫 탭에서 Google 계정 연결을 완료하세요.\n"
               "② [+ 채널 추가] → 목표 국가 JP/KR → 장르 1개 선택\n"
               "③ 두 번째 탭에서 장르 전용 검색어 클릭 → [유튜브 검색 열기] (API 키 불필요)\n"
               "③ 관련 영상 URL을 [후보·장르 검토]에 수동 등록하거나 API 키로 자동 수집\n"
               "④ '적합'만 자동 통과. '검토'는 사유를 20자 이상 적고 수동 승인. '제외'는 저장 안 됨\n"
               "⑤ 실제로 음악을 들으며 메모 → 댓글 초안 복사 → YouTube에서 직접 게시\n"
               "⑥ YouTube Studio 수치를 [국가별 성과]에 기록 → CSV 또는 DB 백업\n\n"
               "중요한 제한\n"
               "· 자동 시청/반복재생/자동 댓글·좋아요·구독 실행 기능 없음\n"
               "· 구글 비밀번호·브라우저 쿠키 수집하지 않음; OAuth 읽기 전용 토큰은 내 PC 로컬 저장\n"
               "· JP/KR regionCode는 일본인/한국인 운영자임을 보증하지 않음\n"
               "· 검색 키워드만으로 적합 판정하지 않음. 영상 제목과 설명 기준으로 보수적 판단\n"
               "· 복수 채널이 같은 영상을 저장해도 메모·결과·지표는 서로 섞이지 않음\n"
               "· 기존 V1 데이터는 수정하지 않음; V2 DB의 기존 데이터는 마이그레이션하여 보존\n\n"
               f"V2 데이터 위치: {store.data_dir()}\n\n"
               "공식 정책: https://support.google.com/youtube/answer/3399767\n"
               "API 검색: https://developers.google.com/youtube/v3/docs/search/list")
        self.help_text=tk.Text(tab,font=("Malgun Gothic",11),wrap="word",bg="white",padx=16,pady=16)
        self.help_text.grid(row=3,column=0,sticky="nsew",pady=10)
        self.help_text.insert("1.0",guide)
        self.help_text.configure(state="disabled")

    def _show_about(self):
        messagebox.showinfo("제품 정보",f"{DISPLAY_NAME}\n{KOREAN_NAME}\n{SLOGAN}\n\n버전 {DISPLAY_VERSION}\n읽기 전용 조사 도구",parent=self)

    def _ai_research_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(4,weight=1)
        ttk.Label(tab,text="원클릭 AI 조사 도우미 · 규칙 우선, AI는 검토 후보만 보조",font=("Malgun Gothic",15,"bold")).grid(row=0,column=0,sticky="w",pady=(4,10))
        ttk.Label(tab,text="오프라인 Demo는 가상 메타데이터와 Mock AI를 사용합니다. Google/OAuth·네트워크·AI 키·사용자 DB를 사용하지 않습니다.",wraplength=1000).grid(row=1,column=0,sticky="w",pady=(0,12))
        controls=ttk.Frame(tab)
        controls.grid(row=2,column=0,sticky="ew",pady=5)
        ttk.Label(controls,text="목표 방식").pack(side="left")
        self.ai_target_mode=tk.StringVar(value="추천 적합 수")
        self.ai_target_mode_combo=ttk.Combobox(controls,textvariable=self.ai_target_mode,values=("추천 적합 수","전체 후보 수"),
            state="readonly",width=12)
        self.ai_target_mode_combo.pack(side="left",padx=4)
        ttk.Label(controls,text="후보 목표").pack(side="left")
        self.ai_target=tk.StringVar(value="10")
        self.ai_target_combo=ttk.Combobox(controls,textvariable=self.ai_target,values=("5","10","20"),state="readonly",width=7)
        self.ai_target_combo.pack(side="left",padx=6)
        self.ai_target_mode_combo.bind("<<ComboboxSelected>>",lambda _event:self.ai_target_combo.configure(
            values=("5","10","20") if self.ai_target_mode.get()=="추천 적합 수" else ("10","30","50")))
        ttk.Label(controls,text="검색 호출 상한").pack(side="left")
        self.ai_max_calls=tk.StringVar(value="3")
        ttk.Combobox(controls,textvariable=self.ai_max_calls,values=tuple(str(x) for x in range(1,13)),state="readonly",width=5).pack(side="left",padx=6)
        ttk.Label(controls,text="AI 보조").pack(side="left")
        self.ai_provider_choice=tk.StringVar(value="규칙만")
        self.ai_provider_ids={"규칙만":"disabled","Ollama (로컬)":"ollama","Gemini":"gemini","OpenAI":"openai"}
        provider_combo=ttk.Combobox(controls,textvariable=self.ai_provider_choice,values=tuple(self.ai_provider_ids),state="readonly",width=15)
        provider_combo.pack(side="left",padx=6)
        provider_combo.bind("<<ComboboxSelected>>",lambda _event:self._ai_refresh_credential_status())
        ttk.Button(controls,text="1. 조사 계획 미리보기",command=self._ai_preview_plan,style="Accent.TButton").pack(side="left",padx=6)
        ttk.Button(controls,text="2. 오프라인 Demo 시작",command=self._ai_start_demo).pack(side="left",padx=6)
        ttk.Button(controls,text="실제 읽기 전용 조사",command=self._ai_start_live).pack(side="left",padx=6)
        self.ai_cancel_button=ttk.Button(controls,text="진행 취소",command=self._ai_cancel,state="disabled")
        self.ai_cancel_button.pack(side="left",padx=6)
        self.ai_pause_button=ttk.Button(controls,text="일시 정지",command=self._ai_pause,state="disabled")
        self.ai_pause_button.pack(side="left",padx=3)
        ttk.Button(controls,text="이전 작업 이어가기",command=self._ai_resume_latest).pack(side="left",padx=3)
        ttk.Label(controls,text="1회 USD 추정 / 1일 상한").pack(side="left",padx=(8,2))
        self.ai_cost_estimate=tk.StringVar(value="")
        self.ai_cost_limit=tk.StringVar(value="")
        ttk.Entry(controls,textvariable=self.ai_cost_estimate,width=7).pack(side="left",padx=2)
        ttk.Entry(controls,textvariable=self.ai_cost_limit,width=7).pack(side="left",padx=2)
        ttk.Button(controls,text="검토 보고서 내보내기",command=self._ai_export_report).pack(side="left",padx=6)
        credential=ttk.LabelFrame(tab,text="선택형 AI 키 보안 저장 · 현재 Windows 사용자 계정에만 복구 가능")
        credential.grid(row=5,column=0,sticky="ew",pady=5)
        self.ai_credential_provider=tk.StringVar(value="openai")
        ttk.Combobox(credential,textvariable=self.ai_credential_provider,values=("openai","gemini"),state="readonly",width=10).pack(side="left",padx=5)
        self.ai_credential_entry=ttk.Entry(credential,show="●",width=42)
        self.ai_credential_entry.pack(side="left",padx=5)
        ttk.Button(credential,text="키 저장/변경",command=self._ai_save_credential).pack(side="left",padx=4)
        ttk.Button(credential,text="키 삭제",command=self._ai_delete_credential).pack(side="left",padx=4)
        self.ai_credential_status=tk.StringVar(value="키 미등록 · 외부 모델 기본 차단")
        ttk.Label(credential,textvariable=self.ai_credential_status).pack(side="left",padx=8)
        gemini_test_frame=ttk.Frame(tab)
        gemini_test_frame.grid(row=6,column=0,sticky="ew",pady=(2,0))
        self.ai_gemini_models_button=ttk.Button(gemini_test_frame,text="Gemini 사용 가능 모델 확인",
            command=self._ai_gemini_list_models)
        self.ai_gemini_models_button.pack(side="left",padx=(0,5))
        ttk.Label(gemini_test_frame,text="Gemini 모델 선택:").pack(side="left")
        self.ai_gemini_model=tk.StringVar(value="gemini-3.5-flash-lite")
        self.ai_gemini_available_models=set()
        self.ai_gemini_model_combo=ttk.Combobox(gemini_test_frame,textvariable=self.ai_gemini_model,
            values=("gemini-3.5-flash-lite",),state="readonly",width=25)
        self.ai_gemini_model_combo.pack(side="left",padx=5)
        self.ai_gemini_test_button=ttk.Button(gemini_test_frame,text="Gemini 실제 연결 테스트 (1회)",
            command=self._ai_gemini_connection_test)
        self.ai_gemini_test_button.pack(side="left")
        ttk.Label(gemini_test_frame,text="모델 확인은 목록 조회만 수행합니다. 생성은 승인 후 가상 자료 1건.").pack(side="left",padx=10)
        ttk.Label(tab,text="사용자 입력 예상 예산은 호출 차단용 추정치이며 실제 청구액을 보장하지 않습니다.",
                  foreground="#8a4b08",wraplength=1000).grid(row=7,column=0,sticky="w",pady=(2,4))
        ttk.Label(tab,text="YouTube Search Queries 기본 제공량은 문서 기준 하루 100회이며 프로젝트별 설정이 다를 수 있습니다. 추가 페이지 요청도 각각 1회로 계산됩니다.",
                  foreground="#52636b",wraplength=1000).grid(row=8,column=0,sticky="w",pady=(0,4))
        self.ai_status=tk.StringVar(value="먼저 작업 프로필을 선택한 뒤 조사 계획을 미리 보세요.")
        ttk.Label(tab,textvariable=self.ai_status,wraplength=1000).grid(row=3,column=0,sticky="w",pady=6)
        body=ttk.Panedwindow(tab,orient="horizontal")
        body.grid(row=4,column=0,sticky="nsew",pady=6)
        results=ttk.Frame(body)
        detail=ttk.Frame(body)
        body.add(results,weight=2); body.add(detail,weight=3)
        self.ai_result_filter=tk.StringVar(value="전체")
        quick=ttk.Frame(results);quick.pack(fill="x",pady=(0,4))
        ttk.Label(quick,text="빠른 보기").pack(side="left")
        ttk.Combobox(quick,textvariable=self.ai_result_filter,
            values=("전체","추천 적합","교차 장르","근거 부족","AI 실패","사람 확인 필요"),
            state="readonly",width=15).pack(side="left",padx=5)
        self.ai_result_filter.trace_add("write",lambda *_:self._ai_apply_filter())
        self.ai_tree=ttk.Treeview(results,columns=("decision","rule","ai","title"),show="headings",height=11)
        for col,label,width in (("decision","추천 결과",85),("rule","규칙",70),("ai","AI 의견",75),("title","후보 제목",300)):
            self.ai_tree.heading(col,text=label,command=lambda c=col:self._sort_tree_click(self.ai_tree,c,"ai")); self.ai_tree.column(col,width=width,stretch=col=="title")
        self.ai_tree.pack(fill="both",expand=True)
        self.ai_tree.bind("<<TreeviewSelect>>",self._ai_show_detail)
        self.ai_listen=tk.BooleanVar(value=False)
        ttk.Checkbutton(detail,text="실제 영상 감상 후 직접 확인함 (데모에서는 기록하지 않음)",variable=self.ai_listen).pack(anchor="w",pady=(0,5))
        self.ai_detail=tk.Text(detail,font=("Malgun Gothic",10),wrap="word",height=14)
        self.ai_detail.pack(fill="both",expand=True)
        self.ai_detail.insert("1.0","결과를 선택하면 규칙 근거, AI 의견, 불확실성을 볼 수 있습니다. AI는 승인이나 댓글 게시를 수행하지 않습니다.")
        self.ai_detail.configure(state="disabled")
        self.ai_reviews={}
        self.ai_candidate_rows={}
        self.ai_expected_profile=None
        self.ai_confirmed_plan=None
        self.ai_demo_queue=queue.Queue()
        self.ai_progress_queue=queue.Queue()
        self.ai_gemini_test_queue=queue.Queue()
        self.ai_gemini_models_queue=queue.Queue()
        self.ai_running=False
        self.ai_cancel_event=None
        self.ai_pause_event=None
        self._ai_refresh_credential_status()
        ttk.Button(results,text="선택한 영상 직접 열기",command=self._ai_open_selected).pack(anchor="e",pady=5)

    def _autopilot_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(15,weight=1)
        ttk.Label(tab,text="AI 자동 조사 · Autopilot",font=("Malgun Gothic",18,"bold")).grid(row=0,column=0,sticky="w",pady=(2,8))
        self.autopilot_profile_text=tk.StringVar(value="작업 프로필을 선택하세요.")
        ttk.Label(tab,textvariable=self.autopilot_profile_text,wraplength=1050).grid(row=1,column=0,sticky="w",pady=3)
        self.autopilot_scope_text=tk.StringVar(value="적합 목표 10 · 규칙 전용 · 검색 3회 이내")
        ttk.Label(tab,textvariable=self.autopilot_scope_text,wraplength=1050).grid(row=2,column=0,sticky="w",pady=3)
        schedule=ttk.LabelFrame(tab,text="연구 시간 설정 · 저부하 권장")
        schedule.grid(row=3,column=0,sticky="ew",pady=4)
        self.schedule_hours=tk.StringVar(value="6")
        self.schedule_research_minutes=tk.StringVar(value="30")
        self.schedule_break_minutes=tk.StringVar(value="30")
        self.schedule_start_time=tk.StringVar(value="")
        for label, variable, values, width in (("전체 시간",self.schedule_hours,("2","4","6","8"),4),
                ("연구 분",self.schedule_research_minutes,("20","30","40"),4),
                ("휴식 분",self.schedule_break_minutes,("15","30","40"),4)):
            ttk.Label(schedule,text=label).pack(side="left",padx=(6,2))
            ttk.Combobox(schedule,textvariable=variable,values=values,state="readonly",width=width).pack(side="left")
        ttk.Label(schedule,text="시작 시각 선택 (HH:MM)").pack(side="left",padx=(8,2))
        ttk.Entry(schedule,textvariable=self.schedule_start_time,width=7).pack(side="left")
        ttk.Button(schedule,text="연구 시간 설정",command=self._autopilot_schedule_preview).pack(side="left",padx=7)
        self.schedule_progress_text=tk.StringVar(value="예약 대기 · 6시간 / 연구 30분 / 휴식 30분")
        ttk.Label(tab,textvariable=self.schedule_progress_text,wraplength=1050).grid(row=4,column=0,sticky="w",pady=3)
        self.autopilot_start_button=ttk.Button(tab,text="AI 자동 조사 시작",command=self._autopilot_start,
                                                style="Accent.TButton")
        self.autopilot_start_button.configure(text="연구 시작")
        self.autopilot_start_button.grid(row=5,column=0,sticky="ew",ipady=10,pady=(7,5))
        self.autopilot_progress=ttk.Progressbar(tab,mode="determinate",maximum=100)
        self.autopilot_progress.grid(row=6,column=0,sticky="ew",pady=4)
        self.autopilot_stage_text=tk.StringVar(value="대기 중 · 시작을 누르면 승인된 범위 안에서 순차 조사합니다.")
        ttk.Label(tab,textvariable=self.autopilot_stage_text,wraplength=1050).grid(row=7,column=0,sticky="w",pady=4)
        self.autopilot_counts_text=tk.StringVar(value="검색 0회 · 후보 0 · 중복 0 · 추천 적합 0/10 · AI 0 · 사람 확인 0 · 잔여 예산 확인 전")
        ttk.Label(tab,textvariable=self.autopilot_counts_text,wraplength=1050).grid(row=8,column=0,sticky="new",pady=4)
        self.autopilot_ai_stats=tk.StringVar(value="AI 요청 통계는 조사 완료 후 실제 요청·성공·실패·캐시·예산 차단을 구분해 표시합니다.")
        ttk.Label(tab,textvariable=self.autopilot_ai_stats,wraplength=1050).grid(row=8,column=0,sticky="sew",pady=(30,4))
        detail=ttk.LabelFrame(tab,text="상세 설정 (필요할 때만 열기)")
        self.autopilot_quota_text=tk.StringVar(value="오늘 검색량: 기록을 불러오는 중입니다. 이 값은 Google Cloud의 실제 잔여량이 아닌 로컬 추정치입니다.")
        ttk.Label(tab,textvariable=self.autopilot_quota_text,wraplength=1050).grid(row=9,column=0,sticky="w",pady=3)
        detail.grid(row=10,column=0,sticky="ew",pady=4)
        self.autopilot_detail_panel=ttk.Frame(detail)
        self.autopilot_detail_panel.pack(fill="x")
        row=ttk.Frame(self.autopilot_detail_panel); row.pack(fill="x",pady=3)
        ttk.Label(row,text="추천 적합 목표").pack(side="left")
        self.autopilot_target=ttk.Combobox(row,textvariable=self.ai_target,values=("5","10","20"),state="readonly",width=5)
        self.autopilot_target.pack(side="left",padx=5)
        ttk.Label(row,text="검색 호출 상한").pack(side="left",padx=(10,0))
        self.autopilot_search_limit=ttk.Combobox(row,textvariable=self.ai_max_calls,
            values=tuple(str(x) for x in range(1,13)),state="readonly",width=5)
        self.autopilot_search_limit.pack(side="left",padx=5)
        ttk.Label(row,text="AI 보조").pack(side="left",padx=(10,0))
        self.autopilot_provider=ttk.Combobox(row,textvariable=self.ai_provider_choice,
            values=tuple(self.ai_provider_ids),state="readonly",width=16)
        self.autopilot_provider.pack(side="left",padx=5)
        ttk.Label(row,text="Gemini 모델").pack(side="left",padx=(10,0))
        ttk.Label(row,textvariable=self.ai_gemini_model).pack(side="left",padx=5)
        ttk.Label(self.autopilot_detail_panel,text="유료 AI 예산은 사용자 입력 기반 예상치이며 실제 청구액을 보장하지 않습니다. 기존 AI 연구 도우미에서 추정액과 1일 상한을 설정하세요.",
                  wraplength=1020,foreground="#8a4b08").pack(anchor="w",pady=3)
        resource_row=ttk.Frame(self.autopilot_detail_panel); resource_row.pack(fill="x",pady=3)
        self.resource_cpu_limit=tk.StringVar(value="80")
        self.resource_cpu_seconds=tk.StringVar(value="60")
        self.resource_ram_gb=tk.StringVar(value="2")
        ttk.Label(resource_row,text="저부하 기준: CPU % / 지속 초 / 사용 가능 RAM GB").pack(side="left")
        for variable,low,high,width in ((self.resource_cpu_limit,10,100,4),
                                       (self.resource_cpu_seconds,10,600,5),
                                       (self.resource_ram_gb,0,64,4)):
            ttk.Spinbox(resource_row,from_=low,to=high,textvariable=variable,width=width).pack(side="left",padx=3)
        ttk.Button(resource_row,text="기준 적용",command=self._apply_resource_limits).pack(side="left",padx=5)
        self.autopilot_detail_panel.pack_forget()
        self.autopilot_details_button=ttk.Button(detail,text="상세 설정 열기",command=self._autopilot_toggle_details)
        self.autopilot_details_button.pack(anchor="w",padx=5,pady=3)
        actions=ttk.Frame(tab); actions.grid(row=10,column=0,sticky="ew",pady=4)
        self.autopilot_pause_button=ttk.Button(actions,text="잠시 멈추기",command=self._autopilot_pause,state="disabled")
        self.autopilot_pause_button.pack(side="left",padx=3)
        self.autopilot_resume_button=ttk.Button(actions,text="이전 작업 이어하기",command=self._autopilot_resume)
        self.autopilot_resume_button.pack(side="left",padx=3)
        self.autopilot_stop_button=ttk.Button(actions,text="안전 중단",command=self._autopilot_stop,state="disabled")
        self.autopilot_stop_button.pack(side="left",padx=3)
        self.resource_low_load_var=tk.BooleanVar(value=True)
        self.resource_guard=ResourceGuard()
        self.research_browser=ResearchBrowserManager(resource_guard=self.resource_guard)
        browser_labels=list(self.research_browser.browser_choices)
        selected_browser=(self.research_browser.preferred_browser
            if self.research_browser.preferred_browser in browser_labels else
            "Google Chrome" if "Google Chrome" in browser_labels else
            browser_labels[0] if browser_labels else "")
        self.research_browser_choice=tk.StringVar(value=selected_browser)
        self.research_browser_status=tk.StringVar(value="전용 인터넷 창: 닫힘 · 연구 시간에만 열 수 있어요")
        self._browser_alerted_schedules=set()
        self._browser_metrics_checked_at=0.0
        self._browser_metrics_running=False
        self._browser_schedule_checked_at=0.0
        self.resource_status_text=tk.StringVar(value="컴퓨터 여유 있음 · 저부하 모드 권장")
        self._resource_was_waiting=False
        ttk.Checkbutton(actions,text="저부하 모드 · 권장",variable=self.resource_low_load_var).pack(side="left",padx=5)
        ttk.Button(actions,text="리소스 상태 보기",command=self._resource_status).pack(side="left",padx=3)
        ttk.Label(actions,textvariable=self.resource_status_text).pack(side="left",padx=6)
        ttk.Button(actions,text="결과 보고서 내보내기",command=self._autopilot_export).pack(side="left",padx=3)
        ttk.Button(actions,text="AI 상세 화면",command=lambda:self.tabs.select(self.frames[6])).pack(side="left",padx=3)
        ttk.Label(tab,text="예외 / 사람 확인 대기함",font=("Malgun Gothic",11,"bold")).grid(row=11,column=0,sticky="w",pady=(6,2))
        self.autopilot_exception_tree=ttk.Treeview(tab,columns=("type","impact","action","retry","status"),show="headings",height=4)
        for col,label,width in (("type","유형",125),("impact","영향",220),("action","권장 조치",390),("retry","재시도",75),("status","상태",85)):
            self.autopilot_exception_tree.heading(col,text=label); self.autopilot_exception_tree.column(col,width=width,stretch=col=="action")
        self.autopilot_exception_tree.grid(row=12,column=0,sticky="nsew",pady=3)
        ttk.Label(tab,text="최근 조사 보고서",font=("Malgun Gothic",11,"bold")).grid(row=13,column=0,sticky="w",pady=(6,2))
        self.autopilot_report=tk.Text(tab,height=7,wrap="word",font=("Consolas",9))
        self.autopilot_report.grid(row=14,column=0,sticky="nsew",pady=3)
        self.autopilot_report.insert("1.0","보고서는 프로필별로 저장됩니다. AI 판정은 사람의 실제 청취 정답과 분리되며 댓글은 게시되지 않습니다.")
        self.autopilot_report.configure(state="disabled")
        browser_panel=ttk.LabelFrame(tab,text="연구용 전용 인터넷 창 · 자동 재생/참여 기능 없음")
        browser_panel.grid(row=11,column=0,sticky="ew",pady=4)
        browser_buttons=ttk.Frame(browser_panel); browser_buttons.pack(fill="x",padx=4,pady=3)
        ttk.Label(browser_buttons,text="전용 브라우저:").pack(side="left",padx=3)
        self.research_browser_combo=ttk.Combobox(browser_buttons,textvariable=self.research_browser_choice,
            values=browser_labels,state="readonly",width=18)
        self.research_browser_combo.pack(side="left",padx=3)
        self.research_browser_combo.bind("<<ComboboxSelected>>",self._research_browser_select)
        ttk.Button(browser_buttons,text="연구용 인터넷 창 열기",command=self._research_browser_open).pack(side="left",padx=3)
        ttk.Button(browser_buttons,text="기본 브라우저에서 수동 열기",command=self._research_browser_manual_open).pack(side="left",padx=3)
        ttk.Button(browser_buttons,text="인터넷 창 닫기",command=self._research_browser_close).pack(side="left",padx=3)
        if self.research_browser.recovery_available:
            ttk.Button(browser_buttons,text="이전 연구 창 확인/닫기",command=self._research_browser_recover).pack(side="left",padx=3)
        ttk.Label(browser_panel,textvariable=self.research_browser_status,wraplength=1000).pack(anchor="w",padx=8,pady=3)
        for child in tab.grid_slaves():
            if child is browser_panel:
                continue
            row_index=int(child.grid_info().get("row",0))
            if row_index>=11:
                child.grid_configure(row=row_index+1)
        tab.rowconfigure(16,weight=1)
        self.autopilot_controller=autopilot.AutopilotController()
        self.autopilot_events=queue.Queue()
        self.autopilot_result=None
        self.autopilot_close_pending=False
        self.autopilot_schedule_id=None
        self.autopilot_schedule_running=False
        self.autopilot_schedule_pause_event=threading.Event()
        self.autopilot_schedule_resume_event=threading.Event()
        self.autopilot_schedule_stop_event=threading.Event()
        self.autopilot_schedule_shutdown_event=threading.Event()
        self.ai_target.trace_add("write",lambda *_: self._autopilot_refresh_profile())
        self.ai_max_calls.trace_add("write",lambda *_: self._autopilot_refresh_profile())
        self.ai_provider_choice.trace_add("write",lambda *_: self._autopilot_refresh_profile())
        self._autopilot_refresh_profile()

    def _autopilot_schedule_preview(self):
        try:
            config=research_scheduler.ScheduleConfig(
                profile_id=(self.current_profile() or {}).get("id",""),
                total_hours=int(self.schedule_hours.get()),
                research_minutes=int(self.schedule_research_minutes.get()),
                break_minutes=int(self.schedule_break_minutes.get()),
                approved_search_calls=int(self.ai_max_calls.get()))
            config.validate()
        except (TypeError,ValueError):
            messagebox.showerror("시간 설정 확인","전체 시간, 연구/휴식 간격과 검색 한도를 다시 확인해 주세요.",parent=self)
            return
        start=self.schedule_start_time.get().strip() or "지금 시작"
        self.schedule_progress_text.set(
            f"설정됨 · {config.total_hours}시간 · 연구 {config.research_minutes}분 / 휴식 {config.break_minutes}분 · 시작 {start} · 검색 한도 {config.approved_search_calls}회")

    def _resource_status(self):
        decision=self.resource_guard.check(sample_resources)
        labels={"available":"컴퓨터 여유 있음","busy":"컴퓨터가 바빠서 잠시 기다리는 중",
                "low_memory":"메모리가 부족해 조사를 기다리는 중",
                "unavailable":"컴퓨터 상태를 확인하지 못해 새 조사를 기다리는 중"}
        self.resource_status_text.set(labels.get(decision.status,"리소스 상태를 확인할 수 없습니다"))
        snap=decision.snapshot
        details=[labels.get(decision.status,"리소스 상태를 확인할 수 없습니다")]
        if snap and snap.system_cpu_percent is not None: details.append(f"시스템 CPU: {snap.system_cpu_percent:.0f}%")
        if snap and snap.available_ram_bytes is not None: details.append(f"사용 가능 메모리: {snap.available_ram_bytes/(1024**3):.1f} GB")
        if snap and snap.process_rss_bytes is not None: details.append(f"ZISU 메모리: {snap.process_rss_bytes/(1024**2):.0f} MB")
        if snap and snap.process_cpu_percent is not None: details.append(f"ZISU CPU: {snap.process_cpu_percent:.1f}%")
        if decision.reasons: details.append("보류 이유: "+", ".join(decision.reasons))
        messagebox.showinfo("리소스 상태", "\n".join(details), parent=self)

    def _apply_resource_limits(self):
        try:
            self.resource_guard=ResourceGuard(ResourceLimits(
                cpu_percent=float(self.resource_cpu_limit.get()),
                cpu_sustain_seconds=float(self.resource_cpu_seconds.get()),
                min_available_ram_bytes=int(float(self.resource_ram_gb.get())*(1024**3))))
        except (TypeError,ValueError):
            messagebox.showerror("기준을 확인해 주세요","CPU, 지속 시간, 메모리 기준을 숫자로 입력해 주세요.",parent=self)
            return
        self.resource_status_text.set("저부하 기준을 적용했습니다")

    def _autopilot_toggle_details(self):
        if self.autopilot_detail_panel.winfo_ismapped():
            self.autopilot_detail_panel.pack_forget(); self.autopilot_details_button.configure(text="상세 설정 열기")
        else:
            self.autopilot_detail_panel.pack(fill="x",before=self.autopilot_details_button); self.autopilot_details_button.configure(text="상세 설정 접기")

    def _autopilot_refresh_profile(self):
        if not hasattr(self,"autopilot_profile_text"): return
        self.autopilot_exception_tree.delete(*self.autopilot_exception_tree.get_children())
        self.autopilot_saved_report=None
        self.autopilot_report.configure(state="normal")
        self.autopilot_report.delete("1.0","end")
        self.autopilot_report.insert("1.0","보고서는 프로필별로 저장됩니다. AI 판정은 사람의 실제 청취 정답과 분리되며 댓글은 게시되지 않습니다.")
        self.autopilot_report.configure(state="disabled")
        profile=self.current_profile()
        if not profile:
            self.autopilot_profile_text.set("작업 프로필 없음 · 먼저 프로필을 추가하세요.")
            return
        connected=oauth.is_connected(profile["id"])
        account=profile.get("connected_email") or ("Google 읽기 전용 연결됨" if connected else "Google 연결 전")
        self.autopilot_profile_text.set(f"프로필: {profile['name']} · {profile['market']} · {_genre_title_for_profile(profile)} · 계정: {account}")
        provider_id=self.ai_provider_ids.get(self.ai_provider_choice.get(),"disabled")
        model=self.ai_gemini_model.get() if provider_id=="gemini" else ("로컬 모델" if provider_id=="ollama" else provider_id)
        self.autopilot_scope_text.set(f"추천 적합 목표 {self.ai_target.get()}개 · AI {model} · 검색 호출 최대 {self.ai_max_calls.get()}회 · 사람 승인/실청취 라벨은 별도")
        recoverable=store.list_resumable_sessions(profile["id"])
        autopilot_sessions=[s for s in recoverable if store.get_autopilot_settings(profile["id"],s["id"])]
        if autopilot_sessions:
            self.autopilot_stage_text.set(f"저장된 작업 {len(autopilot_sessions)}개 복구 가능 · 이어하기를 누르면 원래 승인 범위만 사용합니다.")
        saved_report=store.get_latest_autopilot_report(profile["id"])
        if saved_report:
            self.autopilot_saved_report=saved_report["report"]
            self.autopilot_report.configure(state="normal")
            self.autopilot_report.delete("1.0","end")
            self.autopilot_report.insert("1.0",json.dumps(self.autopilot_saved_report,ensure_ascii=False,indent=2))
            self.autopilot_report.configure(state="disabled")
            for item in saved_report["exceptions"]: self._autopilot_show_exception(item)

    def _autopilot_show_exception(self,item):
        values=(item.get("type","unknown"),item.get("impact",""),item.get("recommended_action",""),
                "가능" if item.get("retry_allowed") else "불가",item.get("status","pending"))
        self.autopilot_exception_tree.insert("","end",values=values)

    def _autopilot_start(self,session_settings=None):
        profile=self.current_profile()
        if not profile:
            self._autopilot_show_exception({"type":"profile_required","impact":"조사 프로필이 없습니다.","recommended_action":"국가와 핵심 장르를 선택해 프로필을 추가하세요.","retry_allowed":False})
            return
        if self.autopilot_controller.running or self.autopilot_schedule_running:
            self.autopilot_stage_text.set("이미 Autopilot 작업이 실행 중입니다. 중복 실행은 차단했습니다.")
            return
        if not session_settings:
            active=[s for s in store.list_resumable_sessions(profile["id"])
                    if store.get_autopilot_settings(profile["id"],s["id"])]
            if active:
                self.autopilot_stage_text.set("복구 가능한 Autopilot 세션이 있습니다. 새 작업 대신 이어하기를 선택하세요.")
                return
            self.ai_target_mode.set("추천 적합 수")
            try:
                target=int(self.ai_target.get()); max_calls=int(self.ai_max_calls.get())
                plan=ai_research.preview_plan(profile["id"],target,max_search_calls=max_calls,target_mode="fit_target")
            except (ValueError,KeyError):
                self._autopilot_show_exception({"type":"settings","impact":"목표 또는 검색 한도를 사용할 수 없습니다.","recommended_action":"상세 설정에서 추천 목표와 1~12회 검색 상한을 선택하세요.","retry_allowed":False})
                return
            provider_id=self.ai_provider_ids.get(self.ai_provider_choice.get(),"disabled")
            key=self.api_key.get().strip()
            if not key and not oauth.is_connected(profile["id"]):
                self._autopilot_show_exception({"type":"google_auth","impact":"검색 권한 또는 YouTube API 키가 없습니다.","recommended_action":"이 프로필의 읽기 전용 Google 연결을 완료하거나 공개 조회용 API 키를 설정하세요.","retry_allowed":False})
                self.autopilot_stage_text.set("시작 전 확인 필요 · 검색은 아직 호출하지 않았습니다.")
                return
            provider=None
            paid=provider_id in {"gemini","openai"}
            cost_estimate=cost_limit=None
            if paid:
                if not CredentialStore().configured(provider_id):
                    self._autopilot_show_exception({"type":"ai_credential","impact":"선택한 AI 키가 안전 저장소에 없습니다.","recommended_action":"상세 AI 화면에서 키를 등록하거나 규칙 전용/Ollama를 선택하세요.","retry_allowed":False})
                    return
                if provider_id=="gemini" and self.ai_gemini_available_models and self.ai_gemini_model.get() not in self.ai_gemini_available_models:
                    self._autopilot_show_exception({"type":"model_unavailable","impact":"선택 모델이 계정에서 확인되지 않았습니다.","recommended_action":"Gemini 사용 가능 모델을 조회한 뒤 선택하세요.","retry_allowed":False})
                    return
                try:
                    cost_estimate,cost_limit=ai_research.validate_cost_budget(self.ai_cost_estimate.get(),self.ai_cost_limit.get())
                except (TypeError,ValueError):
                    self._autopilot_show_exception({"type":"cost_budget_required","impact":"유료 AI 예산이 확인되지 않았습니다.","recommended_action":"상세 AI 화면에서 보수적 추정액과 1일 상한을 설정하세요. 예산 확인 전 외부 호출은 없습니다.","retry_allowed":False})
                    return
                provider=(ai_research.GeminiProvider(model=self.ai_gemini_model.get()) if provider_id=="gemini"
                          else ai_research.OpenAIProvider())
            elif provider_id=="ollama":
                provider=ai_research.OllamaProvider()
            summary=(f"프로필 {profile['name']} ({profile['market']} / {_genre_title_for_profile(profile)})\n"
                     f"목표 적합 {target}개 · YouTube 검색 호출 최대 {max_calls}회 · 검색어 {len(plan['search_terms'])}개\n"
                     f"연구 일정 {self.schedule_hours.get()}시간 · 연구 {self.schedule_research_minutes.get()}분 / 휴식 {self.schedule_break_minutes.get()}분 · 시작 {self.schedule_start_time.get().strip() or '지금'} (PC 현지 시각) · 저부하 {'켜짐' if self.resource_low_load_var.get() else '꺼짐'}\n"
                     f"AI: {provider_id} / {getattr(provider,'model','none')} · AI 호출은 최대 10회/작업이며 검색 한도와 별도입니다.\n"
                     f"유료 AI 비용 추정 {cost_estimate if cost_estimate is not None else '없음'} USD, 입력 상한 {cost_limit if cost_limit is not None else '없음'} USD\n"
                     "계속을 누르면 이 승인 범위 안에서는 단계별 팝업 없이 진행합니다. 외부 권한·예산을 늘리지 않으며 사람 라벨과 댓글 게시를 자동 수행하지 않습니다.")
            if not messagebox.askyesno("AI 자동 조사 승인",summary,parent=self):
                self.autopilot_stage_text.set("사용자 승인 취소 · 외부 요청 0회")
                return
            config=autopilot.AutopilotConfig(profile_id=profile["id"],target=target,
                max_search_calls=max_calls,provider_id=provider_id,allow_paid_external=paid,
                cost_estimate_usd=cost_estimate,cost_limit_usd=cost_limit)
        else:
            settings,session_id,*saved_schedule=session_settings
            schedule_id=saved_schedule[0] if saved_schedule else None
            provider_id=settings["provider_id"]
            provider=(ai_research.GeminiProvider(model=settings["model"]) if provider_id=="gemini" else
                      ai_research.OpenAIProvider() if provider_id=="openai" else
                      ai_research.OllamaProvider(model=settings["model"]) if provider_id=="ollama" else None)
            if provider_id in {"gemini","openai"} and not CredentialStore().configured(provider_id):
                self._autopilot_show_exception({"type":"ai_credential","impact":"저장된 조사에서 AI 키를 사용할 수 없습니다.","recommended_action":"기존 승인 범위의 키를 복구하거나 같은 공급자에 키를 다시 등록하세요. 예산은 늘리지 않았습니다.","retry_allowed":False})
                return
            config=autopilot.AutopilotConfig(profile_id=profile["id"],target=settings["target"],
                max_search_calls=settings["max_search_calls"],provider_id=provider_id,
                allow_paid_external=settings["paid_external_approved"],
                cost_estimate_usd=settings["cost_estimate_usd"],cost_limit_usd=settings["cost_limit_usd"],
                session_id=session_id)
        key=self.api_key.get().strip()
        profile_id=profile["id"]
        low_load_enabled=bool(self.resource_low_load_var.get())
        if session_settings is None:
            schedule_id=None
        if session_settings is None:
            try:
                start_at=None
                start_text=self.schedule_start_time.get().strip()
                if start_text:
                    parsed=datetime.strptime(start_text,"%H:%M").time()
                    local_now=datetime.now().astimezone()
                    start_at=datetime.combine(local_now.date(),parsed,tzinfo=local_now.tzinfo)
                    if start_at < local_now:
                        raise ValueError("시작 시각은 오늘의 현재 시각 이후로 입력해 주세요.")
                schedule=research_scheduler.create(research_scheduler.ScheduleConfig(
                    profile_id=profile_id,total_hours=int(self.schedule_hours.get()),
                    research_minutes=int(self.schedule_research_minutes.get()),
                    break_minutes=int(self.schedule_break_minutes.get()),start_at=start_at,
                    approved_search_calls=config.max_search_calls))
                schedule_id=schedule["id"]
                research_scheduler.checkpoint(schedule_id,profile_id,{
                    "approved_provider_id":config.provider_id,
                    "approved_model":getattr(provider,"model","none") if provider else "none",
                    "approved_target":config.target,
                    "approved_max_search_calls":config.max_search_calls,
                    "paid_external_approved":bool(config.allow_paid_external),
                    "cost_estimate_usd":config.cost_estimate_usd,
                    "cost_limit_usd":config.cost_limit_usd,
                    "processed_video_ids":[],"uncertain_request":False,
                })
            except (ValueError, research_scheduler.ScheduleBusy) as exc:
                if schedule_id:
                    try: research_scheduler.cancel(schedule_id,profile_id)
                    except Exception: pass
                self._autopilot_show_exception({"type":"schedule_settings","impact":"연구 시간을 설정하지 못했습니다.",
                    "recommended_action":str(exc) if isinstance(exc,ValueError) else "이 프로필에 진행 중인 예약이 있습니다. 먼저 이어가거나 안전 중단하세요.",
                    "retry_allowed":False})
                return
        self.autopilot_schedule_id=schedule_id
        self.autopilot_schedule_pause_event.clear()
        self.autopilot_schedule_resume_event.clear()
        self.autopilot_schedule_stop_event.clear()
        self.autopilot_schedule_shutdown_event.clear()
        self.autopilot_result=None
        self.autopilot_exception_tree.delete(*self.autopilot_exception_tree.get_children())
        self.autopilot_progress.configure(value=0)
        self.autopilot_stage_text.set("시작 · 프로필 전용 체크포인트를 생성합니다.")
        self.autopilot_start_button.configure(state="disabled")
        self.autopilot_pause_button.configure(state="normal")
        self.autopilot_stop_button.configure(state="normal")
        self.profile_combo.configure(state="disabled")
        self.autopilot_schedule_running=bool(schedule_id)
        def worker():
            try:
                token=oauth.get_access_token(profile_id) if not key else None
                identity=quota_project_identity(key,
                    (oauth.quota_project_identity(profile_id) or "oauth-project:unknown") if not key else "")
                if identity:
                    project_quota=quota_ledger.summary(identity,store.db_path())
                    profile_quota=quota_ledger.summary(identity,store.db_path(),profile_id=profile_id)
                    self.autopilot_events.put({"stage":"quota_status","profile":profile_quota,
                                               "project":project_quota})
                def resource_check():
                    if not low_load_enabled:
                        return True
                    decision=self.resource_guard.check(sample_resources)
                    labels={"available":"컴퓨터 여유 있음","busy":"컴퓨터가 바빠서 잠시 기다리는 중",
                            "low_memory":"메모리가 부족해 조사를 기다리는 중",
                            "unavailable":"컴퓨터 상태를 확인하지 못해 새 조사를 기다리는 중"}
                    self.autopilot_events.put({"stage":"resource_status",
                        "status":labels.get(decision.status,"리소스 상태를 확인할 수 없습니다"),
                        "allow":decision.allow_new_work})
                    return decision.allow_new_work
                def collect(term,market,limit,page_token=None,order="relevance",quota_context=None):
                    context={"profile_id":profile_id,"path":store.db_path(),
                        "project_identity":quota_project_identity(key,
                            (oauth.quota_project_identity(profile_id) or "oauth-project:unknown") if not key else "")}
                    if quota_context:
                        context.update(quota_context)
                    if context.get("schedule_id") and not context.get("session_id"):
                        context["session_id"]=context["schedule_id"]
                    try:
                        return lookup_videos(key,term,market,limit=min(50,limit),access_token=token,
                                             page_token=page_token,return_page=True,order=order,
                                             quota_context=context)
                    finally:
                        project_usage=quota_ledger.summary(context["project_identity"],store.db_path())
                        profile_usage=quota_ledger.summary(context["project_identity"],store.db_path(),
                                                           profile_id=profile_id)
                        self.autopilot_events.put({"stage":"quota_status","profile":profile_usage,
                                                   "project":project_usage})
                if schedule_id:
                    outcome=self._autopilot_schedule_worker(schedule_id,profile_id,config,provider,
                        collect,resource_check,low_load_enabled)
                else:
                    outcome=self.autopilot_controller.run(config,collect,provider=provider,
                        on_event=self.autopilot_events.put,continue_check=resource_check,
                        resource_check=resource_check)
                self.autopilot_events.put({"stage":"result","result":outcome,"profile_id":profile_id})
            except Exception as exc:
                if schedule_id:
                    try:
                        pending=research_scheduler.get(schedule_id,profile_id)
                        if pending and pending["state"]=="running" and not pending["search_in_flight"]:
                            if isinstance(exc,(APIError,oauth.OAuthError)):
                                research_scheduler.pause_for_api_error(schedule_id,profile_id,exc)
                            else:
                                research_scheduler.pause(schedule_id,profile_id)
                    except Exception:
                        pass
                if isinstance(exc,(APIError,oauth.OAuthError)):
                    self.autopilot_events.put({"stage":"fatal","profile_id":profile_id,
                                               "exception":autopilot._safe_error(exc)})
                    return
                self.autopilot_events.put({"stage":"fatal","profile_id":profile_id,
                    "exception":{"type":"startup","impact":"조사를 시작하지 못했습니다.",
                                 "recommended_action":"프로필 연결·읽기 권한·저장 경로를 확인하세요. 개인 키는 로그에 기록하지 않았습니다.",
                                 "retry_allowed":False,"status":"pending"}})
            finally:
                self.autopilot_schedule_running=False
        threading.Thread(target=worker,name="zisu-autopilot",daemon=True).start()
        self.after(100,self._autopilot_poll)

    def _autopilot_schedule_worker(self,schedule_id,profile_id,config,provider,collector,resource_check,low_load_enabled):
        last_result=None
        settings=autopilot.AutopilotController._settings(config,provider)
        while True:
            if self.autopilot_schedule_shutdown_event.is_set():
                self._research_browser_boundary_close("shutdown")
                item=research_scheduler.get(schedule_id,profile_id)
                if item and item["state"]=="running" and not item["search_in_flight"]:
                    research_scheduler.pause(schedule_id,profile_id)
                self.autopilot_events.put({"stage":"schedule","message":"앱을 닫기 전에 예약 체크포인트를 저장했습니다. 다음 실행에서 이어하기를 선택하세요."})
                break
            if self.autopilot_schedule_stop_event.is_set():
                self._research_browser_boundary_close("stop")
                item=research_scheduler.get(schedule_id,profile_id)
                if item and item["state"] in {"running","scheduled","paused"} and not item["search_in_flight"]:
                    research_scheduler.cancel(schedule_id,profile_id)
                self.autopilot_events.put({"stage":"schedule","message":"예약 조사를 안전하게 중단하고 체크포인트를 보존했습니다."})
                break
            item=research_scheduler.get(schedule_id,profile_id)
            if not item or item["state"] in {"completed","cancelled"}:
                break
            self.autopilot_events.put({"stage":"schedule_status","item":item})
            if self.autopilot_schedule_pause_event.is_set():
                self._research_browser_boundary_close("pause")
                if item["state"]=="running":
                    research_scheduler.pause(schedule_id,profile_id)
                self.autopilot_events.put({"stage":"schedule","message":"연구를 잠시 멈췄습니다. 이어하기를 누르면 저장된 범위 안에서 계속합니다."})
                if self.autopilot_schedule_shutdown_event.is_set(): break
                while not self.autopilot_schedule_resume_event.wait(1):
                    if self.autopilot_schedule_stop_event.is_set() or self.autopilot_schedule_shutdown_event.is_set(): break
                if self.autopilot_schedule_stop_event.is_set(): continue
                if self.autopilot_schedule_shutdown_event.is_set(): break
                self.autopilot_schedule_resume_event.clear()
                item=research_scheduler.get(schedule_id,profile_id)
                if low_load_enabled:
                    decision=self.resource_guard.check(sample_resources)
                    if not decision.allow_new_work:
                        research_scheduler.request_resource_pause(schedule_id,profile_id,
                            decision.reasons[0],now=datetime.now().astimezone())
                        self.autopilot_schedule_pause_event.clear()
                        self.autopilot_events.put({"stage":"resource_status","status":"컴퓨터가 바빠서 잠시 기다리는 중","allow":False})
                        continue
                if item and item["state"]=="paused":
                    research_scheduler.resume(schedule_id,profile_id)
                self.autopilot_schedule_pause_event.clear()
                continue
            if item["state"]=="paused":
                self._research_browser_boundary_close("paused")
                if item.get("pause_reason","").startswith(("system_","process_")) and low_load_enabled:
                    try:
                        research_scheduler.resume_after_resource_recovery(schedule_id,profile_id,
                            resource_guard=self.resource_guard,expected_autopilot_settings=settings,
                            allow_new_autopilot_session=not bool(config.session_id))
                        self.autopilot_events.put({"stage":"schedule","message":"컴퓨터 여유가 회복되었습니다. 승인된 예산과 체크포인트를 확인해 이어갑니다."})
                        continue
                    except research_scheduler.ResourceWorkDeferred as exc:
                        self.autopilot_events.put({"stage":"resource_status","status":"컴퓨터가 바빠서 잠시 기다리는 중","allow":False})
                        self.autopilot_schedule_resume_event.wait(15)
                        self.autopilot_schedule_resume_event.clear()
                        continue
                    except Exception:
                        self.autopilot_events.put({"stage":"schedule","message":"저장된 요청이나 승인 범위를 자동 확인할 수 없습니다. 이어하기 전에 확인이 필요합니다."})
                        break
                self.autopilot_events.put({"stage":"schedule","message":"이전 예약이 복구 대기 중입니다. 이어하기 버튼으로 계속할 수 있습니다."})
                break
            if item["state"]=="scheduled" or item["phase"]=="waiting":
                self.autopilot_events.put({"stage":"schedule","message":f"예약된 시작까지 {item['remaining_seconds']//60}분 남았습니다."})
                self.autopilot_schedule_resume_event.wait(min(15,max(1,item["remaining_seconds"])))
                self.autopilot_schedule_resume_event.clear()
                continue
            if item["phase"]=="break":
                self._research_browser_boundary_close("break")
                self.autopilot_events.put({"stage":"schedule","message":f"휴식 중 · 새 검색과 AI 요청 없이 {item['remaining_seconds']//60}분 남았습니다."})
                self.autopilot_schedule_resume_event.wait(min(15,max(1,item["remaining_seconds"])))
                self.autopilot_schedule_resume_event.clear()
                continue
            if item["search_calls_remaining"]<=0:
                research_scheduler.pause(schedule_id,profile_id)
                self.autopilot_events.put({"stage":"schedule","message":"승인된 검색 한도에 도달해 안전하게 멈췄습니다."})
                break
            self.autopilot_events.put({"stage":"schedule","message":"연구 구간 · 기존 Autopilot으로 승인된 범위에서 검색합니다."})
            resumed_config=autopilot.AutopilotConfig(profile_id=config.profile_id,target=config.target,
                max_search_calls=config.max_search_calls,provider_id=config.provider_id,
                allow_paid_external=config.allow_paid_external,cost_estimate_usd=config.cost_estimate_usd,
                cost_limit_usd=config.cost_limit_usd,session_id=config.session_id)
            last_result=research_scheduler.run_autopilot_block(schedule_id,profile_id,
                self.autopilot_controller,resumed_config,collector,provider=provider,
                on_event=self.autopilot_events.put,resource_guard=self.resource_guard if low_load_enabled else None)
            if last_result.state=="complete":
                self._research_browser_boundary_close("complete")
                research_scheduler.complete(schedule_id,profile_id)
                break
            if self.autopilot_schedule_stop_event.is_set():
                item=research_scheduler.get(schedule_id,profile_id)
                if item and not item["search_in_flight"] and item["state"] in {"running","paused","scheduled"}:
                    research_scheduler.cancel(schedule_id,profile_id)
                self.autopilot_events.put({"stage":"schedule","message":"예약을 안전하게 중단했습니다. 진행 기록은 보존했습니다."})
                break
            item=research_scheduler.get(schedule_id,profile_id)
            if item and item["state"]=="running" and item["phase"]=="research":
                # Any non-resource pause/error is surfaced to the operator; do
                # not automatically replay a request or spin through retries.
                research_scheduler.pause(schedule_id,profile_id)
                self.autopilot_events.put({"stage":"schedule","message":"조사가 일시 정지되었습니다. 결과와 대기함을 확인한 뒤 이어가세요."})
                break
            config.session_id=last_result.session_id
        self._research_browser_boundary_close("worker_exit")
        final=research_scheduler.get(schedule_id,profile_id)
        self.autopilot_events.put({"stage":"schedule_status","item":final})
        return last_result or autopilot.AutopilotResult(profile_id,"","paused",{},(),None)

    def _autopilot_poll(self):
        if not self.winfo_exists(): return
        try:
            while True:
                event=self.autopilot_events.get_nowait()
                stage=event.get("stage")
                if stage=="quota_status":
                    profile_usage=event.get("profile",{})
                    project_usage=event.get("project",{})
                    self.autopilot_quota_text.set(
                        "오늘 검색량(추정) · 프로필 {}/{}회 · 공유 프로젝트 {}/{}회 · 기타 API 예상 단위 {}/{} · 실제 잔여량은 Google Cloud에서 확인하세요.".format(
                            profile_usage.get("search_calls_used_estimate",0),
                            profile_usage.get("search_calls_limit_configured",0),
                            project_usage.get("search_calls_used_estimate",0),
                            project_usage.get("search_calls_limit_configured",0),
                            project_usage.get("other_units_used_estimate",0),
                            project_usage.get("other_units_limit_configured",0)))
                elif stage=="result":
                    self.autopilot_result=event["result"]
                    self.profile_combo.configure(state="readonly")
                    self.autopilot_start_button.configure(state="normal")
                    self.autopilot_pause_button.configure(state="disabled")
                    self.autopilot_stop_button.configure(state="disabled")
                    self._autopilot_render_result(self.autopilot_result)
                elif stage=="fatal":
                    self.profile_combo.configure(state="readonly")
                    self.autopilot_start_button.configure(state="normal")
                    self.autopilot_pause_button.configure(state="disabled")
                    self.autopilot_stop_button.configure(state="disabled")
                    self._autopilot_show_exception(event["exception"])
                    self.autopilot_stage_text.set(event["exception"]["impact"]+" "+event["exception"]["recommended_action"])
                elif stage=="browser":
                    self._browser_operation_running=False
                    error=event.get("error")
                    if error:
                        messages={"resource":"컴퓨터가 바빠 새 연구 창을 열지 않았습니다.",
                            "ownership":"창 소유권을 확인하지 못해 다른 브라우저는 건드리지 않았습니다.",
                            "unavailable":"전용 창을 열지 못했습니다. ‘기본 브라우저에서 수동 열기’를 선택할 수 있습니다.",
                            "unknown":"연구 창을 안전하게 처리하지 못했습니다. 다른 창은 종료하지 않았습니다."}
                        self.research_browser_status.set(messages.get(error,"연구 창 상태를 확인할 수 없습니다."))
                    elif event.get("operation")=="open":
                        self._research_browser_show_metrics(event["result"])
                    elif event.get("operation")=="manual":
                        self.research_browser_status.set(
                            "기본 브라우저를 열었습니다. 이 창은 ZISU가 자동으로 닫지 않으며, 감상 메모는 ZISU에 저장됩니다."
                            if event["result"] else "기본 브라우저를 열지 못했습니다. 브라우저를 직접 열거나 영상을 복사해 주세요.")
                    else:
                        result=event["result"]
                        close_label=("정상 종료" if result.closed and not result.forced_process_ids else
                            "소유한 응답 없는 프로세스 정리" if result.closed else "종료 확인 필요")
                        self.research_browser_status.set(
                            f"전용 창 {close_label} · "
                            f"닫기 전 프로세스 메모리 {result.rss_before_bytes/(1024**2):.0f} MB · "
                            f"남은 프로세스 {len(result.residual_process_ids)}개 · "
                            f"현재 할 일: 휴식 또는 다음 연구 구간을 기다리세요.")
                elif stage=="browser_boundary":
                    result=event.get("result")
                    if result:
                        self.research_browser_status.set(
                            f"연구 구간 종료 · 전용 창 {'정리 완료' if result.closed else '잔류 확인 필요'} · "
                            f"남은 프로세스 {len(result.residual_process_ids)}개 · 새 창은 휴식 중 열리지 않습니다.")
                elif stage=="browser_metrics":
                    if event.get("metrics") is not None:
                        self._research_browser_show_metrics(event["metrics"])
                elif stage=="resource_status":
                    self.resource_status_text.set(event.get("status","리소스 상태를 확인할 수 없습니다"))
                    if not event.get("allow",True):
                        self._resource_was_waiting=True
                        self.autopilot_stage_text.set(event.get("status","리소스 사용량이 높아 잠시 멈췄습니다")+" · 저장된 예산 안에서 다시 시작할 수 있어요")
                    elif self._resource_was_waiting:
                        self._resource_was_waiting=False
                        self.autopilot_stage_text.set("컴퓨터 상태가 좋아졌습니다. 저장된 예산과 체크포인트를 확인해 조사를 이어갑니다.")
                elif stage=="schedule_status" and event.get("item"):
                    item=event["item"]
                    elapsed=item.get("elapsed_seconds",0)
                    total=max(1,int(item.get("total_minutes",1))*60)
                    self.autopilot_progress.configure(value=min(100,100*elapsed/total))
                    self.schedule_progress_text.set(
                        f"{item.get('phase','대기')} · 경과 {elapsed//60}분 · 남은 시간 {item.get('remaining_seconds',0)//60}분 · 검색 잔여 {item.get('search_calls_remaining',0)}회")
                elif stage=="schedule" and event.get("message"):
                    self.autopilot_stage_text.set(event["message"])
                elif stage=="progress":
                    max_calls=max(1,int(self.ai_max_calls.get()))
                    self.autopilot_progress.configure(value=min(95,100*event.get("calls_used",0)/max_calls))
                    self.autopilot_counts_text.set(f"검색 {event.get('calls_used',0)}회 / 잔여 {event.get('budget_remaining',0)} · 후보 {event.get('candidate_count',0)} · 중복 {event.get('duplicate_count',0)} · 추천 적합 {event.get('recommended_fit_count',0)}/{self.ai_target.get()} · AI 검토 {event.get('ai_review_count',0)} · 사람 확인 {event.get('human_reviewed_count',0)} · 불확실 {event.get('uncertain_count',0)}")
                    self.autopilot_stage_text.set("후보 수집·규칙 판정·필요한 경우에만 AI 검토 중")
                elif stage=="search":
                    self.autopilot_stage_text.set(event.get("message") or f"검색 중 · {event.get('keyword','')}")
                elif stage in {"validate","session","resume","drafts","report"}:
                    self.autopilot_stage_text.set(event.get("message",stage))
                elif stage=="exception":
                    for item in event.get("exceptions",[]): self._autopilot_show_exception(item)
                elif stage=="complete":
                    self.autopilot_progress.configure(value=100 if event.get("state")=="complete" else self.autopilot_progress["value"])
                else:
                    if event.get("exception"): self._autopilot_show_exception(event["exception"])
        except queue.Empty:
            pass
        if self.autopilot_controller.running or self.autopilot_schedule_running or not self.autopilot_events.empty():
            self.after(100,self._autopilot_poll)
        elif self.autopilot_close_pending:
            self._prepare_close()
            self.destroy()

    def _autopilot_render_result(self,result):
        report=result.report
        self.autopilot_saved_report=report
        self.autopilot_ai_stats.set(
            f"Gemini/AI 실제 요청 {report.get('ai_requests_used',0)} · 성공 {report.get('ai_request_success',0)} · 실패 {report.get('ai_request_failed',0)} · 캐시 재사용 {report.get('ai_cache_reused',0)} · 예산 차단 {report.get('ai_budget_blocked',0)} · 규칙만 처리 {report.get('rules_only_candidates',0)}")
        self.autopilot_progress.configure(value=100 if result.state=="complete" else self.autopilot_progress["value"])
        self.autopilot_stage_text.set(f"조사 {result.state} · 목표 {report.get('target_fits',0)}개 / 확보 {report.get('counts',{}).get('ai_recommended_fit',0)}개 / 부족 {report.get('shortfall',0)}개")
        self.autopilot_counts_text.set(f"검색 {report.get('search_calls_used',0)}회 / 잔여 {report.get('search_calls_remaining',0)} · 후보 {report.get('candidate_total',0)} · 중복 {report.get('duplicate_removed',0)} · 추천 적합 {report.get('counts',{}).get('ai_recommended_fit',0)}/{report.get('target_fits',0)} · AI 요청 {report.get('ai_requests_used',0)}/10 · 사람 확인 {report.get('counts',{}).get('human_verified',0)}")
        self.autopilot_exception_tree.delete(*self.autopilot_exception_tree.get_children())
        for item in result.exceptions: self._autopilot_show_exception(item)
        self.autopilot_report.configure(state="normal"); self.autopilot_report.delete("1.0","end")
        self.autopilot_report.insert("1.0",json.dumps(report,ensure_ascii=False,indent=2)); self.autopilot_report.configure(state="disabled")
        if result.run:
            self.ai_reviews={item.video_id:item for item in result.run.reviews}
            self.ai_candidate_rows={str(item.get("video_id")):item for item in result.run.candidates}
            self.ai_tree.delete(*self.ai_tree.get_children())
            for item in result.run.reviews:
                row=self.ai_candidate_rows.get(item.video_id,{})
                proposal=ai_research.proposed_verdict(store.get_profile(result.profile_id),item,row)
                self.ai_tree.insert("","end",iid=item.video_id,values=(ai_research.PROPOSED_VERDICT_LABELS[proposal],item.rule_verdict,
                    item.ai_opinion.verdict if item.ai_opinion else "미호출",row.get("video_title",item.video_id)))
        self._ai_apply_filter()
        self._autopilot_refresh_profile()

    def _autopilot_pause(self):
        if self.autopilot_schedule_running:
            self.autopilot_schedule_pause_event.set()
            self.autopilot_controller.pause()
            self.autopilot_stage_text.set("일시 정지 요청 · 진행 중인 페이지를 마친 뒤 체크포인트를 저장합니다.")
        elif self.autopilot_controller.pause(): self.autopilot_stage_text.set("일시 정지 요청 · 현재 페이지 완료 후 체크포인트 저장")

    def _autopilot_stop(self):
        if self.autopilot_schedule_running:
            self.autopilot_schedule_stop_event.set()
            self.autopilot_schedule_resume_event.set()
            self.autopilot_controller.cancel()
            self.autopilot_stage_text.set("안전 중단 요청 · 현재 요청 뒤 예약을 중단하고 체크포인트를 보존합니다.")
        elif self.autopilot_controller.cancel(): self.autopilot_stage_text.set("안전 중단 요청 · 현재 읽기 요청 완료 후 결과와 세션을 보존")

    def _autopilot_resume(self):
        if self.autopilot_schedule_running:
            self.autopilot_schedule_resume_event.set()
            self.autopilot_stage_text.set("저장된 연구 일정을 이어갑니다.")
            return
        profile=self.current_profile()
        if not profile: return
        try:
            research_scheduler.recover_interrupted(profile["id"])
            schedules=research_scheduler.list_active(profile["id"])
        except Exception:
            schedules=[]
        for schedule in schedules:
            if schedule["state"] not in {"paused","scheduled"}: continue
            if not messagebox.askyesno("이전 작업이 발견되었습니다.",
                f"저장 시각: {schedule.get('updated_at','확인 불가')}\n"
                f"프로필: {profile['name']}\n단계: {schedule.get('phase','확인 필요')}\n"
                f"검색 호출: {schedule.get('search_calls_used',0)}회 사용 / {schedule.get('approved_search_calls',0)}회 승인\n\n"
                "이전 작업을 확인하고 이어갈까요? 외부 요청은 자동으로 다시 보내지 않습니다.",parent=self):
                return
            checkpoint=json.loads(schedule.get("checkpoint_json") or "{}")
            autopilot_session_id=checkpoint.get("autopilot_session_id")
            if schedule.get("search_in_flight"):
                confirm=messagebox.askyesno("요청 결과 확인 필요",
                    "종료 직전에 검색 요청이 전송되었을 수 있어 같은 요청을 자동 재전송하지 않습니다.\n\n"
                    "결과가 불확실한 요청을 검색 한도 1회 사용 처리하고 이어갈까요?",parent=self)
                if not confirm: return
                try: research_scheduler.acknowledge_uncertain_search_consumed(schedule["id"],profile["id"])
                except ValueError: return
                schedule=research_scheduler.get(schedule["id"],profile["id"])
            settings=(store.get_autopilot_settings(profile["id"],autopilot_session_id)
                      if autopilot_session_id else None)
            if not settings and all(key in checkpoint for key in (
                    "approved_provider_id","approved_model","approved_target",
                    "approved_max_search_calls","paid_external_approved")):
                settings={"provider_id":checkpoint["approved_provider_id"],
                    "model":checkpoint["approved_model"],"target":checkpoint["approved_target"],
                    "max_search_calls":checkpoint["approved_max_search_calls"],
                    "cost_estimate_usd":checkpoint.get("cost_estimate_usd"),
                    "cost_limit_usd":checkpoint.get("cost_limit_usd"),
                    "paid_external_approved":checkpoint["paid_external_approved"],
                    "report_version":1}
            if not settings:
                self._autopilot_show_exception({"type":"approval_missing","impact":"이전 작업의 승인 범위를 확인할 수 없습니다.",
                    "recommended_action":"이전 외부 요청을 자동 재개하지 않았습니다. 예산을 확인하고 새 조사를 승인하세요.","retry_allowed":False})
                return
            if schedule["state"]=="paused" and not str(schedule.get("pause_reason","")).startswith(("system_","process_")):
                confirm_external=schedule.get("pause_reason") in {
                    "auth_required","permission_denied","quota_exhausted","quota_local_limit","rate_limited"}
                if confirm_external and not messagebox.askyesno(
                        "연결 또는 검색 한도 확인",
                        "Google 계정이나 검색 한도에 관한 대기 상태입니다. 문제가 해결됐는지 확인한 뒤, 저장된 예산 안에서만 이어갑니다. 계속할까요?",
                        parent=self):
                    return
                try: research_scheduler.resume(schedule["id"],profile["id"],
                    external_recovery_confirmed=confirm_external)
                except (ValueError,research_scheduler.UnresolvedSearchCall): return
            self._autopilot_start((settings,autopilot_session_id,schedule["id"]))
            return
        for session in store.list_resumable_sessions(profile["id"]):
            settings=store.get_autopilot_settings(profile["id"],session["id"])
            if settings:
                self._autopilot_start((settings,session["id"]))
                return
        self._autopilot_show_exception({"type":"no_recovery","impact":"이어갈 Autopilot 작업이 없습니다.","recommended_action":"현재 프로필을 확인하거나 새 조사를 시작하세요.","retry_allowed":False,"status":"info"})

    def _autopilot_export(self):
        report=getattr(self,"autopilot_saved_report",None)
        if not report:
            self._autopilot_show_exception({"type":"report_unavailable","impact":"내보낼 보고서가 없습니다.","recommended_action":"Autopilot 조사를 완료하거나 저장된 세션을 재개하세요.","retry_allowed":False})
            return
        target=filedialog.asksaveasfilename(parent=self,defaultextension=".json",initialfile="zisu_autopilot_report.json",filetypes=[("JSON","*.json")])
        if target:
            try:
                autopilot.export_report(report,target)
                self.autopilot_stage_text.set("프로필 전용 조사 보고서를 JSON으로 내보냈습니다.")
            except OSError:
                self._autopilot_show_exception({"type":"report_export","impact":"보고서 파일을 쓸 수 없습니다.","recommended_action":"다른 저장 경로와 폴더 쓰기 권한을 확인하세요.","retry_allowed":True})

    def _research_browser_select(self,_event=None):
        selected=self.research_browser_choice.get()
        try:
            self.research_browser.select_browser(selected,persist=True)
            self.research_browser_status.set(
                f"연구용 브라우저를 {selected}(으)로 저장했습니다. 기존 Chrome·Edge 창과 프로필은 그대로 유지됩니다.")
        except BrowserError as exc:
            self.research_browser_choice.set(self.research_browser.preferred_browser)
            self.research_browser_status.set(str(exc))

    def _research_browser_open(self):
        profile=self.current_profile()
        if not profile or not self.selected_video:
            messagebox.showinfo("영상 선택 필요","후보 영상 하나를 먼저 선택한 다음 연구 창을 열어 주세요.",parent=self)
            return
        schedule_id=getattr(self,"autopilot_schedule_id",None)
        if not schedule_id:
            messagebox.showinfo("연구 시간 필요","먼저 연구 시간을 설정하고 연구를 시작해 주세요.",parent=self)
            return
        schedule=research_scheduler.get(schedule_id,profile["id"])
        if not schedule or schedule["state"]!="running" or schedule["phase"]!="research":
            messagebox.showinfo("휴식 중","전용 인터넷 창은 연구 구간에만 열 수 있어요. 다음 연구 구간에 이용해 주세요.",parent=self)
            return
        candidate=store.get_candidate(profile["id"],self.selected_video)
        video_id=extract_video_id((candidate or {}).get("video_url") or self.selected_video)
        if not video_id:
            messagebox.showwarning("영상 주소 확인","선택한 후보의 YouTube 영상 주소를 확인할 수 없습니다.",parent=self)
            return
        if not messagebox.askyesno("연구용 인터넷 창 열기",
                "선택한 영상만 별도 연구 창에서 엽니다. 재생·좋아요·댓글·구독은 자동으로 하지 않습니다.\n\n"
                "브라우저 로그인 정보나 쿠키는 복사되지 않습니다. 계속할까요?",parent=self):
            return
        url=f"https://www.youtube.com/watch?v={video_id}"
        self.research_browser_status.set("전용 연구 창을 준비하고 있어요…")
        self._browser_operation("open",url)

    def _research_browser_close(self):
        self._browser_operation("close")

    def _research_browser_manual_open(self):
        profile=self.current_profile()
        if not profile or not self.selected_video:
            messagebox.showinfo("영상 선택 필요","후보 영상 하나를 먼저 선택해 주세요.",parent=self)
            return
        schedule_id=getattr(self,"autopilot_schedule_id",None)
        schedule=research_scheduler.get(schedule_id,profile["id"]) if schedule_id else None
        if not schedule or schedule["state"]!="running" or schedule["phase"]!="research":
            messagebox.showinfo("연구 시간 필요","영상은 연구 구간에 열 수 있어요. 먼저 연구를 시작해 주세요.",parent=self)
            return
        candidate=store.get_candidate(profile["id"],self.selected_video)
        video_id=extract_video_id((candidate or {}).get("video_url") or self.selected_video)
        if not video_id:
            messagebox.showwarning("영상 주소 확인","선택한 후보의 YouTube 영상 주소를 확인할 수 없습니다.",parent=self)
            return
        if not messagebox.askyesno("기본 브라우저에서 수동 열기",
                "선택한 영상 주소를 기본 브라우저에서 엽니다.\n"
                "이 창은 ZISU가 관리하거나 자동으로 닫지 않습니다. 감상과 메모는 직접 진행해 주세요.\n\n계속할까요?",parent=self):
            return
        self._browser_operation("manual",f"https://www.youtube.com/watch?v={video_id}")

    def _research_browser_recover(self):
        try:
            metrics=self.research_browser.recover_owned_session()
        except BrowserOwnershipUncertain:
            self.research_browser_status.set("이전 창의 소유권을 확인하지 못해 어떤 창도 닫지 않았어요.")
            return
        except BrowserError:
            self.research_browser_status.set("이전에 남은 전용 연구 창이 없습니다.")
            return
        if metrics.running and messagebox.askyesno("이전 연구 창 확인",
                f"ZISU가 실행한 브라우저 창을 확인했습니다. PID {metrics.root_pid}, 창 {metrics.window_count}개입니다.\n"
                "이 전용 연구 창만 닫을까요?",parent=self):
            self._research_browser_close()
        else:
            self._research_browser_show_metrics(metrics)

    def _browser_operation(self, operation, url=None):
        if getattr(self,"_browser_operation_running",False):
            return
        self._browser_operation_running=True
        def work():
            try:
                if operation=="open":
                    result=self.research_browser.open_selected(url,user_approved=True)
                elif operation=="manual":
                    decision=self.resource_guard.browser_allowed(self.research_browser.resource_sampler())
                    if not decision.allow_new_work:
                        raise BrowserResourceDeferred("resource limited")
                    result=open_manual_research_url(url)
                else:
                    result=self.research_browser.close_owned()
                self.autopilot_events.put({"stage":"browser","operation":operation,"result":result})
            except BrowserResourceDeferred:
                self.autopilot_events.put({"stage":"browser","error":"resource"})
            except BrowserOwnershipUncertain:
                self.autopilot_events.put({"stage":"browser","error":"ownership"})
            except BrowserError:
                self.autopilot_events.put({"stage":"browser","error":"unavailable"})
            except Exception:
                self.autopilot_events.put({"stage":"browser","error":"unknown"})
            finally:
                self._browser_operation_running=False
        threading.Thread(target=work,name="zisu-research-browser",daemon=True).start()

    def _research_browser_show_metrics(self, metrics):
        self.research_browser_status.set(
            f"전용 창 {'실행 중' if metrics.running else '닫힘'} · 창 {metrics.window_count}개 · "
            f"ZISU 소유 프로세스 {metrics.owned_process_count}개 · 메모리 {metrics.owned_rss_bytes/(1024**2):.0f} MB · "
            f"연구 구간 변화 {metrics.rss_change_bytes/(1024**2):+.0f} MB · "
            f"현재 할 일: 후보 영상을 선택해 직접 확인하거나 연구 구간을 마치세요.")

    def _research_browser_metrics_probe(self):
        try:
            metrics=self.research_browser.metrics()
            self.autopilot_events.put({"stage":"browser_metrics","metrics":metrics})
        except Exception:
            pass
        finally:
            self._browser_metrics_running=False

    def _research_browser_boundary_close(self, reason):
        if not self.research_browser.session:
            return
        try:
            result=self.research_browser.close_owned()
            self.autopilot_events.put({"stage":"browser_boundary","reason":reason,"result":result})
        except Exception:
            self.autopilot_events.put({"stage":"browser_boundary","reason":reason,"error":True})

    def _check_research_browser_schedule(self):
        if not self.autopilot_schedule_running or not self.autopilot_schedule_id or not self.active_id:
            return
        now=time.monotonic()
        if now-self._browser_schedule_checked_at<10:
            return
        self._browser_schedule_checked_at=now
        try:
            item=research_scheduler.get(self.autopilot_schedule_id,self.active_id)
        except Exception:
            return
        if not item or item["phase"]!="research":
            return
        if (item["state"]=="running" and 0<int(item.get("phase_remaining_seconds",0))<=60
                and not item.get("research_extension_used")
                and self.autopilot_schedule_id not in self._browser_alerted_schedules):
            self._browser_alerted_schedules.add(self.autopilot_schedule_id)
            extend=messagebox.askyesno("연구 시간이 곧 끝나요",
                "1회에 한해 연구 구간을 10분 연장할까요? 연장은 이 일정에서 한 번만 사용할 수 있습니다.\n\n"
                "연장하지 않으면 연구용 창을 닫고 휴식 시간으로 전환합니다. YouTube 웹페이지의 미게시 입력은 보존되지 않을 수 있어요.",
                parent=self)
            if extend:
                try:
                    research_scheduler.extend_current_research(self.autopilot_schedule_id,self.active_id)
                    self.autopilot_events.put({"stage":"schedule","message":"연구 구간을 10분 연장했습니다. 다음 휴식 전까지 1회만 사용할 수 있어요."})
                except Exception:
                    self.autopilot_events.put({"stage":"schedule","message":"연장을 저장하지 못했습니다. 체크포인트를 보존하고 예정된 휴식으로 전환합니다."})
                    self._browser_operation("close")
            else:
                self._browser_operation("close")

    def _autopilot_window_close(self):
        if self.autopilot_schedule_running:
            self.autopilot_schedule_shutdown_event.set()
            self.autopilot_schedule_pause_event.set()
            self.autopilot_schedule_resume_event.set()
            self.autopilot_controller.pause()
            self.autopilot_close_pending=True
            self.autopilot_stage_text.set("앱 종료 대기 · 진행 중인 요청을 마친 뒤 예약 체크포인트를 저장합니다.")
            self.after(100,self._autopilot_poll)
        elif self.autopilot_controller.running:
            self.autopilot_controller.pause()
            self.autopilot_close_pending=True
            self.autopilot_stage_text.set("앱 종료 대기 · 현재 요청 완료 후 세션을 일시 정지 저장합니다.")
            self.after(100,self._autopilot_poll)
        else:
            self._prepare_close()
            self.destroy()

    def _prepare_close(self):
        if getattr(self,"comment_video",None):self._autosave_comment()
        if getattr(self,"_note_save_after",None) and getattr(self,"selected_video",None):
            try:self.after_cancel(self._note_save_after)
            except tk.TclError:pass
            self._autosave_note()
        self._write_ui_state()

    def _ai_refresh_credential_status(self):
        try:
            credentials=CredentialStore()
            configured=credentials.configured(self.ai_credential_provider.get())
            if configured:
                credentials.load(self.ai_credential_provider.get())
                label="키 등록됨 · 암호화 저장"
            else:
                label="키 미등록 · 외부 모델 기본 차단"
        except CredentialError:
            label="저장 키 복구 실패 · 키를 다시 등록하세요"
        except Exception:
            label="Windows 보안 저장소 확인 불가"
        self.ai_credential_status.set(label)

    def _ai_gemini_list_models(self):
        """Explicit user-triggered account model listing; never calls generateContent."""
        if self.ai_running:
            return
        try:
            if not CredentialStore().load("gemini"):
                raise ai_research.ProviderFailure("missing_key")
        except Exception:
            self.ai_credential_provider.set("gemini")
            self.ai_credential_entry.focus_set()
            messagebox.showinfo("Gemini 키 등록 필요",
                "Gemini 키가 보안 저장소에 없습니다. 마스킹된 입력란에 직접 등록한 뒤 다시 모델 확인을 누르세요.",parent=self)
            return
        self.ai_running=True
        self.ai_gemini_models_button.configure(state="disabled")
        self.ai_status.set("Gemini 모델 목록을 확인 중입니다. 콘텐츠 생성 요청은 하지 않습니다.")
        def worker():
            provider=ai_research.GeminiProvider()
            try:
                models=provider.list_available_models()
                self.ai_gemini_models_queue.put((models,"",provider.list_request_count))
            except ai_research.ProviderFailure as exc:
                self.ai_gemini_models_queue.put(([],exc.code,provider.list_request_count))
            except Exception:
                self.ai_gemini_models_queue.put(([],"unknown",provider.list_request_count))
        threading.Thread(target=worker,name="zisu-gemini-model-list",daemon=True).start()
        self.after(75,self._poll_gemini_models)

    def _poll_gemini_models(self):
        try:
            models,error,list_requests=self.ai_gemini_models_queue.get_nowait()
        except queue.Empty:
            if self.ai_running and self.winfo_exists():
                self.after(75,self._poll_gemini_models)
            return
        self.ai_running=False
        self.ai_gemini_models_button.configure(state="normal")
        if error:
            self.ai_gemini_available_models=set()
            messages={"missing_key":"키 미등록 · 보안 입력란에서 등록하세요.",
                "auth":"인증/권한 오류(401/403) · API 키와 프로젝트 사용 권한을 확인하세요.",
                "not_found":"요청 주소를 찾지 못했습니다(404) · API 사용 설정을 확인하세요.",
                "rate_limit":"조회 한도 초과(429) · 잠시 후 공급자 상태를 확인하세요. 자동 재시도하지 않았습니다.",
                "server":"서버 오류(5xx) · 잠시 뒤 수동으로 다시 확인할 수 있습니다.",
                "timeout":"네트워크 시간 초과 · 결과가 불명확할 수 있습니다. 자동 재시도하지 않았습니다.",
                "network":"네트워크 연결 실패 · 연결을 확인하세요.",
                "bad_response":"모델 목록 응답 형식 오류 · 원문은 표시하지 않았습니다."}
            self.ai_status.set(f"Gemini 모델 조회 실패 · 목록 요청 {list_requests}회 · 생성 요청 0회 · "+messages.get(error,"안전하게 처리하지 못한 오류입니다."))
            return
        names=[item["id"] for item in models]
        self.ai_gemini_available_models=set(names)
        self.ai_gemini_model_combo.configure(values=names)
        if names:
            preferred=("gemini-3.5-flash-lite","gemini-3.8-flash","gemini-3.6-flash",
                       "gemini-3.5-flash","gemini-2.5-flash")
            self.ai_gemini_model.set(next((name for name in preferred if name in names),names[0]))
            self.ai_status.set(f"모델 목록 조회 완료 · 목록 요청 {list_requests}회 · generateContent 및 확인된 JSON 구조화 출력 지원 모델 {len(names)}개 · 생성 요청 0회")
        else:
            self.ai_gemini_available_models=set()
            self.ai_gemini_model_combo.configure(values=())
            self.ai_gemini_model.set("")
            self.ai_status.set(f"현재 계정 목록에서 확인된 구조화 출력 지원 모델이 없습니다 · 목록 요청 {list_requests}회 · 생성 요청 0회")

    def _ai_gemini_connection_test(self):
        if self.ai_running:
            return
        try:
            estimate, ceiling = ai_research.validate_cost_budget(
                self.ai_cost_estimate.get(), self.ai_cost_limit.get())
        except ValueError:
            messagebox.showwarning("예상 예산 확인",
                "공급자의 현재 가격을 확인해 1회 예상 비용과 1일 한도(USD)를 입력하세요. "
                "비용 확인이 어려우면 이 테스트를 진행하지 마세요.", parent=self)
            return
        try:
            stored_key = CredentialStore().load("gemini")
            configured = bool(stored_key)
            del stored_key
        except Exception:
            configured = False
        if not configured:
            self.ai_credential_provider.set("gemini")
            self.ai_credential_entry.focus_set()
            messagebox.showinfo("Gemini 키 등록 필요",
                "Gemini 키가 보안 저장소에 없습니다. 이 화면의 마스킹된 키 입력란에서 직접 저장한 뒤 다시 시작하세요. "
                "키를 채팅이나 로그에 붙여넣지 마세요.", parent=self)
            return
        model_var=getattr(self,"ai_gemini_model",None)
        selected_model=(model_var.get() if model_var is not None else "gemini-3.5-flash-lite").strip()
        if selected_model not in ai_research.GEMINI_REQUEST_CONTRACTS:
            messagebox.showwarning("Gemini 모델 확인 필요","목록 조회에서 확인된 구조화 출력 지원 모델을 선택하세요.",parent=self)
            return
        verified_models=getattr(self,"ai_gemini_available_models",None)
        if verified_models is not None and selected_model not in verified_models:
            messagebox.showinfo("Gemini 모델 확인 필요","먼저 ‘Gemini 사용 가능 모델 확인’을 눌러 이 계정에서 확인된 모델을 선택하세요.",parent=self)
            return
        consent=("Gemini 실제 연결 테스트를 1회 실행합니다.\n\n"
            f"모델: {selected_model}\n"
            "전송 자료: 朝のプレイリスト / 朝に聴きたい落ち着いた音楽。ボーカルとラップの有無は不明です。\n"
            f"입력 예상 비용: ${estimate:g} · 입력 한도: ${ceiling:g}\n\n"
            "예상치는 실제 청구액을 보장하지 않으며 프로젝트 과금이 발생할 수 있습니다. "
            "무료/유료 상태는 앱이 추측하지 않습니다. 확인 후 외부 요청은 최대 1회이며 자동 재시도는 없습니다.\n"
            "YouTube/OAuth, 사용자 DB, 실제 영상·메모·이메일·채널 ID·토큰은 사용하지 않습니다.\n\n"
            "이 요청 1회를 승인하시겠습니까?")
        if not messagebox.askyesno("Gemini 1회 호출 최종 승인",consent,parent=self):
            self.ai_status.set("Gemini 연결 테스트 취소됨 · 외부 요청 0회")
            return
        self.ai_running=True
        self.ai_gemini_test_button.configure(state="disabled")
        self.ai_status.set("Gemini 연결 테스트 중 · 가상 자료 1건만 전송하며 재시도하지 않습니다.")
        def worker():
            try:
                result=ai_research.run_gemini_connection_test(
                    cost_estimate_usd=estimate,cost_limit_usd=ceiling,
                    provider=ai_research.GeminiProvider(model=selected_model),model=selected_model)
            except Exception:
                result=ai_research.GeminiConnectionResult(selected_model,0,False,False,
                    False,"","검토",0,False,True,"unknown")
            self.ai_gemini_test_queue.put(result)
        threading.Thread(target=worker,name="zisu-gemini-one-shot",daemon=True).start()
        self.after(75,self._poll_gemini_connection_test)

    def _poll_gemini_connection_test(self):
        try:
            result=self.ai_gemini_test_queue.get_nowait()
        except queue.Empty:
            if self.ai_running and self.winfo_exists():
                self.after(75,self._poll_gemini_connection_test)
            return
        self.ai_running=False
        self.ai_gemini_test_button.configure(state="normal")
        if result.error_code:
            errors={
                "missing_key":"키 없음 · 이 화면의 보안 입력란에서 Gemini 키를 저장하세요.",
                "auth":"인증 오류 401/403 · 키 유효성, API 사용 설정, 프로젝트 권한을 확인하세요.",
                "invalid_request":"요청 형식 400 · 모델 또는 출력 형식이 거부되었습니다.",
                "rate_limit":"사용량 초과 429 · 할당량/속도 제한을 확인하고 자동 재호출하지 마세요.",
                "server":"서버 오류 5xx · 공급자 상태를 확인하고 결과가 불명확하면 대시보드를 확인하세요.",
                "timeout":"네트워크 시간 초과 · 요청이 처리됐는지 알 수 없으므로 자동 재시도하지 마세요.",
                "network":"네트워크 연결 실패 · 연결을 점검하세요. 자동 재시도하지 않았습니다.",
                "model_unavailable":"모델 사용 불가 · 이 계정의 모델 목록에서 확인된 모델인지 확인하세요.",
                "not_found":"모델 목록 주소를 찾지 못했습니다(404) · Gemini API 설정과 앱 버전을 확인하세요.",
                "json_invalid":"JSON 파싱 실패 · 응답은 저장하거나 표시하지 않았습니다.",
                "schema_invalid":"JSON Schema 검증 실패 · 응답은 저장하거나 판정에 반영하지 않았습니다.",
                "bad_response":"JSON/응답 검증 실패 · 원문 응답은 표시하지 않았습니다.",
                "budget":"예산 검사 차단 · 유효한 양수 예상 비용과 한도를 입력하세요.",
            }
            message=errors.get(result.error_code,"알 수 없는 오류 · 비밀정보와 응답 원문은 폐기했습니다.")
            report=(f"Gemini 연결: FAIL · {message}\n요청 횟수: {result.request_count} · "
                    f"HTTP 응답 성공: {'예' if result.http_success else '아니요'} · "
                    f"소요시간: {result.elapsed_ms}ms · API 키 노출: 없음 · 임시 DB 격리: 예")
        else:
            japanese="PASS (일본어 문자 처리)" if result.japanese_text_processed else "PASS (UTF-8 응답 처리; 일본어 문자 미포함)"
            report=(f"Gemini 연결: PASS\n모델: {result.model}\nHTTP 요청/응답: 성공 · JSON Schema 검증: PASS\n"
                    f"일본어 응답 처리: {japanese}\nAI 판정: {result.ai_verdict} · 최종 판정: {result.final_verdict}\n"
                    f"소요시간: {result.elapsed_ms}ms · 요청 횟수: {result.request_count}\n"
                    "API 키 노출: 없음 · 테스트 DB: 격리됨\n실제 사용료: 공급자 대시보드 확인 필요")
        self.ai_status.set(report)
        messagebox.showinfo("Gemini 연결 테스트 결과",report,parent=self)

    def _ai_save_credential(self):
        provider=self.ai_credential_provider.get()
        secret=self.ai_credential_entry.get()
        try:
            CredentialStore().save(provider,secret)
            self.ai_credential_entry.delete(0,"end")
            self._ai_refresh_credential_status()
            messagebox.showinfo("저장 완료","API 키를 Windows 현재 사용자 DPAPI로 암호화해 저장했습니다. 키 값은 표시하거나 기록하지 않습니다.",parent=self)
        except CredentialError as exc:
            self.ai_credential_entry.delete(0,"end")
            messagebox.showwarning("안전 저장 실패",str(exc),parent=self)

    def _ai_delete_credential(self):
        try:
            CredentialStore().delete(self.ai_credential_provider.get())
            self.ai_credential_entry.delete(0,"end")
            self._ai_refresh_credential_status()
        except CredentialError as exc:
            messagebox.showwarning("삭제 실패",str(exc),parent=self)

    def _ai_pause(self):
        if self.ai_pause_event:
            self.ai_pause_event.set()
            self.ai_status.set("일시 정지 요청됨 · 현재 읽기 요청이 끝나면 체크포인트를 저장합니다.")

    def _ai_resume_latest(self):
        if not self.active_id:
            messagebox.showinfo("프로필 선택","이어갈 조사 프로필을 먼저 선택하세요.",parent=self); return
        sessions=store.list_resumable_sessions(self.active_id)
        if not sessions:
            messagebox.showinfo("이어갈 작업 없음","현재 프로필에 이어갈 조사 작업이 없습니다.",parent=self); return
        session=sessions[0]
        fit_mode=session.get("target_mode") == "fit_target"
        self.ai_target_mode.set("추천 적합 수" if fit_mode else "전체 후보 수")
        self.ai_target_combo.configure(values=("5","10","20") if fit_mode else ("10","30","50"))
        profile=store.get_profile(self.active_id)
        if not profile or (profile["market"],profile["genre"]) != (session["market"],session["genre"]):
            messagebox.showwarning("프로필 불일치","작업 당시 국가·장르와 현재 프로필이 달라 이어갈 수 없습니다.",parent=self); return
        self.ai_target.set(str(session["target"]))
        self.ai_max_calls.set(str(session["max_search_calls"]))
        try:
            plan=ai_research.preview_plan(self.active_id,session["target"],max_search_calls=session["max_search_calls"],
                target_mode="fit_target" if fit_mode else "candidate_count")
        except (ValueError,KeyError):
            messagebox.showwarning("계획 복구 실패","저장된 조사 계획을 다시 만들 수 없습니다.",parent=self); return
        plan["provider_id"]=self.ai_provider_ids.get(self.ai_provider_choice.get(),"disabled")
        plan["session_id"]=session["id"]
        self.ai_confirmed_plan=plan
        if messagebox.askyesno("저장된 조사 이어가기",f"{profile['name']}의 저장된 조사 작업을 이어갑니다.\n중단 중이던 읽기 검색은 재확인되어 YouTube 할당량을 한 번 더 쓸 수 있습니다. 계속할까요?",parent=self):
            self._ai_start_live()

    def _ai_preview_plan(self):
        if not self.active_id:
            messagebox.showinfo("프로필 선택","먼저 작업 프로필을 추가하거나 선택하세요.",parent=self); return
        try:
            target_mode="fit_target" if self.ai_target_mode.get()=="추천 적합 수" else "candidate_count"
            plan=ai_research.preview_plan(self.active_id,int(self.ai_target.get()),max_search_calls=int(self.ai_max_calls.get()),target_mode=target_mode)
        except (ValueError,KeyError) as exc:
            messagebox.showwarning("계획 확인",str(exc),parent=self); return
        self.ai_confirmed_plan=plan
        terms="\n".join("  · "+term for term in plan["search_terms"][:6])
        plan["provider_id"]=self.ai_provider_ids[self.ai_provider_choice.get()]
        provider_label=self.ai_provider_choice.get()
        usage=store.ai_usage_summary(self.active_id).get(plan["provider_id"],{"requests":0,"unknown_cost":0})
        budget="비용 없음 (규칙만)" if provider_label=="규칙만" else ("로컬 Ollama; 기기 자원 사용" if provider_label.startswith("Ollama") else "현재 모델 요금 확인이 필요해 비용을 추정하지 않음")
        self.ai_status.set(f"미리보기: {plan['profile_name']} · {plan['market']} / {plan['genre_title']} · 목표 {plan['target_candidates']}개 · 검색어 호출 최대 {plan['max_search_calls']}회 · AI {provider_label} · {budget}\n"
                           f"공급자 오늘 사용 {usage['requests']}/30회 · 이 작업 AI 최대 10회 · 비용 미확정 {usage['unknown_cost']}회. YouTube 검색 호출과 AI 호출/비용은 별도 집계합니다.\n"
                           f"검색어 예시:\n{terms}\n미리보기 중 API/AI 호출 0회. Demo는 오프라인이며, 실제 조사 버튼은 확인 후 YouTube 읽기 전용 검색을 수행합니다.")

    def _ai_start_demo(self):
        plan=self.ai_confirmed_plan
        if not plan or plan.get("profile_id") != self.active_id:
            messagebox.showinfo("계획 먼저 보기","조사 프로필을 선택한 뒤 계획을 다시 미리보기 하세요.",parent=self); return
        if not messagebox.askyesno("Demo 계획 확인",
             f"{plan['profile_name']} ({plan['market']} / {plan['genre_title']})의 가상 데이터 3건을 검사합니다.\n\n"
             "실제 검색·외부 AI 호출·DB 저장은 하지 않습니다. 진행할까요?",parent=self): return
        if self.ai_running: return
        profile_id=self.active_id
        self.ai_expected_profile=profile_id
        self.ai_running=True
        self.ai_status.set("오프라인 Demo 실행 중… UI는 계속 사용할 수 있습니다.")
        for item in self.ai_tree.get_children(): self.ai_tree.delete(item)
        self.ai_reviews={}
        def worker():
            try: result=ai_research.run_demo(profile_id)
            except Exception as exc: result=exc
            self.ai_demo_queue.put(("demo",profile_id,result))
        threading.Thread(target=worker,name="zisu-ai-demo",daemon=True).start()
        self.after(50,self._poll_ai_demo_result)

    def _poll_ai_demo_result(self):
        try: kind,profile_id,result=self.ai_demo_queue.get_nowait()
        except queue.Empty:
            if self.ai_running and self.winfo_exists(): self.after(50,self._poll_ai_demo_result)
            return
        self.ai_running=False
        self.ai_cancel_button.configure(state="disabled")
        self.ai_pause_button.configure(state="disabled")
        if kind=="live": self._ai_live_done(profile_id,result)
        else: self._ai_demo_done(profile_id,result)

    def _ai_demo_done(self,profile_id,result):
        if not self.winfo_exists(): return
        if self.active_id != profile_id:
            self.ai_status.set("프로필이 전환되어 결과 표시를 생략했습니다. 기존 결과는 저장되지 않았습니다."); return
        if isinstance(result,Exception):
            self.ai_status.set("Demo 실행에 실패했습니다. 프로필·DB 상태를 확인하고 다시 시도하세요."); return
        self.ai_reviews={review.video_id:review for review in result}
        self.ai_candidate_rows={candidate.video_id:{"video_title":candidate.title,"video_url":candidate.url}
                                for candidate in ai_research.demo_candidates("MORNING_CHILL_RAP")}
        for review in result:
            title=next((c.title for c in ai_research.demo_candidates("MORNING_CHILL_RAP") if c.video_id==review.video_id),review.video_id)
            ai_label=review.ai_opinion.verdict if review.ai_opinion else "미호출"
            self.ai_tree.insert("","end",iid=review.video_id,values=(review.final_verdict,review.rule_verdict,ai_label,title))
        self.ai_status.set(f"Demo 완료: {len(result)}개 가상 사례 · 네트워크/OAuth/API 호출 0 · DB 변경 0 · 모든 분류는 사람 승인 전까지 제안입니다.")
        children=self.ai_tree.get_children()
        if children: self.ai_tree.selection_set(children[0]); self._ai_show_detail()
        self._ai_apply_filter()

    def _ai_cancel(self):
        if self.ai_cancel_event:
            self.ai_cancel_event.set()
            self.ai_status.set("취소 요청됨 · 현재 읽기 요청이 끝나면 중단하고 수집된 프로필 전용 결과만 저장합니다.")

    def _ai_start_live(self):
        plan=self.ai_confirmed_plan
        if not plan or plan.get("profile_id") != self.active_id:
            messagebox.showinfo("계획 먼저 보기","프로필과 검색 범위를 미리보기한 뒤 실제 조사를 시작하세요.",parent=self); return
        if (int(self.ai_target.get()) != plan["target_candidates"] or
                int(self.ai_max_calls.get()) != plan["requested_search_calls"] or
                plan.get("target_mode") != ("fit_target" if self.ai_target_mode.get()=="추천 적합 수" else "candidate_count") or
                self.ai_provider_ids.get(self.ai_provider_choice.get()) != plan.get("provider_id")):
            self.ai_confirmed_plan=None
            messagebox.showinfo("계획 변경됨","목표·검색 상한·AI 모드가 바뀌었습니다. 계획을 다시 미리보기 하세요.",parent=self); return
        if self.ai_running: return
        profile_id=self.active_id
        key=self.api_key.get().strip()
        if not key and not oauth.is_connected(profile_id):
            messagebox.showinfo("읽기 전용 연결 필요","먼저 이 프로필의 Google 읽기 전용 연결을 완료하거나 YouTube API 키를 설정하세요. 실제 연결 설정은 이번 오프라인 개발 단계에서는 요구하지 않습니다.",parent=self); return
        provider_id=plan["provider_id"]
        provider=ai_research.create_provider(provider_id)
        if provider_id in ("openai","gemini") and not provider.api_key:
            provider=ai_research.create_provider("disabled")
            provider_note=" · 해당 AI 공급자 키가 없어 규칙 전용으로 실행"
        else: provider_note=""
        cost_estimate=cost_limit=None
        if provider_id in ("openai","gemini") and not provider_note:
            try:
                cost_estimate,cost_limit=ai_research.validate_cost_budget(
                    self.ai_cost_estimate.get(),self.ai_cost_limit.get())
            except (TypeError,ValueError):
                messagebox.showwarning("비용 상한 필요","유료 AI는 공급자의 현재 가격을 직접 확인하고, 안전 여유를 포함한 1회 추정 비용과 1일 상한(USD)을 입력해야 합니다. 가격을 확인할 수 없으면 규칙 전용 또는 Ollama를 사용하세요.",parent=self); return
        ai_privacy=("선택한 공급자에 검토 후보 제목·설명을 보낼 수 있습니다. 이메일, OAuth 토큰, 개인 메모는 보내지 않습니다."
                    if provider_id in ("ollama","openai","gemini") and not provider_note else "AI 외부 전송은 하지 않고 기존 규칙 엔진만 사용합니다.")
        consent_text=(f"{plan['profile_name']} · {plan['market']} · {plan['genre_title']}\n"
                      f"목표 {plan['target_candidates']}개 / 검색어 호출 최대 {plan['max_search_calls']}회\n\n"
                      f"YouTube 읽기 전용 검색을 실행하고 후보를 이 프로필에 저장합니다. {ai_privacy} 취소해도 수집된 결과는 이 프로필에만 남습니다.\n\n계속할까요?")
        if not messagebox.askyesno("실제 조사 실행 확인",consent_text,parent=self): return
        while not self.ai_progress_queue.empty():
            try: self.ai_progress_queue.get_nowait()
            except queue.Empty: break
        self.ai_running=True
        self.ai_cancel_event=threading.Event()
        self.ai_pause_event=threading.Event()
        self.ai_cancel_button.configure(state="normal")
        self.ai_pause_button.configure(state="normal")
        self.ai_status.set("읽기 전용 조사 중… 프로필을 전환해도 결과는 시작한 프로필에만 저장됩니다."+provider_note)
        self.ai_reviews={}
        for item in self.ai_tree.get_children(): self.ai_tree.delete(item)
        def worker():
            try:
                token=oauth.get_access_token(profile_id) if not key else None
                quota_base={"profile_id":profile_id,"path":store.db_path(),
                    "project_identity":quota_project_identity(key,
                        (oauth.quota_project_identity(profile_id) or "oauth-project:unknown") if not key else "")}
                def collect(term,market,limit,page_token=None,order="relevance",quota_context=None):
                    context={**quota_base,**(quota_context or {})}
                    return lookup_videos(key,term,market,limit=min(50,limit),access_token=token,
                                         page_token=page_token,order=order,quota_context=context)
                if plan.get("target_mode") == "fit_target":
                    def collect_page(term,market,limit,page_token=None,quota_context=None,order="relevance"):
                        context={**quota_base,**(quota_context or {})}
                        return lookup_videos(key,term,market,limit=min(50,limit),access_token=token,
                                             page_token=page_token,return_page=True,order=order,
                                             quota_context=context)
                    run=ai_research.run_auto_recommendations(profile_id,collect_page,target=plan["target_candidates"],
                        max_search_calls=plan["max_search_calls"],provider=provider,cancel_event=self.ai_cancel_event,
                        session_id=plan.get("session_id"),pause_event=self.ai_pause_event,
                        progress=self.ai_progress_queue.put,
                        allow_paid_external=provider_id in ("openai","gemini") and not provider_note,
                        cost_estimate_usd=cost_estimate,cost_limit_usd=cost_limit)
                else:
                    run=ai_research.run_bounded_research(profile_id,collect,target=plan["target_candidates"],
                        max_search_calls=plan["max_search_calls"],provider=provider,cancel_event=self.ai_cancel_event,
                        session_id=plan.get("session_id"),pause_event=self.ai_pause_event,
                        allow_paid_external=provider_id in ("openai","gemini") and not provider_note,
                        cost_estimate_usd=cost_estimate,cost_limit_usd=cost_limit)
                counts=store.save_candidates(profile_id,run.candidates) if run.candidates else {"적합":0,"검토":0,"제외":0}
                ai_research.persist_review_decisions(profile_id,run.reviews,run.candidates)
                result=(run,counts,provider_note)
            except (APIError,oauth.OAuthError,ValueError,OSError):
                result=RuntimeError("조사를 완료하지 못했습니다. 연결·권한·저장 경로를 확인한 뒤 저장된 작업을 이어가세요.")
            except Exception:
                result=RuntimeError("조사 중 예기치 않은 문제가 발생했습니다. 연결·공급자 설정을 확인하고 다시 시도하세요.")
            self.ai_demo_queue.put(("live",profile_id,result))
        threading.Thread(target=worker,name="zisu-ai-research",daemon=True).start()
        self.after(100,self._poll_ai_progress)
        self.after(50,self._poll_ai_demo_result)

    def _poll_ai_progress(self):
        if not self.winfo_exists(): return
        try:
            progress=self.ai_progress_queue.get_nowait()
        except queue.Empty:
            if self.ai_running: self.after(250,self._poll_ai_progress)
            return
        if progress.get("profile_id")==self.active_id:
            self.ai_status.set(f"자동 조사 진행 중 · 검색 {progress['calls_used']}회 / 잔여 {progress['budget_remaining']}회"
                f" · 후보 {progress['candidate_count']} · 중복 제거 {progress['duplicate_count']}"
                f" · 추천 적합 {progress['recommended_fit_count']}"
                f" · AI 검토 {progress['ai_review_count']} · 사람 확인 {progress['human_reviewed_count']}"
                f" · 불확실 {progress['uncertain_count']}")
        if self.ai_running: self.after(100,self._poll_ai_progress)

    def _ai_live_done(self,profile_id,result):
        if not self.winfo_exists(): return
        if self.active_id != profile_id:
            self.ai_status.set("작업 중 프로필이 전환되어 현재 화면에는 결과를 표시하지 않습니다. 후보와 AI 검토 캐시는 시작한 원래 프로필에만 저장됐습니다."); return
        while not self.ai_progress_queue.empty():
            try: self.ai_progress_queue.get_nowait()
            except queue.Empty: break
        if isinstance(result,Exception):
            self.ai_status.set("읽기 전용 조사에 실패했습니다: "+str(result)[:300]); return
        run,counts,provider_note=result
        self.ai_reviews={review.video_id:review for review in run.reviews}
        self.ai_candidate_rows={str(row.get("video_id")):row for row in run.candidates}
        for review in run.reviews:
            candidate=self.ai_candidate_rows.get(review.video_id,{})
            ai_label=review.ai_opinion.verdict if review.ai_opinion else "미호출"
            proposal=ai_research.PROPOSED_VERDICT_LABELS[ai_research.proposed_verdict(store.get_profile(profile_id),review,candidate)]
            self.ai_tree.insert("","end",iid=review.video_id,values=(proposal,review.rule_verdict,ai_label,candidate.get("video_title",review.video_id)))
        if isinstance(run,ai_research.AutoResearchRun):
            self.ai_status.set(f"자동 조사 {run.state}: 추천 적합 {run.recommended_fits}/{run.target_fits} (부족 {run.shortfall})"
                f" · 검색 호출 {run.calls_used}회 · 중복 제거 {run.duplicate_count} · 후보 {len(run.candidates)}"
                f" · AI 검토 {run.ai_review_count} · 사람 확인 {run.human_reviewed_count} · 부족 후보는 승격하지 않습니다.{provider_note}"
                +(f" · {run.warning}" if run.warning else ""))
        else:
            state="취소되어 일부 결과" if run.cancelled else "완료"
            self.ai_status.set(f"실제 읽기 전용 조사 {state}: 후보 {run.unique_count}개 · 검색 호출 {run.calls_used}회 · 저장 통계 적합 {counts['적합']} / 검토 {counts['검토']} / 제외 {counts['제외']}{provider_note}. AI 의견은 승인 아님.")
        children=self.ai_tree.get_children()
        if children: self.ai_tree.selection_set(children[0]); self._ai_show_detail()

    def _ai_show_detail(self,_event=None):
        selected=self.ai_tree.selection()
        review=self.ai_reviews.get(selected[0]) if selected else None
        if not review: return
        opinion=review.ai_opinion
        candidate=getattr(self,"ai_candidate_rows",{}).get(review.video_id,{})
        proposal=ai_research.PROPOSED_VERDICT_LABELS[ai_research.proposed_verdict(store.get_profile(review.profile_id),review,candidate)]
        text=(f"규칙 분류: {review.rule_verdict} ({review.rule_score}/100)\n"
              f"규칙 근거: {review.rule_reason}\n\n최종 제안: {review.final_verdict}\n"
              f"출처: {review.provenance}\n경고: {review.warning}\n")
        text+=f"AI/규칙 추천: {proposal} (사람 승인 아님; 사람 라벨은 실제 청취 후 별도 기록)\n"
        if opinion:
            text+=(f"\nAI 의견(자동 승인 아님): {opinion.verdict}\n"
                   f"긍정 신호: {', '.join(opinion.positive_signals) or '없음'}\n"
                   f"충돌 신호: {', '.join(opinion.conflict_signals) or '없음'}\n"
                   f"메타데이터 인용: {', '.join(opinion.evidence_from_metadata) or '검증된 인용 없음'}\n"
                   f"불확실성: {opinion.uncertainty or '남아 있을 수 있음'}\n"
                   f"사람이 확인할 점: {opinion.recommended_human_check}\n")
        url=candidate.get("video_url") or candidate.get("url","")
        if not url and re.fullmatch(r"[\w-]{11}",review.video_id):
            url=f"https://www.youtube.com/watch?v={review.video_id}"
        text+=f"\n영상 URL: {url or 'Demo 가상 표본'}\n직접 영상을 듣고 최종 분류·승인을 판단하세요. 감상 체크는 자동 저장되지 않습니다."
        self.ai_detail.configure(state="normal"); self.ai_detail.delete("1.0","end"); self.ai_detail.insert("1.0",text); self.ai_detail.configure(state="disabled")

    def _ai_refresh_saved_results(self):
        if not self.active_id or not hasattr(self,"ai_tree"):return
        selected=self.ai_tree.selection();selected_id=selected[0] if selected else None
        try:
            with store.connect() as db:
                row=db.execute("SELECT reviews_json,candidates_json FROM research_sessions WHERE profile_id=? ORDER BY updated_at DESC LIMIT 1",(self.active_id,)).fetchone()
            payload_reviews=json.loads(row[0]) if row else []
            payload_candidates=json.loads(row[1]) if row else []
            reviews=[ai_research._review_from_payload(item) for item in payload_reviews]
        except (sqlite3.Error,ValueError,KeyError,TypeError):
            self._safe_diagnostic("ai_saved_result_reload_failed");return
        self.ai_tree.delete(*self.ai_tree.get_children())
        self.ai_reviews={item.video_id:item for item in reviews}
        self.ai_candidate_rows={str(item.get("video_id")):item for item in payload_candidates}
        profile=store.get_profile(self.active_id)
        for review in reviews:
            candidate=self.ai_candidate_rows.get(review.video_id,{})
            proposal=ai_research.proposed_verdict(profile,review,candidate)
            ai_label=review.ai_opinion.verdict if review.ai_opinion else "미사용"
            self.ai_tree.insert("","end",iid=review.video_id,values=(ai_research.PROPOSED_VERDICT_LABELS[proposal],review.rule_verdict,ai_label,candidate.get("video_title",review.video_id)))
        if selected_id and self.ai_tree.exists(selected_id):self.ai_tree.selection_set(selected_id)
        elif reviews:self.ai_tree.selection_set(reviews[0].video_id)
        if self.ai_tree.selection():self._ai_show_detail()
        self._ai_apply_filter()
        self._autopilot_refresh_profile()

    def _ai_apply_filter(self):
        if not hasattr(self,"ai_tree"):return
        choice=self.ai_result_filter.get()
        for video_id,review in getattr(self,"ai_reviews",{}).items():
            if not self.ai_tree.exists(video_id):continue
            candidate=getattr(self,"ai_candidate_rows",{}).get(video_id,{})
            proposal=ai_research.proposed_verdict(store.get_profile(review.profile_id),review,candidate)
            opinion=review.ai_opinion
            if choice=="전체":show=True
            elif choice=="추천 적합":show=proposal=="auto_fit"
            elif choice=="교차 장르":show=proposal in {"cross_genre_review","related_reference"}
            elif choice=="근거 부족":show=not opinion or not opinion.evidence_from_metadata or bool(opinion.uncertainty)
            elif choice=="AI 실패":show=review.ai_source=="failure"
            else:show=review.rule_verdict=="검토" or (opinion is not None and opinion.recommended_human_check not in ("", "없음"))
            parent=self.ai_tree.parent(video_id)
            if show and not parent:self.ai_tree.move(video_id,"","end")
            elif not show and parent=="":self.ai_tree.detach(video_id)

    def _ai_open_selected(self):
        selected=self.ai_tree.selection()
        if not selected: return
        candidate=getattr(self,"ai_candidate_rows",{}).get(selected[0],{})
        url=candidate.get("video_url") or candidate.get("url")
        if not url and re.fullmatch(r"[\w-]{11}",selected[0]):
            url=f"https://www.youtube.com/watch?v={selected[0]}"
        if url: webbrowser.open(url,new=2)

    def _ai_export_report(self):
        if not self.ai_reviews:
            messagebox.showinfo("내보낼 결과 없음","먼저 오프라인 Demo를 실행하세요.",parent=self); return
        target=filedialog.asksaveasfilename(parent=self,defaultextension=".csv",initialfile="zisu_ai_review.csv",filetypes=[("CSV","*.csv")])
        if not target: return
        try:
            count=ai_research.export_review_report(self.ai_reviews.values(),target)
            messagebox.showinfo("보고서 저장 완료",f"{count}개 검토 결과를 UTF-8 CSV로 저장했습니다.\n{target}",parent=self)
        except OSError:
            messagebox.showerror("저장 실패","선택한 폴더에 쓸 수 없습니다. 쓰기 권한이 있는 폴더를 선택하세요.",parent=self)

    def save_schedule(self):
        try:
            store.update_schedule(self.active_id,self.schedule_text.get())
            self.reload_profiles(self.active_id)
            self.footer.set("현재 채널 예약 검색 시각을 저장했습니다.")
        except ValueError as exc:messagebox.showwarning("예약 시간",str(exc))

    def _scheduled_tick(self):
        if self.scheduler_enabled.get() and not self.search_inflight:
            now=datetime.now()
            hhmm=now.strftime("%H:%M")
            stamp=now.strftime("%Y%m%d-%H:%M")
            for p in store.list_profiles():
                stamp_key=(p["id"],stamp)
                if hhmm in p["schedule"].split(",") and stamp_key not in self.scheduler_fired and (self.api_key.get().strip() or oauth.is_connected(p["id"])):
                    self.scheduler_fired.add(stamp_key)
                    terms,_,_=ai_research._profile_query_plan(p,12)
                    if not terms:
                        continue
                    pos=(now.toordinal()+now.hour)%len(terms)
                    self.api_search(scheduled=True,profile_id=p["id"],keywords=[terms[pos]])
                    break
        self.after(15000,self._scheduled_tick)


def main():
    try:
        Studio().mainloop()
    except (OSError, sqlite3.Error) as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "ZISU 시작 실패",
            "사용자 데이터 폴더를 만들거나 데이터베이스를 열 수 없습니다.\n"
            "폴더 쓰기 권한과 남은 디스크 공간을 확인한 뒤 다시 실행하세요.\n\n"
            f"오류 종류: {type(exc).__name__}\n"
            "개인정보가 제외된 진단은 오류진단하기.bat에서 확인할 수 있습니다.",
            parent=root,
        )
        root.destroy()
        raise SystemExit(10) from None
