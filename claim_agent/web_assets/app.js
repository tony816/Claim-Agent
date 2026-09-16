"use strict";
history.replaceState(null, "", "/");
const $ = id => document.getElementById(id);
const state = {id:null, data:null, pending:[], busy:false, uploading:0, composing:false, nodes:new Map(), generation:0, project:null, view:"chat", projectData:null, log:{cursor:null, text:""}};
const drafts = new Map();
let modelSettings=null, authTimer=null;
function settingsError(text){$("model-settings-error").textContent=text;$("model-settings-error").hidden=!text;}
function settingsChanged(){$("model-settings-saved").textContent="저장하지 않은 변경사항";}
function settingSelect(label, options, value, change){
  const wrap=el("label","model-field",label),select=el("select");select.setAttribute("aria-label",label);
  for(const [id,title] of options){const option=el("option","",title);option.value=id;select.append(option);}
  select.value=value;select.onchange=()=>{change(select.value);settingsChanged();};wrap.append(select);return wrap;
}
function modelPicker(kind,value,label,inherit,onchange){
  const models=modelSettings.providers[kind].models,choices=inherit?[["","기본 모델 따름 · "+modelSettings.defaults[kind]]]:[];
  for(const model of models)choices.push([model,model==="default"?"계정 기본 모델 (CLI 자동 선택)":model]);
  choices.push(["__custom","모델 ID 직접 입력…"]);
  const custom=value!==null&&!models.includes(value), wrap=settingSelect(label,choices,custom?"__custom":value||"",v=>{
    input.hidden=v!=="__custom";input.required=v==="__custom";
    onchange(v==="__custom"?input.value:v||null);if(v==="__custom")input.focus();
  });
  const input=el("input");input.placeholder="모델 ID 입력";input.setAttribute("aria-label",label+" 직접 입력");input.value=custom?value:"";input.hidden=!custom;input.required=custom;input.maxLength=160;
  input.oninput=()=>{onchange(input.value.trim());settingsChanged();};wrap.append(input);return wrap;
}
function renderModelSettings(){
  const defaults=$("default-model-controls");defaults.replaceChildren();
  const choices=Object.entries(modelSettings.providers).map(([id,p])=>[id,p.label]);
  defaults.append(settingSelect("기본 연결 방식",choices,modelSettings.provider,v=>{for(const role of modelSettings.roles){if(!role.provider)role.model=null;}modelSettings.provider=v;renderModelSettings();}));
  defaults.append(modelPicker(modelSettings.provider,modelSettings.defaults[modelSettings.provider],"기본 모델",false,v=>{
    modelSettings.defaults[modelSettings.provider]=v;
  }));
  const rows=$("role-model-controls");rows.replaceChildren();
  for(const role of modelSettings.roles){
    const row=el("div","role-model-row"),title=el("div","role-model-name");title.append(el("strong","",role.label),el("small","",role.id));row.append(title);
    row.append(settingSelect(role.label+" 연결",[["","기본 설정 따름"],...choices],role.provider||"",v=>{role.provider=v||null;role.model=null;renderModelSettings();}));
    row.append(modelPicker(role.provider||modelSettings.provider,role.model,role.label+" 모델",true,v=>{role.model=v;}));
    row.append(settingSelect(role.label+" 추론",modelSettings.thinking_levels.map(x=>[x,{MINIMAL:"최소",LOW:"낮음",MEDIUM:"보통",HIGH:"높음"}[x]]),role.thinking_level,v=>{role.thinking_level=v;}));
    rows.append(row);
  }
  $("model-settings-save").disabled=modelSettings.busy;
  if(modelSettings.busy)$("model-settings-saved").textContent="실행 중에는 저장할 수 없습니다. 완료 또는 중지 후 다시 여세요.";
}
async function refreshAuth(){
  clearTimeout(authTimer);const data=await api("/api/auth/status"),box=$("auth-connections");box.replaceChildren();
  for(const [kind,status] of Object.entries(data)){
    const card=el("div","auth-card"),meta=modelSettings.providers[kind];card.append(el("strong","",meta.label));
    card.append(el("span",status.connected?"auth-connected":"auth-disconnected",status.connected?"● 연결됨":"○ 연결 필요"));
    card.append(el("p","",status.pending?status.login_message:status.message));
    if(status.login_message&&!status.pending&&!status.connected)card.append(el("p","",status.login_message));
    if(kind.endsWith("_oauth")){
      const button=el("button","",status.pending?"브라우저 승인 대기 중…":status.connected?"다른 구독 계정으로 로그인":"구독 계정 연결");button.type="button";button.disabled=!status.installed||status.pending||modelSettings.busy;
      button.onclick=async()=>{button.disabled=true;settingsError("");try{await api("/api/auth/login",{provider:kind});await refreshAuth();}catch(e){settingsError(e.message);button.disabled=false;}};card.append(button);
      const link=el("a","","CLI 설치 안내 ↗");link.href=meta.install_url;link.target="_blank";link.rel="noopener noreferrer";card.append(link);
    }
    box.append(card);
  }
  if(Object.values(data).some(s=>s.pending)&&$("model-settings-dialog").open)authTimer=setTimeout(()=>refreshAuth().catch(e=>settingsError(e.message)),3000);
}
$("model-settings-open").onclick=async()=>{
  settingsError("");$("model-settings-saved").textContent="";
  try{modelSettings=await api("/api/model-settings");renderModelSettings();$("model-settings-dialog").showModal();$("auth-connections").textContent="연결 확인 중…";await refreshAuth();}catch(e){settingsError(e.message);error(e.message);}
};
$("model-settings-close").onclick=()=>$("model-settings-dialog").close();
$("model-settings-dialog").onclose=()=>clearTimeout(authTimer);
$("auth-refresh").onclick=()=>refreshAuth().catch(e=>settingsError(e.message));
$("models-apply-all").onclick=()=>{for(const role of modelSettings.roles){role.provider=null;role.model=null;}renderModelSettings();settingsChanged();};
$("model-settings-form").onsubmit=async event=>{
  event.preventDefault();settingsError("");$("model-settings-save").disabled=true;
  try{
    const roles=Object.fromEntries(modelSettings.roles.map(r=>[r.id,{provider:r.provider,model:r.model,thinking_level:r.thinking_level}]));
    modelSettings=await api("/api/model-settings",{provider:modelSettings.provider,defaults:modelSettings.defaults,roles});renderModelSettings();
    $("model-settings-saved").textContent="저장됨 · 다음 요청부터 적용됩니다";await list();
  }catch(e){settingsError(e.message);}finally{$("model-settings-save").disabled=!!modelSettings.busy;}
};
const routeNames = {CHAT:"일반 대화",META:"설정·진행 확인",AUTHORING_DRAFT:"청구항 작성·수정",FINALIZATION:"출원용 최종 검증",REVIEW_ONLY:"특허 의견·제한 검수"};
const el = (tag, cls, text) => {const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;};
function error(message){$("error").textContent=message;$("error").hidden=!message;}
async function api(path, body, raw=false){
  const options=body===undefined?{}:{method:"POST",headers:{"X-Claim-Request":"1",...(raw?{}:{"Content-Type":"application/json"})},body:raw?body:JSON.stringify(body)};
  const response=await fetch(path,options);const result=await response.json();
  if(!response.ok)throw new Error(result.error||"요청을 처리하지 못했습니다.");return result;
}
function inline(node,text){
  // Build DOM nodes, never inject model/user HTML.
  const pieces=text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  for(const piece of pieces){if(piece.startsWith("**")&&piece.endsWith("**"))node.append(el("strong","",piece.slice(2,-2)));else if(piece.startsWith("`")&&piece.endsWith("`"))node.append(el("code","",piece.slice(1,-1)));else node.append(document.createTextNode(piece));}
}
function markdown(node,text){
  node.replaceChildren();const lines=text.split("\n");let i=0;
  while(i<lines.length){let line=lines[i];
    if(line.startsWith("```")){const code=[];i++;while(i<lines.length&&!lines[i].startsWith("```"))code.push(lines[i++]);i++;const pre=el("pre");pre.append(el("code","",code.join("\n")));node.append(pre);continue;}
    if(!line.trim()){i++;continue;}
    const heading=line.match(/^(#{1,3})\s+(.*)$/);if(heading){const h=el("h"+heading[1].length);inline(h,heading[2]);node.append(h);i++;continue;}
    if(line.includes("|")&&i+1<lines.length&&/^\s*\|?\s*:?-{3}/.test(lines[i+1])){const table=el("table");const addRow=(value,head)=>{const row=el("tr");for(const cell of value.trim().replace(/^\||\|$/g,"").split("|")){const c=el(head?"th":"td");inline(c,cell.trim());row.append(c);}table.append(row);};addRow(line,true);i+=2;while(i<lines.length&&lines[i].includes("|"))addRow(lines[i++],false);node.append(table);continue;}
    const list=line.match(/^\s*(?:[-*]|\d+\.)\s+/);if(list){const ordered=/^\s*\d/.test(line),ul=el(ordered?"ol":"ul");while(i<lines.length&&/^\s*(?:[-*]|\d+\.)\s+/.test(lines[i])){const li=el("li");inline(li,lines[i++].replace(/^\s*(?:[-*]|\d+\.)\s+/,""));ul.append(li);}node.append(ul);continue;}
    const p=el("p",line.startsWith("※ ")?"note":"");inline(p,line);node.append(p);i++;
  }
}
function resize(){const p=$("prompt");p.style.height="auto";p.style.height=Math.min(p.scrollHeight,180)+"px";}
function controls(){const running=!!state.data?.running;$("send").hidden=running;$("stop").hidden=!running;$("send").disabled=state.busy||state.uploading>0||!$("prompt").value.trim()||!claimTarget().ok;$("attach").disabled=state.uploading>0;$("mode").disabled=running||state.busy;$("options").hidden=$("mode").value!=="AUTHORING_DRAFT";$("prompt").placeholder=$("mode").value==="CHAT"?"무엇이든 물어보세요":state.data?.run_id?"수정하고 싶은 내용을 알려주세요":"발명을 설명하거나 자료를 첨부해 주세요";}
// ---- 작성 대상: 독립항만 / 특정 항 1개 / 종속항 범위. 서버의 resolve_claim_target와 같은 정규형(2~4,6)을 미리 보여 준다.
const target={segments:[{from:2,to:8}]};
function formatTarget(numbers){const a=[...numbers].sort((x,y)=>x-y);if(!a.length)return "";const parts=[];let start=a[0],prev=a[0];for(const n of a.slice(1).concat([null])){if(n!==null&&n===prev+1){prev=n;continue;}parts.push(start===prev?String(start):start+"~"+prev);if(n!==null)start=prev=n;}return parts.join(",");}
function targetMode(){return document.querySelector("input[name=target-mode]:checked")?.value||"independent";}
function claimTarget(){
  const mode=targetMode();const out={target_mode:mode,target_claim:null,target_segments:[],dependent:false,target:null,ok:true,label:"독립항만",error:""};
  if(mode==="single"){const n=parseInt($("target-claim").value,10);if(!(n>=1&&n<=1000)){out.ok=false;out.error="항 번호는 1~1000 사이 숫자여야 합니다.";}else{out.target_claim=n;if(n>1){out.dependent=true;out.target=String(n);}out.label=n===1?"1항(독립항)만":n+"항만";}}
  else if(mode==="range"){const numbers=new Set();for(const seg of target.segments){const lo=parseInt(seg.from,10),hi=parseInt(seg.to===""||seg.to===undefined?seg.from:seg.to,10);if(!(lo>=2&&hi>=lo&&hi<=1000)){out.ok=false;out.error=lo===1||hi===1?"종속항 범위에는 2항 이상만 넣을 수 있습니다.":"구간은 시작 항 ≤ 끝 항, 2~1000 사이여야 합니다.";break;}for(let n=lo;n<=hi;n++)numbers.add(n);}
    if(out.ok&&!numbers.size){out.ok=false;out.error="구간을 하나 이상 추가하세요.";}
    if(out.ok){out.dependent=true;out.target=formatTarget(numbers);out.target_segments=target.segments.map(s=>({from:parseInt(s.from,10),to:parseInt(s.to===""||s.to===undefined?s.from:s.to,10)}));out.label=out.target+"항";}}
  return out;
}
function renderSegments(){
  const box=$("target-segments");box.replaceChildren();const mode=targetMode();
  target.segments.forEach((seg,i)=>{const row=el("div","target-row");const from=el("input");from.type="number";from.min="2";from.max="1000";from.value=seg.from;from.setAttribute("aria-label","시작 항");const to=el("input");to.type="number";to.min="2";to.max="1000";to.value=seg.to;to.setAttribute("aria-label","끝 항");to.placeholder=String(seg.from);
    from.oninput=()=>{seg.from=from.value;updateTarget();};to.oninput=()=>{seg.to=to.value;updateTarget();};from.disabled=to.disabled=mode!=="range";
    const remove=el("button","","×");remove.type="button";remove.setAttribute("aria-label","구간 제거");remove.disabled=mode!=="range"||target.segments.length<2;remove.onclick=()=>{target.segments.splice(i,1);renderSegments();updateTarget();};
    row.append(document.createTextNode("제 "),from,document.createTextNode(" ~ "),to,document.createTextNode(" 항"),remove);box.append(row);});
  $("target-add").disabled=mode!=="range";$("target-claim").disabled=mode!=="single";
}
function updateTarget(){const t=claimTarget();$("target-preview").textContent=t.ok?(t.dependent?t.target:"독립항만"):"—";$("target-preview").className=t.ok?"":"invalid";$("target-preview").title=t.error;$("options-summary").textContent="작성 옵션 · "+(t.ok?t.label:"입력 확인");controls();}
for(const radio of document.querySelectorAll("input[name=target-mode]"))radio.onchange=()=>{renderSegments();updateTarget();};
$("target-claim").oninput=updateTarget;$("target-add").onclick=()=>{const last=target.segments.at(-1);const next=(parseInt(last?.to||last?.from,10)||1)+1;target.segments.push({from:next,to:next});renderSegments();updateTarget();};
renderSegments();updateTarget();
function pending(){const container=$("attachments");container.replaceChildren();for(const file of state.pending){const chip=el("div","attachment");chip.append(el("span","","▤ "+file.name));const remove=el("button","","×");remove.type="button";remove.setAttribute("aria-label",file.name+" 첨부 취소");remove.onclick=()=>{state.pending=state.pending.filter(x=>x.id!==file.id);pending();};chip.append(remove);container.append(chip);}controls();}
function render(data){
  const box=$("conversation"),follow=box.scrollHeight-box.scrollTop-box.clientHeight<100;
  state.data=data;$("welcome").hidden=data.messages.length>0;
  const chip=$("project-chip");if(data.project){chip.hidden=false;chip.textContent="▣ "+data.project.name+(data.project.files?" · 파일 "+data.project.files:"")+(data.project.has_instructions?" · 지침":"");chip.onclick=()=>openProject(data.project.id).catch(e=>error(e.message));}else chip.hidden=true;
  for(const m of data.messages){
    let view=state.nodes.get(m.id);
    if(!view){
      const article=el("article","message "+m.role),body=el("div","body");let logs,pre,summary,routeLabel,full,fullBody;
      if(m.role==="assistant"){
        const label=el("div","assistant-label");label.append(el("span","mark","C"),document.createTextNode("Claim-Agent"));article.append(label);
        routeLabel=el("div","route-label");article.append(routeLabel);
        logs=el("details","logs");summary=el("summary","","작업 로그 펼치기");pre=el("pre");logs.append(summary,pre);article.append(logs);
        logs.ontoggle=async()=>{if(logs.open&&view.status!=="running"&&!pre.textContent){try{const value=await api(`/api/log?id=${data.id}&message=${m.id}`);pre.textContent=value.text;}catch(e){pre.textContent=e.message;}}};
        full=el("details","logs full-report");const fs=el("summary","","전체 보고서 펼치기 (게이트 표·근거·역할별 원 보고서)");fullBody=el("div","body");full.append(fs,fullBody);article.append(full);
        full.ontoggle=async()=>{if(full.open&&!fullBody.textContent){try{const value=await api(`/api/report?id=${data.id}&message=${m.id}`);markdown(fullBody,value.text);}catch(e){fullBody.textContent=e.message;}}};
      }
      article.append(body);
      if(m.files?.length){const files=el("div","message-files");for(const f of m.files)files.append(el("span","file-tag","▤ "+f.name));article.append(files);}
      if(m.role==="assistant"){
        const actions=el("div","message-actions"),copy=el("button","","복사"),revise=el("button","","수정 요청");
        copy.onclick=async()=>{try{await navigator.clipboard.writeText(view.text);copy.textContent="복사됨";setTimeout(()=>copy.textContent="복사",1300);}catch(e){error("텍스트를 선택한 뒤 Ctrl+C로 복사하세요.");}};
        revise.onclick=()=>{$("mode").value=m.mode;$("prompt").focus();controls();};actions.append(copy,revise);article.append(actions);
      }
      $("messages").append(article);view={article,body,logs,pre,summary,routeLabel,full,fullBody,text:null,status:null};state.nodes.set(m.id,view);
    }
    if(view.full)view.full.hidden=!(m.pipeline_run_id&&m.status&&m.status!=="running");
    if(view.routeLabel){view.routeLabel.textContent=m.execution_mode?"자동 분류 · "+(routeNames[m.execution_mode]||m.execution_mode):m.mode==="AUTHORING_DRAFT"?"청구항 작성·수정":"";view.routeLabel.title=m.route_reason||"";}
    if(view.text!==m.text||view.status!==m.status){if(m.role==="user")view.body.textContent=m.text;else if(m.text)markdown(view.body,m.text);else view.body.replaceChildren(el("span","pending",m.mode==="CHAT"?"답변을 작성하고 있어요…":"에이전트가 자료를 검토하고 있어요…"));view.text=m.text;}
    if(view.logs){
      if(m.status==="running"){view.pre.textContent=state.log.text||"연결 중…";view.summary.textContent="작업 중 · 실시간 로그";if(view.status!=="running")view.logs.open=true;}
      else {view.summary.textContent=(m.status==="error"?"오류 확인 · ":m.status==="review"?"검토 필요 · ":"")+"작업 로그 펼치기";if(view.status==="running"){view.logs.open=false;view.pre.textContent="";}}
    }
    view.status=m.status;
  }
  renderRun(data);controls();if(follow)box.scrollTop=box.scrollHeight;
}
// 실시간 로그는 커서 이후의 새 부분만 받아 이어 붙인다(매 폴링마다 30만 자 전문을 다시 받지 않는다).
async function fetchSession(id, incremental){
  const cursor=incremental&&state.log.cursor!==null?"&log_from="+state.log.cursor:"";
  const data=await api("/api/session?id="+encodeURIComponent(id)+cursor);
  if(!incremental||data.log_reset)state.log.text=data.live_log||"";
  else if(data.live_log)state.log.text+=data.live_log;
  // 서버 창과 같은 길이로 유지하되, 시작 부분(첫 역할 헤더)은 남기고 가운데를 잘라낸다.
  if(state.log.text.length>300000)state.log.text=state.log.text.slice(0,60000)+"\n\n…(실시간 로그 일부 생략)…\n\n"+state.log.text.slice(-240000);
  state.log.cursor=data.log_cursor??null;
  return data;
}
async function list(){
  const data=await api("/api/sessions");$("model").textContent=data.model;
  const names=new Map(data.projects.map(p=>[p.id,p.name]));
  if(state.project&&!names.has(state.project))state.project=null;
  const projects=$("projects");projects.replaceChildren();
  const all=el("button",!state.project&&state.view==="chat"?"active":"","전체 대화");all.onclick=()=>{state.project=null;localStorage.removeItem("claim-project");showChat();list().catch(e=>error(e.message));};projects.append(all);
  for(const p of data.projects){const b=el("button",p.id===state.project?"active":"");b.append(el("span","","▣ "+p.name),el("span","count",String(p.sessions)));b.title=p.name+" · 대화 "+p.sessions+"개 · 파일 "+p.files+"개";b.onclick=()=>openProject(p.id).catch(e=>error(e.message));projects.append(b);}
  const sessions=$("sessions");sessions.replaceChildren();
  const visible=data.sessions.filter(s=>!state.project||s.project_id===state.project);
  for(const s of visible){const row=el("div","session-row"+(s.id===state.id&&state.view==="chat"?" active":""));const b=el("button","session-open");b.append(document.createTextNode(s.title));if(!state.project&&s.project_id&&names.has(s.project_id))b.append(el("span","tag",names.get(s.project_id)));b.onclick=()=>open(s.id).catch(e=>error(e.message));const del=el("button","session-delete","×");del.title="대화 삭제";del.setAttribute("aria-label",s.title+" 삭제");del.onclick=()=>deleteSession(s.id,s.title).catch(e=>error(e.message));row.append(b,del);sessions.append(row);}
  if(!visible.length)sessions.append(el("p","side-empty",state.project?"아직 대화가 없습니다.":"대화가 없습니다."));
  $("new-chat").textContent=state.project&&names.has(state.project)?"＋ 새 대화 · "+names.get(state.project):"＋ 새 대화";
  return data.sessions;
}
function showChat(){state.view="chat";$("project-panel").hidden=true;$("conversation").hidden=false;document.querySelector("main footer").hidden=false;}
async function open(id){
  if(state.busy||state.uploading)return;
  if(state.id)drafts.set(state.id,{text:$("prompt").value,files:state.pending,mode:$("mode").value});
  const version=++state.generation;state.log={cursor:null, text:""};const data=await fetchSession(id,false);if(version!==state.generation)return;
  state.id=id;state.nodes.clear();$("messages").replaceChildren();state.pending=drafts.get(id)?.files||[];
  $("prompt").value=drafts.get(id)?.text||localStorage.getItem("draft-"+id)||"";
  $("mode").value=drafts.get(id)?.mode||data.messages.filter(m=>m.role==="user").at(-1)?.mode||"CHAT";
  state.project=data.project?data.project.id:null;if(state.project)localStorage.setItem("claim-project",state.project);else localStorage.removeItem("claim-project");
  showChat();localStorage.setItem("claim-session",id);document.body.classList.remove("sidebar-open");error("");render(data);pending();resize();await list();$("conversation").scrollTop=$("conversation").scrollHeight;$("prompt").focus();
}
async function deleteSession(id,title){
  if(state.busy||state.uploading)return;
  if(!confirm(`"${title}" 대화와 첨부 파일을 삭제할까요? 청구항 작업 기록(runs/)은 남습니다.`))return;
  await api("/api/delete",{id});drafts.delete(id);localStorage.removeItem("draft-"+id);
  if(state.view==="project"&&state.projectData){await openProject(state.projectData.id);return;}
  if(id!==state.id){await list();return;}
  state.id=null;state.data=null;state.nodes.clear();$("messages").replaceChildren();localStorage.removeItem("claim-session");
  const sessions=await list();const next=sessions.find(s=>!state.project||s.project_id===state.project);
  if(next)await open(next.id);else await create();
}
async function create(projectId){if(state.busy||state.uploading)return;const data=await api("/api/new",{project_id:projectId===undefined?state.project:projectId});await open(data.id);}
async function refresh(){const id=state.id,version=state.generation;if(!id)return;const data=await fetchSession(id,true);if(id===state.id&&version===state.generation){const wasRunning=state.data?.running;render(data);if(wasRunning&&!data.running)await list();}}
async function upload(files){
  if(state.view==="project")return projectUpload(files);
  if(!files.length)return;const id=state.id;if(!id)return;state.uploading++;controls();error("");
  try{for(const file of files){if(file.size>20*1024*1024)throw new Error(file.name+": 파일당 20MB까지 첨부할 수 있습니다.");const item=await api(`/api/upload?id=${id}&name=${encodeURIComponent(file.name||"붙여넣은 이미지.png")}`,file,true);if(state.id===id){state.pending.push(item);pending();}}}catch(e){error(e.message);}finally{state.uploading--;controls();}
}
async function send(event){
  event.preventDefault();if(state.composing||state.busy||state.uploading||state.data?.running||!$("prompt").value.trim())return;
  state.busy=true;controls();error("");const id=state.id,text=$("prompt").value;
  const t=claimTarget();if(!t.ok){error(t.error);return;}
  try{await api("/api/send",{id,text,mode:$("mode").value,files:state.pending.map(f=>f.id),target_mode:t.target_mode,target_claim:t.target_claim,target_segments:t.target_segments,dependent:t.dependent,target:t.target});$("prompt").value="";localStorage.removeItem("draft-"+id);state.pending=[];drafts.delete(id);pending();resize();await refresh();restartPoll();await list();$("conversation").scrollTop=$("conversation").scrollHeight;}
  catch(e){error(e.message);}finally{state.busy=false;controls();$("prompt").focus();}
}
$("composer").onsubmit=send;
$("prompt").addEventListener("compositionstart",()=>state.composing=true);
$("prompt").addEventListener("compositionend",()=>{state.composing=false;controls();});
$("prompt").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing&&!state.composing&&event.keyCode!==229){event.preventDefault();$("composer").requestSubmit();}});
$("prompt").oninput=()=>{resize();controls();if(state.id)localStorage.setItem("draft-"+state.id,$("prompt").value);};
$("prompt").onpaste=event=>{const files=[...event.clipboardData.files];if(files.length){event.preventDefault();upload(files);}};
$("attach").onclick=()=>$("file-picker").click();$("file-picker").onchange=event=>{upload([...event.target.files]);event.target.value="";};
let drag=0;document.addEventListener("dragenter",event=>{if(event.dataTransfer.types.includes("Files")){event.preventDefault();drag++;$("drop-overlay").hidden=false;}});
document.addEventListener("dragover",event=>{if(event.dataTransfer.types.includes("Files"))event.preventDefault();});
document.addEventListener("dragleave",()=>{if(--drag<=0){drag=0;$("drop-overlay").hidden=true;}});
document.addEventListener("drop",event=>{drag=0;$("drop-overlay").hidden=true;if(event.dataTransfer.files.length){event.preventDefault();upload([...event.dataTransfer.files]);}});
$("new-chat").onclick=()=>create().catch(e=>error(e.message));$("menu").onclick=()=>document.body.classList.toggle("sidebar-open");
$("new-project").onclick=()=>createProject().catch(e=>error(e.message));
$("mode").onchange=controls;$("stop").onclick=async()=>{try{await api("/api/stop",{id:state.id});await refresh();}catch(e){error(e.message);}};
$("shutdown").onclick=async()=>{if(state.data?.running&&!confirm("진행 중인 응답을 중지하고 프로그램을 종료할까요?"))return;try{await api("/api/shutdown",{});stopPolling();$("composer").hidden=true;error("프로그램을 종료했습니다. 다시 이용하려면 바탕화면 실행기를 더블클릭하세요.");}catch(e){error(e.message);}};
for(const button of document.querySelectorAll("[data-prompt]"))button.onclick=()=>{$("prompt").value=button.dataset.prompt;resize();controls();$("prompt").focus();};
let timer=null,polling=true;
function poll(){
  if(!polling)return;
  const wait=state.data?.running?700:2500;      // 작업 중에는 촘촘히, 대기 중에는 느슨하게
  timer=setTimeout(async()=>{
    if(!state.busy){try{await refresh();}catch(e){error("프로그램에 연결할 수 없습니다. 실행기를 다시 더블클릭해 주세요.");}}
    poll();
  },wait);
}
function stopPolling(){polling=false;clearTimeout(timer);}
function restartPoll(){clearTimeout(timer);poll();}     // 전송·재개 직후에는 대기 간격을 기다리지 않는다
poll();
(async()=>{try{state.project=localStorage.getItem("claim-project")||null;const sessions=await list();const saved=localStorage.getItem("claim-session");if(sessions.length)await open(sessions.find(s=>s.id===saved)?.id||sessions[0].id);else await create(null);}catch(e){error(e.message);}})();

