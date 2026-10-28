/** PF detached public rendering. No database, cookies or authority secrets. */
import { readFile, chmod, lstat } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:http';
import { resolve, dirname } from 'node:path';

export async function render(bundlePath, path, initialData) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.length > 4096) throw new Error('invalid_route');
  const { renderPage } = await import(pathToFileURL(bundlePath).href);
  return renderPage(path, initialData, { assetPath: '/assets/browser.js', nonce: initialData.nonce });
}

export async function serve(socketPath, bundlePath) {
  const socket = resolve(socketPath), bundle = resolve(bundlePath);
  const parent = await lstat(dirname(socket));
  if (!parent.isDirectory() || parent.isSymbolicLink() || (parent.mode & 0o077)) throw new Error('protected_socket_directory_required');
  try { await lstat(socket); throw new Error('socket_already_exists'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  await import(pathToFileURL(bundle).href);
  let active=0, closed=false;
  const server=createServer(async (request,response)=>{
    const reply=(status,data)=>{const text=JSON.stringify(data);response.writeHead(status,{'Content-Type':'application/json','Content-Length':Buffer.byteLength(text),'Cache-Control':'no-store'});response.end(text);};
    if (closed) return reply(503,{error:'draining'});
    if (request.method==='GET' && request.url==='/health') return reply(200,{ready:true});
    if (request.method!=='POST'||request.url!=='/render') return reply(404,{error:'not_found'});
    if(active>=2) return reply(503,{error:'capacity'});
    if(request.headers['content-type']!=='application/json'||request.headers['transfer-encoding']) return reply(400,{error:'invalid_framing'});
    const length=Number(request.headers['content-length']);
    if(!Number.isSafeInteger(length)||length<2||length>1048576)return reply(413,{error:'limit'});
    active+=1;
    try {
      const parts=[];let size=0;
      for await (const chunk of request) {size+=chunk.length;if(size>1048576)throw new Error('limit');parts.push(chunk);}
      if(size!==length)throw new Error('incomplete');
      const value=JSON.parse(Buffer.concat(parts).toString('utf8'));
      if(!value||typeof value!=='object'||Object.keys(value).sort().join(',')!=='initialData,path'||!value.initialData||typeof value.initialData!=='object')throw new Error('invalid_contract');
      const html=await render(bundle,value.path,value.initialData);
      if(Buffer.byteLength(html)>1900000)throw new Error('response_limit');
      reply(200,{html});
    } catch {reply(422,{error:'render_contract_error'});} finally {active-=1;}
  });
  server.requestTimeout=5000;server.headersTimeout=5000;server.keepAliveTimeout=1000;server.maxHeadersCount=20;
  await new Promise((yes,no)=>{server.once('error',no);server.listen(socket,yes);});await chmod(socket,0o600);
  process.on('SIGTERM',()=>{closed=true;server.close(()=>process.exit(0));setTimeout(()=>process.exit(1),30000).unref();});
  return server;
}

if(process.argv[2]==='--listen') {
  await serve(process.argv[3],process.argv[4]);
} else if (process.argv[2]) {
  const input = await readFile('/dev/stdin', 'utf8');
  if (Buffer.byteLength(input) > 1048576) throw new Error('view_model_limit');
  process.stdout.write(await render(process.argv[2], process.argv[3] || '/', JSON.parse(input)));
}
