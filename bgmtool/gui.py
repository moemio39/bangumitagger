import json
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from .api import Client
from .common import atomic_json, episode_number, load_config, read_json, roots_from, save_config
from .executor import Executor, unlock_stale
from .planner import Planner, scan, suggest

class App:
    def __init__(self, root, data):
        self.root = root; self.data = Path(data).absolute(); self.data.mkdir(parents=True, exist_ok=True)
        self.config = load_config(self.data)
        self.groups = []; self.selections = read_json(self.data / 'selections.json', {})
        self.candidates = {}; self.run = read_json(self.data / 'last-run.json', {}).get('run')
        self.messages = queue.Queue(); self.busy = False; self.buttons = []
        root.title('Bangumi → Jellyfin 原位刮削工具 1.0')
        root.geometry('1280x900'); root.minsize(1050, 740)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.build()
        root.after(100, self.drain)

    def button(self, frame, text, command):
        b = ttk.Button(frame, text=text, command=command); self.buttons.append(b)
        return b

    def build(self):
        outer = ttk.Frame(self.root, padding=12); outer.pack(fill='both', expand=True)
        top = ttk.LabelFrame(outer, text='媒体目录与网络（原位移动，不复制视频；默认保留已有 NFO）', padding=8)
        top.pack(fill='x')
        self.vars = {}
        for row, (name, label) in enumerate([('shows_root','剧集目录'),('movies_root','电影目录'),('proxy','HTTP 代理')]):
            ttk.Label(top, text=label).grid(row=row, column=0, sticky='w', padx=4, pady=3)
            v = tk.StringVar(value=self.config[name]); self.vars[name] = v
            ttk.Entry(top, textvariable=v).grid(row=row, column=1, sticky='ew', padx=4)
            if name != 'proxy':
                self.button(top, '浏览', lambda n=name:self.browse_root(n)).grid(row=row, column=2)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text='可选 Token').grid(row=3, column=0, sticky='w', padx=4, pady=3)
        self.token = tk.StringVar()
        ttk.Entry(top, textvariable=self.token, show='●').grid(row=3, column=1, sticky='ew', padx=4)
        ttk.Label(top, text='只存内存；公开数据通常无需 Token').grid(row=3, column=2, padx=6)
        options = ttk.Frame(outer); options.pack(fill='x', pady=6)
        self.replace = tk.BooleanVar(value=False); self.pause = tk.BooleanVar(value=False)
        self.resources = tk.BooleanVar(value=self.config['collect_resources'])
        ttk.Checkbutton(options, text='替换旧 NFO（直接删除，不备份）', variable=self.replace).pack(side='left', padx=4)
        ttk.Checkbutton(options, text='收拢无视频的剩余资料', variable=self.resources).pack(side='left', padx=4)
        ttk.Checkbutton(options, text='整理时暂停 Jellyfin，结束恢复原状态（需管理员）', variable=self.pause).pack(side='left', padx=4)
        toolbar = ttk.Frame(outer); toolbar.pack(fill='x', pady=4)
        for text, command in [('1 扫描新增',self.scan), ('2 批量搜索建议',self.search_all),
                              ('接受高置信建议',self.accept_high), ('3 生成预览',self.preview),
                              ('4 执行预览',self.apply), ('恢复中断',lambda:self.execute('resume')),
                              ('回滚',lambda:self.execute('rollback'))]:
            self.button(toolbar, text, command).pack(side='left', padx=3)
        panes = ttk.Panedwindow(outer, orient='horizontal'); panes.pack(fill='both', expand=True, pady=6)
        left = ttk.Frame(panes); right = ttk.Frame(panes); panes.add(left, weight=5); panes.add(right, weight=6)
        ttk.Label(left, text='待处理组：选中作品后确认条目；有可靠既有条目 ID 的新集可直接预览').pack(anchor='w')
        treeframe = ttk.Frame(left); treeframe.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(treeframe, columns=('count','id','state'), selectmode='browse')
        self.tree.heading('#0', text='片源 / 季目录'); self.tree.column('#0', width=265)
        for name, label, width in [('count','视频',48), ('id','条目 ID',80), ('state','状态',80)]:
            self.tree.heading(name, text=label); self.tree.column(name, width=width, stretch=False)
        scroll = ttk.Scrollbar(treeframe, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set); scroll.pack(side='right', fill='y'); self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_group)
        self.files = scrolledtext.ScrolledText(left, height=7, wrap='none'); self.files.pack(fill='x', pady=5)
        searchbar = ttk.Frame(right); searchbar.pack(fill='x')
        self.keyword = tk.StringVar()
        ttk.Entry(searchbar, textvariable=self.keyword).pack(side='left', fill='x', expand=True)
        self.button(searchbar, '搜索当前组', self.search_one).pack(side='left', padx=4)
        self.results = ttk.Treeview(right, columns=('id','date','platform','score'), height=8, selectmode='browse')
        self.results.heading('#0', text='Bangumi 候选标题'); self.results.column('#0', width=230)
        for name, label, width in [('id','ID',65), ('date','日期',85), ('platform','类型',60), ('score','相似度',65)]:
            self.results.heading(name, text=label); self.results.column(name, width=width, stretch=False)
        self.results.pack(fill='both', expand=True, pady=6); self.results.bind('<<TreeviewSelect>>', self.select_candidate)
        edit = ttk.LabelFrame(right, text='确认归属（第二季合并时填根作品 ID，并选择 Season 02）', padding=8)
        edit.pack(fill='x')
        self.sid = tk.StringVar(); self.rootid = tk.StringVar(); self.season = tk.StringVar(value='1')
        self.offset = tk.StringVar(value='0'); self.kind = tk.StringVar(value='show'); self.numbering = tk.StringVar(value='auto')
        fields = [('条目 ID / URL',self.sid),('根作品 ID（可留空）',self.rootid),('Jellyfin 季号',self.season),('集号减去的偏移',self.offset)]
        for row, (label, variable) in enumerate(fields):
            ttk.Label(edit, text=label).grid(row=row, column=0, sticky='w', pady=2)
            ttk.Entry(edit, textvariable=variable).grid(row=row, column=1, sticky='ew', padx=6)
        ttk.Label(edit, text='库类型').grid(row=4, column=0, sticky='w')
        ttk.Combobox(edit, textvariable=self.kind, values=('show','movie'), state='readonly').grid(row=4,column=1,sticky='ew',padx=6)
        ttk.Label(edit, text='章节号依据').grid(row=5,column=0,sticky='w')
        ttk.Combobox(edit, textvariable=self.numbering, values=('auto','ep','sort'), state='readonly').grid(row=5,column=1,sticky='ew',padx=6)
        edit.columnconfigure(1, weight=1)
        ttk.Label(edit, text='逐文件覆盖 JSON（可留空；例见使用说明）').grid(row=6,column=0,columnspan=2,sticky='w',pady=3)
        self.overrides = scrolledtext.ScrolledText(edit, height=3, wrap='word')
        self.overrides.grid(row=7,column=0,columnspan=2,sticky='ew')
        self.button(edit, '确认并保存当前组绑定', self.confirm).grid(row=8,column=0,columnspan=2,sticky='ew',pady=5)
        bottom = ttk.Frame(outer); bottom.pack(fill='x')
        self.status = tk.StringVar(value='请先扫描。只处理确认项；不确定的文件留在待确认列表。')
        ttk.Label(bottom, textvariable=self.status).pack(side='left', fill='x', expand=True)
        self.button(bottom, '选择历史记录', self.choose_run).pack(side='right', padx=3)
        self.button(bottom, '清理失效执行锁', self.unlock).pack(side='right', padx=3)
        self.button(bottom, '打开工具记录', self.open_data).pack(side='right', padx=3)
        self.logs = scrolledtext.ScrolledText(outer, height=6, wrap='word', state='disabled')
        self.logs.pack(fill='x', pady=4)

    def config_snapshot(self):
        config = dict(self.config)
        for k,v in self.vars.items(): config[k] = v.get().strip()
        config['collect_resources'] = bool(self.resources.get())
        roots_from(config)
        # Validate proxy/token without putting the token in the saved config.
        Client(self.data / 'cache', config['proxy'], self.token.get().strip())
        save_config(self.data, config); self.config = config
        return config

    def worker(self, function, callback):
        if self.busy: return
        self.busy = True
        for b in self.buttons: b.state(['disabled'])
        def run():
            try: self.messages.put(('done',(callback,function())))
            except Exception as e: self.messages.put(('error',str(e)))
        threading.Thread(target=run, daemon=True).start()

    def emit(self, text): self.messages.put(('log',str(text)))

    def drain(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == 'log': self.log(value)
                else:
                    self.busy = False
                    for b in self.buttons: b.state(['!disabled'])
                    if kind == 'error':
                        self.log('停止：' + value); messagebox.showerror('任务停止（记录已保留）',value)
                    else:
                        callback, result = value
                        try: callback(result)
                        except Exception as e: messagebox.showerror('界面错误',str(e))
        except queue.Empty: pass
        self.root.after(100, self.drain)

    def log(self, text):
        self.logs.configure(state='normal'); self.logs.insert('end',text+'\n')
        self.logs.see('end'); self.logs.configure(state='disabled')

    def browse_root(self, name):
        p = filedialog.askdirectory(initialdir=self.vars[name].get())
        if p: self.vars[name].set(p)

    def scan(self):
        try: config = self.config_snapshot()
        except Exception as e: messagebox.showerror('配置错误',str(e)); return
        self.worker(lambda:scan(config), self.scan_done)

    def scan_done(self, result):
        self.groups = result['groups']; self.tree.delete(*self.tree.get_children())
        for g in self.groups:
            if g.get('hint_subject_id') and g.get('hint_root_id'):
                self.selections.setdefault(g['id'], {'confirmed':True, 'subject_id':g['hint_subject_id'],
                    'root_subject_id':g['hint_root_id'], 'season':g['hint_season'], 'kind':g['kind']})
            self.tree.insert('', 'end', iid=g['id'], text=g['name'], values=self.row_values(g))
        stats = result['stats']
        self.status.set(f"待处理 {len(self.groups)} 组；已有 NFO 跳过 {stats['already_scraped']} 个；下载未稳定跳过 {stats['still_downloading']} 个")
        self.log(self.status.get())

    def row_values(self, g):
        selection = self.selections.get(g['id'], {})
        return (len(g['videos']), selection.get('subject_id',''), '已确认' if selection.get('confirmed') else '待确认')

    def group(self):
        selection = self.tree.selection()
        return next((g for g in self.groups if selection and g['id']==selection[0]), None)

    def select_group(self, event=None):
        g = self.group()
        if not g: return
        s = self.selections.get(g['id'], {})
        self.keyword.set(g['query']); self.sid.set(s.get('subject_id',g.get('hint_subject_id') or ''))
        self.rootid.set(s.get('root_subject_id',g.get('hint_root_id') or '')); self.season.set(s.get('season',g['hint_season']))
        self.offset.set(s.get('offset',0)); self.kind.set(s.get('kind',g['kind'])); self.numbering.set(s.get('numbering','auto'))
        self.overrides.delete('1.0','end'); self.overrides.insert('end',json.dumps(s.get('overrides',{}),ensure_ascii=False,indent=2))
        self.files.delete('1.0','end')
        self.files.insert('end',g['path']+'\n\n')
        for p in g['videos']: self.files.insert('end',str(Path(p).relative_to(g['path']))+'  '+str(episode_number(Path(p).name))+'\n')
        self.show_candidates(self.candidates.get(g['id'], []))

    def show_candidates(self, rows):
        self.results.delete(*self.results.get_children())
        for c in rows:
            self.results.insert('', 'end', iid=str(c['id']), text=c.get('name_cn') or c.get('name'),
                values=(c['id'],c.get('date',''),c.get('platform',''),f"{c['match_score']:.0%}"))

    def select_candidate(self, event=None):
        selected = self.results.selection()
        if selected: self.sid.set(selected[0])

    def search_one(self):
        g = self.group()
        if not g: messagebox.showinfo('提示','先在左侧选择一个片源组'); return
        try: config = self.config_snapshot()
        except Exception as e: messagebox.showerror('配置错误',str(e)); return
        token = self.token.get().strip(); keyword = self.keyword.get().strip(); gid = g['id']
        def work(): return suggest(keyword, Client(self.data/'cache',config['proxy'],token).search(keyword))
        def done(rows): self.candidates[gid] = rows; self.show_candidates(rows); self.log('候选仅为建议，请确认条目及季号。')
        self.worker(work,done)

    def search_all(self):
        try: config = self.config_snapshot()
        except Exception as e: messagebox.showerror('配置错误',str(e)); return
        token = self.token.get().strip(); groups = list(self.groups)
        def work():
            client = Client(self.data/'cache',config['proxy'],token); results = {}
            for g in groups:
                if self.selections.get(g['id'],{}).get('confirmed'): continue
                self.emit('搜索：'+g['query']); results[g['id']] = suggest(g['query'],client.search(g['query']))
            return results
        def done(result): self.candidates.update(result); self.select_group(); self.log('批量搜索完成；尚未改动媒体文件。')
        self.worker(work,done)

    @staticmethod
    def parse_id(value):
        value = value.strip()
        match = re.fullmatch(r'(?:https?://(?:bgm\.tv|bangumi\.tv)/subject/)?(\d+)/?',value)
        if not match: raise ValueError('条目 ID 请填数字或 Bangumi subject URL')
        return int(match[1])

    def confirm(self):
        g = self.group()
        if not g: return
        try:
            overrides = json.loads(self.overrides.get('1.0','end').strip() or '{}')
            if not isinstance(overrides,dict): raise ValueError('逐文件覆盖必须是 JSON 对象')
            s = {'confirmed':True,'subject_id':self.parse_id(self.sid.get()),'kind':self.kind.get(),
                 'season':int(self.season.get()),'offset':int(self.offset.get()),'numbering':self.numbering.get(),'overrides':overrides}
            if self.rootid.get().strip(): s['root_subject_id'] = self.parse_id(self.rootid.get())
            self.selections[g['id']] = s; atomic_json(self.data/'selections.json',self.selections)
            self.tree.item(g['id'],values=self.row_values(g)); self.log('已确认：'+g['name'])
        except Exception as e: messagebox.showerror('输入错误',str(e))

    def accept_high(self):
        count = 0
        for g in self.groups:
            if self.selections.get(g['id'],{}).get('confirmed'): continue
            rows = self.candidates.get(g['id'],[])
            if rows and ((g['kind']=='movie' and rows[0].get('platform') in ('TV','TV动画')) or
                         (g['kind']=='show' and rows[0].get('platform') in ('剧场版','Movie'))):
                continue  # Cross-library classification needs manual confirmation.
            if rows and rows[0]['match_score'] >= .94 and (len(rows)==1 or rows[0]['match_score']-rows[1]['match_score'] >= .12):
                self.selections[g['id']] = {'confirmed':True,'subject_id':rows[0]['id'],'kind':g['kind'],'season':g['hint_season']}
                self.tree.item(g['id'],values=self.row_values(g)); count += 1
        atomic_json(self.data/'selections.json',self.selections)
        self.select_group(); self.log(f'接受 {count} 个高置信建议；执行前仍需检查预览、季号和待确认项。')

    def preview(self):
        try: config = self.config_snapshot()
        except Exception as e: messagebox.showerror('配置错误',str(e)); return
        groups = list(self.groups); selections = json.loads(json.dumps(self.selections)); token = self.token.get().strip(); replace = self.replace.get()
        def work(): return Planner(config,self.data,Client(self.data/'cache',config['proxy'],token),self.emit).build(groups,selections,replace)
        def done(result):
            run, plan = result; self.run = str(run); atomic_json(self.data/'last-run.json',{'run':self.run})
            self.status.set('预览：'+json.dumps(plan['summary'],ensure_ascii=False)); self.log('预览记录：'+str(run))
            self.preview_window(plan)
        self.worker(work,done)

    def preview_window(self, plan):
        window = tk.Toplevel(self.root); window.title('预览 — 尚未修改媒体'); window.geometry('1100x700')
        tabs = ttk.Notebook(window); tabs.pack(fill='both',expand=True,padx=8,pady=8)
        for label, value in [('汇总',{'summary':plan['summary'],'warnings':plan['warnings'],'run':self.run}),
                             ('文件移动',plan['moves']),('NFO / 图片写入',plan['writes']),
                             ('旧 NFO 删除（无备份）',plan['deletions']),('待确认 / 未处理',plan['review'])]:
            frame = ttk.Frame(tabs); tabs.add(frame,text=label)
            text = scrolledtext.ScrolledText(frame,wrap='none'); text.pack(fill='both',expand=True)
            text.insert('end',json.dumps(value,ensure_ascii=False,indent=2)); text.configure(state='disabled')

    def apply(self): self.execute('apply')

    def execute(self, action):
        if not self.run: messagebox.showinfo('提示','先生成预览或选择历史运行记录'); return
        try:
            config = self.config_snapshot(); executor = Executor(config,self.data,self.run,self.emit)
            if action=='apply' and not any(executor.plan[k] for k in ('moves','writes','deletions')):
                raise ValueError('预览中没有可执行项目；先确认作品归属')
        except Exception as e: messagebox.showerror('不能执行',str(e)); return
        prompt = {'apply':'按当前预览原位移动文件并写入元数据？','resume':'按当前记录恢复中断的执行？','rollback':'撤销当前运行的移动和未被修改的新元数据？'}[action]
        if executor.plan['deletions']: prompt += '\n旧 NFO 不备份，删除后不能通过回滚找回。'
        if not messagebox.askyesno('确认 '+action,prompt): return
        pause = self.pause.get()
        def work():
            if action=='rollback': return executor.rollback(pause)
            return executor.apply(resume=action=='resume',pause=pause)
        def done(result): self.status.set(action+' 完成，记录：'+str(self.run)); messagebox.showinfo('完成',self.status.get())
        self.worker(work,done)

    def choose_run(self):
        runs = self.data/'runs'; runs.mkdir(exist_ok=True)
        path = filedialog.askdirectory(initialdir=runs,title='选择包含 plan.json 的运行记录目录')
        if path:
            self.run = path; self.log('当前运行记录：'+path)

    def unlock(self):
        try: unlock_stale(self.data); self.log('失效执行锁已清理（仍运行的进程不会被解锁）。')
        except Exception as e: messagebox.showerror('不能解锁',str(e))

    def open_data(self):
        if os.name=='nt': os.startfile(self.data)
        else: messagebox.showinfo('记录目录',str(self.data))

    def close(self):
        if self.busy:
            messagebox.showinfo('正在执行','请等当前步骤完成后再关闭，以保留完整执行记录。'); return
        self.token.set(''); self.root.destroy()

def launch(data, smoke=False):
    root = tk.Tk(); app = App(root,data)
    if smoke:
        root.update(); print('GUI SMOKE PASS:',root.title(),root.winfo_width(),root.winfo_height())
        root.after(100,root.destroy)
    root.mainloop()
