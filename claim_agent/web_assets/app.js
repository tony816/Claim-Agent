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
  for(const [id,title] of options){const option=el("option","",title);option.value=id;option.title=title;select.append(option);}
  select.value=value;select.onchange=()=>{change(select.value);settingsChanged();};wrap.append(select);return wrap;
}
// OpenRouter처럼 모델이 많은 목록은 검색 칸을 붙인다. 검색어(공백으로 나눈 낱말 모두)가 ID·이름·가격·컨텍스트에 들어 있는 모델만 남긴다.
const SEARCH_MIN=20;
function modelTitle(providers,kind,model){const label=providers[kind]?.labels?.[model];return label?`${model} — ${label}`:model;}
function modelSearch(select,choices,fixed,total,label,expand){
  const box=el("input"),count=el("small","model-search-count");box.type="search";box.className="model-search";
  box.placeholder="모델 검색 — 이름·제공사·가격 (예: claude opus, free, 1M)";box.setAttribute("aria-label",label+" 검색");
  const apply=()=>{
    const words=box.value.toLowerCase().split(/\s+/).filter(Boolean),current=select.value;
    const shown=choices.filter(([id,title])=>fixed.includes(id)||id===current||words.every(w=>(id+" "+title).toLowerCase().includes(w)));
    fillSelect(select,shown,current);
    const hits=shown.filter(([id])=>!fixed.includes(id)).length;
    count.textContent=words.length?`${total}개 중 ${hits}개`:`${total}개`;
    if(expand)select.size=words.length?Math.max(2,Math.min(8,shown.length)):0;
  };
  box.oninput=apply;
  box.onkeydown=e=>{    // Enter: 첫 검색 결과 선택, ↓: 결과 목록으로 이동
    const first=[...select.options].find(o=>!fixed.includes(o.value)&&o.value!==select.value)||[...select.options].find(o=>!fixed.includes(o.value));
    if(e.key==="Enter"){e.preventDefault();if(first){select.value=first.value;select.dispatchEvent(new Event("change"));}}
    else if(e.key==="ArrowDown"){e.preventDefault();select.focus();}
  };
  apply();return [box,count];
}
function modelPicker(kind,value,label,inherit,onchange){
  const models=modelSettings.providers[kind].models,choices=inherit?[["","기본 모델 따름 · "+modelSettings.defaults[kind]]]:[];
  for(const model of models)choices.push([model,model==="default"?"계정 기본 모델 (CLI 자동 선택)":modelTitle(modelSettings.providers,kind,model)]);
  choices.push(["__custom","모델 ID 직접 입력…"]);
  const custom=value!==null&&!models.includes(value), wrap=settingSelect(label,choices,custom?"__custom":value||"",v=>{
    input.hidden=v!=="__custom";input.required=v==="__custom";
    onchange(v==="__custom"?input.value:v||null);if(v==="__custom")input.focus();
  });
  if(models.length>SEARCH_MIN){const select=wrap.querySelector("select");select.before(...modelSearch(select,choices,["","__custom"],models.length,label,true));}
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
  if(modelSettings.busy)$("model-settings-saved").textContent="실행 중인 응답은 시작할 때의 설정 그대로 끝납니다. 저장하면 다음 요청부터 적용됩니다.";
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
    await loadProviderSwitch();
    $("model-settings-saved").textContent="저장됨 · 다음 요청부터 적용됩니다 (헤더에서 연결 방식·모델을 따로 고른 대화는 그 선택을 유지합니다)";await list();
  }catch(e){settingsError(e.message);}finally{$("model-settings-save").disabled=false;}
};
// 헤더의 연결 방식·모델 선택: 열린 대화 하나에만 기억되어 그 대화의 다음 요청부터 적용되고, 다른 대화와
// 모델 설정 패널의 기본값, 서브에이전트별 개별 지정은 그대로 둔다.
let providerInfo=null,providerFor=null;
function fillSelect(select,options,value){
  select.replaceChildren();
  for(const [id,title] of options){const option=el("option","",title);option.value=id;option.title=title;select.append(option);}
  select.value=value;
}
function providerLock(){    // 목록을 다시 만들지 않는 잠금 갱신: 열려 있는 선택 상자를 건드리지 않는다.
  const kinds=$("provider-kind"),models=$("provider-model");
  const running=!!state.data?.running,fixed=providerInfo?providerInfo.roles.filter(r=>r.provider||r.model).length:0;
  kinds.disabled=models.disabled=running||!providerInfo||!state.id;
  const search=document.querySelector(".provider-switch .model-search");if(search)search.disabled=models.disabled;
  const note=!providerInfo?"":!state.id?"대화를 열면 바꿀 수 있습니다":running?"응답이 끝난 뒤 바꿀 수 있습니다":fixed?`이 대화의 다음 요청부터 적용 · 서브에이전트 ${fixed}개는 개별 지정`:"이 대화의 다음 요청부터 적용됩니다";
  $("provider-note").textContent=note;kinds.title=models.title=note;
}
function renderProviderSwitch(){
  if(providerInfo){
    fillSelect($("provider-kind"),Object.entries(providerInfo.providers).map(([id,p])=>[id,p.label]),providerInfo.provider);
    const current=providerInfo.defaults[providerInfo.provider],names=providerInfo.providers[providerInfo.provider].models.slice();
    if(!names.includes(current))names.unshift(current);
    const choices=names.map(m=>[m,m==="default"?"계정 기본 모델":modelTitle(providerInfo.providers,providerInfo.provider,m)]);
    fillSelect($("provider-model"),choices,current);
    for(const node of document.querySelectorAll(".provider-switch .model-search,.provider-switch .model-search-count"))node.remove();
    if(names.length>SEARCH_MIN)$("provider-model").before(...modelSearch($("provider-model"),choices,[],names.length,"사용 모델",false));
  }
  providerLock();
}
async function loadProviderSwitch(){
  const id=state.id,data=await api("/api/model-settings"+(id?"?id="+encodeURIComponent(id):""));
  if(id===state.id){providerInfo=data;providerFor=id;renderProviderSwitch();}
}
async function switchProvider(body){
  const previous=providerInfo;
  try{
    providerInfo=await api("/api/provider",{...body,id:state.id});providerFor=state.id;renderProviderSwitch();
    const status=providerInfo.connection;
    error(status&&!status.connected?"연결 방식을 바꿨지만 아직 사용할 수 없습니다. "+status.message:"");
  }catch(e){providerInfo=previous;renderProviderSwitch();error(e.message);}
}
$("provider-kind").onchange=()=>switchProvider({provider:$("provider-kind").value});
$("provider-model").onchange=()=>switchProvider({provider:providerInfo.provider,model:$("provider-model").value});
const routeNames = {CHAT:"일반 대화",META:"설정·진행 확인",AUTHORING_DRAFT:"청구항 작성·수정",FINALIZATION:"출원용 최종 검증",REVIEW_ONLY:"특허 의견·제한 검수"};
// ---- 자유 작업 모드: 일반 에이전트와 1차 작업하고, 고른 메시지의 청구항에 고른 게이트 역할만 실행한다(REVIEW_ONLY, LOCK 없음).
// 작업 도구 패널의 검수 도구·피드백 멘트는 누르면 고르기만 하고, 입력창의 Enter가 덧붙인 말과 함께 실행한다.
const REVIEW_ROLES=[["claim-style-adjuster","문체 조정","스타일가이드·용어 출처 게이트로 범위를 바꾸지 않고 문언을 다듬어 냅니다 (함께 고른 검수는 다듬은 문언을 봅니다)"],["syntax-scope-reviewer","통사·범위","귀속·결속·복수 분배·범위 불변을 봅니다"],["oa-strategy-reviewer","OA","명확성·기능적 표현·뒷받침 등 OA 위험을 봅니다"],["claim-success-reviewer","성공조건","독립항 성공조건과 원자료 근거를 감사합니다"],["blind-claim-reconstruction-reviewer","블라인드 복원","발명을 모르는 기술자가 독립항 문언만 읽고 무엇을 그리는지 봅니다 (기준 비교 없음)"]];
const REVIEW_LABEL=Object.fromEntries(REVIEW_ROLES.map(([id,label])=>[id,label]));
// 판례 도구(두 작업 방식 모두): 모델 없이 판례 기록으로 답하고, Jev를 쓸 수 있으면 관련성을 확인한다. 답은 대화에 보이지만 에이전트가 읽는 기록에는 들어가지 않는다.
const CASELAW_TOOLS=[["enforce","권리행사 점검","대상 청구항에서 기능적 표현·전제부·수치 범위·여러 실시 주체 같은 권리행사 위험 문언을 찾고 비슷한 판례를 붙입니다"],["find","판례 찾기","입력창에 적은 지적·문언과 같은 종류의 결함을 법원이 판단한 판례를 찾습니다 (명확성·뒷받침·권리범위·권리행사·특허성)"],["features","특허성 신호","입력창에 적은 특징(한 줄에 하나)마다 법원의 신규성·진보성 판단 분포와 원자료 확인 질문을 봅니다"]];
const CASELAW_LABEL=Object.fromEntries(CASELAW_TOOLS.map(([id,label])=>[id,label]));
const CASELAW_PLACEHOLDER={enforce:"Enter → 대상 청구항을 권리행사 판례로 점검합니다 · 입력창의 말은 쓰지 않습니다",find:"지적이나 청구항 문언을 적고 Enter · 예: '대략 수평으로'는 기준면과 허용 각도가 없음",features:"특징을 한 줄에 하나씩 적고 Enter · 유형을 알면 '가열 온도 300~400℃::F-NUMERIC'처럼"};
const CLAIM_HEAD=/^[ \t]*(?:\*\*)?(?:【\s*청구항\s*\d+\s*】|\[\s*청구항\s*\d+\s*\])/m;
const tools={open:false,roles:new Set(),caselaw:null,target:"",options:null,phrases:[],editing:false};   // target "" = 가장 최근 청구항을 따라감
function freeMode(){return state.data?.work_mode==="free";}
async function setWorkMode(mode){
  if(!state.id||state.busy||state.data?.running||state.data?.work_mode===mode)return;
  try{await api("/api/work-mode",{id:state.id,mode});try{localStorage.setItem("claim-work-mode",mode);}catch(e){}tools.roles.clear();await refresh();await list();}catch(e){error(e.message);}
}
for(const button of document.querySelectorAll("#work-mode button"))button.onclick=()=>setWorkMode(button.dataset.mode);
function renderWorkMode(){
  const free=freeMode(),locked=!!state.data?.running||state.busy;
  for(const button of document.querySelectorAll("#work-mode button")){const on=button.dataset.mode===(free?"free":"gated");button.classList.toggle("is-on",on);button.setAttribute("aria-pressed",String(on));button.disabled=locked;}
  $("free-banner").hidden=!free;$("options").hidden=free;
}
function error(message){$("error").textContent=message;$("error").hidden=!message;}
async function api(path, body, raw=false){
  const options=body===undefined?{}:{method:"POST",headers:{"X-Claim-Request":"1",...(raw?{}:{"Content-Type":"application/json"})},body:raw?body:JSON.stringify(body)};
  const response=await fetch(path,options);const result=await response.json();
  if(!response.ok)throw new Error(result.error||"요청을 처리하지 못했습니다.");return result;
}
function resize(){const p=$("prompt");p.style.height="auto";p.style.height=Math.min(p.scrollHeight,180)+"px";}
function controls(){const running=!!state.data?.running,free=freeMode(),pick=reviewPick(),lookup=caselawPick(),typed=!!$("prompt").value.trim();providerLock();renderWorkMode();$("send").hidden=running;$("stop").hidden=!running;$("send").disabled=state.busy||state.uploading>0||(lookup?lookup.tool!=="enforce"&&!typed:(!typed&&!pick)||(!free&&!claimTarget().ok));$("attach").disabled=state.uploading>0;for(const button of document.querySelectorAll("#caselaw-buttons button"))button.disabled=running||state.busy||(button.dataset.tool==="enforce"&&!reviewTarget());$("prompt").placeholder=lookup?CASELAW_PLACEHOLDER[lookup.tool]:pick?"검수자에게 덧붙일 말이 있으면 적고 Enter · 없으면 그냥 Enter":free?"자유롭게 요청하세요 · 답변은 미검수 초안이며, 작업 도구에서 필요한 검수만 골라 실행합니다":state.data?.run_id?"질문하거나 수정하고 싶은 내용을 알려주세요":"무엇이든 물어보세요 · 청구항 작성은 발명을 설명하거나 자료를 첨부해 주세요";for(const button of document.querySelectorAll("#review-buttons button"))button.disabled=!free||running||state.busy||!reviewTarget();renderChip();}
// ---- 작성 대상: 미지정 / 독립항만 / 특정 항 1개 / 종속항 범위. 서버의 resolve_claim_target와 같은 정규형(2~4,6)을 미리 보여 준다.
const target={segments:[{from:2,to:8}]};
function formatTarget(numbers){const a=[...numbers].sort((x,y)=>x-y);if(!a.length)return "";const parts=[];let start=a[0],prev=a[0];for(const n of a.slice(1).concat([null])){if(n!==null&&n===prev+1){prev=n;continue;}parts.push(start===prev?String(start):start+"~"+prev);if(n!==null)start=prev=n;}return parts.join(",");}
function targetMode(){return document.querySelector("input[name=target-mode]:checked")?.value||"auto";}
function claimTarget(){
  const mode=targetMode();const out={target_mode:mode,target_claim:null,target_segments:[],dependent:false,target:null,ok:true,label:mode==="auto"?"미지정":"독립항만",error:""};
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
function updateTarget(){const t=claimTarget();$("target-preview").textContent=t.ok?(t.dependent?t.target:t.target_mode==="auto"?"요청 내용대로":"독립항만"):"—";$("target-preview").className=t.ok?"":"invalid";$("target-preview").title=t.error;$("options-summary").textContent="작성 옵션 · "+(t.ok?t.label:"입력 확인");controls();}
for(const radio of document.querySelectorAll("input[name=target-mode]"))radio.onchange=()=>{renderSegments();updateTarget();};
// 작성 대상은 미지정이 기본이다. 다른 대화로 옮기면 미지정으로 되돌리고 옵션 창을 닫는다.
function resetTarget(){const auto=document.querySelector("input[name=target-mode][value=auto]");if(auto)auto.checked=true;$("options").open=false;renderSegments();updateTarget();}
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
        revise.onclick=()=>{$("prompt").focus();controls();};actions.append(copy,revise);article.append(actions);
      }
      $("messages").append(article);view={article,body,logs,pre,summary,routeLabel,full,fullBody,text:null,status:null};state.nodes.set(m.id,view);
    }
    if(view.full)view.full.hidden=!(m.pipeline_run_id&&m.status&&m.status!=="running");
    if(view.logs)view.logs.hidden=!m.log_id;      // 작업 로그가 없는 답(판례 도구 등)에는 펼칠 로그가 없다
    if(view.routeLabel){view.routeLabel.textContent=m.mode==="CASELAW"?"판례 도구 · "+(CASELAW_LABEL[m.caselaw?.tool]||"")+" · 참고 — 대화 기록에 넣지 않음":m.mode==="FREE"?"자유 작업 · 미검수 초안":m.mode==="REVIEW"?(m.review?.revise?"검수 반영 · "+reviewLabels(m.review)+" · 미검수 초안":m.review?.asked?"검수 + 요청 · "+reviewLabels(m.review)+" · LOCK 없음":"선택 검수 · "+reviewLabels(m.review)+" · LOCK 없음"):m.execution_mode?"자동 분류 · "+(routeNames[m.execution_mode]||m.execution_mode):"";view.routeLabel.title=m.route_reason||"";}
    if(view.text!==m.text||view.status!==m.status){if(m.role==="user")view.body.textContent=m.text;else if(m.text)markdown(view.body,m.text);else view.body.replaceChildren(el("span","pending",m.mode==="FREE"?"답변을 준비하고 있어요…":m.mode==="REVIEW"?(m.review?.asked?"검수한 뒤 입력한 요청대로 처리하고 있어요…":"검수 역할을 실행하고 있어요…"):"요청을 분류하고 답변을 준비하고 있어요…"));view.text=m.text;}
    if(view.logs){
      if(m.status==="running"){view.pre.textContent=state.log.text||"연결 중…";view.summary.textContent="작업 중 · 실시간 로그";if(view.status!=="running")view.logs.open=true;}
      else {view.summary.textContent=(m.status==="error"?"오류 확인 · ":m.status==="review"?"검토 필요 · ":"")+"작업 로그 펼치기";if(view.status==="running"){view.logs.open=false;view.pre.textContent="";}}
    }
    view.status=m.status;
  }
  renderRun(data);renderTools();controls();if(follow)box.scrollTop=box.scrollHeight;
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
  const data=await api("/api/sessions");
  if(!providerInfo||providerFor!==state.id)await loadProviderSwitch();
  const names=new Map(data.projects.map(p=>[p.id,p.name]));
  if(state.project&&!names.has(state.project))state.project=null;
  const projects=$("projects");projects.replaceChildren();
  const all=el("button",!state.project&&state.view==="chat"?"active":"","전체 대화");all.onclick=()=>{state.project=null;localStorage.removeItem("claim-project");showChat();list().catch(e=>error(e.message));};projects.append(all);
  for(const p of data.projects){const b=el("button",p.id===state.project?"active":"");b.append(el("span","","▣ "+p.name),el("span","count",String(p.sessions)));b.title=p.name+" · 대화 "+p.sessions+"개 · 파일 "+p.files+"개";b.onclick=()=>openProject(p.id).catch(e=>error(e.message));projects.append(b);}
  const sessions=$("sessions");sessions.replaceChildren();
  const visible=data.sessions.filter(s=>!state.project||s.project_id===state.project);
  for(const s of visible){const row=el("div","session-row"+(s.id===state.id&&state.view==="chat"?" active":""));const b=el("button","session-open");b.append(document.createTextNode(s.title));if(s.work_mode==="free")b.append(el("span","tag tag-free","자유"));if(!state.project&&s.project_id&&names.has(s.project_id))b.append(el("span","tag",names.get(s.project_id)));b.onclick=()=>open(s.id).catch(e=>error(e.message));const del=el("button","session-delete","×");del.title="대화 삭제";del.setAttribute("aria-label",s.title+" 삭제");del.onclick=()=>deleteSession(s.id,s.title).catch(e=>error(e.message));row.append(b,del);sessions.append(row);}
  if(!visible.length)sessions.append(el("p","side-empty",state.project?"아직 대화가 없습니다.":"대화가 없습니다."));
  $("new-chat").textContent=state.project&&names.has(state.project)?"＋ 새 대화 · "+names.get(state.project):"＋ 새 대화";
  return data.sessions;
}
function showChat(){state.view="chat";$("project-panel").hidden=true;$("improve-panel").hidden=true;$("conversation").hidden=false;document.querySelector("main footer").hidden=false;renderTools();}
async function open(id){
  if(state.busy||state.uploading)return;
  if(state.id)drafts.set(state.id,{text:$("prompt").value,files:state.pending});
  const version=++state.generation;state.log={cursor:null, text:""};const data=await fetchSession(id,false);if(version!==state.generation)return;
  if(state.id!==id){resetTarget();tools.roles.clear();tools.target="";tools.options=null;}
  state.id=id;state.nodes.clear();$("messages").replaceChildren();state.pending=drafts.get(id)?.files||[];
  $("prompt").value=drafts.get(id)?.text||localStorage.getItem("draft-"+id)||"";
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
// 새 대화는 마지막으로 고른 작업 방식(게이트 자동 / 자유 작업)으로 시작한다.
function lastWorkMode(){try{return localStorage.getItem("claim-work-mode")==="free"?"free":"gated";}catch(e){return "gated";}}
async function create(projectId){if(state.busy||state.uploading)return;const data=await api("/api/new",{project_id:projectId===undefined?state.project:projectId,work_mode:lastWorkMode()});await open(data.id);}
async function refresh(){const id=state.id,version=state.generation;if(!id)return;const data=await fetchSession(id,true);if(id===state.id&&version===state.generation){const wasRunning=state.data?.running;render(data);if(wasRunning&&!data.running)await list();}}
async function upload(files){
  if(state.view==="project")return projectUpload(files);
  if(!files.length)return;const id=state.id;if(!id)return;state.uploading++;controls();error("");
  try{for(const file of files){if(file.size>20*1024*1024)throw new Error(file.name+": 파일당 20MB까지 첨부할 수 있습니다.");const item=await api(`/api/upload?id=${id}&name=${encodeURIComponent(file.name||"붙여넣은 이미지.png")}`,file,true);if(state.id===id){state.pending.push(item);pending();}}}catch(e){error(e.message);}finally{state.uploading--;controls();}
}
async function send(event){
  // 작업 도구에서 검수 역할을 골라 두었으면 Enter가 그 검수를 실행하고, 입력한 말은 검수자에게 추가 요청으로 간다(비어 있어도 실행).
  // 판례 도구를 골라 두었으면 Enter가 그 조회를 실행한다. 판례 찾기·특허성 신호는 입력한 말이 조회 문장이고, 권리행사 점검은 대상 청구항을 본다.
  const pick=reviewPick(),lookup=caselawPick(),typed=!!$("prompt").value.trim();
  event.preventDefault();if(state.composing||state.busy||state.uploading||state.data?.running||(lookup?lookup.tool!=="enforce"&&!typed:!pick&&!typed))return;
  if(pick&&state.pending.length){error("검수 실행에는 첨부 파일을 함께 보낼 수 없습니다. 첨부를 빼거나 고른 검수를 취소하세요.");return;}
  if(lookup&&state.pending.length){error("판례 도구에는 첨부 파일을 함께 보낼 수 없습니다. 첨부를 빼거나 고른 판례 도구를 취소하세요.");return;}
  const free=freeMode(),t=claimTarget();if(!free&&!lookup&&!t.ok){error(t.error);return;}
  state.busy=true;controls();error("");const id=state.id,text=$("prompt").value;
  // 자유 작업에는 작성 대상·선행기술 검색 옵션이 없다(일반 에이전트가 요청 문장대로 답한다).
  const [path,body]=lookup?["/api/caselaw",{id,tool:lookup.tool,message:lookup.target?.m.id,text:lookup.tool==="enforce"?"":text}]:pick?["/api/review",{id,message:pick.m.id,roles:[...tools.roles],text}]:["/api/send",free?{id,text,files:state.pending.map(f=>f.id)}:{id,text,files:state.pending.map(f=>f.id),target_mode:t.target_mode,target_claim:t.target_claim,target_segments:t.target_segments,dependent:t.dependent,target:t.target,prior_search:$("prior-search").checked}];
  const keep=lookup?.tool==="enforce";        // 권리행사 점검은 입력창의 말을 보내지 않으므로 지우지 않는다
  try{await api(path,body);if(pick)tools.roles.clear();if(lookup)tools.caselaw=null;if(!keep){$("prompt").value="";localStorage.removeItem("draft-"+id);drafts.delete(id);}state.pending=[];pending();resize();await refresh();restartPoll();await list();$("conversation").scrollTop=$("conversation").scrollHeight;}
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
$("stop").onclick=async()=>{try{await api("/api/stop",{id:state.id});await refresh();}catch(e){error(e.message);}};
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
  $("conversation").hidden=true;document.querySelector("main footer").hidden=true;$("improve-panel").hidden=true;$("project-panel").hidden=false;$("project-saved").textContent="";
  document.body.classList.remove("sidebar-open");error("");renderTools();renderProject(p);await list();
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

// ---- 작업 상태 패널: 단계 스텝퍼 · 게이트 표 · 중지 사유 · 내보내기 · 리비전 대조 (수정·결정은 입력창으로)
const STAGES_ROOT=["ARCHITECT","DRAFT","STYLE","SUCCESS","SYNTAX","OA","BLIND","PICTURE","LOCK"],STAGES_DEP=["DEP_ARCHITECT","DEP_DRAFT","DEP_STYLE","DEP_SUCCESS","DEP_SYNTAX","DEP_OA","DEP_RECON","DEP_LOCK"];
const STAGE_LABEL={ARCHITECT:"설계",DRAFT:"의미 초안",STYLE:"문체",SUCCESS:"성공조건",SYNTAX:"통사",OA:"OA",BLIND:"블라인드",PICTURE:"기준 비교",LOCK:"독립항 LOCK",DEP_ARCHITECT:"종속 설계",DEP_DRAFT:"종속 초안",DEP_STYLE:"종속 문체",DEP_SUCCESS:"종속 성공조건",DEP_SYNTAX:"종속 통사",DEP_OA:"종속 OA",DEP_RECON:"종속 역구성",DEP_LOCK:"종속 LOCK"};
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
    if(run.halt.open_issues&&run.halt.open_issues.length){const ul=el("ul");for(const o of run.halt.open_issues)ul.append(el("li","",`[${o.kind||""}] ${o.code||""} ${o.text||""}`));halt.append(ul);}
    halt.append(el("p","run-chat-hint","이 작업은 결정을 기다립니다. 결정이나 수정할 내용을 입력창에 보내면 같은 작업이 이어집니다. 명세서·선행기술이 없어 남은 미검증을 그대로 받아들이려면 '미검증 수용'이라고 보내세요."));}
  else halt.hidden=true;
  const table=$("run-gate-table");table.replaceChildren();
  const head=el("tr");for(const h of ["단계","역할","상태","게이트","비고"]){head.append(el("th","",h));}table.append(head);
  for(const r of run.stages||[]){const tr=el("tr");tr.className=r.superseded||r.stale?"superseded":"";for(const v of [r.stage,r.role,r.status,Object.entries(r.gates||{}).map(([k,v])=>k+": "+v).join(", "),(r.superseded?"superseded ":"")+(r.stale?"STALE":"")])tr.append(el("td","",v||""));table.append(tr);}
  const base=`/api/export?id=${encodeURIComponent(data.id)}`;
  $("export-docx").href=base+"&format=docx";$("export-md").href=base+"&format=md";$("export-evidence").href=base+"&format=docx&evidence=1";
  const pc=$("prior-candidates");pc.hidden=!run.prior_art;
  if(run.prior_art)pc.textContent=`선행기술 후보 (선별 ${run.prior_art.screening||"-"}, 기준일 ${run.prior_art.base_date}) — 대비할 문헌의 skey를 쉼표로, 기준일을 YYYY-MM-DD로 입력창에 적어 보내세요 (고르지 않으려면 none)\n`+run.prior_art.candidates.map(c=>`${c.sealed?"●":"○"} ${c.skey}  ${c.number}  ${c.title}  ${c.published||"-"}  Jev ${c.p_screen==null?"-":c.p_screen.toFixed(2)}  ${c.relevance||""}`).join("\n");
  const key=data.id+"|"+(run.outcome||"")+"|"+(run.revision||"")+"|"+(run.dependent&&run.dependent.draft_set_lock||"");
  if(key!==runKey){runKey=key;$("run-diff").open=false;$("run-diff-body").textContent="";}
}
$("run-diff").ontoggle=async()=>{if($("run-diff").open&&!$("run-diff-body").textContent){try{const r=await api(`/api/diff?id=${encodeURIComponent(state.id)}`);$("run-diff-body").textContent=r.text;}catch(e){$("run-diff-body").textContent=e.message;}}};