// ---- 프로젝트: 지침·USER_LOCK·소스 파일을 미리 설정해 두는 폴더. 속한 대화의 매 요청에 자동으로 전달된다.
const CATEGORY_LABEL={invention:"발명 자료",drawing:"도면",prior_art:"선행기술",spec:"정식 명세서"};
function fmtSize(n){return n>=1048576?(n/1048576).toFixed(1)+" MB":n>=1024?Math.round(n/1024)+" KB":n+" B";}
function renderProject(p){
  state.projectData=p;$("project-title").textContent=p.name;
  if(document.activeElement!==$("project-name"))$("project-name").value=p.name;
  if(document.activeElement!==$("project-description"))$("project-description").value=p.description||"";
  if(document.activeElement!==$("project-instructions"))$("project-instructions").value=p.instructions||"";
  if(document.activeElement!==$("project-user-lock"))$("project-user-lock").value=p.user_lock||"";
  const files=$("project-file-list");files.replaceChildren();
  for(const f of p.files){const li=el("li");li.append(el("span","cat",CATEGORY_LABEL[f.category]||f.category),el("span","name","▤ "+f.name),el("span","size",fmtSize(f.size)));const remove=el("button","","제거");remove.type="button";remove.onclick=async()=>{if(!confirm(f.name+" 파일을 프로젝트에서 제거할까요? 이미 실행된 작업의 기록은 유지됩니다."))return;try{await api("/api/project/remove-file",{id:p.id,file:f.id});await openProject(p.id);}catch(e){error(e.message);}};li.append(remove);files.append(li);}
  if(!p.files.length)files.append(el("li","empty","등록된 소스 파일이 없습니다. 발명 설명·도면·선행기술을 추가해 두면 이 프로젝트의 모든 대화에서 사용됩니다."));
  const sessions=$("project-session-list");sessions.replaceChildren();
  for(const s of p.sessions){const li=el("li");const b=el("button","",s.title);b.type="button";b.onclick=()=>open(s.id).catch(e=>error(e.message));const del=el("button","session-delete","×");del.type="button";del.title="대화 삭제";del.setAttribute("aria-label",s.title+" 삭제");del.onclick=()=>deleteSession(s.id,s.title).catch(e=>error(e.message));li.append(b,del);sessions.append(li);}
  if(!p.sessions.length)sessions.append(el("li","empty","아직 대화가 없습니다. 위의 버튼으로 이 프로젝트의 첫 대화를 시작하세요."));
}
async function openProject(id){
  if(state.busy||state.uploading)return;
  const p=await api("/api/project?id="+encodeURIComponent(id));
  state.project=id;state.view="project";localStorage.setItem("claim-project",id);
  $("conversation").hidden=true;document.querySelector("main footer").hidden=true;$("project-panel").hidden=false;$("project-saved").textContent="";
  document.body.classList.remove("sidebar-open");error("");renderProject(p);await list();
}
async function createProject(){if(state.busy||state.uploading)return;const p=await api("/api/project/new",{name:"새 프로젝트"});await openProject(p.id);$("project-name").select();}
async function projectUpload(files){
  const p=state.projectData;if(!p||!files.length)return;state.uploading++;controls();error("");
  try{for(const file of files){if(file.size>20*1024*1024)throw new Error(file.name+": 파일당 20MB까지 첨부할 수 있습니다.");await api(`/api/project/upload?id=${encodeURIComponent(p.id)}&name=${encodeURIComponent(file.name||"붙여넣은 이미지.png")}&category=${$("project-category").value}`,file,true);}}
  catch(e){error(e.message);}finally{state.uploading--;controls();}
  try{await openProject(p.id);}catch(e){error(e.message);}
}
$("project-form").onsubmit=async event=>{
  event.preventDefault();const p=state.projectData;if(!p)return;
  try{const updated=await api("/api/project/update",{id:p.id,name:$("project-name").value,description:$("project-description").value,instructions:$("project-instructions").value,user_lock:$("project-user-lock").value});
    renderProject(updated);$("project-saved").textContent="저장됨";setTimeout(()=>$("project-saved").textContent="",1500);await list();}
  catch(e){error(e.message);}
};
$("project-new-chat").onclick=()=>{const p=state.projectData;if(p)create(p.id).catch(e=>error(e.message));};
$("project-attach").onclick=()=>$("project-file-picker").click();$("project-file-picker").onchange=event=>{projectUpload([...event.target.files]);event.target.value="";};
$("project-delete").onclick=async()=>{const p=state.projectData;if(!p)return;if(!confirm(`"${p.name}" 프로젝트와 저장된 지침·소스 파일을 삭제할까요? 대화 ${p.sessions.length}개는 남고 프로젝트 연결만 해제됩니다.`))return;
  try{await api("/api/project/delete",{id:p.id});state.project=null;localStorage.removeItem("claim-project");const sessions=await list();if(sessions.length)await open(sessions[0].id);else await create(null);}catch(e){error(e.message);}};

