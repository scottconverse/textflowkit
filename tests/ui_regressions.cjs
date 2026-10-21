const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../src/textflowkit/ui/static/app.js'), 'utf8');
class Element {
  constructor() { this.children=[]; this.listeners={}; this.value=''; this.hidden=true; this.className=''; this.dataset={}; this.attrs={}; this.writes=[]; this.classList={add:()=>{},remove:()=>{}}; }
  set textContent(t) { this.text=t; this.writes.push(t); }
  get textContent() { return this.text || ''; }
  replaceChildren(...items) { this.children=items; }
  appendChild(item) { this.children.push(item); return item; }
  addEventListener(name, fn) { this.listeners[name]=fn; }
  setAttribute(k,v) { this.attrs[k]=v; }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  remove() {}
  load() {}
  pause() {}
  checkValidity() { try { return ['http:', 'https:'].includes(new URL(this.value).protocol); } catch { return false; } }
  reportValidity() { this.reported=true; return this.checkValidity(); }
}
const deferred=()=>{let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject};};
const response=(body,status=200)=>({ok:status<400,status,json:async()=>body});
function setup(fetcher=async()=>response({jobs:[]})) {
  const nodes=new Map(); const $=id=>{if(!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id);};
  const context={document:{querySelector:()=>null,querySelectorAll:()=>[],getElementById:$,createElement:()=>new Element(),addEventListener:()=>{}},
    window:{dispatchEvent:()=>{}},CustomEvent:class {},URL:class extends URL {
      static createObjectURL(file) { return `blob:${file.name}`; }
      static revokeObjectURL() {}
    },fetch:fetcher,setTimeout:()=>1,clearTimeout:()=>{},console};
  vm.createContext(context);
  // Expose the real closure to drive deterministic transport races without changing production exports.
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, 'globalThis.app={state,wire,watchJob,pollJob,loadTranscript,searchTranscript,submitJob,setUpload,renderJob};})();'),context);
  context.app.wire();
  return {...context.app,$,nodes};
}
for(const page of [1,2]) test(`UX1 transcript page ${page} rejection is recoverable`,async()=>{
  const a=setup(async url=>{
    if(!url.includes('/transcript')) return response({jobs:[]});
    if(page===2 && url.includes('offset=0')) return response({has_more:true,transcript:{segments:[{start:0,end:1,text:'first'}]}});
    throw Error('offline');
  });
  a.state.jobId='a';
  await a.loadTranscript('a');
  assert.match(a.$('results-status').textContent,/transcript.*offline/i);
  assert.match(a.$('results-status').textContent,/reopen|retry/i);
  assert.equal(a.state.jobId,'a'); assert.equal(a.state.transcript,null);
});
test('UX1 old transcript rejection does not replace current status',async()=>{
  const d=deferred(),a=setup(()=>d.promise);a.state.jobId='a';
  const p=a.loadTranscript('a');a.state.jobId='b';a.$('results-status').textContent='new job';
  d.reject(Error('old failure'));await p;assert.equal(a.$('results-status').textContent,'new job');
});
for(const reject of [false,true]) test(`UX2 stale job search ${reject?'error':'result'} discarded`,async()=>{
  const d=deferred(),a=setup(url=>url.includes('/search')?d.promise:Promise.resolve(response({jobs:[]})));
  a.state.jobId='a';a.$('search-input').value='old';const p=a.searchTranscript();
  a.watchJob('b');a.$('results-status').textContent='new job';
  if(reject)d.reject(Error('stale error'));else d.resolve(response({matches:[{start:0,text:'old'}]}));
  await p;assert.equal(a.$('search-results').hidden,true);assert.equal(a.$('search-results').children.length,0);
  assert.equal(a.$('results-status').textContent,'new job');
});
for(const reject of [false,true]) test(`UX2 query generation discards older ${reject?'error':'result'}`,async()=>{
  const d=deferred();let calls=0;const a=setup(()=>++calls===1?d.promise:Promise.resolve(response({matches:[]})));
  a.state.jobId='a';a.$('search-input').value='first';const p=a.searchTranscript();
  a.$('search-input').value='second';await a.searchTranscript();
  if(reject)d.reject(Error('old query'));else d.resolve(response({matches:[{start:0,text:'old'}]}));
  await p;assert.equal(a.$('search-results').children.length,1);assert.equal(a.$('search-results').children[0].textContent,'No matches.');
  assert.equal(a.$('results-status').textContent,'');
});
test('UX2 editing query invalidates pending response before next search',async()=>{
  const d=deferred(),a=setup(()=>d.promise);a.state.jobId='a';a.$('search-input').value='first';const p=a.searchTranscript();
  a.$('search-input').value='edited';a.$('search-input').listeners.input?.();d.resolve(response({matches:[{start:0,text:'old'}]}));
  await p;assert.equal(a.$('search-results').children.length,0);
});
for(const status of [429,503]) test(`UX3 staged upload survives ${status} then retry without reupload`,async()=>{
  let uploads=0,jobs=0;const payloads=[];
  const a=setup(async(url,opts)=>{
    if(url==='/ui/uploads'){uploads++;return response({upload_id:'staged'});}
    if(url==='/ui/jobs'){payloads.push(JSON.parse(opts.body));return ++jobs===1?response({error:'busy'},status):response({id:'a'});}
    return response({jobs:[],id:'a',state:'pending'});
  });
  a.state.pendingFile={name:'clip.wav',size:10};await a.submitJob();await a.submitJob();
  assert.equal(uploads,1);assert.equal(jobs,2);assert.equal(payloads[1].upload_id,'staged');assert.equal(a.state.jobId,'a');
});
for(const mode of ['url','file']) test(`UX3 selecting ${mode} invalidates staged upload`,async()=>{
  const a=setup();a.state.upload={upload_id:'old'};
  if(mode==='url'){a.$('source-input').value='https://example.com/clip';a.$('source-input').listeners.input();}
  else a.setUpload(null);
  assert.equal(a.state.upload,null);
});
test('UX4 terminal error announced once; running polls are quiet',()=>{
  const a=setup();a.state.jobId='a';const live=a.$('results-status');
  a.renderJob({id:'a',state:'running'});a.renderJob({id:'a',state:'running'});assert.equal(live.writes.length,0);
  a.renderJob({id:'a',state:'error',error:'decoder failed'});assert.match(live.textContent,/decoder failed/);
  const n=live.writes.length;a.renderJob({id:'a',state:'error',error:'decoder failed'});assert.equal(live.writes.length,n);
});
for(const value of ['not-a-url','C:\\clip.wav','file:///clip.wav','ftp://example.com/clip']) test(`UX5 refuses browser source ${value}`,async()=>{
  let jobs=0;const a=setup(async()=>{jobs++;return response({id:'bad'});});a.$('source-input').value=value;
  a.$('source-input').listeners.input();a.$('source-use').listeners.click();
  assert.equal(a.state.source,'');assert.match(a.$('preflight').textContent,/valid.*https?/i);
  await a.submitJob();assert.equal(jobs,0);
});
test('UX5 valid HTTPS source submits normally',async()=>{
  const payloads=[];const a=setup(async(url,opts)=>{
    if(url==='/ui/jobs'){payloads.push(JSON.parse(opts.body));return response({id:'a'});}return response({jobs:[],id:'a',state:'pending'});
  });
  a.$('source-input').value='https://example.com/clip';a.$('source-input').listeners.input();
  await a.submitJob();assert.equal(payloads[0].source,'https://example.com/clip');
});

