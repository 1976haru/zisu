"""Scrollable beginner-facing UI for manual link research."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox

from . import manual_research


class ManualResearchWindow(tk.Toplevel):
    def __init__(self, root, profile, *, opener, path=None):
        super().__init__(root)
        self.profile, self.opener, self.path = profile, opener, path
        self.batch_id = None
        self.selected_video_id = None
        self._last_alarm_slot = None
        self.title("수동 링크 연구 · 최대 50개")
        self.geometry("1100x760"); self.minsize(720, 560); self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _e: self._close())
        self._build()
        self._load_latest()

    def _build(self):
        canvas = tk.Canvas(self, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas, padding=14)
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        self.body.columnconfigure(0, weight=1)
        ttk.Label(self.body, text="수동 링크 연구", font=("Malgun Gothic", 18, "bold")).grid(row=0,column=0,sticky="w")
        ttk.Label(self.body, text=f"현재 채널: {self.profile['name']} · 시장 {self.profile['market']} · 핵심 장르 {self.profile['genre']}").grid(row=1,column=0,sticky="w",pady=(2,8))
        self.guide = tk.StringVar(value="1. 링크를 붙여넣고 미리보기 하세요. 영상은 자동으로 열리지 않습니다.")
        ttk.Label(self.body,textvariable=self.guide,wraplength=1000).grid(row=2,column=0,sticky="ew",pady=4)
        mode=ttk.LabelFrame(self.body,text="연구 모드",padding=8);mode.grid(row=3,column=0,sticky="ew",pady=5)
        ttk.Label(mode,text="A. 수동 링크 연구 (기본 추천)   B. 자동 후보 탐색 (기존 기능)   C. VPH 연구 (기존 기능)").pack(anchor="w")
        entry=ttk.LabelFrame(self.body,text="YouTube 링크 · 한 줄에 하나 (1~50개)",padding=8);entry.grid(row=4,column=0,sticky="ew",pady=5)
        entry.columnconfigure(0,weight=1)
        self.links=tk.Text(entry,height=7,wrap="none");self.links.grid(row=0,column=0,columnspan=6,sticky="ew")
        self.hours=tk.StringVar(value="1");self.analysis=tk.StringVar(value="15");self.breaks=tk.StringVar(value="5")
        for col,(label,var,values) in enumerate((("전체 연구 시간(1~24시간)",self.hours,tuple(range(1,25))),
                ("영상별 분석 알림(분)",self.analysis,(10,15,20)),("휴식 알림(분)",self.breaks,(0,5,10,15,20)))):
            ttk.Label(entry,text=label).grid(row=1,column=col*2,sticky="w",pady=(7,2))
            ttk.Combobox(entry,textvariable=var,values=values,width=9).grid(row=1,column=col*2+1,sticky="w",padx=(3,14),pady=(7,2))
        buttons=ttk.Frame(entry);buttons.grid(row=2,column=0,columnspan=6,sticky="ew",pady=5)
        ttk.Button(buttons,text="등록 전 미리보기",command=self._preview).pack(side="left")
        ttk.Button(buttons,text="연구 목록 저장",command=self._create,style="Accent.TButton").pack(side="left",padx=6)
        self.preview_text=tk.StringVar(value="유효 0 · 중복 0 · 오류 0")
        ttk.Label(entry,textvariable=self.preview_text,wraplength=900).grid(row=3,column=0,columnspan=6,sticky="w")
        work=ttk.LabelFrame(self.body,text="연구 목록과 진행",padding=8);work.grid(row=5,column=0,sticky="nsew",pady=5)
        work.columnconfigure(0,weight=1);work.rowconfigure(1,weight=1)
        self.status=tk.StringVar(value="저장된 수동 연구 없음")
        ttk.Label(work,textvariable=self.status).grid(row=0,column=0,columnspan=4,sticky="ew",pady=(0,4))
        self.timer_text=tk.StringVar(value="분석 타이머 대기")
        ttk.Label(work,textvariable=self.timer_text,font=("Malgun Gothic",11,"bold")).grid(row=3,column=0,columnspan=4,sticky="w")
        self.tree=ttk.Treeview(work,columns=("order","state","genre","title"),show="headings",height=9,selectmode="browse")
        for key,label,width in (("order","순서",55),("state","상태",90),("genre","자동 판정",90),("title","영상",600)):
            self.tree.heading(key,text=label);self.tree.column(key,width=width,stretch=key=="title")
        self.tree.grid(row=1,column=0,columnspan=3,sticky="nsew")
        bar=ttk.Scrollbar(work,orient="vertical",command=self.tree.yview);bar.grid(row=1,column=3,sticky="ns");self.tree.configure(yscrollcommand=bar.set)
        self.tree.bind("<<TreeviewSelect>>",self._select);self.tree.bind("<Return>",self._select)
        controls=ttk.Frame(work);controls.grid(row=2,column=0,columnspan=4,sticky="ew",pady=5)
        self.open_button=ttk.Button(controls,text="영상 열기",command=self._open,state="disabled")
        self.open_button.pack(side="left")
        ttk.Button(controls,text="타이머 시작/이어하기",command=self._resume).pack(side="left",padx=4)
        ttk.Button(controls,text="일시정지",command=self._pause).pack(side="left",padx=4)
        ttk.Button(controls,text="휴식 시작",command=self._break).pack(side="left",padx=4)
        ttk.Button(controls,text="새로고침",command=self._refresh).pack(side="left",padx=4)
        form=ttk.LabelFrame(self.body,text="사람 평가와 메모",padding=8);form.grid(row=6,column=0,sticky="ew",pady=5);form.columnconfigure(1,weight=1)
        self.human_fit=tk.StringVar(value="보류")
        ttk.Label(form,text="분류").grid(row=0,column=0,sticky="w")
        ttk.Combobox(form,textvariable=self.human_fit,values=("핵심 적합","관련 참고","보류","제외"),state="readonly").grid(row=0,column=1,sticky="w")
        self.fields={}
        labels=(("human_genre_label","사람 장르 판정"),("mood","분위기·감성"),("intro_notes","도입부"),("vocal_notes","보컬 특성"),
                ("rap_melody_notes","랩·멜로디 비중"),("beat_tempo_notes","비트와 템포"),("hook_structure_notes","후크·곡 전개"),
                ("title_thumbnail_notes","제목·썸네일 특징"),("listened_segments","사람이 감상한 구간"),("reuse_ideas","재사용 가능한 제작 아이디어"),("notes","분석 메모"))
        for row,(key,label) in enumerate(labels,1):
            ttk.Label(form,text=label).grid(row=row,column=0,sticky="nw",pady=2)
            widget=tk.Text(form,height=2,wrap="word");widget.grid(row=row,column=1,sticky="ew",pady=2);self.fields[key]=widget
        self.save_state=tk.StringVar(value="메모 저장 전")
        ttk.Button(form,text="평가·메모 저장",command=self._save,style="Accent.TButton").grid(row=len(labels)+1,column=1,sticky="e",pady=5)
        ttk.Label(form,textvariable=self.save_state).grid(row=len(labels)+1,column=0,sticky="w")
        ttk.Label(self.body,text="계획 시간·활성 분석 시간·휴식·감상 구간·YouTube Analytics 시청시간은 서로 다른 지표입니다. 타이머는 알림일 뿐 재생이나 시청 완료를 뜻하지 않습니다.",wraplength=1000,foreground="#8a4b08").grid(row=7,column=0,sticky="ew",pady=8)
        self.after(1000,self._tick_timer)

    def _preview(self):
        result=manual_research.preview_links(self.links.get("1.0","end-1c"))
        detail="; ".join([f"{line}행: {reason}" for line,reason in result.errors[:5]])
        self.preview_text.set(f"유효 {len(result.valid)} · 중복 {len(result.duplicates)} · 오류 {len(result.errors)}"+(f" · {detail}" if detail else ""))
        return result

    def _create(self):
        if not self._preview().can_save:return
        try:self.batch_id=manual_research.create_batch(self.profile["id"],self.links.get("1.0","end-1c"),planned_hours=self.hours.get(),analysis_minutes=self.analysis.get(),break_minutes=self.breaks.get(),path=self.path)
        except ValueError as exc:messagebox.showwarning("연구 목록 확인",str(exc),parent=self);return
        self.guide.set("2. 목록에서 영상 하나를 선택하세요. 선택해야만 영상 열기가 활성화됩니다.");self._refresh()

    def _load_latest(self):
        batches=manual_research.list_batches(self.profile["id"],self.path)
        if batches:self.batch_id=batches[0]["id"];self._refresh()

    def _refresh(self):
        selected_video_id=self.selected_video_id
        for iid in self.tree.get_children():self.tree.delete(iid)
        if not self.batch_id:return
        rows=manual_research.list_items(self.batch_id,self.profile["id"],self.path);summary=manual_research.summary(self.batch_id,self.profile["id"],self.path)
        batch=manual_research.get_batch(self.batch_id,self.profile["id"],self.path)
        for row in rows:self.tree.insert("","end",iid=row["video_id"],values=(row["position"],row["research_state"],row["auto_genre_decision"],row["title"] or f"제목 미확인 · {row['video_id']}"))
        if selected_video_id and self.tree.exists(selected_video_id):
            self.tree.selection_set(selected_video_id);self.tree.see(selected_video_id)
            self.open_button.configure(state="normal")
        else:
            self.open_button.configure(state="disabled")
        self.status.set(f"등록 {summary['total']} · 검토 완료 {summary['completed']} · REVIEW {summary['reviews']} · 상태 {batch['state']} · 활성 분석 {batch['active_seconds']//60}분 · 휴식 {batch['break_seconds']//60}분")

    def _select(self,_event=None):
        selected=self.tree.selection();self.selected_video_id=selected[0] if selected else None
        self.open_button.configure(state="normal" if self.selected_video_id else "disabled")
        if not self.selected_video_id:return
        row=next(r for r in manual_research.list_items(self.batch_id,self.profile["id"],self.path) if r["video_id"]==self.selected_video_id)
        self.human_fit.set(row["human_fit"] or "보류")
        for key,widget in self.fields.items():widget.delete("1.0","end");widget.insert("1.0",row[key] or "")

    def _open(self):
        if not self.selected_video_id:return
        if messagebox.askyesno("선택 영상 열기","선택한 영상 하나를 기본 브라우저에서 엽니다. 자동 재생·순환·참여 작업은 하지 않습니다.",parent=self):
            self.opener(f"https://www.youtube.com/watch?v={self.selected_video_id}")

    def _resume(self):
        if not self.selected_video_id:messagebox.showinfo("영상 선택","먼저 영상 하나를 선택하세요.",parent=self);return
        try:manual_research.start_or_resume(self.batch_id,self.profile["id"],self.selected_video_id,self.path)
        except ValueError as exc:messagebox.showwarning("이어하기",str(exc),parent=self);return
        self.guide.set("분석 타이머가 시작되었습니다. 영상 재생 또는 시청 완료로 기록되지는 않습니다.");self._refresh()

    def _pause(self):
        try:manual_research.pause(self.batch_id,self.profile["id"],self.path)
        except ValueError as exc:messagebox.showwarning("일시정지",str(exc),parent=self);return
        self._refresh()

    def _break(self):
        try:manual_research.start_break(self.batch_id,self.profile["id"],self.path)
        except ValueError as exc:messagebox.showwarning("휴식 시작",str(exc),parent=self);return
        self.guide.set("휴식 중입니다. 이어하기를 눌러야 분석 타이머가 다시 시작됩니다.");self._refresh()

    def _tick_timer(self):
        if not self.winfo_exists():return
        batch=manual_research.get_batch(self.batch_id,self.profile["id"],self.path) if self.batch_id else None
        if batch and batch["state"]=="running" and batch["running_since_utc"]:
            import datetime
            started=datetime.datetime.fromisoformat(batch["running_since_utc"])
            elapsed=max(0,int((datetime.datetime.now(datetime.timezone.utc)-started).total_seconds()))
            interval=max(1,int(batch["analysis_minutes"]))*60;remaining=max(0,interval-(elapsed%interval))
            self.timer_text.set(f"활성 분석 {elapsed//60:02d}:{elapsed%60:02d} · 다음 분석 알림 {remaining//60:02d}:{remaining%60:02d}")
            slot=elapsed//interval
            if elapsed and elapsed%interval==0 and slot!=self._last_alarm_slot:
                self._last_alarm_slot=slot;self.bell();self.guide.set("분석 알림 시간입니다. 메모를 저장하거나 직접 휴식을 선택하세요.")
        elif batch and batch["break_started_utc"]:self.timer_text.set("휴식 중 · 이어하기 전까지 활성 분석 시간은 늘지 않습니다.")
        else:self.timer_text.set("분석 타이머 일시정지")
        self.after(1000,self._tick_timer)

    def _save(self):
        if not self.selected_video_id:messagebox.showinfo("영상 선택","먼저 영상 하나를 선택하세요.",parent=self);return
        values={key:widget.get("1.0","end-1c") for key,widget in self.fields.items()};values["human_fit"]=self.human_fit.get()
        values["research_state"]="completed"
        try:manual_research.save_evaluation(self.batch_id,self.profile["id"],self.selected_video_id,values,self.path)
        except ValueError as exc:messagebox.showwarning("저장 확인",str(exc),parent=self);return
        self.save_state.set("메모 저장 완료");self._refresh()

    def _close(self):
        batch=manual_research.get_batch(self.batch_id,self.profile["id"],self.path) if self.batch_id else None
        if batch and batch["state"]=="running":manual_research.pause(self.batch_id,self.profile["id"],self.path)
        self.destroy()
