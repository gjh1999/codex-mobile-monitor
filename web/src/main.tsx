import {useEffect, useRef, useState, type FormEvent, type PointerEvent, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  ArrowUp, ChevronLeft, ChevronRight, CircleCheck, Layers, LogOut, MessageSquare,
  Plus, Search, ShieldCheck, Terminal, Wallet, Waves, X,
} from 'lucide-react';
import './style.css';

type Client = 'codex' | 'claude';
type Page = 'tasks' | 'approvals' | 'quota';
type Task = {id: string; title?: string; name?: string; status?: {type: string}};
type Control = {threadId: string; title?: string; paused?: boolean; managed?: boolean; canControl?: boolean};
type Approval = {token: string; method: string; expires: number; decision?: string; details: Record<string, unknown>};
type Operation = {id: string; status: string; message?: string};
type WindowQuota = {usedPercent?: number; resetsAt?: number};
type Snapshot = {
  connection?: string;
  threads?: Task[];
  controls?: {tasks?: Control[]; names?: Record<string, string>; operations?: Operation[]};
  approvals?: Approval[];
  quota?: {default?: {primary?: WindowQuota; secondary?: WindowQuota}};
  quotaStatus?: string;
};
type Message = {role: string; text: string};
type Pending = Record<string, {client: Client; thread: string; text: string}>;

const clientNames = {codex: 'Codex', claude: 'Claude Code'};
const pages = [
  {key: 'tasks', label: '任务总览', Icon: Layers},
  {key: 'approvals', label: '审批中心', Icon: ShieldCheck},
  {key: 'quota', label: '账户额度', Icon: Wallet},
] as const;
const completed = {succeeded: '操作已完成', failed: '操作失败', unknown: '结果待核实', cancelled: '操作已取消'};
const taskStates: Record<string, string> = {active: '运行中', idle: '空闲', notLoaded: '未连接', systemError: '异常'};
const compactQuery = '(max-width: 700px)';

async function api<T = unknown>(url: string, data?: unknown): Promise<T> {
  const response = await fetch(url, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json', 'X-Monitor-Request': '1'},
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '请求失败 (' + response.status + ')');
  return value;
}

// getRandomValues also works when an iPhone visits a local HTTP address.
const requestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');

function Dialog({title, onClose, children}: {title: string; onClose: () => void; children: ReactNode}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const drag = useRef<{id: number; start: number; offset: number; y: number; time: number; velocity: number} | null>(null);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (!event.isPrimary || event.button !== 0) return;
    const element = dialog.current!;
    const offset = Number.parseFloat(getComputedStyle(element).translate.split(' ')[1]) || 0;
    drag.current = {id: event.pointerId, start: event.clientY, offset, y: event.clientY, time: event.timeStamp, velocity: 0};
    element.dataset.dragging = 'true';
    element.style.setProperty('--sheet-offset', offset + 'px');
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    const gesture = drag.current;
    if (!gesture || gesture.id !== event.pointerId) return;
    const offset = Math.max(0, gesture.offset + event.clientY - gesture.start);
    gesture.velocity = (event.clientY - gesture.y) / Math.max(1, event.timeStamp - gesture.time);
    gesture.y = event.clientY;
    gesture.time = event.timeStamp;
    dialog.current!.style.setProperty('--sheet-offset', offset + 'px');
  }

  function finishDrag(event: PointerEvent<HTMLDivElement>) {
    const gesture = drag.current;
    if (!gesture || gesture.id !== event.pointerId) return;
    drag.current = null;
    const element = dialog.current!;
    const offset = Number.parseFloat(element.style.getPropertyValue('--sheet-offset')) || 0;
    delete element.dataset.dragging;
    const fastRelease = event.timeStamp - gesture.time < 80 && gesture.velocity > .55;
    if (event.type === 'pointerup' && (offset > 92 || (offset > 24 && fastRelease))) {
      onClose();
    } else {
      element.style.removeProperty('--sheet-offset');
    }
  }

  return (
    <dialog ref={dialog} aria-label={title} onCancel={event => {event.preventDefault(); onClose();}}>
      <div className="sheet-handle" aria-hidden="true" onPointerDown={startDrag} onPointerMove={moveDrag}
        onPointerUp={finishDrag} onPointerCancel={finishDrag} onLostPointerCapture={finishDrag}/>
      <div className="dialog-body">
        <div className="dialog-heading">
          <h2>{title}</h2>
          <button type="button" className="icon-button" aria-label="关闭弹窗" onClick={onClose}><X size={18}/></button>
        </div>
        {children}
      </div>
    </dialog>
  );
}

