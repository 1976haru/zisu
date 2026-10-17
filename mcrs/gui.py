"""Beginners-first KR/JP multi-channel GUI with read-only account connection."""
from __future__ import annotations
import os, re, threading, time, webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from datetime import datetime, date
from urllib.parse import urlencode, urlparse, parse_qs
from . import store
from .genres import GENRES, search_terms, assess
from .youtube_api import lookup_videos, APIError
from . import oauth

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
        store.initialize()
        first_run = not store.list_profiles()
        self.title("ZISU Research Studio V4 · KR / JP 장르 안전 조사")
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
        for label in ("① 계정 연결","② 장르별 채널 검색","③ 후보·장르 검토","④ 댓글 초안","⑤ 국가별 성과","⑥ 예약·도움말"):
            tab=ttk.Frame(self.tabs,padding=14)
            self.tabs.add(tab,text=label)
            self.frames.append(tab)
        self._account_ui(self.frames[0])
        self._search_ui(self.frames[1])
        self._candidate_ui(self.frames[2])
        self._comment_ui(self.frames[3])
        self._metrics_ui(self.frames[4])
        self._help_ui(self.frames[5])
        ttk.Label(self,textvariable=self.footer).pack(anchor="w",padx=24,pady=(0,9))
        self.reload_profiles()
        if first_run:
            self.after(150, self.start_onboarding)
        self.after(1000,self._tick)
        self.after(14000,self._scheduled_tick)

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
        self.selected_video=None
        self.comment_video=None
        self.comment_label.set("영상 선택 없음")
        state = "Google 연결됨" if oauth.is_connected(self.active_id) else "연결 전"
        self.profile_description.set(f'{profile["market"]} · {GENRES[profile["genre"]].title} · {state}')
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
        tab.rowconfigure(2,weight=1)
        panel=ttk.Frame(tab)
        panel.grid(row=0,column=0,sticky="ew",pady=8)
        ttk.Label(panel,text="추가 API 키 (선택 · 연결 계정만으로도 검색 가능)").pack(side="left")
        ttk.Entry(panel,textvariable=self.api_key,width=40,show="●").pack(side="left",padx=10)
        ttk.Label(panel,text="연결 계정은 읽기 전용 · 계정이 없으면 웹 검색 가능").pack(side="left")
        ttk.Label(tab,text="선택한 채널의 국가/장르만 검색합니다. 일본어 검색 = 일본인 운영 채널 인증은 아닙니다.").grid(row=1,column=0,sticky="w",pady=6)
        pane=ttk.Panedwindow(tab,orient="horizontal")
        pane.grid(row=2,column=0,sticky="nsew")
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
        self.genre_reason.set(f"현재 채널 후보 {len(rows)}개 (제외된 장르는 저장되지 않음)")

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
    Studio().mainloop()
