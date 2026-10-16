const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('app.js', 'utf8');
const fn = source.slice(source.indexOf('function trendState('), source.indexOf('function scoreState('));
const context = vm.createContext({});
vm.runInContext(fn, context);
function classify(values, phase='watch', closed=null) {
  const labels=['相对放量','市值抬升','持有人增长','流动性稳定'];
  return context.trendState({phase,closed,evidence:{score:100,scoreParts:labels.map((label,i)=>({label,ok:values[i]})),reasons:['来源检查']}}).key;
}
assert.equal(classify([false,true,true,true]),'watch');
assert.equal(classify([true,true,false,true]),'near');
assert.equal(classify([true,true,true,true],'early'),'early');
assert.equal(classify([true,true,true,true],'confirmed'),'confirmed');
assert.equal(classify([true,true,true,true],'unavailable'),'unknown');
assert.equal(classify([null,null,true,true],'warming'),'unknown');
assert.equal(classify([true,true,true,null]),'unknown');
assert.equal(classify([true,true,true,true],'risk'),'risk');
assert.equal(classify([true,true,true,true],'confirmed',123),'ended');
console.log('9 trend classification tests passed');
const enhanced={phase:'early',evidence:{reasons:[],scoreParts:[],historyTrend:{items:[{key:'sustained',ok:true},{key:'structure',ok:true}]}}};
assert.equal(context.trendState(enhanced).key,'early');
enhanced.phase='unavailable';assert.equal(context.trendState(enhanced).key,'unknown');
enhanced.phase='watch';enhanced.evidence.historyTrend.items[0].ok=false;assert.equal(context.trendState(enhanced).key,'watch');
console.log('3 history classification tests passed');
