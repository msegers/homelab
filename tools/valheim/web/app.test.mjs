// Browser-independent UI behavior tests; no npm dependencies or build pipeline.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page(options = {}) {
  const ids = new Map(['running', 'game', 'players', 'world', 'idle', 'message', 'running-world', 'backup-policy', 'saved-world', 'new-world', 'select-world', 'delete-world', 'create-world']
    .map(id => [id, { textContent: '', dataset: {}, value: '', disabled: true,
      replaceChildren(...children) { this.children = children; this.value = children[0]?.value || ''; },
      addEventListener(name, handler) { this[name] = handler; },
    }]));
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
      createElement: () => ({ value: '', textContent: '' }),
    },
    confirm: () => options.confirm !== false,
    prompt: () => options.confirmation ?? null,
    setInterval: callback => { poll = callback; },
    fetch: async (path, init) => {
      requests.push({ path, init });
      if (path === '/api/session') return { ok: true, json: async () => ({ token: 'csrf-token' }) };
      if (path === '/api/worlds') {
        if (options.worldsUnavailable) return { ok: false, status: 503, json: async () => ({ ok: false, error: 'World list unavailable' }) };
        return { ok: true, json: async () => ({ ok: true, result: ['Home', 'Other'] }) };
      }
      if (path === '/api/status') {
        if (options.unavailable) return { ok: false, status: 503, json: async () => ({ ok: false, error: 'Helper unavailable' }) };
        return { ok: true, json: async () => ({ ok: true, result: {
          running: options.running ?? false, game_state: 'RUNNING', players: options.players ?? 0,
          selected_world: options.selectedWorld ?? '<script>not markup</script>', idle_enabled: true, idle_timeout_seconds: 3600,
          pre_change_backups: options.backups ?? true,
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

test('unavailable world list preserves its error and disables only world controls', async () => {
  const { ids, buttons } = await page({ worldsUnavailable: true });
  assert.equal(ids.get('message').textContent, 'World list unavailable');
  assert.equal(buttons[0].disabled, false);
  for (const id of ['saved-world', 'new-world', 'create-world', 'select-world', 'delete-world']) {
    assert.equal(ids.get(id).disabled, true);
  }
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

test('world selection sends a named action only after confirmation', async () => {
  const { ids, requests } = await page({ selectedWorld: 'Home' });
  ids.get('saved-world').value = 'Other';
  ids.get('saved-world').change();
  assert.equal(ids.get('delete-world').disabled, false);
  await ids.get('select-world').click();
  const request = requests.find(request => request.path === '/api/action');
  assert.deepEqual(JSON.parse(request.init.body), { action: 'world', world: 'Other' });
});

test('new worlds validate names and canceled switches do nothing', async () => {
  const { ids, requests } = await page({ confirm: false });
  ids.get('new-world').value = '../escape';
  await ids.get('create-world').click();
  assert.match(ids.get('message').textContent, /Enter a world name/);
  ids.get('new-world').value = 'NewWorld';
  await ids.get('create-world').click();
  assert.ok(!requests.some(request => request.path === '/api/action'));
});

test('deletion requires exact typed confirmation and warns when backups are off', async () => {
  for (const confirmation of ['wrong', 'Other']) {
    const { ids, requests } = await page({ selectedWorld: 'Home', backups: false, confirmation });
    assert.match(ids.get('backup-policy').textContent, /OFF/);
    assert.equal(ids.get('delete-world').disabled, true);
    ids.get('saved-world').value = 'Other';
    await ids.get('delete-world').click();
    const request = requests.find(request => request.path === '/api/action');
    if (confirmation === 'Other') {
      assert.deepEqual(JSON.parse(request.init.body), { action: 'delete', world: 'Other', confirmation: 'Other' });
    } else {
      assert.equal(request, undefined);
    }
  }
});
