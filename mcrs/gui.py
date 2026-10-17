"""Beginners-first KR/JP multi-channel GUI with read-only account connection."""
from __future__ import annotations
import os
import platform
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
from .genres import GENRES, search_terms, assess
from .youtube_api import lookup_videos, APIError
from . import oauth
from . import ai_research
from .credential_store import CredentialStore, CredentialError
from .startup_probe import write_stage as _write_smoke_stage

BG = "#F5F7FA"
ACCENT = "#20635A"


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


class ProfileDialog(tk.Toplevel):
    def __init__(self,root,initial=None):
        super().__init__(root)
        self.title("작업 채널 또는 조사 계정 추가")
        self.result = None
        self.transient(root)
        self.grab_set()
        self.resizable(False,False)
        self.geometry("510x310")
        self.configure(padx=22,pady=14)
        initial=initial or {}
        self.name=tk.StringVar(value=initial.get("name",""))
        self.market=tk.StringVar(value=initial.get("market","JP"))
        self.genre=tk.StringVar(value=initial.get("genre","MORNING_CHILL_RAP"))
        ttk.Label(self,text="1단계 · 알아보기 쉬운 작업 프로필 이름").pack(anchor="w",pady=(0,5))
        ttk.Entry(self,textvariable=self.name,width=56).pack(anchor="w",fill="x")
        ttk.Label(self,text="2단계 · 조사 국가 (한국 KR / 일본 JP)").pack(anchor="w",pady=(16,3))
        ttk.Combobox(self,textvariable=self.market,values=["JP","KR"],state="readonly",width=12).pack(anchor="w")
        ttk.Label(self,text="3단계 · 핵심 음악 장르 (프로필마다 하나)").pack(anchor="w",pady=(16,3))
        self.genre_keys=list(GENRES)
        self.genre_names=[GENRES[k].title for k in self.genre_keys]
        self.genre_display=tk.StringVar(value=GENRES[self.genre.get()].title)
        ttk.Combobox(self,textvariable=self.genre_display,values=self.genre_names,state="readonly",width=45).pack(anchor="w")
        ttk.Button(self,text="저장하고 Google 연결로 이동",command=self._save).pack(anchor="e",pady=16)
        self.bind("<Escape>",lambda _e:self.destroy())
        self.wait_window()

    def _save(self):
        name=self.name.get().strip()
        if not 2<=len(name)<=70:
            messagebox.showwarning("이름 확인","채널 이름을 2~70자로 입력해 주세요.",parent=self)
            return
        self.result=(name,self.market.get(),self.genre_keys[self.genre_names.index(self.genre_display.get())])
        self.destroy()