function approvalTitle(approval: Approval) {
  if (approval.method.includes('commandExecution')) return '运行命令';
  if (approval.method.includes('fileChange')) return '修改文件';
  if (approval.method.includes('permissions')) return '授予权限';
  return '请求确认';
}

function App() {
  const [logged, setLogged] = useState(false);
  const [checking, setChecking] = useState(true);
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [client, setClient] = useState<Client>('codex');
  const [snap, setSnap] = useState<Snapshot>({});
  const [selected, setSelected] = useState('');
  const [page, setPage] = useState<Page>('tasks');
  const [search, setSearch] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [online, setOnline] = useState(false);
  const [pending, setPending] = useState<Pending>({});
  const [notice, setNotice] = useState('');
  const [adding, setAdding] = useState(false);
  const [rename, setRename] = useState<string | null>(null);
  const [reviewToken, setReviewToken] = useState('');
  const [compact, setCompact] = useState(() => matchMedia(compactQuery).matches);
  const threadTitle = useRef<HTMLHeadingElement>(null);

  const draftKey = client + ':' + selected;
  const draft = drafts[draftKey] ?? '';

  useEffect(() => {
    api('/api/state').then(() => setLogged(true)).catch(() => {}).finally(() => setChecking(false));
    const query = matchMedia(compactQuery);
    const resize = () => setCompact(query.matches);
    query.addEventListener('change', resize);
    return () => query.removeEventListener('change', resize);
  }, []);

  useEffect(() => {
    if (!logged) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const data = await api<Snapshot>('/api/state?client=' + client);
        if (!alive) return;
        setSnap(data);
        setOnline(true);
        if (selected) {
          const history = await api<{messages?: Message[]}>('/api/conversation/' + encodeURIComponent(selected) + '?client=' + client);
          if (alive) setMessages(history.messages || []);
        }
      } catch (cause) {
        if (alive) {
          setOnline(false);
          setError((cause as Error).message);
        }
      } finally {
        if (alive) timer = setTimeout(poll, 2000);
      }
    }
    void poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [logged, client, selected]);

  useEffect(() => {
    for (const operation of snap.controls?.operations || []) {
      const own = pending[operation.id];
      const label = completed[operation.status as keyof typeof completed];
      if (!own || own.client !== client || !label) continue;
      setNotice(operation.message || label);
      if (operation.status === 'failed' || operation.status === 'unknown') {
        setError(operation.message || '结果尚未确认，请检查任务后再试。');
        if (own.text) {
          const key = own.client + ':' + own.thread;
          setDrafts(previous => ({...previous, [key]: previous[key] || own.text}));
        }
      }
      setPending(previous => {
        const next = {...previous};
        delete next[operation.id];
        return next;
      });
    }
  }, [snap, pending, client]);

  useEffect(() => {
    if (compact && selected && page === 'tasks') threadTitle.current?.focus({preventScroll: true});
  }, [compact, selected, page]);

  const tasks = new Map<string, Task>();
  for (const task of snap.threads || []) tasks.set(task.id, task);
  for (const task of snap.controls?.tasks || []) {
    if (!tasks.has(task.threadId)) tasks.set(task.threadId, {id: task.threadId, title: task.title});
  }
  const title = (task: Task) => snap.controls?.names?.[task.id] || task.title || task.name || '未命名任务';
  const current = tasks.get(selected);
  const control = snap.controls?.tasks?.find(task => task.threadId === selected);
  const approvals = (snap.approvals || []).filter(item => !item.decision && item.expires > Date.now() / 1000);
  const review = approvals.find(item => item.token === reviewToken);
  const taskApprovals = approvals.filter(item => item.details.threadId === selected);
  const running = [...tasks.values()].filter(task => task.status?.type === 'active').length;
  const filtered = [...tasks.values()].filter(task => title(task).toLowerCase().includes(search.trim().toLowerCase()));
  const canSend = online && !busy && !!draft.trim() && control?.canControl && !control.paused;
  const desktopDetail = !!selected;
  const mobileDetail = compact && desktopDetail && page === 'tasks';

  function select(task: string) {
    if (task === selected) return;
    setSelected(task);
    setMessages([]);
  }

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api('/api/login', {password});
      setPassword('');
      setLogged(true);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setError('');
    try {
      await api('/api/logout', {});
      setLogged(false);
      setOnline(false);
      setSnap({});
      select('');
      setReviewToken('');
      setNotice('');
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  function switchClient(value: Client) {
    if (value === client) return;
    setClient(value);
    select('');
    setReviewToken('');
    setOnline(false);
    setSnap({});
    setSearch('');
    setError('');
    setNotice('');
  }

  function closeEditor() {
    setAdding(false);
    setRename(null);
  }

  async function action(name: string, text = '') {
    setBusy(true);
    setError('');
    try {
      const result = await api<{id: string}>('/api/operations', {client, thread: selected, action: name, text, request_id: requestId()});
      setPending(previous => ({...previous, [result.id]: {client, thread: selected, text: name === 'message' ? text : ''}}));
      setNotice('已提交，等待后台确认');
      if (name === 'message') setDrafts(previous => ({...previous, [draftKey]: previous[draftKey] === text ? '' : previous[draftKey]}));
      closeEditor();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function decide(item: Approval, allow: boolean) {
    setBusy(true);
    setError('');
    try {
      const result = await api<{result: string}>('/api/approvals', {token: item.token, allow});
      setNotice(result.result === 'allowed' ? '已允许一次' : '已拒绝');
      setSnap(previous => ({...previous, approvals: previous.approvals?.filter(value => value.token !== item.token)}));
      setReviewToken('');
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function approvalRow(item: Approval) {
    const command = item.details.command;
    return (
      <button className="approval-row" key={item.token} onClick={() => setReviewToken(item.token)}>
        <span className="approval-icon"><ShieldCheck size={19}/></span>
        <span className="approval-summary">
          <strong>{approvalTitle(item)}</strong>
          {typeof command === 'string' && <code>{command}</code>}
        </span>
        <span className="review-label">查看详情</span><ChevronRight size={16}/>
      </button>
    );
  }

  if (checking) return <main className="loading" role="status">正在打开澜汐…</main>;

  if (!logged) return (
    <main className="welcome">
      <div className="welcome-copy">
        <div className="wordmark"><Waves size={23}/> 澜汐</div>
        <h1>潮平两岸阔，<br/>风正一帆悬。</h1>
        <p>唐 · 王湾《次北固山下》</p>
      </div>
      <form className="sign-in" onSubmit={login}>
        <h2>欢迎回来</h2>
        <p>未竟的一行，还在原处。</p>
        <label htmlFor="password">访问密码</label>
        <input id="password" type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" required/>
        <button className="primary" disabled={busy}>进入工作台 <ChevronRight size={16}/></button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    </main>
  );

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="wordmark"><Waves size={21}/> 澜汐</div>
        <nav aria-label="工作台">
          {pages.map(({key, label, Icon}) => (
            <button key={key} aria-current={page === key ? 'page' : undefined} onClick={() => {
              if (key === 'tasks' && page === 'tasks' && compact) select('');
              setPage(key);
            }}>
              <Icon size={19}/><span>{label}</span>
              {key === 'approvals' && approvals.length > 0 && <b className="count">{approvals.length}</b>}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className="connection"><i className={online ? 'running' : ''}/>{online ? '本机已连接' : '连接中断'}</span>
          <button className="icon-button" aria-label="退出登录" title="退出登录" onClick={logout}><LogOut size={17}/></button>
        </div>
      </aside>
      <main className="content">
        <header className="topbar">
          {mobileDetail
            ? <button className="back" onClick={() => select('')}><ChevronLeft size={19}/> 返回任务</button>
            : <h1>{pages.find(item => item.key === page)?.label}</h1>}
          <div className="clients" role="group" aria-label="客户端">
            {(Object.keys(clientNames) as Client[]).map(value => (
              <button key={value} aria-pressed={client === value} onClick={() => switchClient(value)}>{clientNames[value]}</button>
            ))}
          </div>
          <button className="icon-button mobile-logout" aria-label="退出登录" onClick={logout}><LogOut size={18}/></button>
        </header>
        {error && <div className="banner error" role="alert"><span>{error}</span><button onClick={() => setError('')}>关闭</button></div>}
        {notice && <div className="banner notice" role="status">{notice}</div>}

        {page === 'tasks' && (
          <>
            <section className={'cover' + (mobileDetail ? ' cover-hidden' : '')} aria-label="澜汐工作台">
              <div>
                <h2>一处开始，<br/>处处继续。</h2>
                <p>白日写到的那一行，暮归时仍等着你。</p>
              </div>
              <dl className="cover-stats">
                <div><dt>任务</dt><dd>{tasks.size}</dd></div>
                <div><dt>运行中</dt><dd>{running}</dd></div>
                <div><dt>待确认</dt><dd>{approvals.length}</dd></div>
              </dl>
            </section>

            <div className="workspace" data-detail={desktopDetail}>
              <section className="task-panel" aria-label="任务列表" inert={mobileDetail}>
                <div className="section-title">
                  <h2>我的任务 <span>{tasks.size}</span></h2>
                  <button className="icon-button" aria-label="创建测试任务" title="创建测试任务" onClick={() => setAdding(true)}><Plus size={20}/></button>
                </div>
                <label className="search"><Search size={17}/><input aria-label="搜索任务" placeholder="搜索任务" value={search} onChange={event => setSearch(event.target.value)}/></label>
                <div className="task-list">
                  {filtered.map(task => (
                    <button key={task.id} className="task" aria-current={selected === task.id ? 'true' : undefined} onClick={() => select(task.id)}>
                      <span className="task-icon"><Terminal size={18}/></span>
                      <span className="task-text">
                        <strong>{title(task)}</strong>
                        <small><i className={task.status?.type === 'active' ? 'running' : ''}/>{taskStates[task.status?.type || ''] || '待接入'}</small>
                      </span>
                      <ChevronRight size={15}/>
                    </button>
                  ))}
                  {filtered.length === 0 && <div className="empty small-empty">
                    <Layers size={23}/><h3>{tasks.size ? '没有找到匹配的任务' : '新的篇章，等你写下。'}</h3>
                    <p>{tasks.size ? '换个关键词试试。' : '本机客户端连接后，任务将在这里出现。'}</p>
                  </div>}
                </div>
                <div className="panel-footer"><i className={snap.connection === 'connected' ? 'running' : ''}/>{snap.connection === 'connected' ? '客户端已连接' : '客户端连接待检查'}</div>
              </section>

              <section className="chat" aria-label="任务对话" inert={compact && !mobileDetail}>
                <div className="chat-heading">
                  <div><small>任务对话</small><h2 tabIndex={-1} ref={threadTitle}>{current ? title(current) : '继续一段对话'}</h2></div>
                  <MessageSquare size={21}/>
                </div>
                {selected && <div className="toolbar" aria-label="任务操作">
                  {[
                    ['attach', '接入'], ['pause', '暂停'], ['resume', '继续'],
                    [control?.managed ? 'unmanage' : 'manage', control?.managed ? '关闭自动推进' : '自动推进'],
                    ['detach', '移出'],
                  ].map(([key, text]) => <button key={key} disabled={busy || !online || (key !== 'attach' && !control)} onClick={() => action(key)}>{text}</button>)}
                  <button disabled={busy || !online} onClick={() => setRename(current ? title(current) : '')}>重命名</button>
                </div>}
                <div className="messages">
                  {messages.length ? messages.map((message, index) => (
                    <article key={index} className={'message ' + (message.role === 'user' ? 'user' : 'assistant')}>
                      <small>{message.role === 'user' ? '你' : message.role === 'assistant' ? clientNames[client] : message.role}</small>
                      <div>{message.text}</div>
                    </article>
                  )) : (
                    <div className="empty">
                      <MessageSquare size={30}/>
                      <h3>{selected ? '话还未起，纸上留白。' : '未尽的话，可以接着说。'}</h3>
                      <p>{selected ? '接入任务即可发送第一条指令。' : '选择一个任务，查看已有对话。'}</p>
                    </div>
                  )}
                  {taskApprovals.map(approvalRow)}
                </div>
                {selected && <form className="compose" onSubmit={event => {event.preventDefault(); if (canSend) void action('message', draft);}}>
                  <textarea aria-label="任务指令" value={draft} placeholder="写下下一步"
                    onChange={event => setDrafts(previous => ({...previous, [draftKey]: event.target.value}))}
                    onKeyDown={event => {
                      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && canSend) {
                        event.preventDefault();
                        void action('message', draft);
                      }
                    }}/>
                  <div className="compose-footer">
                    <small>{control?.paused ? '任务已暂停，请先继续' : control?.canControl ? 'Ctrl / ⌘ + Enter 发送' : '接入并取得控制权后即可发送'}</small>
                    <button aria-label="发送指令" className="primary send" disabled={!canSend}><ArrowUp size={20}/></button>
                  </div>
                </form>}
              </section>
            </div>
          </>
        )}

        {page === 'approvals' && (
          <section className="standalone" aria-label="待审批操作">
            {approvals.length ? approvals.map(approvalRow) : <div className="empty">
              <CircleCheck size={29}/><h2>此刻风平，暂无待审批。</h2>
              <p>需要你确认的操作会出现在这里。</p>
            </div>}
          </section>
        )}

        {page === 'quota' && (
          <section className="standalone quota" aria-label="账户用量">
            {(['primary', 'secondary'] as const).map((key, index) => {
              const item = snap.quota?.default?.[key];
              const label = index ? '长周期额度' : '短周期额度';
              const remaining = typeof item?.usedPercent === 'number' ? Math.max(0, 100 - item.usedPercent) : null;
              return (
                <div className="quota-row" key={key}>
                  <div><h2>{label}</h2><strong>{remaining === null ? '暂不可用' : '剩余 ' + remaining + '%'}</strong></div>
                  {remaining !== null && <progress aria-label={label + '剩余'} max={100} value={remaining}/>}
                  <small>{item?.resetsAt ? '重置时间：' + new Date(item.resetsAt * 1000).toLocaleString() : '接口尚未提供重置时间'}</small>
                </div>
              );
            })}
            <p>{client === 'claude' ? 'Claude 官方剩余额度目前未知。' : snap.quotaStatus}</p>
          </section>
        )}
      </main>

      {review && <Dialog title={approvalTitle(review)} onClose={() => setReviewToken('')}>
        <p className="dialog-note">{review.method}</p>
        <pre className="approval-details">{JSON.stringify(review.details, null, 2)}</pre>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="actions">
          <button disabled={busy || !online} onClick={() => decide(review, false)}>拒绝</button>
          <button className="primary" disabled={busy || !online} onClick={() => decide(review, true)}>允许一次</button>
        </div>
      </Dialog>}

      {(adding || rename !== null) && <Dialog title={adding ? '创建测试任务' : '修改任务名称'} onClose={closeEditor}>
        <form onSubmit={event => {event.preventDefault(); void action(adding ? 'create_test' : 'rename', rename || '');}}>
          {adding ? <p>将调用本机后台创建一个测试任务。</p> : <label className="dialog-field">
            任务名称 <input autoFocus maxLength={80} value={rename || ''} onChange={event => setRename(event.target.value)}/>
          </label>}
          {error && <p className="error" role="alert">{error}</p>}
          <div className="actions">
            <button type="button" onClick={closeEditor}>取消</button>
            <button className="primary" disabled={busy || !online}>确认</button>
          </div>
        </form>
      </Dialog>}
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<App/>);
