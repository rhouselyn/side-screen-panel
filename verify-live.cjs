const assert = require('assert');
// Node 21: node --experimental-websocket verify-live.cjs；Node 22+ 可直接运行。
(async () => {
 let socket;
 const timeout = setTimeout(() => { console.error('面板验证超时（10 秒）'); process.exit(1); }, 10000);
 try {
 assert(typeof WebSocket !== 'undefined', '请使用 Node 22+，或加 --experimental-websocket');
 const tabs = await (await fetch('http://127.0.0.1:9228/json', {signal: AbortSignal.timeout(3000)})).json();
 const tab = tabs.find(t => t.url.includes('dashboard.html'));
 assert(tab, 'dashboard page exists');
 socket = new WebSocket(tab.webSocketDebuggerUrl);
 await new Promise((resolve, reject) => {socket.onopen=resolve; socket.onerror=reject});
 const result = await new Promise((resolve, reject) => {
  socket.onerror = reject;
  socket.onclose = () => reject(new Error('调试连接提前关闭'));
  socket.onmessage = ({data}) => {
   const message=JSON.parse(data);
   if(message.id===1) {
    if(message.error || message.result?.exceptionDetails) reject(new Error(JSON.stringify(message.error || message.result.exceptionDetails)));
    else resolve(message.result?.result?.value);
   }
  };
  socket.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:`JSON.stringify({
   cpu:document.querySelector('.metric.cpu .value').textContent,
   gpu:document.querySelector('.metric.gpu .value').textContent,
   disk:document.querySelector('.metric.disk .value').textContent,
   quotas:[...document.querySelectorAll('.window .value')].map(node=>node.textContent),
   credit:document.querySelector('.credit-head strong').textContent,
   cards:document.querySelectorAll('.cards li').length,
   progress:[...document.querySelectorAll('.task')].map(node=>node.dataset.state),
   rows:document.querySelectorAll('.task').length,
   area:getComputedStyle(document.querySelector('.panel')).gridTemplateAreas,
   cells:[...document.querySelectorAll('.right,.quota,.deepseek')].map(x=>Math.round(x.getBoundingClientRect().height)),
   scrolling:getComputedStyle(document.querySelector('.tasks')).overflowY,
   link:typeof qt !== 'undefined',
   net:document.querySelector('.network > .speeds .value').textContent
  })`,returnByValue:true}}));
 });
 console.log(result);
 const data=JSON.parse(result);
 assert(data.link && data.cpu.includes('%') && !data.cpu.includes('—'));
 assert(data.quotas.length===2 && data.quotas.every(value=>/^(?:-?\d+(?:\.\d+)?|—)%$/.test(value)));
 assert(data.credit==='—' || /^\d+ 张$/.test(data.credit));
 assert(!data.disk.includes('—') && data.cards>=1);
 assert(data.area.includes('hardware') && data.area.includes('right'));
 assert(data.scrolling==='auto');
 } finally {
  clearTimeout(timeout);
  if (socket) socket.close();
 }
})().catch(error => { console.error(error);process.exitCode=1; });

