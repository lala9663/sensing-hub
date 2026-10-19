// 화면 챗봇(static/chat.js)의 바로 가기 판별(Chat.keywordOf·Chat.naverQuery)이 서버(ChatShortcuts.java)와 같은지
// 같은 사례 파일(backend/src/test/resources/chat-shortcuts-cases.tsv)로 확인한다. 서버 쪽은 ChatShortcutsTest가 본다.
// 실행: node scripts/dev/check-chat-shortcuts.mjs   (브라우저 없이 App만 흉내 내어 chat.js를 읽는다)
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const source = readFileSync(join(root, 'backend/src/main/resources/static/chat.js'), 'utf8');
const cases = readFileSync(join(root, 'backend/src/test/resources/chat-shortcuts-cases.tsv'), 'utf8')
  .split(/\r?\n/).filter(l => l && !l.startsWith('#')).map(l => l.split('\t'));

const App = { api: async () => ({}), esc: s => String(s), route: () => {}, html: () => null };
const context = vm.createContext({ App, localStorage: null, window: {}, location: {}, history: {} });
vm.runInContext(`${source}\nthis.Chat = Chat;`, context);
const { Chat } = context;

let failed = 0;
for (const [kind, input, expected] of cases) {
  const result = kind === 'naver' ? Chat.naverQuery(input) : Chat.keywordOf(input);
  const got = result === null ? 'NONE' : result === '' ? 'EMPTY' : result;
  if (got !== expected) {
    failed += 1;
    console.log(`FAIL ${kind} [${input}] → [${got}], expected [${expected}]`);
  }
}
console.log(`${cases.length - failed}/${cases.length} cases passed`);
process.exit(failed ? 1 : 0);
