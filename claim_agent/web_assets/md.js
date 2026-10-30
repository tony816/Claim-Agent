"use strict";
// 대화 페이지(app.js)와 선행기술조사 페이지(prior_art.js)가 함께 쓰는 DOM·마크다운 도우미. 모델·사용자 HTML은 넣지 않는다.
const el = (tag, cls, text) => {const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;};
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
