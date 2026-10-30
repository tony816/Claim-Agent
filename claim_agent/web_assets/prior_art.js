"use strict";
// 선행기술조사 페이지: 조사 시작·진행·후보 표·보고서. 조사는 서버의 claim_agent.prior_art.survey 하위 프로세스가 돌린다.
const $ = id => document.getElementById(id);
const S = {id:null, files:[], uploading:0, timer:null, options:null};
const TOOL_LABEL = {wips_search:"WIPS 검색", wips_read:"전문 읽기", rate:"선별 기록", caselaw_signals:"판례 그래프 · 유형 신호", caselaw_search:"판례 그래프 · 사례", oa_search:"OA 그래프"};
const STATUS_LABEL = {running:"조사 중", complete:"완료", error:"오류", stopped:"중지됨"};
const DEFAULT_COUNTRIES = ["KR", "US"];

function error(text){$("error").textContent=text||"";$("error").hidden=!text;}
async function api(path, body, raw=false){
  const options=body===undefined?{}:{method:"POST",headers:{"X-Claim-Request":"1",...(raw?{}:{"Content-Type":"application/json"})},body:raw?body:JSON.stringify(body)};
  const response=await fetch(path,options);const result=await response.json();
  if(!response.ok)throw new Error(result.error||"요청을 처리하지 못했습니다.");return result;
}
function when(t){const d=new Date(t*1000);return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;}

// ---- 목록
async function loadList(){
  const data=await api("/api/surveys");S.options=data;
  $("survey-model").textContent=`선행기술 검색 역할 · ${data.provider} · ${data.model}`;
  const warn=[];if(!data.wips)warn.push(".env에 WIPS_API_KEY가 없어 조사를 시작할 수 없습니다.");if(!data.connected)warn.push("모델 연결이 확인되지 않았습니다. 대화 화면의 에이전트 모델 설정에서 '선행기술 검색' 역할의 연결을 확인하세요.");
  $("survey-warn").textContent=warn.join(" ");$("survey-warn").hidden=!warn.length;
  if(!$("survey-country-boxes").childElementCount){
    for(const c of data.countries){const label=el("label","survey-country"),box=el("input");box.type="checkbox";box.value=c;box.checked=DEFAULT_COUNTRIES.includes(c);label.append(box,document.createTextNode(" "+c));$("survey-country-boxes").append(label);}
  }
  const nav=$("survey-list");nav.replaceChildren();
  for(const s of data.surveys){
    const b=el("button",s.id===S.id?"active":"");b.title=s.title;
    b.append(el("span","survey-dot "+s.status,"●"),document.createTextNode(" "+s.title+" "),el("small","survey-when",when(s.created)));
    b.onclick=()=>openSurvey(s.id);nav.append(b);
  }
  if(!data.surveys.length)nav.append(el("p","survey-empty","아직 조사 기록이 없습니다."));
}

// ---- 새 조사
function showForm(){S.id=null;stopPolling();$("survey-view").hidden=true;$("survey-form-panel").hidden=false;document.body.classList.remove("sidebar-open");loadList().catch(e=>error(e.message));$("survey-text").focus();}
function renderFiles(){
  const list=$("survey-file-list");list.replaceChildren();
  for(const f of S.files){const li=el("li");li.append(el("span","",f.name+" · "+Math.ceil(f.size/1024)+"KB"));const x=el("button","","✕");x.type="button";x.setAttribute("aria-label",f.name+" 빼기");x.onclick=()=>{S.files=S.files.filter(y=>y.id!==f.id);renderFiles();};li.append(x);list.append(li);}
  if(S.uploading)list.append(el("li","pending","올리는 중…"));
  $("survey-start").disabled=S.uploading>0;
}
async function upload(files){
  for(const file of files){
    S.uploading++;renderFiles();
    try{const item=await api("/api/survey/upload?name="+encodeURIComponent(file.name),file,true);S.files.push(item);error("");}
    catch(e){error(e.message);}
    finally{S.uploading--;renderFiles();}
  }
}
async function start(event){
  event.preventDefault();
  const countries=[...document.querySelectorAll("#survey-country-boxes input:checked")].map(x=>x.value);
  try{
    $("survey-start").disabled=true;
    const res=await api("/api/survey/start",{text:$("survey-text").value,files:S.files.map(f=>f.id),countries,cutoff:$("survey-cutoff").value,depth:$("survey-depth").value});
    $("survey-text").value="";S.files=[];renderFiles();error("");await openSurvey(res.id);
  }catch(e){error(e.message);}
  finally{$("survey-start").disabled=S.uploading>0;}
}