// ---- 작업 상태 패널: 단계 스텝퍼 · 게이트 표 · 중지 사유 · 재개 · 내보내기 · 리비전 대조
const STAGES_ROOT=["ARCHITECT","DRAFT","STYLE","SUCCESS","SYNTAX","OA","BLIND","PICTURE","LOCK"],STAGES_DEP=["DEP_ARCHITECT","DEP_DRAFT","DEP_STYLE","DEP_SUCCESS","DEP_SYNTAX","DEP_OA","DEP_RECON","DEP_LOCK"];
const STAGE_LABEL={ARCHITECT:"설계",DRAFT:"의미 초안",STYLE:"문체",SUCCESS:"성공조건",SYNTAX:"통사",OA:"OA",BLIND:"블라인드",PICTURE:"기준 비교",LOCK:"독립항 LOCK",DEP_ARCHITECT:"종속 설계",DEP_DRAFT:"종속 초안",DEP_STYLE:"종속 문체",DEP_SUCCESS:"종속 성공조건",DEP_SYNTAX:"종속 통사",DEP_OA:"종속 OA",DEP_RECON:"종속 역구성",DEP_LOCK:"종속 LOCK"};
const KIND_LABEL={none:"같은 단계 재실행",accept_unverified:"미검증 수용(명세서·선행기술 부재만)",style:"스타일만 수정 (r+1)",meaning:"의미 수정 (r+1)",redesign:"재설계 (d+1)",restart:"처음부터"};
let runKey="";
function renderRun(data){
  const panel=$("run-panel"),run=data.run;
  if(!run||data.running){panel.hidden=true;return;}
  panel.hidden=false;
  $("run-outcome").textContent=(run.outcome||"")+" · "+(run.revision||"")+"/"+(run.design_revision||"");
  const u=run.usage||{};
  $("run-usage").textContent=u.calls?`호출 ${u.calls}회 · ${((u.latency_ms||0)/1000).toFixed(0)}s · 입력 ${Math.round(u.prompt_tokens||0).toLocaleString()} / 출력 ${Math.round((u.output_tokens||0)+(u.thoughts_tokens||0)).toLocaleString()} 토큰 · 비용 ${u.unpriced_calls?"N/A":"$"+(u.cost_usd||0).toFixed(4)}`:"";
  const seen={};for(const r of run.stages||[]){if(!r.superseded&&!r.stale)seen[r.stage]=r;}
  const steps=$("run-steps");steps.replaceChildren();
  const order=STAGES_ROOT.concat(run.dependent?STAGES_DEP:[]);
  for(const st of order){const li=el("li","",STAGE_LABEL[st]||st);const rec=seen[st];
    if(run.halt&&run.halt.stage===st)li.className="halt";else if(rec||(st==="LOCK"&&run.draft_claim_lock)||(st==="DEP_LOCK"&&run.dependent&&run.dependent.draft_set_lock))li.className="done";
    li.title=rec?`${rec.role}: ${rec.status} ${Object.entries(rec.gates||{}).map(([k,v])=>k+"="+v).join(", ")}`:"";steps.append(li);}
  const halt=$("run-halt");halt.replaceChildren();
  if(run.halt){halt.hidden=false;halt.append(el("strong","",`${run.halt.kind} @ ${run.halt.stage} (${run.halt.role||""})`),document.createTextNode("\n"+(run.halt.message||"")));
    if(run.halt.open_issues&&run.halt.open_issues.length){const ul=el("ul");for(const o of run.halt.open_issues)ul.append(el("li","",`[${o.kind||""}] ${o.code||""} ${o.text||""}`));halt.append(ul);}}
  else halt.hidden=true;
  const table=$("run-gate-table");table.replaceChildren();
  const head=el("tr");for(const h of ["단계","역할","상태","게이트","비고"]){head.append(el("th","",h));}table.append(head);
  for(const r of run.stages||[]){const tr=el("tr");tr.className=r.superseded||r.stale?"superseded":"";for(const v of [r.stage,r.role,r.status,Object.entries(r.gates||{}).map(([k,v])=>k+": "+v).join(", "),(r.superseded?"superseded ":"")+(r.stale?"STALE":"")])tr.append(el("td","",v||""));table.append(tr);}
  const base=`/api/export?id=${encodeURIComponent(data.id)}`;
  $("export-docx").href=base+"&format=docx";$("export-md").href=base+"&format=md";$("export-evidence").href=base+"&format=docx&evidence=1";
  const kinds=run.halt?["none","style","meaning","redesign","accept_unverified"]:["style","meaning","redesign"];
  const select=$("resume-kind");const current=select.value;select.replaceChildren();
  for(const k of kinds){const o=el("option","",KIND_LABEL[k]);o.value=k;select.append(o);}
  if(kinds.includes(current))select.value=current;
  $("resume-scope").hidden=!(run.dependent&&(run.dependent.draft_set_lock||run.dependent.final_set_lock));
  $("resume-files").textContent=state.pending.length?`첨부 ${state.pending.length}개 → 재설계로 실행`:"";
  const key=data.id+"|"+(run.outcome||"")+"|"+(run.revision||"")+"|"+(run.dependent&&run.dependent.draft_set_lock||"");
  if(key!==runKey){runKey=key;$("run-diff").open=false;$("run-diff-body").textContent="";}
}
$("run-diff").ontoggle=async()=>{if($("run-diff").open&&!$("run-diff-body").textContent){try{const r=await api(`/api/diff?id=${encodeURIComponent(state.id)}`);$("run-diff-body").textContent=r.text;}catch(e){$("run-diff-body").textContent=e.message;}}};
$("resume-form").onsubmit=async event=>{
  event.preventDefault();if(state.busy||state.data?.running)return;
  const kind=$("resume-kind").value,text=$("resume-text").value.trim();
  if(kind!=="accept_unverified"&&!text){error("결정 내용을 입력하세요.");return;}
  state.busy=true;controls();error("");
  try{await api("/api/resume",{id:state.id,kind:state.pending.length&&kind!=="none"&&kind!=="accept_unverified"?"restart":kind,text,files:state.pending.map(f=>f.id),scope:$("resume-scope").hidden?"":$("resume-scope").value});
    $("resume-text").value="";state.pending=[];pending();await refresh();restartPoll();$("conversation").scrollTop=$("conversation").scrollHeight;}
  catch(e){error(e.message);}finally{state.busy=false;controls();}
};
