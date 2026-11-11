// Serve only the brochure's public files, never the repository or runtime data.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const pages = ['index.html', 'quickstart.html', 'teams.html', 'security.html', 'styles.css', 'site.js', 'robots.txt', 'sitemap.xml', 'site.webmanifest'];
// The quickstart page's steps (devhelpers/scripts/screenshots/quickstart-steps.mjs).
const quickstart = ['01-sign-in', '02-issuer', '03-machines', '04-model-servers', '05-add-provider', '06-search', '07-results', '08-pulled', '09-start-agent', '10-agent-ready', '11-answer', '12-requests'].map(name => `quickstart-${name}`);
// Its other levels' walks: the one container (basic) and the Helm chart (helm).
const walk = ['model-servers', 'add-provider', 'search', 'results', 'pulled', 'start-agent', 'agent-ready', 'answer', 'requests'];
const levels = [
  ...['sign-in', 'issuer', 'machines', 'start-ollama', ...walk].map(name => `quickstart-basic-${name}`),
  ...['sign-in', 'issuer', 'approve-code', 'approve', 'machines', ...walk].map(name => `quickstart-helm-${name}`),
];
const captures = [...quickstart, ...levels, 'terminal-grid-dark', 'real-opencode', 'real-copilot', 'real-qwen', 'live-dark', 'terminal-phone', 'overview-dark', 'agents', 'machines', 'session-chat', 'machine-mcp', 'terminal'];
// The web-sized copies the homepage shows (devhelpers/scripts/screenshots/site-images.py).
const screens = [
  ...['terminal-grid-dark', 'live-dark', 'overview-dark', 'agents', 'machines', 'session-chat', 'machine-mcp', 'terminal'].flatMap(name => [800, 1440].map(width => `screens/${name}-${width}.webp`)),
  ...[400, 618].map(width => `screens/terminal-phone-${width}.webp`),
  ...[...quickstart, ...levels].flatMap(name => (/0[12]-|-(sign-in|issuer)$/.test(name) ? [540, 1080] : [800, 1440]).map(width => `screens/${name}-${width}.webp`)),
  ...['real-opencode', 'real-copilot', 'real-qwen'].flatMap(name => [666, 1333].map(width => `screens/${name}-${width}.webp`)),
];
const assets = ['icon.svg', 'social.svg', 'social.png', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png', ...screens];
export const publicFiles = new Map([
  ...pages.map(name => [`/${name}`, `site/${name}`]),
  ...captures.map(name => [`/screenshots/${name}.png`, `docs/screenshots/${name}.png`]),
  ...assets.map(name => [`/assets/${name}`, `site/assets/${name}`]),
]);
const types = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.txt': 'text/plain', '.xml': 'application/xml', '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp' };

export async function startPreview(port = 0) {
  const server = createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    if (pathname === '/health') {
      response.writeHead(200, { 'Content-Type': 'text/plain' });
      return response.end('ok\n');
    }
    const file = publicFiles.get(pathname === '/' ? '/index.html' : pathname);
    if (!file) { response.writeHead(404); return response.end('Not found'); }
    try {
      const body = await readFile(path.join(root, file));
      response.writeHead(200, { 'Content-Type': types[path.extname(file)], 'Cache-Control': 'no-store' });
      response.end(body);
    } catch { response.writeHead(404); response.end('Not found'); }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  return { server, url: `http://127.0.0.1:${server.address().port}` };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const { url } = await startPreview(Number(process.argv[2] || 8088));
  console.log(`Brochure preview: ${url}`);
}