// ---- 조사 보기
function stopPolling(){if(S.timer){clearTimeout(S.timer);S.timer=null;}}
async function openSurvey(id){
  S.id=id;stopPolling();document.body.classList.remove("sidebar-open");
  $("survey-form-panel").hidden=true;$("survey-view").hidden=false;$("survey-report").replaceChildren();$("survey-progress").replaceChildren();
  await refresh();loadList().catch(()=>{});
}
async function refresh(){
  const id=S.id;if(!id)return;
  let data;
  try{data=await api("/api/survey?id="+id);}catch(e){error(e.message);return;}
  if(id!==S.id)return;
  render(data);
  if(data.running)S.timer=setTimeout(refresh,1500);else loadList().catch(()=>{});
}
function args(tool,a){
  if(!a)return "";
  if(tool==="wips_search")return "“"+(a.query||"")+"”"+(a.countries?" · "+a.countries.join(","):"");
  if(tool==="wips_read")return [a.ref,a.part,a.find?"찾기: "+a.find:"",a.start?"@"+a.start:""].filter(Boolean).join(" · ");
  if(tool==="rate")return (a.items||[]).map(i=>i.ref+" "+i.relevance).join(", ");
  if(tool==="caselaw_signals")return (a.type_code?a.type_code+" · ":"")+"“"+(a.feature||"")+"”";
  if(tool==="caselaw_search")return (a.set||"patentability")+" · “"+(a.text||"")+"”";
  if(tool==="oa_search")return (a.kind||"전체")+" · “"+(a.text||"")+"”";
  return JSON.stringify(a);
}
function render(data){
  const s=data.survey;
  $("survey-title").textContent=s.title;
  const status=data.running?"running":s.status;
  $("survey-status").textContent=STATUS_LABEL[status]||status;$("survey-status").className="survey-status "+status;
  $("survey-meta").textContent=[`조사 국가 ${s.countries.join(", ")}`,`기준일 ${s.cutoff||"미지정"}`,`깊이 ${s.depth}`,`시작 ${when(s.created)}`].join(" · ");
  $("survey-input-text").textContent=(s.text||"")+(s.files&&s.files.length?"\n\n[첨부] "+s.files.join(", "):"");
  $("survey-stop").hidden=!data.running;$("survey-delete").hidden=data.running;
  $("survey-export").hidden=!data.report;$("survey-export").href="/api/survey/export?id="+s.id;
  // 진행
  const list=$("survey-progress"),atBottom=list.scrollTop+list.clientHeight>=list.scrollHeight-20;list.replaceChildren();
  const P=data.progress;
  P.forEach((p,i)=>{
    if(p.kind==="think"){if(i===P.length-1&&data.running)list.append(el("li","think pending",p.closing?"보고서 쓰는 중…":`단계 ${p.step}/${p.total} · 다음 행동을 정하는 중…`));return;}
    if(p.kind==="step"){if(p.memo)list.append(el("li","memo",(p.final?"보고서 · ":`단계 ${p.step} · `)+p.memo));return;}
    if(p.kind==="call"){const li=el("li","call "+(p.tool.startsWith("wips")||p.tool==="rate"?"wips":"graph")+(p.ok?"":" failed"));li.append(el("strong","",TOOL_LABEL[p.tool]||p.tool),el("span","call-args"," "+args(p.tool,p.args)),el("span","call-sum"," → "+p.summary));list.append(li);return;}
    if(p.kind==="error"){list.append(el("li","failed","오류: "+p.text));return;}
    if(p.kind==="done")list.append(el("li","done","조사 완료"));
  });
  if(!data.running&&s.error)list.append(el("li","failed",s.error));
  if(!P.length&&data.running)list.append(el("li","think pending","시작하는 중…"));
  if(atBottom)list.scrollTop=list.scrollHeight;
  // 후보 표
  const t=$("survey-candidates");t.replaceChildren();const C=data.candidates;
  $("survey-count").textContent=C.length?`${C.length}건 · 전문 ${C.filter(c=>c.read&&c.read.length).length}건`:"";
  if(!C.length){t.append(el("caption","survey-empty","아직 찾은 문헌이 없습니다."));}
  else{
    const head=el("tr");for(const h of ["ref","번호","명칭","공개일","관련도","읽음","점수"])head.append(el("th","",h));t.append(head);
    for(const c of C){
      const r=el("tr","rel-"+(c.relevance||"none"));
      r.append(el("td","",c.ref),el("td","mono",c.number));
      const name=el("td","survey-name");name.append(el("span","",c.title||"(명칭 없음)"));if(c.reason)name.append(el("small","",c.reason));name.title=c.abstract||"";r.append(name);
      r.append(el("td","",(c.published||"-")+(c.after_cutoff?" ⚠":"")),el("td","",c.relevance||"-"),el("td","",(c.read||[]).map(x=>x==="claims"?"청구":"설명").join("·")||"-"),el("td","mono",String(c.score)));
      if(c.after_cutoff)r.title="기준일 이후 공개 — 선행기술 아님";
      t.append(r);
    }
  }
  // 보고서
  if(data.report)markdown($("survey-report"),data.report);
  else $("survey-report").replaceChildren(el("p","survey-empty",data.running?"조사가 끝나면 보고서가 여기에 나옵니다.":"보고서가 없습니다."));
}

// ---- 이벤트
$("survey-new").onclick=showForm;
$("survey-form").onsubmit=start;
$("survey-attach").onclick=()=>$("survey-picker").click();
$("survey-picker").onchange=e=>{upload([...e.target.files]);e.target.value="";};
$("survey-stop").onclick=async()=>{try{await api("/api/survey/stop",{id:S.id});}catch(e){error(e.message);}};
$("survey-delete").onclick=async()=>{if(!confirm("이 조사 기록을 삭제할까요?"))return;try{await api("/api/survey/delete",{id:S.id});showForm();}catch(e){error(e.message);}};
$("menu").onclick=()=>document.body.classList.toggle("sidebar-open");
document.addEventListener("dragover",e=>e.preventDefault());
document.addEventListener("drop",e=>{e.preventDefault();if(!$("survey-form-panel").hidden&&e.dataTransfer.files.length)upload([...e.dataTransfer.files]);});
loadList().catch(e=>error(e.message));