test('UX1 successful pagination publishes only complete transcript',async()=>{
  let calls=0;const a=setup(async()=>response({has_more:++calls===1,transcript:{segments:[{start:calls,end:calls+1,text:`page ${calls}`}]}}));
  a.state.jobId='a';await a.loadTranscript('a');assert.equal(calls,2);
  assert.equal(a.state.transcript.segments.length,2);assert.match(a.$('results-status').textContent,/2 segments loaded/);
});
test('UX3 source switch during upload discards stale upload identity',async()=>{
  const d=deferred();let jobs=0;const a=setup(async url=>{
    if(url==='/ui/uploads')return d.promise;
    if(url==='/ui/jobs')jobs++;
    return response({jobs:[]});
  });
  a.state.pendingFile={name:'old.wav',size:2};const p=a.submitJob();
  await Promise.resolve();
  a.$('source-input').value='https://example.com/new';a.$('source-input').listeners.input();
  d.resolve(response({upload_id:'old'}));await p;
  assert.equal(a.state.upload,null);assert.equal(a.state.source,'https://example.com/new');assert.equal(jobs,0);
});

test('UX3 source switch during job acknowledgment preserves new file and preview',async()=>{
  const ack=deferred();const a=setup(async url=>{
    if(url==='/ui/jobs')return ack.promise;
    return response({jobs:[]});
  });
  a.state.upload={upload_id:'old'};
  const pending=a.submitJob();
  const file={name:'new.wav',size:2,type:'audio/wav'};a.setUpload(file);
  const preview=a.state.mediaEl;assert.ok(preview);
  ack.resolve(response({id:'old-job'}));await pending;
  assert.equal(a.state.pendingFile,file);assert.equal(a.state.mediaEl,preview);
  assert.equal(a.state.mediaJobId,null);assert.notEqual(a.state.jobId,'old-job');
});
test('UX4 actual HTML exposes results-status through a polite live region',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../src/textflowkit/ui/static/index.html'),'utf8');
  const tag=html.match(/<[^>]+id="results-status"[^>]*>/)?.[0];
  assert.ok(tag);assert.match(tag,/role="status"/);assert.match(tag,/aria-live="polite"/);
});