// ---- 개선 / Approval Inbox (ACE): 자동 제안은 전부 승인 대기. 승인해도 회귀 평가 전에는 주입되지 않는다.
const GATE_CLASS={PENDING:"gate-pending",CANDIDATE_APPROVED:"gate-wait",ACTIVE_APPROVED:"gate-active",APPROVED_BUT_FAILED_EVAL:"gate-failed",HELD:"gate-held"};
const METRIC_ROWS=[
  ["escaped_to_lock_rate","Escaped-to-Lock Rate","LOCK까지 간 run 중 뒤에서 오류가 확인된 비율 — 최상위 품질 지표",true],
  ["failure_detection_rate","Failure Detection Rate","파이프라인이 스스로 잡은 실패 비율",false],
  ["first_correct_detector_rate","First Correct Detector","최초 탐지 지점이 기대 지점과 일치한 비율",false],
  ["false_block_rate","False Block Rate","문언 변경 없이 뒤집힌 비-PASS 판정 비율",false],
  ["regression_rate","Regression Rate","직전 eval에서 PASS였다가 최신에서 FAIL이 된 케이스",false],
  ["lesson_hit_rate","Lesson Hit Rate","활성 교훈 중 같은 failure mode가 재발하지 않은 비율",false],
];
function pct(v){return v===null||v===undefined?"—":Math.round(v*100)+"%";}
function renderImproveMetrics(m){
  const box=$("improve-metrics");box.replaceChildren();
  for(const [key,label,note,top] of METRIC_ROWS){
    const card=el("div","metric-card"+(top?" metric-top":""));
    card.append(el("span","metric-label",label),el("strong","metric-value",pct(m[key])),el("span","metric-note",note));
    card.title=note;box.append(card);
  }
  const stage=el("div","metric-card");
  stage.append(el("span","metric-label","Mean Detection Stage"),el("strong","metric-value",m.mean_detection_stage===null||m.mean_detection_stage===undefined?"—":String(m.mean_detection_stage)),
    el("span","metric-note",(m.mean_detection_stage_label||"-")+" · 작을수록 이른 단계에서 잡힌다"));
  box.append(stage);
}
function lessonCard(card){
  const node=el("div","improve-card");
  const head=el("div","improve-card-head");
  head.append(el("strong","",card.id),el("span","gate-chip "+(GATE_CLASS[card.gate_status]||""),card.gate_label));
  if(card.llm_drafted)head.append(el("span","tag","LLM 초안"));
  if(card.source)head.append(el("span","tag",card.source));
  node.append(head);
  const text=el("textarea","improve-text");text.rows=3;text.value=card.text_ko;text.setAttribute("aria-label",card.id+" 교훈 문구");node.append(text);
  const roles=el("input","improve-roles");roles.value=(card.target_roles||[]).join(",");roles.placeholder="대상 역할 (비우면 전체 역할)";roles.setAttribute("aria-label",card.id+" 대상 역할");node.append(roles);
  const meta=el("div","improve-meta");
  meta.append(el("span","","대상 역할 "+card.impact.role_count+"개"),el("span","","failure mode "+(card.failure_mode_id||"-")),
              el("span","","근거 run "+((card.evidence||[]).slice(0,4).join(", ")||"-")));
  if((card.similar||[]).length)meta.append(el("span","warn","유사·중복 교훈: "+card.similar.join(", ")));
  meta.append(el("span","","helpful "+card.helpful+" / harmful "+card.harmful));
  node.append(meta);
  const refl=card.reflection||{};
  if(refl.what_failed||refl.why_missed){
    const d=el("details","improve-reflect");d.append(el("summary","","Reflector 요약"));
    if(refl.what_failed)d.append(el("p","","무엇이 실패했나: "+refl.what_failed));
    if(refl.why_missed)d.append(el("p","","왜 놓쳤나: "+refl.why_missed));
    if(refl.first_detector)d.append(el("p","","기대 최초 탐지: "+[refl.first_detector.stage,refl.first_detector.role,refl.first_detector.gate].filter(Boolean).join(" / ")));
    if(card.rationale)d.append(el("p","","근거: "+card.rationale));
    node.append(d);
  }
  if(card.eval_record&&card.eval_record.verdict){
    const d=el("details","improve-reflect");d.append(el("summary","","회귀 평가 결과 · "+card.eval_record.verdict));
    for(const c of card.eval_record.criteria||[])d.append(el("p","",c.name+": baseline "+c.baseline+" → candidate "+c.candidate+" — "+c.verdict+(c.note?" ("+c.note+")":"")));
    if(card.eval_record.note)d.append(el("p","warn",card.eval_record.note));
    node.append(d);
  }
  const actions=el("div","improve-actions");
  const decide=async(action)=>{
    const body={lesson_id:card.id,action};
    if(action==="edit_approve"){body.text_ko=text.value.trim();body.target_roles=roles.value.split(",").map(s=>s.trim()).filter(Boolean);}
    if(action==="reject"||action==="hold"){const note=prompt(action==="reject"?"거절 사유 (선택)":"보류 사유 (선택)","");if(note===null)return;body.note=note||null;}
    try{await api("/api/improve/decide",body);await loadImprove();}catch(e){error(e.message);}
  };
  const approve=el("button","primary","승인");approve.type="button";approve.onclick=()=>decide("approve");
  const edit=el("button","","수정 후 승인");edit.type="button";edit.onclick=()=>decide("edit_approve");
  const hold=el("button","","보류");hold.type="button";hold.onclick=()=>decide("hold");
  const reject=el("button","danger","거절");reject.type="button";reject.onclick=()=>decide("reject");
  if(card.gate_status==="PENDING"||card.gate_status==="HELD"||card.gate_status==="APPROVED_BUT_FAILED_EVAL")actions.append(approve,edit,hold,reject);
  else actions.append(reject);
  if(card.gate_status==="CANDIDATE_APPROVED"||card.gate_status==="APPROVED_BUT_FAILED_EVAL"){
    const job=((state.improve||{}).jobs||{})[card.id];
    const regress=el("button","primary",job&&!job.done?"회귀 평가 실행 중…":"회귀 평가 실행");
    regress.type="button";regress.disabled=!!(job&&!job.done);
    regress.onclick=async()=>{try{await api("/api/improve/regress",{lesson_id:card.id});await loadImprove();}catch(e){error(e.message);}};
    actions.append(regress);
    if(job&&job.done&&job.error)actions.append(el("span","warn",job.error));
    if(job&&job.done&&job.verdict)actions.append(el("span","","판정 "+job.verdict));
  }
  node.append(actions);
  if(card.gate_status==="CANDIDATE_APPROVED")node.append(el("p","improve-hint","사람이 승인했지만 아직 주입되지 않습니다. 회귀 평가를 통과해야 활성화됩니다."));
  if(card.gate_status==="APPROVED_BUT_FAILED_EVAL")node.append(el("p","improve-hint warn",
    (card.eval_record||{}).verdict==="UNVERIFIED"
      ?"회귀 평가가 판정하지 못했습니다(대개 목표 적대 케이스가 없거나 리플레이 모드). 케이스를 승인한 뒤 live 모드로 다시 실행하세요. 현재 주입되지 않습니다."
      :"회귀 평가에서 실패했습니다. 문구를 고쳐 다시 승인하거나 거절하세요. 현재 주입되지 않습니다."));
  return node;
}
function caseCard(card){
  const node=el("div","improve-card");
  const head=el("div","improve-card-head");
  head.append(el("strong","",card.case_id),el("span","gate-chip "+(card.status==="approved"?"gate-active":card.status==="rejected"?"gate-failed":"gate-pending"),
    card.status==="approved"?"regression set에 편입됨":card.status==="rejected"?"거절됨":"승인 대기"));
  node.append(head);
  const meta=el("div","improve-meta");
  meta.append(el("span","","seed 케이스 "+card.seed_case),el("span","","변형 "+card.mutation_type),
              el("span","","기대 최초 탐지 "+card.expected_first_detector_label),
              el("span","","통과하면 안 되는 게이트 "+((card.must_not_pass_gates||[]).join(", ")||"-")),
              el("span","","expected_return_to "+(card.expected_return_to||"-")),
              el("span","","escaped_to_lock_must_be "+(card.escaped_to_lock_must_be?"true":"false")));
  node.append(meta);
  node.append(el("p","improve-defect","주입한 결함: "+card.injected_defect));
  if(card.failure){
    const d=el("details","improve-reflect");d.append(el("summary","","근거 실패 "+card.failure.failure_id+" ("+card.failure.failure_type+")"));
    d.append(el("p","",card.failure.root_cause_summary),el("p","","근거 run: "+(card.failure.source_run_ids||[]).join(", ")));
    node.append(d);
  }
  if(card.status==="pending"){
    const actions=el("div","improve-actions");
    const decide=async(action,patch)=>{try{await api("/api/improve/decide",{case_id:card.case_id,action,patch:patch||null});await loadImprove();}catch(e){error(e.message);}};
    const approve=el("button","primary","Regression Set에 승인");approve.type="button";
    approve.onclick=()=>{if(confirm(card.case_id+" 케이스를 eval/cases/로 편입할까요? 이후 모든 회귀 평가가 이 케이스를 함께 돌립니다."))decide("approve");};
    const edit=el("button","","수정 후 승인");edit.type="button";
    edit.onclick=()=>{const gates=prompt("통과하면 안 되는 게이트 (쉼표)",(card.must_not_pass_gates||[]).join(","));if(gates===null)return;
      const ret=prompt("expected_return_to",card.expected_return_to||"");if(ret===null)return;
      decide("edit_approve",{must_not_pass_gates:gates.split(",").map(s=>s.trim()).filter(Boolean),expected_return_to:ret||null});};
    const reject=el("button","danger","거절");reject.type="button";
    reject.onclick=()=>{const note=prompt("거절 사유 (선택)","");if(note===null)return;decide("reject",null);};
    actions.append(approve,edit,reject);node.append(actions);
  }
  return node;
}
function renderImprove(data){
  state.improve=data;
  renderImproveMetrics(data.metrics||{});
  const c=data.counts||{};
  $("improve-count-lessons").textContent=String((data.lessons||[]).length);
  $("improve-count-cases").textContent=String((data.cases||[]).filter(x=>x.status==="pending").length);
  const badge=$("improve-badge"),waiting=(c.pending_lessons||0)+(c.pending_cases||0);
  badge.textContent=String(waiting);badge.hidden=!waiting;
  const lessons=$("improve-lessons");lessons.replaceChildren();
  for(const card of data.lessons||[])lessons.append(lessonCard(card));
  if(!(data.lessons||[]).length)lessons.append(el("p","improve-empty","승인 대기 교훈이 없습니다. ‘실패 수집 · 회고 실행’을 눌러 최근 실행에서 실패 후보를 모아 보세요."));
  if((data.active_lessons||[]).length){
    lessons.append(el("h3","improve-subhead","활성 교훈 (후속 run에 주입됨)"));
    for(const card of data.active_lessons)lessons.append(lessonCard(card));
  }
  const cases=$("improve-cases");cases.replaceChildren();
  for(const card of data.cases||[])cases.append(caseCard(card));
  if(!(data.cases||[]).length)cases.append(el("p","improve-empty","적대 평가 케이스 초안이 없습니다."));
  const audit=$("improve-audit");audit.replaceChildren();
  for(const row of [...(data.audit||[])].reverse()){
    const line=el("div","audit-row");
    line.append(el("span","audit-ts",row.ts),el("span","audit-action",row.action),el("span","audit-actor",row.actor||""),
                el("span","",[row.lesson_id,row.case_id,row.failure_id,row.verdict].filter(Boolean).join(" · ")));
    audit.append(line);
  }
  if(!(data.audit||[]).length)audit.append(el("p","improve-empty","기록된 승인·거절 이력이 없습니다."));
}
async function loadImprove(){const data=await api("/api/improve");renderImprove(data);return data;}
async function openImprove(){
  if(state.busy||state.uploading)return;
  state.view="improve";renderTools();
  $("conversation").hidden=true;document.querySelector("main footer").hidden=true;$("project-panel").hidden=true;$("improve-panel").hidden=false;
  document.body.classList.remove("sidebar-open");error("");
  try{await loadImprove();}catch(e){error(e.message);}
}
$("improve-open").onclick=()=>openImprove();
$("improve-refresh").onclick=()=>loadImprove().catch(e=>error(e.message));
$("improve-mine").onclick=async()=>{
  const button=$("improve-mine");button.disabled=true;button.textContent="분석 중…";
  try{const out=await api("/api/improve/mine",{});
    await loadImprove();error("");
    if(!out.lessons.length&&!out.cases.length)error("새로 제안할 항목이 없습니다. 실패 기록 "+out.created.length+"건 신규, "+out.reinforced.length+"건 근거 보강.");
  }catch(e){error(e.message);}
  finally{button.disabled=false;button.textContent="실패 수집 · 회고 실행";}
};
for(const tab of document.querySelectorAll(".improve-tab"))tab.onclick=()=>{
  for(const other of document.querySelectorAll(".improve-tab"))other.classList.toggle("is-on",other===tab);
  for(const name of ["lessons","cases","audit"])$("improve-"+name).hidden=name!==tab.dataset.tab;
};
// ---- 작업 도구 패널 (오른쪽): 대상 청구항 + 검수 도구 + 판례 도구 + 피드백 멘트. 누르면 고르기만 하고, 실행은 입력창의 Enter다.
function reviewLabels(review){return ((review?.roles)||[review?.role]).map(r=>REVIEW_LABEL[r]).filter(Boolean).join(" · ");}
// 대상 후보: 【청구항 N】 문언이 있는 메시지, 최근 것부터. 검수 반영으로 고친 답은 넣고, 문언을 인용만 한 예전 검수 보고서와 판례 도구 답은 뺀다.
function reviewCandidates(){
  const fixed=m=>m.role==="assistant"&&m.review?.revise;
  const out=[];(state.data?.messages||[]).forEach((m,i)=>{if(m.status!=="running"&&m.mode!=="CASELAW"&&(m.mode!=="REVIEW"||fixed(m))&&CLAIM_HEAD.test(m.text||""))out.push({m,n:i+1});});
  return out.reverse();
}
function reviewTarget(){const c=reviewCandidates();return tools.target?c.find(x=>x.m.id===tools.target)||null:c[0]||null;}
function reviewPick(){return tools.roles.size&&freeMode()?reviewTarget():null;}
// 고른 판례 도구. 권리행사 점검은 대상 청구항이 있어야 고를 수 있다.
function caselawPick(){const tool=tools.caselaw;if(!tool)return null;const target=tool==="enforce"?reviewTarget():null;return tool==="enforce"&&!target?null:{tool,target};}
// 서버의 review_target와 같은 규칙: 첫 머리줄부터 마지막 청구항 끝(제목·※·구분선·코드 울타리 앞)까지.
function claimBlock(text){
  const t=(text||"").replace(/\r\n/g,"\n").replace(/\*\*(【\s*청구항\s*\d+\s*】|\[\s*청구항\s*\d+\s*\])\*\*/g,"$1");
  const heads=[...t.matchAll(/^[ \t]*(?:【\s*청구항\s*\d+\s*】|\[\s*청구항\s*\d+\s*\])/gm)];if(!heads.length)return "";
  const last=heads.at(-1),from=last.index+last[0].length,end=t.slice(from).search(/\n[ \t]*(?:#|※|---|\*\*\*|```)|\n[ \t]*\n[ \t]*\n/);
  return t.slice(heads[0].index,end<0?t.length:from+end).trim();
}
// 문체 조정 답은 다듬은 문언을 따로 들고 있다(보고서 사이에 있어 본문에서 잘라 내지 않는다).
function claimOf(m){return m.review?.revise&&m.review?.claim_text||claimBlock(m.text);}
function candidateLabel({m,n}){const first=(claimOf(m).split("\n").find(l=>l.trim()&&!CLAIM_HEAD.test(l))||"").trim();return (m.role==="user"?"내 메시지":"에이전트 답변")+" #"+n+" · "+(first.length>24?first.slice(0,24)+"…":first);}
function renderTools(){
  const visible=tools.open&&state.view==="chat",free=freeMode(),cands=reviewCandidates();
  $("tool-panel").hidden=!visible;document.body.classList.toggle("tools-open",visible);$("tool-toggle").setAttribute("aria-expanded",String(visible));$("tool-toggle").classList.toggle("is-on",visible);
  if(tools.target&&!cands.some(x=>x.m.id===tools.target))tools.target="";
  const select=$("review-target"),key=cands.map(x=>x.m.id+":"+x.n).join(",");
  if(key!==tools.options){tools.options=key;select.replaceChildren();const auto=el("option","",cands.length?"가장 최근 청구항 — "+candidateLabel(cands[0]):"청구항이 있는 메시지 없음");auto.value="";select.append(auto);for(const c of cands){const o=el("option","",candidateLabel(c));o.value=c.m.id;select.append(o);}}
  select.value=tools.target;select.disabled=!cands.length;
  const target=reviewTarget();if(!target||!free)tools.roles.clear();if(!target&&tools.caselaw==="enforce")tools.caselaw=null;
  const preview=$("review-preview");preview.hidden=!target;if(target){const block=claimOf(target.m);preview.textContent=block.length>320?block.slice(0,320)+"…":block;}
  $("review-note").textContent=!free?"검수 도구는 자유 작업 모드에서 씁니다. 위쪽에서 자유 작업으로 바꾸세요.":!target?"아직 【청구항 N】 문언이 있는 메시지가 없습니다. 초안을 받거나 청구항을 붙여 넣으면 고를 수 있습니다.":"고른 메시지의 청구항 블록에 고른 검수를 실행합니다. 입력창이 비어 있으면 검수 결과만, 요청을 적으면 그 방향으로 검수한 뒤 요청대로 처리합니다. 여러 개를 함께 고를 수 있고, 결과는 LOCK 근거가 아닙니다.";
  for(const button of $("review-buttons").children){const on=tools.roles.has(button.dataset.role);button.classList.toggle("is-on",on);button.setAttribute("aria-pressed",String(on));}
  for(const button of $("caselaw-buttons").children){const on=tools.caselaw===button.dataset.tool;button.classList.toggle("is-on",on);button.setAttribute("aria-pressed",String(on));}
  const lookup=caselawPick(),marked=lookup?lookup.target:free&&target;   // 대상 청구항을 쓰는 도구가 있을 때만 대화에서 표시한다
  for(const [id,view] of state.nodes)view.article.classList.toggle("review-target",visible&&!!marked&&id===marked.m.id);
  const chip=$("tool-chip");chip.replaceChildren();chip.hidden=!(lookup||(target&&tools.roles.size));
  if(!chip.hidden){
    const cancel=el("button","","×");cancel.type="button";cancel.title=lookup?"고른 판례 도구 취소":"고른 검수 취소";cancel.setAttribute("aria-label",cancel.title);cancel.onclick=()=>{tools.roles.clear();tools.caselaw=null;renderTools();controls();$("prompt").focus();};
    const what=lookup?"판례 도구 · "+CASELAW_LABEL[lookup.tool]+(lookup.target?" → "+candidateLabel(lookup.target):""):reviewLabels({roles:REVIEW_ROLES.map(([r])=>r).filter(r=>tools.roles.has(r))})+" → "+candidateLabel(target);
    chip.append(el("span","tool-chip-label",""),el("span","",what),cancel,el("span","tool-chip-hint",""));
  }
  renderChip();
}
// 입력창이 비어 있으면 검수만, 적혀 있으면 그 요청 방향으로 검수한 뒤 요청대로 처리(고쳐 달라면 고치고, 아니면 검수 결과로 답함).
// 판례 도구는 참고 조회다: 찾기·특허성 신호는 입력한 말로 찾고, 권리행사 점검은 대상 청구항만 본다.
const CASELAW_HINT={enforce:()=>"Enter → 대상 청구항의 권리행사 위험 문언과 비슷한 판례를 봅니다 · 입력창의 말은 쓰지 않으며, 답은 대화 기록에 넣지 않습니다",
  find:asked=>asked?"Enter → 적은 지적과 같은 종류의 결함을 법원이 판단한 판례를 찾습니다 · 답은 대화 기록에 넣지 않습니다":"입력창에 지적이나 청구항 문언을 적어야 찾을 수 있습니다",
  features:asked=>asked?"Enter → 특징마다 신규성·진보성 판단 분포와 원자료 확인 질문을 봅니다 · 판정이 아닙니다":"입력창에 특징을 한 줄에 하나씩 적어야 볼 수 있습니다"};
function renderChip(){
  const chip=$("tool-chip");if(chip.hidden||!chip.firstChild)return;const asked=!!$("prompt").value.trim(),lookup=caselawPick();
  if(lookup){chip.querySelector(".tool-chip-label").textContent="판례 참고";chip.querySelector(".tool-chip-hint").textContent=CASELAW_HINT[lookup.tool](asked);return;}
  chip.querySelector(".tool-chip-label").textContent=asked?"검수 + 내 요청":"검수만";
  chip.querySelector(".tool-chip-hint").textContent=asked?"Enter → 입력한 요청 방향으로 검수한 뒤 요청대로 처리합니다 (고쳐 달라면 고친 청구항, 아니면 검수 결과로 답)":"Enter → 검수 결과만 받습니다 · 요청을 적으면 그 방향으로 검수하고 요청대로 처리합니다 (예: \"명확성 위주로 봐 줘\", \"지적대로 고쳐 줘\")";
}
function setTools(open){tools.open=open;try{localStorage.setItem("claim-tools",open?"1":"0");}catch(e){}renderTools();}
for(const [role,label,title] of REVIEW_ROLES){
  const button=el("button","review-tool");button.type="button";button.dataset.role=role;button.setAttribute("aria-pressed","false");button.setAttribute("aria-label",label+" — "+title);button.append(el("strong","",label),el("span","",title));
  button.onclick=()=>{if(tools.roles.has(role))tools.roles.delete(role);else tools.roles.add(role);tools.caselaw=null;renderTools();controls();$("prompt").focus();};
  $("review-buttons").append(button);
}
// 판례 도구는 하나만 고른다. 고르면 고른 검수는 풀린다(Enter 하나가 한 가지를 실행한다).
for(const [tool,label,title] of CASELAW_TOOLS){
  const button=el("button","review-tool");button.type="button";button.dataset.tool=tool;button.setAttribute("aria-pressed","false");button.setAttribute("aria-label",label+" — "+title);button.append(el("strong","",label),el("span","",title));
  button.onclick=()=>{tools.caselaw=tools.caselaw===tool?null:tool;tools.roles.clear();renderTools();controls();$("prompt").focus();};
  $("caselaw-buttons").append(button);
}
$("review-target").onchange=()=>{tools.target=$("review-target").value;renderTools();controls();const t=reviewTarget();if(t)state.nodes.get(t.m.id)?.article.scrollIntoView({block:"center",behavior:"smooth"});};
$("tool-toggle").onclick=()=>setTools(!tools.open);$("tool-close").onclick=()=>setTools(false);
// 피드백 멘트: 누르면 입력창 커서 자리에 넣는다(보내지 않음). 목록은 이 컴퓨터에 저장되고, 저장된 목록이 없으면 기본 목록이다.
function insertPhrase(text){
  const p=$("prompt"),start=p.selectionStart??p.value.length,end=p.selectionEnd??p.value.length,before=p.value.slice(0,start),after=p.value.slice(end);
  const lead=before&&!/\s$/.test(before)?"\n":"",tail=after&&!/^\s/.test(after)?"\n":"";
  p.value=before+lead+text+tail+after;const caret=(before+lead+text).length;p.focus();p.setSelectionRange(caret,caret);
  resize();controls();if(state.id)localStorage.setItem("draft-"+state.id,p.value);
}
function renderPhrases(){
  const box=$("phrase-list"),groups=new Map();box.replaceChildren();
  for(const p of tools.phrases){if(!groups.has(p.group))groups.set(p.group,[]);groups.get(p.group).push(p);}
  for(const [group,items] of groups){
    box.append(el("p","phrase-group",group));
    for(const p of items){const row=el("div","phrase-row"),button=el("button","phrase",p.text);button.type="button";button.title="입력창에 넣기 (보내지 않음)";button.onclick=()=>insertPhrase(p.text);row.append(button);
      if(tools.editing){const del=el("button","phrase-delete","×");del.type="button";del.setAttribute("aria-label","멘트 삭제: "+p.text);del.onclick=()=>savePhrases(tools.phrases.filter(x=>x.id!==p.id));row.append(del);}
      box.append(row);}
  }
  if(!tools.phrases.length)box.append(el("p","tool-note","저장된 멘트가 없습니다. 편집에서 추가하세요."));
  const list=$("phrase-groups");list.replaceChildren();for(const g of groups.keys()){const o=el("option");o.value=g;list.append(o);}
  $("phrase-form").hidden=!tools.editing;$("phrase-edit").textContent=tools.editing?"완료":"편집";$("phrase-edit").setAttribute("aria-pressed",String(tools.editing));
}
async function loadPhrases(){try{tools.phrases=(await api("/api/phrases")).phrases;renderPhrases();}catch(e){error(e.message);}}
async function savePhrases(list){try{tools.phrases=(await api("/api/phrases",{phrases:list})).phrases;renderPhrases();return true;}catch(e){error(e.message);return false;}}
$("phrase-edit").onclick=()=>{tools.editing=!tools.editing;renderPhrases();if(tools.editing)$("phrase-text").focus();};
$("phrase-form").onsubmit=async event=>{event.preventDefault();const text=$("phrase-text").value.trim();if(!text)return;if(await savePhrases([...tools.phrases,{group:$("phrase-group").value.trim(),text}]))$("phrase-text").value="";};
$("phrase-reset").onclick=async()=>{if(!confirm("피드백 멘트를 기본 목록으로 되돌릴까요? 추가하거나 지운 내용은 사라집니다."))return;try{tools.phrases=(await api("/api/phrases/reset",{})).phrases;renderPhrases();}catch(e){error(e.message);}};
try{const saved=localStorage.getItem("claim-tools");tools.open=saved===null?window.innerWidth>=1200:saved==="1";}catch(e){tools.open=window.innerWidth>=1200;}
renderTools();loadPhrases();
