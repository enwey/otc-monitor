'use strict';
const $=s=>document.querySelector(s);
const phaseNames={preview:'新币弱预警',pullback:'回落观察',early:'早期异动',confirmed:'动能确认',revival:'老币复苏',watch:'观察中',warming:'累积资料',unavailable:'资料不足',delayed:'资料延迟',risk:'风险排除',faded:'动能消退',expired:'追踪结束',pool_changed:'交易池变更'};
let data=null,view='live',selected=null,visible=[],refreshing=false;
phaseNames.rule_changed='旧规则已结束';phaseNames.inactive='低活跃';phaseNames.curve='曲线市场异动';
let paused=false,replayFrames=[],replayIndex=null,composing=false;
const stableOrders=new Map(),changes=new Map();
const el=(tag,value,cls)=>{const e=document.createElement(tag);if(value!=null)e.textContent=value;if(cls)e.className=cls;return e};
const fmt=(n,d=2)=>n==null||!Number.isFinite(Number(n))?'—':Number(n).toLocaleString('en-US',{maximumFractionDigits:d});
const usd=n=>n==null?'—':'$'+fmt(n,n<.01?8:2);
const pct=n=>n==null?'—':(n>0?'+':'')+fmt(n)+'%';
const date=n=>n?new Date(n*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'—';
function badge(phase){return el('span',phaseNames[phase]||phase,'badge '+phase)}
function xLinks(value){
 if(typeof value!=='string'||!value.trim())return null;
 let raw=value.trim();if(/^@?[A-Za-z0-9_]{1,15}$/.test(raw))raw='https://x.com/'+raw.replace(/^@/,'');
 if(/^(www\.|mobile\.)?(x\.com|twitter\.com)\//i.test(raw))raw='https://'+raw;
 try{const u=new URL(raw);if(!['https:','http:'].includes(u.protocol)||!['x.com','www.x.com','mobile.x.com','twitter.com','www.twitter.com','mobile.twitter.com'].includes(u.hostname)||u.username||u.password||u.port)return null;
 const path=u.pathname,first=path.split('/')[1];if(!first)return null;
 const account=/^[A-Za-z0-9_]{1,15}$/.test(first)&&!['i','intent','search','home','share','explore','hashtag','settings'].includes(first.toLowerCase())?first:null;
 return {url:'https://x.com'+path+u.search+u.hash,account};}catch{return null}
}
async function loadSocial(mint){
 const box=$('#social-links');box.replaceChildren(el('span','正在获取 OTC 的 X 链接…'));
 try{const response=await fetch('/api/social?mint='+encodeURIComponent(mint));if(!response.ok)throw Error();const result=await response.json();if(selected?.mint!==mint)return;
 const links=xLinks(result.twitter);box.replaceChildren();if(!links){box.append(el('span',result.twitter?'OTC 的 X 链接格式无效':'OTC 未提供 X 链接'));return}
 const link=el('a',links.url);link.href=links.url;link.target='_blank';link.rel='noopener noreferrer';box.append(el('span','X 原始链接：'),link);
 if(links.account){const account=el('a','账号 @'+links.account);account.href='https://x.com/'+links.account;account.target='_blank';account.rel='noopener noreferrer';box.append(account)}
 box.append(el('small','来源：OTC 项目资料，账号归属与叙事未经核验'));
 }catch{if(selected?.mint===mint)box.replaceChildren(el('span','X 链接获取失败'),Object.assign(el('button','重试','plain'),{onclick:()=>loadSocial(mint)}))}
}
function setText(node,value){value=String(value??'');if(node.textContent!==value)node.textContent=value}
function liquidityText(m){return m.liquidity!=null?usd(m.liquidity):m.referenceLiquidity!=null?usd(m.referenceLiquidity)+'（OTC参考）':'未提供'}
function trendState(r){
 const e=r.evidence,parts=e.scoreParts||e.checks||[],core=['相对放量','市值抬升','持有人增长','流动性稳定'];
 const status=Object.fromEntries(parts.map(p=>[p.label,p.ok]));
 const missing=core.filter(k=>status[k]!==true),unknown=core.filter(k=>status[k]==null);
 const result=(key,title,reason,next)=>({key,title,reason,next});
 if(r.closed)return result('ended','历史信号已结束','这是结束时的观测，不代表当前趋势。','查看后续观测，不作为当前信号');
 if(r.phase==='risk')return result('risk','风险排除',e.reasons?.[0]||'未通过风险检查','等待风险条件解除后重新判断');
 if(r.phase==='inactive')return result('quiet','当前低活跃',e.reasons?.[0]||'未观察到足够成交活动','等待成交活动恢复');
 if(e.historyTrend&&!['unavailable','delayed','warming'].includes(r.phase)){
  const h=e.historyTrend,flags=Object.fromEntries(h.items.map(i=>[i.key,i.ok]));
  if(r.phase==='pullback')return result('pullback','趋势回落观察','此前满足规则，当前动能回落。','观察是否重新放量及风险变化');
  if(['early','revival','confirmed'].includes(r.phase)&&flags.sustained===true&&['structure','retest','multi'].some(k=>flags[k]===true))return result(r.phase==='confirmed'?'confirmed':'early',r.phase==='confirmed'?'趋势条件持续满足':r.phase==='revival'?'老币复苏迹象':'持续放量与结构成立','持续放量与走势结构同步成立，持有人增长作为辅助证据。','规则信号不等于上涨概率，关注后续失效');
  if(r.phase==='preview')return result('near','基础异动，持续性待核实','基础异动成立，较长历史尚不足。','等待两个 5 分钟放量窗口及历史基准');
  if(flags.sustained===false)return result('watch','放量持续性不足','未通过双窗口持续放量或单分钟集中度检查。','查看历史指标实测值');
  if(flags.sustained==null)return result('unknown','持续趋势待核实','持续放量所需历史不完整或存在缺口。','查看详情中的缺失原因；持有人增长仅作辅助');
  return result('near','持续放量，结构待配合','持续放量成立，但走势结构或流动性条件尚未全部满足。','查看阶梯抬升、突破回踩、多周期及流动性条件');
 }
 if(['unavailable','delayed','warming'].includes(r.phase)||unknown.length)return result('unknown','启动趋势待核实',e.reasons?.[0]||'关键证据不完整',unknown.length?'待补齐：'+unknown.join('、'):'等待来源恢复并重新计算');
 if(r.phase==='pullback')return result('pullback','启动后回落观察','此前形成动能，目前未持续满足全部条件。','等待再次放量抬升；回落扩大则失效');
 if(r.phase==='confirmed'&&!missing.length)return result('confirmed','启动动能已确认','四项核心条件成立，且后续新收线再次确认。','继续关注放量持续性及流动性，确认不保证上涨');
 if(!missing.length)return result('early',r.phase==='preview'?'短历史启动迹象':'已出现启动迹象','相对放量、走势抬升、持有人增长及流动性稳定同时成立。',r.phase==='preview'?'等待足够历史与后续新收线确认':'等待后续新收线再次确认');
 if(status['相对放量']===true&&status['市值抬升']===true)return result('near','量价异动，配合不足','放量与走势抬升已成立，但还不是完整启动信号。','尚缺：'+missing.join('、'));
 return result('watch','暂未形成启动趋势',status['市值抬升']===true?'走势有所抬升，但尚未形成同步启动证据。':status['相对放量']===true?'成交量放大，但走势尚未同步抬升。':'目前没有同时出现放量和走势抬升。','尚缺：'+missing.join('、'));
}
function scoreState(r){
 const e=r.evidence,parts=e.scoreParts||e.checks?.map(c=>({...c,points:20}))||[];
 const blocked=['unavailable','delayed','warming','risk','inactive'].includes(r.phase)||!parts.length;
 const partial=blocked||parts.some(p=>p.ok==null),known=parts.filter(p=>p.ok!=null).length;
 const points=parts.reduce((sum,p)=>sum+(p.ok===true?p.points:0),0);
 return {parts,title:r.phase==='risk'?'风险排除':r.phase==='inactive'?'低活跃':!known?'等待证据':partial?`已验证 ${points} 分 · 部分证据`:`${e.score} 分`,note:partial?(e.reasons?.[0]||'证据不完整，不构成完整评分'):'',partial};
}
function renderScore(target,r){
 const trend=trendState(r),interpretation=el('div',null,'trend-reading');interpretation.append(el('div',trend.reason),el('small',trend.next));
 const state=scoreState(r),heading=el('div',null,'score-heading');heading.append(el('b',state.title),el('small',state.parts.length?'基础证据分 · 非上涨概率':''));
 const tags=el('div',null,'evidence-tags');for(const p of state.parts){const unknown=p.ok==null;const tag=el('span',`${unknown?'未知':p.ok?'已满足':'未满足'} · ${p.label}${p.ok?' +'+p.points:''}`,'evidence-tag '+(unknown?'unknown':p.ok?'met':'pending'));tag.title=unknown?(p.reason||'缺少有效数据'):p.ok?`${p.label}：计入 ${p.points} 分`:`${p.label}：本次未满足，不加分`;tags.append(tag);if(unknown&&p.reason){const reason=el('small',p.label+'：'+p.reason,'score-unknown-reason');tags.append(reason)}}
 if(state.note)tags.append(el('small',state.note,'score-note'));
 const history=el('div',null,'history-summary');
 if(r.evidence.historyTrend){const h=r.evidence.historyTrend;history.append(el('b',`历史指标 ${h.items.filter(i=>i.ok===true).length} 项满足 · ${h.known}/${h.total} 项可判断`));
 if(target.id==='detail-evidence'){
 const list=el('div',null,'history-indicators');
 for(const i of h.items){const row=el('div',null,'history-indicator');row.append(el('b',i.label),el('span',i.ok==null?'待核实':i.ok?'满足':'未满足','evidence-tag '+(i.ok==null?'unknown':i.ok?'met':'pending')),el('div',i.value),el('small',i.rule),el('small',i.reason||''));list.append(row)}
 history.append(el('small',`本地归档 ${h.coverage.bars} 根 · ${date(h.coverage.first)} 至 ${date(h.coverage.last)}`),list);
 }else{history.append(el('small',h.items.filter(i=>i.ok===true).map(i=>i.label).join('、')||'暂无历史条件同时支持'))}
 }
 stableChildren(target,interpretation,heading,tags,history);
}
function reconcileRows(items){
 const body=$('#rows'),existing=new Map([...body.querySelectorAll('tr[data-key]')].map(n=>[n.dataset.key,n])),wanted=new Set();
 if(items.length)for(const n of body.querySelectorAll('tr:not([data-key])'))n.remove();
 items.forEach((r,index)=>{const key=r.id?'event:'+r.id:'mint:'+r.mint;wanted.add(key);let tr=existing.get(key);
 if(!tr){tr=el('tr');tr.dataset.key=key;for(let i=0;i<12;i++)tr.append(el('td'));const button=el('button',null,'token');button.type='button';button.append(el('b'),el('small'));button.addEventListener('click',()=>openDetail(tr.record));tr.children[0].append(button,el('small',null,'pinned-label'),el('small',null,'change-label'));tr.children[1].append(el('span',null,'badge'),el('small',null,'data-quality'));const canvas=el('canvas',null,'spark');canvas.setAttribute('aria-label','完整分钟市值走势');tr.children[8].append(canvas);tr.children[11].className='reason';}
 tr.record=r;const e=r.evidence,m=e.metrics,cells=tr.children,button=cells[0].querySelector('button');button.title=r.name+' · '+r.mint;setText(button.children[0],r.symbol||r.name);setText(button.children[1],r.name);setText(cells[0].querySelector('.pinned-label'),data.favoriteMints.includes(r.mint)?'已关注':'');const change=changes.get(r.id||r.mint);setText(cells[0].querySelector('.change-label'),change&&Date.now()-change.at<600000?change.label:'');const b=cells[1].children[0];const trend=trendState(r);b.className='badge trend-'+trend.key;setText(b,trend.title);setText(cells[1].children[1],(phaseNames[r.phase]||r.phase)+' · '+(e.quality||'部分资料'));
 const values={3:pct(m.gain5m),4:fmt(m.volume5m),5:fmt(m.holdersAdded,0),6:m.candleRatio==null?'—':fmt(m.candleRatio)+'×',7:liquidityText(m),9:date(r.opened),10:pct(r.since),11:e.reasons[0]||'—'};for(const [i,value] of Object.entries(values))setText(cells[i],value);cells[2].className='score';renderScore(cells[2],r);cells[3].className=m.gain5m==null?'':m.gain5m>=0?'positive':'negative';cells[10].className=r.since==null?'':r.since>=0?'positive':'negative';cells[7].title=m.liquidity==null&&m.referenceLiquidity!=null?'OTC 平台参考值，不是 DEX 同池深度；时间 '+date(m.referenceAt):'OTC 平台流动性';
 if(body.children[index]!==tr)body.insertBefore(tr,body.children[index]||null);draw(cells[8].firstChild,e.bars||[]);
 });
 for(const [key,node] of existing)if(!wanted.has(key))node.remove();
 if(!items.length){const title=view==='live'?'暂无符合条件的启动讯号':'目前没有符合筛选条件的纪录';if(!body.querySelector('.empty')||body.querySelector('.empty b')?.textContent!==title){const tr=el('tr'),td=el('td',null,'empty');td.colSpan=12;td.append(el('b',title),el('span',view==='live'?'等待 OTC 相对量、持有人与完整分钟走势形成同步证据。':'可调整类型或搜寻条件。'));tr.append(td);body.replaceChildren(tr)}}
}
function metric(label,value){const n=el('div');n.append(el('span',label),el('b',value));return n}
function stableChildren(node,...children){const signature=children.map(n=>n.outerHTML).join('');if(node.innerHTML!==signature)node.replaceChildren(...children)}
function draw(canvas,bars,large=false){
 const colors=getComputedStyle(document.documentElement),muted=colors.getPropertyValue('--muted').trim(),line=colors.getPropertyValue('--line').trim(),green=colors.getPropertyValue('--green').trim(),red=colors.getPropertyValue('--red').trim();const box=canvas.getBoundingClientRect(),w=box.width||100,h=box.height||34,dpr=window.devicePixelRatio||1;
 const signature=JSON.stringify([bars,w,h,dpr,large?selected?.opened:null]);if(canvas.dataset.signature===signature)return;canvas.dataset.signature=signature;
 canvas.width=w*dpr;canvas.height=h*dpr;const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
 if(bars.length<2){if(large){ctx.fillStyle=muted;ctx.font='13px system-ui';ctx.fillText('尚无足够的完整分钟 K 线',18,80)}return}
 const pad=large?{l:65,r:18,t:16,b:30}:{l:2,r:2,t:3,b:3},lo=Math.min(...bars.map(b=>b.close)),hi=Math.max(...bars.map(b=>b.close)),range=hi-lo||hi*.01;
 const t0=bars[0].time,t1=bars[bars.length-1].time;
 const x=b=>pad.l+(b.time-t0)/(t1-t0)*(w-pad.l-pad.r),y=b=>h-pad.b-(b.close-lo)/range*(h-pad.t-pad.b);
 if(large){ctx.font='10px system-ui';for(let i=0;i<4;i++){let yy=pad.t+i*(h-pad.t-pad.b)/3;ctx.strokeStyle=line;ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();ctx.fillStyle=muted;ctx.fillText(fmt(hi-i*range/3,0),2,yy+3)}ctx.fillText(new Date(t0*1000).toLocaleTimeString(),pad.l,h-6);ctx.fillText(new Date(t1*1000).toLocaleTimeString(),Math.max(pad.l,w-90),h-6)}
 ctx.strokeStyle=bars.at(-1).close>=bars[0].close?green:red;ctx.lineWidth=large?2:1.7;ctx.beginPath();bars.forEach((b,i)=>{if(!i||b.time-bars[i-1].time>90)ctx.moveTo(x(b),y(b));else ctx.lineTo(x(b),y(b))});ctx.stroke();
 if(large&&selected&&selected.id&&selected.opened>=t0&&selected.opened<=t1){const xx=x({time:selected.opened});ctx.strokeStyle=green;ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(xx,pad.t);ctx.lineTo(xx,h-pad.b);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=green;ctx.fillText('首次发现',Math.min(xx+4,w-66),pad.t+12)}
}
function filtered(){
 if(view==='quality')return [];
 const q=$('#search').value.toLowerCase(),kind=$('#kind').value;
 let items=(data?.[view]||[]).filter(r=>`${r.symbol} ${r.name} ${r.mint}`.toLowerCase().includes(q)&&(!kind||(kind.startsWith('trend:')?trendState(r).key===kind.slice(6):r.phase===kind)));
 const sort=$('#sort').value;
 if(sort==='stable'){let order=stableOrders.get(view)||[];if(!order.length)items.sort((a,b)=>b.evidence.score-a.evidence.score);for(const r of items){const key=r.id||r.mint;if(!order.includes(key))order.push(key)}stableOrders.set(view,order);return items.sort((a,b)=>order.indexOf(a.id||a.mint)-order.indexOf(b.id||b.mint))}
 return items.sort((a,b)=>sort==='newest'?b.opened-a.opened:(b.evidence.score-a.evidence.score)||b.opened-a.opened);
}
function renderPaths(){
 if(view!=='quality')return;
 const target=$('#quality-view');target.append(el('h2','信号后完整分钟市值路径'),el('p','最近 200 个 OTC 事件，按规则版本分组。从信号后的首个完整分钟开始；缺一根即记缺测。非正变化不是已验证误报，以下统计不是交易收益。'));
 const groups=data.quality.paths?.groups||[];
 if(!groups.length){target.append(el('p','暂无可统计事件'));return}
 const table=el('table'),head=el('tr');
 for(const label of ['版本','窗口','总数 / 完整 / 缺测 / 待到期','期末变化中位数','正变化比例','非正变化数','最大上行中位数','最大下行中位数','收盘回撤中位数'])head.append(el('th',label));table.append(head);
 for(const g of groups){const row=el('tr');for(const value of [g.version,g.minutes+' 分钟',`${g.total} / ${g.observed} / ${g.missing} / ${g.pending}`,pct(g.change),g.positive==null?'—':fmt(g.positive*100)+'%',g.nonpositive,pct(g.maximum),pct(g.minimum),pct(g.drawdown)])row.append(el('td',value));table.append(row)}
 const wrap=el('div',null,'quality-table');wrap.append(table);target.append(wrap);
}
function render(){
 if(composing)return;
 if(!data)return;const s=data.state.radar||{},stale=!s.at||data.now-s.at>120;
 const affected=data.watch.filter(r=>['unavailable','delayed'].includes(r.phase)).length;
 $('#health').classList.toggle('bad',stale||!!s.errors?.length||affected>0);stableChildren($('#health'),el('strong',stale?'采集延迟':s.errors?.length||affected?'部分来源异常或延迟':'采集中'),el('span','最近完成 '+date(s.at)),el('span',`本轮 ${s.requests??0} 次请求 · ${s.seconds??0} 秒`),el('span',`观察池 ${data.watch.length}/${s.limit||90} · 来源受影响 ${affected}`));
 const errors=[...(s.errors||[])];if(data.state.error)errors.push(data.state.error);if(data.state.discoveryGap)errors.push('新币增量超出采集预算，可能漏项');$('#errors').textContent=errors.join('；');$('#errors').classList.toggle('hidden',!errors.length);
 for(const p of ['early','confirmed','revival'])$('#count-'+p).textContent=data.live.filter(x=>x.phase===p).length;
 $('#count-watch').textContent=data.watch.length;
 $('#quality-view').classList.toggle('hidden',view!=='quality');$('.table-wrap').classList.toggle('hidden',view==='quality');$('.controls').classList.toggle('hidden',view==='quality');$('#export').classList.toggle('hidden',view==='quality');renderQuality();
 renderPaths();
 $('#preview-count').textContent=`弱预警 ${data.live.filter(r=>r.phase==='preview').length} · 回落观察 ${data.live.filter(r=>r.phase==='pullback').length}`;
 visible=filtered();reconcileRows(visible);
 $('#row-count').textContent=view==='quality'?`${data.quality.events} 个已保存讯号 · 不含未保存的旧事件`:`${visible.length} 笔 · ${view==='archive'?'截至结束时的最后观测':'30 秒采集目标，10 秒更新画面'}`;
 $('#last-source').textContent='新币 '+date(data.state.radar_new_at)+' · 活跃榜 '+date(data.state.radar_board_at);
 const near=data.watch.filter(x=>trendState(x).key==='near'&&!data.live.some(l=>l.mint===x.mint));$('#near').textContent=near.length?`异动待配合 ${near.length} 个：`+near.slice(0,3).map(r=>`${r.symbol}：${trendState(r).next}`).join('；'):'目前没有接近完整趋势条件的候选';
 if(selected&&$('#detail').open&&replayIndex===null){const r=[...data.live,...data.archive,...data.watch].find(x=>selected.id?x.id===selected.id:x.mint===selected.mint);if(r)fillDetail(r)}
}
function fillDetail(r){renderScore($('#detail-evidence'),r);selected=r;const e=r.evidence,m=e.metrics;setText($('#detail-title'),r.symbol+' · '+r.name);setText($('#address'),r.mint);stableChildren($('#detail-state'),el('span',trendState(r).title,'badge trend-'+trendState(r).key));stableChildren($('#detail-metrics'),metric('目前观测价格',usd(m.price)),metric('首次讯号价格',usd(r.initial_price)),metric('发现后变化',pct(r.since)),metric('5 分钟市值涨幅',pct(m.gain5m)),metric('5 分钟量（原始单位）',fmt(m.volume5m)),metric('持有人增量 / 基准分钟',`${fmt(m.holdersAdded,0)} / ${fmt(m.holderWindow,1)}`),metric('3 分钟相对量',m.candleRatio==null?'—':fmt(m.candleRatio)+'×'),metric('流动性',liquidityText(m)));
 $('#reasons').replaceChildren(...e.reasons.map(x=>el('li',x)));$('#risks').replaceChildren(...e.risks.map(x=>el('li',x)));
 if(e.missing?.length)$('#reasons').append(el('li','基础证据未满足（不等于趋势硬门槛）：'+e.missing.join('、')));
 if(e.cohort){const c=e.cohort;$('#reasons').append(el('li',`${c.label} · 本地同组 ${c.size} 个 · 成交额百分位 ${c.percentile}% · ${c.applied?'套用分组活跃门槛 $'+fmt(c.minimumVolume):'样本不足 8 个，沿用固定底线'}`))}
 $('#favorite-toggle').textContent=data.favoriteMints.includes(r.mint)?'取消关注':'加入关注';$('#favorite-toggle').disabled=!data.watch.some(x=>x.mint===r.mint)&&!data.favoriteMints.includes(r.mint);
 $('#source-times').textContent=`OTC 快照：${date(m.snapshotAt)} · 接收：${date(m.observedAt)} · 完整 K 线截至：${date(m.chartAt)} · 首次发现：${date(r.opened)} · 规则 ${e.version||'radar-1.0'}`;
 $('#otc-link').href='https://otcdesks.cash/coin/'+encodeURIComponent(r.mint);draw($('#chart'),e.bars||[],true);
 if($('#social-links').dataset.mint!==r.mint){$('#social-links').dataset.mint=r.mint;loadSocial(r.mint)}
}
async function openDetail(r){replayIndex=null;replayFrames=[];$('#replay-panel').classList.add('hidden');$('#detail').showModal();fillDetail(r);if(!r.id)return;try{const response=await fetch('/api/replay?id='+r.id);if(!response.ok)throw Error('回放载入失败');const result=await response.json();if(selected?.id!==r.id)return;replayFrames=result.frames;$('#replay-panel').classList.remove('hidden');$('#replay-slider').max=Math.max(0,replayFrames.length-1);$('#replay-slider').value=Math.max(0,replayFrames.length-1);$('#replay-slider').disabled=!replayFrames.length;$('#replay-time').textContent=replayFrames.length?`${replayFrames.length} 个已保存时点 · 最新状态`:'此讯号未保存当时证据，不能重建回放';$('#outcomes').textContent=result.outcomes.map(o=>`${o.horizon/60} 分钟：${o.status==='observed'?pct(o.change):o.status==='pending'?'待到期':'缺测'}`).join(' · ')}catch(e){$('#action-status').textContent=e.message}}
async function refresh(){if(refreshing)return;refreshing=true;try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);const next=await r.json();if(data){const old=new Map([...data.watch,...data.live,...data.archive].map(x=>[x.id||x.mint,x]));for(const x of [...next.watch,...next.live,...next.archive]){const key=x.id||x.mint,p=old.get(key);if(!p||p.phase!==x.phase)changes.set(key,{at:Date.now(),label:!p?'新出现':`${phaseNames[p.phase]} → ${phaseNames[x.phase]}`})}}data=next;render()}catch(e){$('#health').classList.add('bad');$('#health').replaceChildren(el('strong','服务未连接'),el('span',e.message));$('#errors').textContent='画面可能是旧资料，请勿视为即时讯号';$('#errors').classList.remove('hidden')}finally{refreshing=false}}
function renderQuality(){const target=$('#quality-view');target.replaceChildren(el('h2','讯号后续观测'),el('p',`已保存 ${data.quality.events} 个讯号。包含消退讯号；缺测不算成功或失败。变化未扣滑点与费用，不是交易收益。`));const table=el('table'),head=el('tr');for(const s of ['规则 / 首次类型','观测窗口','总讯号','已观测','待到期','缺测','变化中位数','正变化比例'])head.append(el('th',s));table.append(head);for(const g of data.quality.groups)for(const h of g.horizons){const tr=el('tr');for(const s of [g.version+' / '+(phaseNames[g.phase]||g.phase),h.horizon/60+' 分钟',h.total,h.observed,h.pending,h.missing,pct(h.median),h.positive==null?'—':fmt(h.positive*100,1)+'%'])tr.append(el('td',s));table.append(tr)}if(!data.quality.groups.length)target.append(el('p','尚未有可统计的讯号，不填入示例绩效。'));else{const wrap=el('div',null,'quality-table');wrap.append(table);target.append(wrap)}}
function mountUpgrade(){
 for(const [key,label] of [['confirmed','启动动能已确认'],['early','已出现启动迹象'],['near','量价异动，配合不足'],['watch','暂未形成启动趋势'],['unknown','启动趋势待核实']]){const option=el('option',label);option.value='trend:'+key;$('#kind').append(option)}
 const evidencePanel=el('section');evidencePanel.id='detail-evidence';evidencePanel.setAttribute('aria-label','证据评分明细');$('#detail-metrics').after(evidencePanel);
 const social=el('div',null,'dialog-links');social.id='social-links';social.style.cssText='display:flex;flex-wrap:wrap;gap:8px;overflow-wrap:anywhere;margin:12px 0';social.setAttribute('aria-live','polite');$('#address').after(social);
 for(const key of ['inactive']){const option=el('option',phaseNames[key]);option.value=key;$('#kind').append(option)}
 $('#search').addEventListener('compositionstart',()=>{composing=true});$('#search').addEventListener('compositionend',()=>{composing=false;render()});
 for(const p of document.querySelectorAll('details p'))if(p.textContent.includes('至少 15 根连续'))p.textContent=p.textContent.replace('至少 15 根连续且新鲜的分钟 K 线才判断。','正式讯号需 15 根连续完整分钟 K 线；首小时新币有 6 根可产生弱预警，不能直接确认。');
 const tabs=$('.tabs');for(const [key,label] of [['favorites','我的关注'],['quality','品质统计']]){const b=el('button',label);b.dataset.view=key;b.setAttribute('role','tab');b.setAttribute('aria-selected','false');tabs.append(b)}
 const option=el('option','稳定排序');option.value='stable';$('#sort').prepend(option);$('#sort').value='stable';for(const key of ['preview','pullback']){const o=el('option',phaseNames[key]);o.value=key;$('#kind').append(o)}
 const bar=el('div',null,'review-bar'),label=el('label'),pause=el('input');pause.type='checkbox';pause.id='pause';label.append(pause,document.createTextNode('暂停画面更新'));const read=el('button','清除变化标记','plain');read.addEventListener('click',()=>{changes.clear();render()});const hint=el('span');hint.id='preview-count';bar.append(label,read,hint);$('.toolbar').before(bar);const near=el('div',null,'near');near.id='near';bar.after(near);pause.addEventListener('change',()=>{paused=pause.checked;$('#pause-notice').classList.toggle('hidden',!paused);if(!paused)refresh()});const notice=el('div','画面更新已暂停，数值停留在上次接收时点；后端仍持续采集。','notice hidden');notice.id='pause-notice';bar.after(notice);
 const quality=el('section',null,'hidden');quality.id='quality-view';$('.table-wrap').before(quality);
 const fav=el('button','加入关注','plain');fav.id='favorite-toggle';fav.addEventListener('click',async()=>{fav.disabled=true;try{const r=await fetch('/api/favorite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mint:selected.mint,enabled:!data.favoriteMints.includes(selected.mint)})});const result=await r.json();if(!r.ok)throw Error(result.error||'关注设定失败');$('#action-status').textContent='已更新关注';await refresh()}catch(e){$('#action-status').textContent=e.message}finally{fav.disabled=false}});$('#detail-state').after(fav);const status=el('div',null,'source-times');status.id='action-status';status.setAttribute('role','status');fav.after(status);
 const panel=el('section',null,'hidden');panel.id='replay-panel';const title=el('h3','当时证据回放'),slider=el('input');slider.id='replay-slider';slider.type='range';slider.min=0;slider.step=1;slider.setAttribute('aria-label','回放时点');const time=el('span');time.id='replay-time';const latest=el('button','返回最新','plain');latest.addEventListener('click',()=>{replayIndex=null;$('#replay-time').textContent='最新状态';refresh()});const outcomes=el('p');outcomes.id='outcomes';panel.append(title,time,slider,latest,outcomes);$('#detail-metrics').before(panel);slider.addEventListener('input',()=>{replayIndex=Number(slider.value);const f=replayFrames[replayIndex];if(!f)return;const frame={...selected,phase:f.phase,evidence:f.evidence,since:selected.initial_price?(f.evidence.metrics.price/selected.initial_price-1)*100:null};fillDetail(frame);$('#replay-time').textContent='回放 '+date(f.ts)+' · 不包含此时点之后的证据'});
}
mountUpgrade();
phaseNames.preview='基础弱预警';phaseNames.confirmed='趋势条件持续满足';
for(const option of $('#kind').options){if(option.value==='confirmed'||option.value==='trend:confirmed')option.textContent='趋势条件持续满足';if(option.value==='preview')option.textContent='基础弱预警';if(option.value==='trend:near')option.textContent='异动待进一步配合'}
$('#count-confirmed').previousElementSibling.textContent='趋势持续';
for(const p of document.querySelectorAll('#rules p')){
 if(p.textContent.startsWith('OTC 2.0：'))p.textContent='历史规则 1.0：基础异动仍保留为弱预警。正式趋势需两个 5 分钟窗口持续放量，并至少满足阶梯抬升、突破回踩、5/15 分钟共振之一；同时平台流动性 ≥ $5,000、较基准下降不超过 5%，5 分钟市值涨幅 >0% 且 ≤15%。持有人增长仅作辅助，不再是正式趋势硬门槛。基础证据分不包含历史指标，也不是概率。';
 if(p.textContent.startsWith('基准为来源'))p.textContent='持有人/流动性基准为来源时间相差 4～15 分钟且最接近 5 分钟的快照。快照超过 10 分钟不采信，最新分钟收线超过 3 分钟不采信。历史指标逐项检查连续性；无数据不等于不满足。后续新收线再次成立才可升级为趋势条件持续满足。';
}
$('#rules>div').append(el('p','本地归档：完整分钟市值 K 线及来源快照保留 30 天，按代币与来源时间去重，已归档收线保留首次观测版本。休眠、掉线和未入池时段不补造。老币复苏至少需 24 小时连续基准；具体算法和实测值见代币详情。阈值为启发式初始参数，未经收益验证。'));
function refreshIcons(){if(window.lucide)window.lucide.createIcons()}
function applyTheme(dark){document.documentElement.dataset.theme=dark?'dark':'light';const b=$('#theme-toggle');b.title=b.ariaLabel=dark?'切换浅色模式':'切换深色模式';const i=el('i');i.dataset.lucide=dark?'sun':'moon';b.replaceChildren(i);refreshIcons();document.querySelectorAll('canvas').forEach(c=>delete c.dataset.signature);render()}
let savedTheme=null;try{savedTheme=localStorage.getItem('otc-radar-theme')}catch{}
applyTheme(savedTheme==='dark');
$('#theme-toggle').addEventListener('click',()=>{const dark=document.documentElement.dataset.theme!=='dark';try{localStorage.setItem('otc-radar-theme',dark?'dark':'light')}catch{}applyTheme(dark)});
function closeRail(){document.body.classList.remove('rail-open');$('#rail-overlay').hidden=true;$('#menu-toggle').setAttribute('aria-expanded','false')}
$('#menu-toggle').addEventListener('click',()=>{const open=!document.body.classList.contains('rail-open');document.body.classList.toggle('rail-open',open);$('#rail-overlay').hidden=!open;$('#menu-toggle').setAttribute('aria-expanded',String(open))});
$('#rail-overlay').addEventListener('click',closeRail);document.addEventListener('keydown',e=>{if(e.key==='Escape')closeRail()});
document.querySelectorAll('.rail a').forEach(a=>a.addEventListener('click',()=>{closeRail();if(a.getAttribute('href')==='#rules')$('#rules').open=true}));
const tabIcons={live:'activity',watch:'scan-eye',archive:'history',favorites:'star',quality:'chart-no-axes-combined'};
document.querySelectorAll('[data-view]').forEach(b=>{const i=el('i');i.dataset.lucide=tabIcons[b.dataset.view];b.prepend(i)});refreshIcons();
for(const b of document.querySelectorAll('[data-view]'))b.addEventListener('click',()=>{view=b.dataset.view;document.querySelectorAll('[data-view]').forEach(x=>x.setAttribute('aria-selected',x===b));render()});
$('#search').addEventListener('input',render);$('#kind').addEventListener('change',render);$('#sort').addEventListener('change',render);$('#refresh').addEventListener('click',refresh);$('#close').addEventListener('click',()=>$('#detail').close());
$('#export').addEventListener('click',()=>{const blob=new Blob([JSON.stringify({exportedAt:new Date().toISOString(),view,records:visible},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='otc-radar-'+view+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
window.addEventListener('resize',()=>{render();if(selected&&$('#detail').open)draw($('#chart'),selected.evidence.bars||[],true)});refresh();setInterval(()=>{if(!paused)refresh()},10000);