class Studio(tk.Tk):
    def __init__(self):
        super().__init__()
        _write_smoke_stage("tk_root_created")
        _write_smoke_stage("database_initializing")
        store.initialize()
        _write_smoke_stage("database_initialized")
        first_run = not store.list_profiles()
        smoke_report = os.environ.get("ZISU_EXE_SMOKE_REPORT", "").strip()
        self.title("ZISU Research Studio V4.5.4 · AI 보조 장르 안전 조사")
        self.geometry("1320x840")
        self.minsize(1060,710)
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
        self.api_key=tk.StringVar(value=os.environ.get("YOUTUBE_API_KEY",""))
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
        self.oauth_status=tk.StringVar(value="계정 상태 확인 중...")
        self.oauth_busy=False
        self.search_inflight=False
        self.scheduler_enabled=tk.BooleanVar(value=False)
        self.scheduler_fired=set()
        self.terms_seen=0
        self._header()
        self.tabs=ttk.Notebook(self)
        self.tabs.pack(fill="both",expand=True,padx=15,pady=8)
        self.frames=[]
        for label in ("① 계정 연결","② 장르별 채널 검색","③ 후보·장르 검토","④ 댓글 초안","⑤ 국가별 성과","⑥ 예약·도움말","⑦ AI 연구 도우미"):
            tab=ttk.Frame(self.tabs,padding=14)
            self.tabs.add(tab,text=label)
            self.frames.append(tab)
        self._account_ui(self.frames[0])
        self._search_ui(self.frames[1])
        self._candidate_ui(self.frames[2])
        self._comment_ui(self.frames[3])
        self._metrics_ui(self.frames[4])
        self._help_ui(self.frames[5])
        self._ai_research_ui(self.frames[6])
        ttk.Label(self,textvariable=self.footer).pack(anchor="w",padx=24,pady=(0,9))
        self.reload_profiles()
        if first_run and not smoke_report:
            self.after(150, self.start_onboarding)
        self.after(1000,self._tick)
        self.after(14000,self._scheduled_tick)
        if smoke_report:
            _write_smoke_stage("gui_widgets_ready")
            self.after(250, lambda: self._complete_exe_smoke(smoke_report))

    def _complete_exe_smoke(self, report_path):
        """CI-only startup probe. It never performs OAuth or network requests."""
        self.update_idletasks()
        payload = {
            "app_version": "4.5.4",
            "gui_initialized": len(self.frames) == 7 and bool(self.winfo_exists()),
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
        _write_smoke_stage("offline_smoke_complete", **payload)
        self.destroy()

    def _header(self):
        top=ttk.Frame(self,padding=(18,14))
        top.pack(fill="x")
        ttk.Label(top,text="ZISU  |  Genre-Safe Research",font=("Malgun Gothic",17,"bold")).pack(side="left")
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

    def reload_profiles(self,select_id=None):
        self.profiles=store.list_profiles()
        values=[f'{x["name"]}  |  {x["market"]}  |  {GENRES[x["genre"]].title}' for x in self.profiles]
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
        self.profile_description.set(f'{profile["market"]} · {GENRES[profile["genre"]].title} · {state}')
        account = profile.get("connected_email") or "Google 미연결"
        channel = profile.get("youtube_channel_title") or "채널 정보 없음"
        self.active_account_banner.set(
            f"현재 작업: {profile['name']} | {profile['market']} | {GENRES[profile['genre']].title} | {account} | {channel}")
        self.refresh_account_status()
        self._reload_terms()
        self.reload_candidates()
        self.reload_metrics()
        self.schedule_text.set(profile["schedule"])
        self.footer.set("채널을 전환했습니다. 다른 채널의 검색 후보와 성과는 분리됩니다.")

    def add_profile(self):
        dialog=ProfileDialog(self)
        if dialog.result:
            try:
                name,market,genre=dialog.result
                ident=store.create_profile(name,market,genre)
                self.reload_profiles(ident)
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
                name,market,genre=dialog.result
                store.update_profile(old["id"],name=name,market=market,genre=genre)
                self.reload_profiles(old["id"])
            except Exception as exc: messagebox.showerror("채널 수정 실패",str(exc))

    def _account_ui(self, tab):
        tab.columnconfigure(0,weight=1)
        ttk.Label(tab,text="첫 단계: 내 YouTube 채널을 안전하게 연결하세요.",
                  font=("Malgun Gothic",16,"bold")).grid(row=0,column=0,sticky="w",pady=(6,10))
        ttk.Label(tab,text="Google OAuth 로그인 · 조회 전용 권한 · 비밀번호 입력/댓글 게시/구독 실행 권한 없음").grid(row=1,column=0,sticky="w",pady=4)
        panel=ttk.LabelFrame(tab,text="연결할 채널 (위의 작업 프로필별 독립 연결)",padding=20)
        panel.grid(row=2,column=0,sticky="ew",pady=14)
        panel.columnconfigure(0,weight=1)
        ttk.Label(panel,textvariable=self.oauth_status,font=("Malgun Gothic",12,"bold"),wraplength=950).grid(row=0,column=0,columnspan=3,sticky="w",pady=(0,13))
        ttk.Button(panel,text="1. Google Cloud OAuth JSON 선택",command=self.choose_client_file).grid(row=1,column=0,sticky="w",pady=6)
        ttk.Button(panel,text="2. 새 계정으로 Google 로그인 및 연결",command=self.connect_google,
                   style="Accent.TButton").grid(row=2,column=0,sticky="w",pady=6)
        ttk.Button(panel,text="연결 상태 새로고침",command=self.refresh_account_status).grid(row=3,column=0,sticky="w",pady=8)
        ttk.Button(panel,text="선택 프로필 연결 해제",command=self.disconnect_google).grid(row=3,column=1,sticky="w",pady=8,padx=15)
        ttk.Label(panel,textvariable=self.oauth_client_file,wraplength=800,foreground="#697A88").grid(row=4,column=0,columnspan=3,sticky="w",pady=7)
        ttk.Button(panel,text="Google Cloud 없이 먼저 유튜브 열기 (브라우저 로그인만)",
                   command=lambda:webbrowser.open("https://www.youtube.com/",new=2)).grid(row=5,column=0,columnspan=3,sticky="w",pady=7)
        ttk.Label(tab,text="연결을 위한 준비 (최초 1회)",font=("Malgun Gothic",12,"bold")).grid(row=3,column=0,sticky="w",pady=(14,7))
        instructions=("① 'OAuth_모듈_설치하기.bat' 더블클릭 (최초 1회)\n"
                      "② Google Cloud에서 YouTube Data API v3 사용 설정\n"
                      "③ OAuth 동의 화면 및 '데스크톱 앱' OAuth 클라이언트 생성\n"
                      "④ 테스트 모드라면 연결할 구글 계정을 테스트 사용자에 추가\n"
                      "⑤ 클라이언트 JSON을 선택하고 [2. 새 계정으로 로그인] 클릭\n"
                      "⑥ 브라우저에서 Morning Chill Rap 새 구글 계정 선택 → 이메일·채널명 확인 후 승인\n"
                      "    (유튜브 채널을 아직 만들지 않은 조사 전용 구글 계정도 연결 가능)\n\n"
                      "간편 체험: 위 '유튜브 열기'로 새 계정에 로그인하고 웹 검색만 먼저 써도 됩니다.\n"
                      "(브라우저 로그인은 앱의 OAuth 계정 연결과 별개입니다.)\n"
                      "계정마다 작업 프로필을 추가한 뒤 각각 연결하세요.\n"
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
        tab.rowconfigure(4,weight=1)

    def choose_client_file(self):
        path=filedialog.askopenfilename(title="Google Cloud '데스크톱 앱' OAuth JSON 선택",filetypes=[("Google OAuth JSON","*.json")])
        if path:
            self.oauth_client_file.set(path)

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
                + " · " + GENRES[profile['genre']].title)

    def connect_google(self):
        if self.oauth_busy:return
        profile=store.get_profile(self.active_id)
        if not profile:return
        client_file=self.oauth_client_file.get().strip()
        if not client_file:
            messagebox.showinfo("준비 필요","먼저 Google Cloud 데스크톱 앱 OAuth JSON을 선택하세요.")
            return
        profile_id=profile['id']
        self.oauth_busy=True
        self.oauth_status.set("브라우저에서 Google 로그인 중... 새 계정을 직접 선택하세요.")
        def worker():
            try:
                channels,token_json=oauth.begin_connect(client_file)
                error=None
            except oauth.OAuthError as exc:
                channels,token_json,error=[],None,str(exc)
            except Exception as exc:
                channels,token_json,error=[],None,type(exc).__name__
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
                try:
                    oauth.save_connection(profile_id,chosen,token_json)
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

    def _search_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(3,weight=1)
        panel=ttk.Frame(tab)
        panel.grid(row=0,column=0,sticky="ew",pady=8)
        ttk.Label(panel,text="추가 API 키 (선택 · 연결 계정만으로도 검색 가능)").pack(side="left")
        ttk.Entry(panel,textvariable=self.api_key,width=40,show="●").pack(side="left",padx=10)
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
        terms=store.list_profile_search_terms(profile["id"],profile["market"])
        if not terms:
            terms=search_terms(profile["genre"],profile["market"])
        self.keyword_list.delete(0,tk.END)
        for item in terms:self.keyword_list.insert(tk.END,item)
        self.keyword_list.select_set(0)
        self.search_tree.delete(*self.search_tree.get_children())
        self.search_status.set(f"{profile['market']} / {GENRES[profile['genre']].title} 전용 검색어 {len(terms)}개 준비됨")

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
                    collected.extend(lookup_videos(key,term,profile["market"],limit=12,access_token=token))
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
                        decision=assess(profile["genre"],row.get("video_title",""),row.get("description",""))
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
            self.candidates.heading(col,text=title)
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
        actions=ttk.Frame(detail)
        actions.grid(row=2,column=0,columnspan=2,sticky="ew",pady=6)
        self.item_status=tk.StringVar(value="미확인")
        ttk.Combobox(actions,textvariable=self.item_status,values=["미확인","검토 중","보류","완료"],state="readonly",width=10).pack(side="left")
        for text,method in (("영상 열기",self.open_candidate),("메모 저장",self.save_candidate),("장르 수동 승인",self.approve_candidate),("댓글 초안",self.go_comment)):
            ttk.Button(actions,text=text,command=method).pack(side="left",padx=4)
        ttk.Label(actions,text="분석 알림").pack(side="left",padx=(12,4))
        for m in (10,15,20):ttk.Button(actions,text=f"{m}분",command=lambda mins=m:self.start_timer(mins)).pack(side="left",padx=2)
        ttk.Label(actions,textvariable=self.timer_text).pack(side="right")

    def reload_candidates(self):
        if not hasattr(self,"candidates") or not self.active_id:return
        rows=store.list_candidates(self.active_id,self.filter.get(),self.decision_filter.get())
        self.candidates.delete(*self.candidates.get_children())
        for item in rows:
            self.candidates.insert("","end",iid=item["video_id"],values=(item["channel_title"],item["video_title"],item["genre_decision"],item["genre_score"],item["status"]))
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
        tree=ttk.Treeview(window,columns=("title","predicted","score","human","reason"),show="headings")
        for col,title,width in (("title","영상 제목",360),("predicted","자동 판정",90),("score","점수",60),
                                ("human","사람 정답",90),("reason","판정 이유",400)):
            tree.heading(col,text=title); tree.column(col,width=width,stretch=col in ("title","reason"))
        tree.pack(fill="both",expand=True,padx=12,pady=8)
        notes=tk.Text(window,height=4,wrap="word",font=("Malgun Gothic",10)); notes.pack(fill="x",padx=12,pady=6)
        def reload_audit():
            tree.delete(*tree.get_children())
            for row in store.list_genre_audit(self.active_id):
                tree.insert("","end",iid=row["video_id"],values=(row["video_title"],row["predicted_decision"],
                    row["predicted_score"],row["human_label"] or "미라벨",row["reason"]))
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
        self.item_status.set(item["status"])
        self.notes.delete("1.0",tk.END)
        self.notes.insert("1.0",item["notes"])
        self.genre_reason.set(f"{item['genre_decision']} · {item['genre_score']}점 · {item['genre_reason']}")

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
            if item:webbrowser.open(item["video_url"],new=2)

    def save_candidate(self):
        if not self.selected_video:return
        item=store.get_candidate(self.active_id,self.selected_video)
        store.update_candidate(self.active_id,self.selected_video,self.item_status.get(),self.notes.get("1.0",tk.END).strip(),item["draft"])
        self.reload_candidates()
        self.candidates.selection_set(self.selected_video)
        self.footer.set("선택 채널의 메모만 저장했습니다.")

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

    def _tick(self):
        if self.timer_end:
            remain=max(0,int(self.timer_end-time.monotonic()))
            self.timer_text.set(f"{remain//60:02d}:{remain%60:02d}")
            if remain==0:
                self.timer_end=0
                self.timer_text.set("알림 완료")
                messagebox.showinfo("분석 시간","실제 감상한 점을 메모하세요. 프로그램은 영상을 자동 재생하지 않습니다.")
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

    def go_comment(self):
        if not self.selected_video:return
        self.comment_video=(self.active_id,self.selected_video)
        item=store.get_candidate(*self.comment_video)
        self.comment_label.set(item["channel_title"]+" · "+item["video_title"][:70])
        self.observation.delete("1.0",tk.END)
        self.draft.delete("1.0",tk.END)
        self.draft.insert("1.0",item["draft"])
        self.tabs.select(self.frames[3])

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
        tab.rowconfigure(2,weight=1)
        ttk.Label(tab,text="각 채널별 검색 예약 · 프로그램이 켜져 있을 때만 동작").grid(row=0,column=0,sticky="w",pady=8)
        bar=ttk.Frame(tab)
        bar.grid(row=1,column=0,sticky="ew",pady=6)
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
        self.help_text.grid(row=2,column=0,sticky="nsew",pady=10)
        self.help_text.insert("1.0",guide)
        self.help_text.configure(state="disabled")

    def _ai_research_ui(self,tab):
        tab.columnconfigure(0,weight=1)
        tab.rowconfigure(4,weight=1)
        ttk.Label(tab,text="원클릭 AI 조사 도우미 · 규칙 우선, AI는 검토 후보만 보조",font=("Malgun Gothic",15,"bold")).grid(row=0,column=0,sticky="w",pady=(4,10))
        ttk.Label(tab,text="오프라인 Demo는 가상 메타데이터와 Mock AI를 사용합니다. Google/OAuth·네트워크·AI 키·사용자 DB를 사용하지 않습니다.",wraplength=1000).grid(row=1,column=0,sticky="w",pady=(0,12))
        controls=ttk.Frame(tab)
        controls.grid(row=2,column=0,sticky="ew",pady=5)
        ttk.Label(controls,text="후보 목표").pack(side="left")
        self.ai_target=tk.StringVar(value="10")
        ttk.Combobox(controls,textvariable=self.ai_target,values=("10","30","50"),state="readonly",width=7).pack(side="left",padx=6)
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
        self.ai_status=tk.StringVar(value="먼저 작업 프로필을 선택한 뒤 조사 계획을 미리 보세요.")
        ttk.Label(tab,textvariable=self.ai_status,wraplength=1000).grid(row=3,column=0,sticky="w",pady=6)
        body=ttk.Panedwindow(tab,orient="horizontal")
        body.grid(row=4,column=0,sticky="nsew",pady=6)
        results=ttk.Frame(body)
        detail=ttk.Frame(body)
        body.add(results,weight=2); body.add(detail,weight=3)
        self.ai_tree=ttk.Treeview(results,columns=("decision","rule","ai","title"),show="headings",height=11)
        for col,label,width in (("decision","추천 결과",85),("rule","규칙",70),("ai","AI 의견",75),("title","후보 제목",300)):
            self.ai_tree.heading(col,text=label); self.ai_tree.column(col,width=width,stretch=col=="title")
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
        self.ai_gemini_test_queue=queue.Queue()
        self.ai_gemini_models_queue=queue.Queue()
        self.ai_running=False
        self.ai_cancel_event=None
        self.ai_pause_event=None
        self._ai_refresh_credential_status()
        ttk.Button(results,text="선택한 영상 직접 열기",command=self._ai_open_selected).pack(anchor="e",pady=5)

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
        profile=store.get_profile(self.active_id)
        if not profile or (profile["market"],profile["genre"]) != (session["market"],session["genre"]):
            messagebox.showwarning("프로필 불일치","작업 당시 국가·장르와 현재 프로필이 달라 이어갈 수 없습니다.",parent=self); return
        self.ai_target.set(str(session["target"]))
        self.ai_max_calls.set(str(session["max_search_calls"]))
        try:
            plan=ai_research.preview_plan(self.active_id,session["target"],max_search_calls=session["max_search_calls"])
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
            plan=ai_research.preview_plan(self.active_id,int(self.ai_target.get()),max_search_calls=int(self.ai_max_calls.get()))
        except (ValueError,KeyError) as exc:
            messagebox.showwarning("계획 확인",str(exc),parent=self); return
        self.ai_confirmed_plan=plan
        terms="\n".join("  · "+term for term in plan["search_terms"][:6])
        plan["provider_id"]=self.ai_provider_ids[self.ai_provider_choice.get()]
        provider_label=self.ai_provider_choice.get()
        usage=store.ai_usage_summary(self.active_id).get(plan["provider_id"],{"requests":0,"unknown_cost":0})
        budget="비용 없음 (규칙만)" if provider_label=="규칙만" else ("로컬 Ollama; 기기 자원 사용" if provider_label.startswith("Ollama") else "현재 모델 요금 확인이 필요해 비용을 추정하지 않음")
        self.ai_status.set(f"미리보기: {plan['profile_name']} · {plan['market']} / {GENRES[plan['genre']].title} · 목표 {plan['target_candidates']}개 · 검색어 호출 최대 {plan['max_search_calls']}회 · AI {provider_label} · {budget}\n"
                           f"공급자 오늘 사용 {usage['requests']}/30회 · 이 작업 AI 최대 10회 · 비용 미확정 {usage['unknown_cost']}회. YouTube 검색 호출과 AI 호출/비용은 별도 집계합니다.\n"
                           f"검색어 예시:\n{terms}\n미리보기 중 API/AI 호출 0회. Demo는 오프라인이며, 실제 조사 버튼은 확인 후 YouTube 읽기 전용 검색을 수행합니다.")

    def _ai_start_demo(self):
        plan=self.ai_confirmed_plan
        if not plan or plan.get("profile_id") != self.active_id:
            messagebox.showinfo("계획 먼저 보기","조사 프로필을 선택한 뒤 계획을 다시 미리보기 하세요.",parent=self); return
        if not messagebox.askyesno("Demo 계획 확인",
             f"{plan['profile_name']} ({plan['market']} / {GENRES[plan['genre']].title})의 가상 데이터 3건을 검사합니다.\n\n"
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
        consent_text=(f"{plan['profile_name']} · {plan['market']} · {GENRES[plan['genre']].title}\n"
                      f"목표 {plan['target_candidates']}개 / 검색어 호출 최대 {plan['max_search_calls']}회\n\n"
                      f"YouTube 읽기 전용 검색을 실행하고 후보를 이 프로필에 저장합니다. {ai_privacy} 취소해도 수집된 결과는 이 프로필에만 남습니다.\n\n계속할까요?")
        if not messagebox.askyesno("실제 조사 실행 확인",consent_text,parent=self): return
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
                def collect(term,market,limit):
                    return lookup_videos(key,term,market,limit=min(20,limit),access_token=token)
                run=ai_research.run_bounded_research(profile_id,collect,target=plan["target_candidates"],
                    max_search_calls=plan["max_search_calls"],provider=provider,cancel_event=self.ai_cancel_event,
                    session_id=plan.get("session_id"),pause_event=self.ai_pause_event,
                    allow_paid_external=provider_id in ("openai","gemini") and not provider_note,
                    cost_estimate_usd=cost_estimate,cost_limit_usd=cost_limit)
                counts=store.save_candidates(profile_id,run.candidates) if run.candidates else {"적합":0,"검토":0,"제외":0}
                result=(run,counts,provider_note)
            except (APIError,oauth.OAuthError,ValueError,OSError):
                result=RuntimeError("조사를 완료하지 못했습니다. 연결·권한·저장 경로를 확인한 뒤 저장된 작업을 이어가세요.")
            except Exception:
                result=RuntimeError("조사 중 예기치 않은 문제가 발생했습니다. 연결·공급자 설정을 확인하고 다시 시도하세요.")
            self.ai_demo_queue.put(("live",profile_id,result))
        threading.Thread(target=worker,name="zisu-ai-research",daemon=True).start()
        self.after(50,self._poll_ai_demo_result)

    def _ai_live_done(self,profile_id,result):
        if not self.winfo_exists(): return
        if self.active_id != profile_id:
            self.ai_status.set("작업 중 프로필이 전환되어 현재 화면에는 결과를 표시하지 않습니다. 후보와 AI 검토 캐시는 시작한 원래 프로필에만 저장됐습니다."); return
        if isinstance(result,Exception):
            self.ai_status.set("읽기 전용 조사에 실패했습니다: "+str(result)[:300]); return
        run,counts,provider_note=result
        self.ai_reviews={review.video_id:review for review in run.reviews}
        self.ai_candidate_rows={str(row.get("video_id")):row for row in run.candidates}
        for review in run.reviews:
            candidate=self.ai_candidate_rows.get(review.video_id,{})
            ai_label=review.ai_opinion.verdict if review.ai_opinion else "미호출"
            self.ai_tree.insert("","end",iid=review.video_id,values=(review.final_verdict,review.rule_verdict,ai_label,candidate.get("video_title",review.video_id)))
        state="취소되어 일부 결과" if run.cancelled else "완료"
        self.ai_status.set(f"실제 읽기 전용 조사 {state}: 후보 {run.unique_count}개 · 검색어 호출 {run.calls_used}회 · 저장 통계 적합 {counts['적합']} / 검토 {counts['검토']} / 제외 {counts['제외']}{provider_note}. AI 의견은 승인 아님.")
        children=self.ai_tree.get_children()
        if children: self.ai_tree.selection_set(children[0]); self._ai_show_detail()

    def _ai_show_detail(self,_event=None):
        selected=self.ai_tree.selection()
        review=self.ai_reviews.get(selected[0]) if selected else None
        if not review: return
        opinion=review.ai_opinion
        text=(f"규칙 분류: {review.rule_verdict} ({review.rule_score}/100)\n"
              f"규칙 근거: {review.rule_reason}\n\n최종 제안: {review.final_verdict}\n"
              f"출처: {review.provenance}\n경고: {review.warning}\n")
        if opinion:
            text+=(f"\nAI 의견(자동 승인 아님): {opinion.verdict}\n"
                   f"긍정 신호: {', '.join(opinion.positive_signals) or '없음'}\n"
                   f"충돌 신호: {', '.join(opinion.conflict_signals) or '없음'}\n"
                   f"메타데이터 인용: {', '.join(opinion.evidence_from_metadata) or '검증된 인용 없음'}\n"
                   f"불확실성: {opinion.uncertainty or '남아 있을 수 있음'}\n"
                   f"사람이 확인할 점: {opinion.recommended_human_check}\n")
        candidate=getattr(self,"ai_candidate_rows",{}).get(review.video_id,{})
        url=candidate.get("video_url") or candidate.get("url","")
        text+=f"\n영상 URL: {url or 'Demo 가상 표본'}\n직접 영상을 듣고 최종 분류·승인을 판단하세요. 감상 체크는 자동 저장되지 않습니다."
        self.ai_detail.configure(state="normal"); self.ai_detail.delete("1.0","end"); self.ai_detail.insert("1.0",text); self.ai_detail.configure(state="disabled")

    def _ai_open_selected(self):
        selected=self.ai_tree.selection()
        if not selected: return
        candidate=getattr(self,"ai_candidate_rows",{}).get(selected[0],{})
        url=candidate.get("video_url") or candidate.get("url")
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
                    terms=search_terms(p["genre"],p["market"])
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
