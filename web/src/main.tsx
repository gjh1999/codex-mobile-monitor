import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Terminal, Layers, MessageSquare, ShieldCheck, ArrowUp, RefreshCw, Activity, LogOut, Search, Plus, ChevronRight, Monitor, Clock, Wallet} from 'lucide-react';
import './style.css';
type Task={id:string;title?:string;name?:string;status?:{type:string}};
type Control={threadId:string;title?:string;paused?:boolean;managed?:boolean;canControl?:boolean;message?:string};
type Approval={token:string;method:string;expires:number;decision?:string;details:Record<string,unknown>};
type Operation={id:string;status:string;message?:string};
type WindowQuota={usedPercent?:number;resetsAt?:number};
type Snapshot={connection?:string;threads?:Task[];controls?:{tasks?:Control[];names?:Record<string,string>;operations?:Operation[]};approvals?:Approval[];quota?:{default?:{primary?:WindowQuota;secondary?:WindowQuota}};quotaStatus?:string};
type Message={role:string;text:string};
// getRandomValues also works on an iPhone visiting an HTTP LAN address.
const requestId=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
async function api(url:string, data?:unknown){
 const r=await fetch(url,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Monitor-Request':'1'},body:data===undefined?undefined:JSON.stringify(data)});
 const value=await r.json();if(!r.ok)throw new Error(typeof value.detail==='string'?value.detail:`请求失败 (${r.status})`);return value;
}
function App(){
 const [logged,setLogged]=useState(false),[password,setPassword]=useState(''),[error,setError]=useState('');
 const [client,setClient]=useState('codex'),[snap,setSnap]=useState<Snapshot>({}),[selected,setSelected]=useState('');
 const [page,setPage]=useState('tasks'),[search,setSearch]=useState(''),[messages,setMessages]=useState<Message[]>([]);
 const [drafts,setDrafts]=useState<Record<string,string>>({}),[busy,setBusy]=useState(false),[online,setOnline]=useState(false);
 const [pending,setPending]=useState<Record<string,{client:string;thread:string;text:string}>>({});
 const [notice,setNotice]=useState(''),[adding,setAdding]=useState(false),[rename,setRename]=useState<string|null>(null);
 const draftKey=`${client}:${selected}`,draft=drafts[draftKey]||'';
 useEffect(()=>{api('/api/state').then(()=>setLogged(true)).catch(()=>{});},[]);
 useEffect(()=>{
  if(!logged)return;let alive=true;let timer:ReturnType<typeof setTimeout>;
  async function poll(){try{const data=await api(`/api/state?client=${client}`);if(!alive)return;setSnap(data);setOnline(true);
   if(selected){const history=await api(`/api/conversation/${encodeURIComponent(selected)}?client=${client}`);if(alive)setMessages(history.messages||[]);}
  }catch(e){if(alive){setOnline(false);setError((e as Error).message);}}finally{if(alive)timer=setTimeout(poll,2000);}}
  poll();return()=>{alive=false;clearTimeout(timer);};
 },[logged,client,selected]);
 useEffect(()=>{
  for(const op of snap.controls?.operations||[]){const own=pending[op.id];if(!own||own.client!==client||!['succeeded','failed','unknown','cancelled'].includes(op.status))continue;
   setNotice(op.message||op.status);if(['failed','unknown'].includes(op.status)){setError(op.message||'操作未完成，请核对任务后再重试');if(own.text)setDrafts(d=>({...d,[`${own.client}:${own.thread}`]:d[`${own.client}:${own.thread}`]||own.text}));}
   setPending(p=>{const n={...p};delete n[op.id];return n;});
  }
 },[snap,pending,client]);
 const tasks=new Map<string,Task>();for(const t of snap.threads||[])tasks.set(t.id,t);
 for(const t of snap.controls?.tasks||[])if(!tasks.has(t.threadId))tasks.set(t.threadId,{id:t.threadId,title:t.title});
 const title=(t:Task)=>snap.controls?.names?.[t.id]||t.title||t.name||'未命名任务';
 const current=tasks.get(selected),control=snap.controls?.tasks?.find(t=>t.threadId===selected);
 const approvals=(snap.approvals||[]).filter(a=>!a.decision&&a.expires>Date.now()/1000);
 const taskApprovals=approvals.filter(a=>a.details.threadId===selected);
 const stateLabel=(t:Task)=>({active:'运行中',idle:'空闲',notLoaded:'未连接',systemError:'异常'}[t.status?.type||'']||'待接入');
 async function action(action:string,text=''){
  setBusy(true);setError('');try{const r=await api('/api/operations',{client,thread:selected,action,text,request_id:requestId()});setPending(p=>({...p,[r.id]:{client,thread:selected,text:action==='message'?text:''}}));setNotice('已提交，等待后台确认');if(action==='message')setDrafts(d=>({...d,[draftKey]:''}));setAdding(false);setRename(null);}catch(e){setError((e as Error).message);}finally{setBusy(false);}
 }
 async function decide(a:Approval,allow:boolean){setBusy(true);try{const r=await api('/api/approvals',{token:a.token,allow});setNotice(r.result==='allowed'?'已允许一次':'已拒绝');setSnap(s=>({...s,approvals:s.approvals?.filter(x=>x.token!==a.token)}));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
 const approvalCard=(a:Approval)=><article className="approval" key={a.token}><div className="section-title"><ShieldCheck size={19}/><h3>需要你的确认</h3></div><p>{a.method}</p><pre>{JSON.stringify(a.details,null,2)}</pre><div className="actions"><button disabled={busy||!online} onClick={()=>decide(a,false)}>拒绝</button><button className="primary" disabled={busy||!online} onClick={()=>decide(a,true)}>允许一次</button></div></article>;
 if(!logged)return <div className="login"><form onSubmit={async e=>{e.preventDefault();try{await api('/api/login',{password});setPassword('');setLogged(true);setError('');}catch(e){setError((e as Error).message);}}}><div className="logo"><Terminal/></div><h1>欢迎回到任务小站</h1><p>与你的 Agent，保持连接。</p><input aria-label="访问密码" type="password" placeholder="输入控制台访问密码" value={password} onChange={e=>setPassword(e.target.value)} autoComplete="current-password" required/><button className="primary">进入工作空间 <ChevronRight size={16}/></button>{error&&<p className="error" role="alert">{error}</p>}<small>PRIVATE WORKSPACE · 私人工作空间</small></form></div>;
 return <div className="app"><aside className="sidebar"><div className="brand"><div className="logo"><Terminal size={22}/></div><div>任务小站<small>YOUR AGENT WORKSPACE</small></div></div><div className="nav-label">工作空间</div><nav>{[['tasks','任务总览',Layers],['approvals','审批中心',ShieldCheck],['quota','账户额度',Wallet]].map(([key,text,Icon])=><button key={String(key)} className={page===key?'active':''} onClick={()=>setPage(String(key))}>{React.createElement(Icon as typeof Layers,{size:19})}{String(text)}{key==='approvals'&&approvals.length>0&&<b className="count">{approvals.length}</b>}</button>)}</nav><div className="nav-label">客户端</div><div className="clients">{['codex','claude'].map(c=><button key={c} className={client===c?'chosen':''} onClick={()=>{setClient(c);setSelected('');setMessages([]);setSnap({});}}><Terminal size={17}/>{c==='codex'?'Codex':'Claude Code'}<span className="client-dot"/></button>)}</div><div className="sidebar-bottom"><Monitor size={17}/><span>本机工作空间<small>{online?'控制台已连接':'正在重新连接'}</small></span><button aria-label="退出登录" onClick={async()=>{await api('/api/logout',{});setLogged(false);setSnap({});setMessages([]);}}><LogOut size={16}/></button></div></aside>
 <main><header><div><div className="eyebrow">WORKSPACE / {client.toUpperCase()}</div><h1>{page==='approvals'?'审批中心':page==='quota'?'账户额度':'让每一步，都有进展。'}</h1><p>任务、对话与下一步，尽在你的掌握。</p></div><span className={'connection '+(online?'connected':'')}><i/>{online?'实时同步':'连接中断'}</span></header>
 {error&&<div className="error banner" role="alert">{error}<button onClick={()=>setError('')}>关闭</button></div>}
 {notice&&<div className="notice" role="status">{notice}</div>}
 <div className="metrics">{[[tasks.size,'全部任务',Layers],[Array.from(tasks.values()).filter(t=>t.status?.type==='active').length,'正在运行',Activity],[approvals.length,'待你确认',ShieldCheck],[control?.managed?'开启':'关闭','当前任务自动推进',Clock]].map(([value,text,Icon])=><section key={String(text)}><div>{String(text)}{React.createElement(Icon as typeof Layers,{size:18})}</div><strong>{String(value)}</strong><small>{text==='待你确认'?'每一次授权，都由你决定':text==='当前任务自动推进'?'遵循现有后端调度规则':client==='codex'?'Codex 工作空间':'Claude Code 工作空间'}</small></section>)}</div>
 {page==='approvals'?<div className="approval-grid">{approvals.length?approvals.map(approvalCard):<div className="empty"><ShieldCheck/><h3>一切就绪</h3><p>需要确认的操作会显示在这里。</p></div>}</div>:page==='quota'?<section className="quota panel"><h2>账户额度</h2><p>显示后端返回的真实额度，未知数据不会显示为零。</p>{(['primary','secondary'] as const).map((key,i)=>{const q=snap.quota?.default?.[key];return <div key={key} className="quota-row"><div><span>{i?'长周期额度':'短周期额度'}</span><b>{typeof q?.usedPercent==='number'?`剩余 ${Math.max(0,100-q.usedPercent)}%`:'暂不可用'}</b></div>{typeof q?.usedPercent==='number'&&<progress max={100} value={Math.max(0,100-q.usedPercent)}/>}<small>{q?.resetsAt?`重置时间：${new Date(q.resetsAt*1000).toLocaleString()}`:'接口尚未提供重置时间'}</small></div>;})}<p>{client==='claude'?'Claude 官方剩余额度目前未知。':snap.quotaStatus}</p></section>:<div className="workspace"><section className="task-panel panel"><div className="section-title"><h2>我的任务 <small>{tasks.size}</small></h2><button aria-label="创建测试任务" onClick={()=>setAdding(true)}><Plus size={18}/></button></div><div className="search"><Search size={16}/><input placeholder="搜索任务…" aria-label="搜索任务" value={search} onChange={e=>setSearch(e.target.value)}/></div><div className="task-list">{Array.from(tasks.values()).filter(t=>title(t).toLowerCase().includes(search.toLowerCase())).map(t=><button key={t.id} className={'task '+(selected===t.id?'selected':'')} onClick={()=>{setSelected(t.id);setMessages([]);}}><div className="task-icon"><Terminal size={18}/></div><div><strong>{title(t)}</strong><small><i className={t.status?.type==='active'?'running':''}/>{stateLabel(t)}</small></div><ChevronRight size={14}/></button>)}{!tasks.size&&<div className="empty"><Layers/><h3>等待任务接入</h3><p>本机 Codex / Claude 连接后，任务会出现在这里。</p></div>}</div><footer><i/>{snap.connection==='connected'?'客户端已连接':'客户端连接待检查'}</footer></section>
 <section className="chat panel"><div className="chat-heading"><div><span className="eyebrow">CONVERSATION</span><h2>{current?title(current):'选择任务，开始对话'}</h2></div><MessageSquare size={22}/></div>{selected&&<div className="toolbar">{[['attach','接入'],['pause','暂停'],['resume','继续'],[control?.managed?'unmanage':'manage',control?.managed?'关闭自动推进':'自动推进'],['detach','移出']].map(([key,text])=><button key={key} disabled={busy||!online||(key!=='attach'&&!control)} onClick={()=>action(key)}>{text}</button>)}<button disabled={busy} onClick={()=>setRename(current?title(current):'')}>重命名</button></div>}
 <div className="messages">{messages.length?messages.map((m,i)=><article key={i} className={'message '+(m.role==='user'?'user':'assistant')}><small>{m.role==='user'?'你':m.role==='assistant'?(client==='codex'?'Codex':'Claude Code'):m.role}</small><div>{m.text}</div></article>):<div className="empty chat-empty"><div className="empty-icon"><MessageSquare size={28}/></div><h3>{selected?'留一点空间，给新的想法。':'所有进展，在这里发生。'}</h3><p>{selected?'暂无对话记录。接入任务后，发送你的下一步指令。':'从左侧选择一个任务，查看消息与运行状态。'}</p></div>}{taskApprovals.map(approvalCard)}</div>
 <div className="compose"><textarea aria-label="任务指令" value={draft} onChange={e=>setDrafts(d=>({...d,[draftKey]:e.target.value}))} placeholder="输入下一步指令，或补充你的想法…" disabled={!selected} onKeyDown={e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'&&draft.trim()&&!busy&&control?.canControl&&!control.paused&&online){e.preventDefault();action('message',draft);}}}/><div><small>{control?.paused?'任务已暂停，请先继续':control?.canControl?'Ctrl / ⌘ + Enter 发送':'接入并取得控制权后即可发送'}</small><button aria-label="发送指令" className="primary send" disabled={busy||!online||!draft.trim()||!control?.canControl||control.paused} onClick={()=>action('message',draft)}><ArrowUp size={20}/></button></div></div><div className="chat-foot">消息与操作记录保存在本机 · 提交后等待后台确认</div></section></div>}
 <div className="bottom-note">任务小站 · 为专注而设计</div></main>
 {(adding||rename!==null)&&<div className="overlay"><section role="dialog" aria-modal="true" aria-label={adding?'创建测试任务':'修改名称'} className="modal"><h2>{adding?'创建测试任务':'修改任务名称'}</h2>{adding?<p>将调用现有后台创建一个测试任务。</p>:<input autoFocus maxLength={80} value={rename||''} onChange={e=>setRename(e.target.value)}/>}<div className="actions"><button onClick={()=>{setAdding(false);setRename(null);}}>取消</button><button className="primary" disabled={busy||!online} onClick={()=>action(adding?'create_test':'rename',rename||'')}>确认</button></div></section></div>}</div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
