"use strict";
history.replaceState(null, "", "/");
const $ = id => document.getElementById(id);
const state = {id:null, data:null, pending:[], busy:false, uploading:0, composing:false, nodes:new Map(), generation:0};
const drafts = new Map();
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
    const p=el("p");inline(p,line);node.append(p);i++;
  }
}
function resize(){const p=$("prompt");p.style.height="auto";p.style.height=Math.min(p.scrollHeight,180)+"px";}
function controls(){const running=!!state.data?.running;$("send").hidden=running;$("stop").hidden=!running;$("send").disabled=state.busy||state.uploading>0||!$("prompt").value.trim();$("attach").disabled=state.uploading>0;$("mode").disabled=running||state.busy;$("options").hidden=$("mode").value!=="AUTHORING_DRAFT";$("prompt").placeholder=$("mode").value==="CHAT"?"무엇이든 물어보세요":state.data?.run_id?"수정하고 싶은 내용을 알려주세요":"발명을 설명하거나 자료를 첨부해 주세요";}
function pending(){const container=$("attachments");container.replaceChildren();for(const file of state.pending){const chip=el("div","attachment");chip.append(el("span","","▤ "+file.name));const remove=el("button","","×");remove.type="button";remove.setAttribute("aria-label",file.name+" 첨부 취소");remove.onclick=()=>{state.pending=state.pending.filter(x=>x.id!==file.id);pending();};chip.append(remove);container.append(chip);}controls();}
function render(data){
  const box=$("conversation"),follow=box.scrollHeight-box.scrollTop-box.clientHeight<100;
  state.data=data;$("welcome").hidden=data.messages.length>0;
  for(const m of data.messages){
    let view=state.nodes.get(m.id);
    if(!view){
      const article=el("article","message "+m.role),body=el("div","body");let logs,pre,summary,routeLabel;
      if(m.role==="assistant"){
        const label=el("div","assistant-label");label.append(el("span","mark","C"),document.createTextNode("Claim-Agent"));article.append(label);
        routeLabel=el("div","route-label");article.append(routeLabel);
        logs=el("details","logs");summary=el("summary","","작업 로그 펼치기");pre=el("pre");logs.append(summary,pre);article.append(logs);
        logs.ontoggle=async()=>{if(logs.open&&view.status!=="running"&&!pre.textContent){try{const value=await api(`/api/log?id=${data.id}&message=${m.id}`);pre.textContent=value.text;}catch(e){pre.textContent=e.message;}}};
      }
      article.append(body);
      if(m.files?.length){const files=el("div","message-files");for(const f of m.files)files.append(el("span","file-tag","▤ "+f.name));article.append(files);}
      if(m.role==="assistant"){
        const actions=el("div","message-actions"),copy=el("button","","복사"),revise=el("button","","수정 요청");
        copy.onclick=async()=>{try{await navigator.clipboard.writeText(view.text);copy.textContent="복사됨";setTimeout(()=>copy.textContent="복사",1300);}catch(e){error("텍스트를 선택한 뒤 Ctrl+C로 복사하세요.");}};
        revise.onclick=()=>{$("mode").value=m.mode;$("prompt").focus();controls();};actions.append(copy,revise);article.append(actions);
      }
      $("messages").append(article);view={article,body,logs,pre,summary,routeLabel,text:null,status:null};state.nodes.set(m.id,view);
    }
    if(view.routeLabel){view.routeLabel.textContent=m.execution_mode?"자동 분류 · "+(routeNames[m.execution_mode]||m.execution_mode):m.mode==="AUTHORING_DRAFT"?"청구항 작성·수정":"";view.routeLabel.title=m.route_reason||"";}
    if(view.text!==m.text||view.status!==m.status){if(m.role==="user")view.body.textContent=m.text;else if(m.text)markdown(view.body,m.text);else view.body.replaceChildren(el("span","pending",m.mode==="CHAT"?"답변을 작성하고 있어요…":"에이전트가 자료를 검토하고 있어요…"));view.text=m.text;}
    if(view.logs){
      if(m.status==="running"){view.pre.textContent=data.live_log||"연결 중…";view.summary.textContent="작업 중 · 실시간 로그";if(view.status!=="running")view.logs.open=true;}
      else {view.summary.textContent=(m.status==="error"?"오류 확인 · ":m.status==="review"?"검토 필요 · ":"")+"작업 로그 펼치기";if(view.status==="running"){view.logs.open=false;view.pre.textContent="";}}
    }
    view.status=m.status;
  }
  renderRun(data);controls();if(follow)box.scrollTop=box.scrollHeight;
}
async function list(){const data=await api("/api/sessions");$("model").textContent=data.model;$("sessions").replaceChildren();for(const s of data.sessions){const b=el("button",s.id===state.id?"active":"",s.title);b.onclick=()=>open(s.id).catch(e=>error(e.message));$("sessions").append(b);}return data.sessions;}
async function open(id){
  if(state.busy||state.uploading)return;
  if(state.id)drafts.set(state.id,{text:$("prompt").value,files:state.pending,mode:$("mode").value});
  const version=++state.generation;const data=await api("/api/session?id="+id);if(version!==state.generation)return;
  state.id=id;state.nodes.clear();$("messages").replaceChildren();state.pending=drafts.get(id)?.files||[];
  $("prompt").value=drafts.get(id)?.text||localStorage.getItem("draft-"+id)||"";
  $("mode").value=drafts.get(id)?.mode||data.messages.filter(m=>m.role==="user").at(-1)?.mode||"CHAT";
  localStorage.setItem("claim-session",id);document.body.classList.remove("sidebar-open");error("");render(data);pending();resize();await list();$("conversation").scrollTop=$("conversation").scrollHeight;$("prompt").focus();
}
async function create(){if(state.busy||state.uploading)return;const data=await api("/api/new",{});await open(data.id);}
async function refresh(){const id=state.id,version=state.generation;if(!id)return;const data=await api("/api/session?id="+id);if(id===state.id&&version===state.generation){const wasRunning=state.data?.running;render(data);if(wasRunning&&!data.running)await list();}}
async function upload(files){
  if(!files.length)return;const id=state.id;if(!id)return;state.uploading++;controls();error("");
  try{for(const file of files){if(file.size>20*1024*1024)throw new Error(file.name+": 파일당 20MB까지 첨부할 수 있습니다.");const item=await api(`/api/upload?id=${id}&name=${encodeURIComponent(file.name||"붙여넣은 이미지.png")}`,file,true);if(state.id===id){state.pending.push(item);pending();}}}catch(e){error(e.message);}finally{state.uploading--;controls();}
}
async function send(event){
  event.preventDefault();if(state.composing||state.busy||state.uploading||state.data?.running||!$("prompt").value.trim())return;
  state.busy=true;controls();error("");const id=state.id,text=$("prompt").value;
  try{await api("/api/send",{id,text,mode:$("mode").value,files:state.pending.map(f=>f.id),dependent:$("dependent").checked,target:$("target").value});$("prompt").value="";localStorage.removeItem("draft-"+id);state.pending=[];drafts.delete(id);pending();resize();await refresh();await list();$("conversation").scrollTop=$("conversation").scrollHeight;}
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
$("mode").onchange=controls;$("stop").onclick=async()=>{try{await api("/api/stop",{id:state.id});await refresh();}catch(e){error(e.message);}};
$("shutdown").onclick=async()=>{if(state.data?.running&&!confirm("진행 중인 응답을 중지하고 프로그램을 종료할까요?"))return;try{await api("/api/shutdown",{});clearInterval(timer);$("composer").hidden=true;error("프로그램을 종료했습니다. 다시 이용하려면 바탕화면 실행기를 더블클릭하세요.");}catch(e){error(e.message);}};
for(const button of document.querySelectorAll("[data-prompt]"))button.onclick=()=>{$("prompt").value=button.dataset.prompt;resize();controls();$("prompt").focus();};
const timer=setInterval(()=>{if(!state.busy)refresh().catch(()=>error("프로그램에 연결할 수 없습니다. 실행기를 다시 더블클릭해 주세요."));},700);
(async()=>{try{const sessions=await list();const saved=localStorage.getItem("claim-session");if(sessions.length)await open(sessions.find(s=>s.id===saved)?.id||sessions[0].id);else await create();}catch(e){error(e.message);}})();

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
    $("resume-text").value="";state.pending=[];pending();await refresh();$("conversation").scrollTop=$("conversation").scrollHeight;}
  catch(e){error(e.message);}finally{state.busy=false;controls();}
};
