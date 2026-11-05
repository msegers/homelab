// Browser-independent UI behavior tests; no npm dependencies or build pipeline.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page(options = {}) {
  const ids = new Map(['running', 'game', 'players', 'world', 'idle', 'message']
    .map(id => [id, { textContent: '', dataset: {} }]));
  const buttons = ['start', 'stop', 'restart'].map(action => ({
    dataset: { action }, disabled: true,
    addEventListener(_name, handler) { this.click = handler; },
  }));
  const requests = [];
  let poll;
  const context = vm.createContext({
    document: {
      querySelectorAll: () => buttons,
      getElementById: id => ids.get(id),
    },
    confirm: () => options.confirm !== false,
    setInterval: callback => { poll = callback; },
    fetch: async (path, init) => {
      requests.push({ path, init });
      if (path === '/api/session') return { ok: true, json: async () => ({ token: 'csrf-token' }) };
      if (path === '/api/status') {
        if (options.unavailable) return { ok: false, status: 503, json: async () => ({ ok: false, error: 'Helper unavailable' }) };
        return { ok: true, json: async () => ({ ok: true, result: {
          running: options.running ?? false, game_state: 'RUNNING', players: options.players ?? 0,
          selected_world: '<script>not markup</script>', idle_enabled: true, idle_timeout_seconds: 3600,
        } }) };
      }
      if (options.actionWait) await options.actionWait;
      return { ok: true, json: async () => ({ ok: true, result: 'Operation completed' }) };
    },
  });
  vm.runInContext(source, context);
  await flush();
  return { ids, buttons, requests, poll };
}

test('stopped server enables only start; status is rendered as text', async () => {
  const { ids, buttons } = await page();
  assert.equal(ids.get('running').textContent, 'Stopped');
  assert.equal(ids.get('players').textContent, 0);
  assert.equal(ids.get('world').textContent, '<script>not markup</script>');
  assert.equal(ids.get('idle').textContent, '60 minutes empty');
  assert.deepEqual(buttons.map(b => b.disabled), [false, true, true]);
});

test('running server enables stop/restart and sends authenticated POST', async () => {
  const { buttons, requests, ids } = await page({ running: true, players: 2 });
  assert.deepEqual(buttons.map(b => b.disabled), [true, false, false]);
  await buttons[1].click();
  const action = requests.find(request => request.path === '/api/action');
  assert.equal(action.init.method, 'POST');
  assert.equal(action.init.headers['X-Valheim-Token'], 'csrf-token');
  assert.equal(JSON.parse(action.init.body).action, 'stop');
  assert.equal(ids.get('message').textContent, 'Operation completed');
});

test('canceling confirmation does not send an action', async () => {
  const { buttons, requests } = await page({ running: true, confirm: false });
  await buttons[2].click();
  assert.ok(!requests.some(request => request.path === '/api/action'));
});

test('unavailable status disables all controls', async () => {
  const { ids, buttons } = await page({ unavailable: true });
  assert.equal(ids.get('running').textContent, 'Unavailable');
  assert.equal(ids.get('message').textContent, 'Helper unavailable');
  assert.ok(buttons.every(button => button.disabled));
});

test('long operations disable controls and pause polling until completion', async () => {
  let finish;
  const actionWait = new Promise(resolve => { finish = resolve; });
  const { buttons, requests, poll } = await page({ actionWait });
  const pending = buttons[0].click();
  await flush();
  assert.ok(buttons.every(button => button.disabled));
  const count = requests.length;
  poll();
  assert.equal(requests.length, count);
  finish();
  await pending;
  assert.equal(buttons[0].disabled, false);
});
