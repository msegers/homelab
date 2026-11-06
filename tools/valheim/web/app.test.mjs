// Browser-independent UI behavior tests; no npm dependencies or build pipeline.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page(options = {}) {
  const ids = new Map(['running', 'game', 'players', 'world', 'idle', 'message', 'world-message', 'running-world', 'saved-world', 'new-world', 'new-seed', 'select-world', 'delete-world', 'create-world']
    .map(id => [id, { textContent: '', dataset: {}, value: '', disabled: true,
      replaceChildren(...children) { this.children = children; this.value = children[0]?.value || ''; },
      addEventListener(name, handler) { this[name] = handler; },
    }]));
  const buttons = ['start', 'stop', 'restart'].map(action => ({
    dataset: { action }, disabled: true,
    addEventListener(_name, handler) { this.click = handler; },
  }));
  const requests = [];
  const prompts = [];
  let actionFinished = false;
  let poll;
  const context = vm.createContext({
    document: {
      querySelectorAll: () => buttons,
      getElementById: id => ids.get(id),
      createElement: () => ({ value: '', textContent: '' }),
    },
    confirm: message => { prompts.push(message); return options.confirm !== false; },
    prompt: message => { prompts.push(message); return options.confirmation ?? null; },
    setInterval: callback => { poll = callback; },
    fetch: async (path, init) => {
      requests.push({ path, init });
      if (path === '/api/session') return { ok: true, json: async () => ({ token: 'csrf-token' }) };
      if (path === '/api/worlds') {
        if (options.worldsUnavailable || (actionFinished && options.refreshFailsAfterAction)) return { ok: false, status: 503, json: async () => ({ ok: false, error: 'World list unavailable' }) };
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
      actionFinished = true;
      if (options.actionError) return { ok: false, status: 503, json: async () => ({ ok: false, error: options.actionError }) };
      return { ok: true, json: async () => ({ ok: true, result: 'Operation completed' }) };
    },
  });
  vm.runInContext(source, context);
  await flush();
  return { ids, buttons, requests, prompts, poll };
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
  for (const id of ['saved-world', 'new-world', 'new-seed', 'create-world', 'select-world', 'delete-world']) {
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
  ids.get('new-seed').value = 'Unrelated';
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
  assert.match(ids.get('world-message').textContent, /Enter a world name/);
  ids.get('new-world').value = 'NewWorld';
  await ids.get('create-world').click();
  assert.ok(!requests.some(request => request.path === '/api/action'));
});

test('deletion requires exact typed confirmation and always promises a mandatory archive', async () => {
  for (const confirmation of ['wrong', 'Other']) {
    const { ids, requests, prompts } = await page({ selectedWorld: 'Home', backups: false, confirmation });
    assert.equal(ids.get('delete-world').disabled, true);
    ids.get('saved-world').value = 'Other';
    await ids.get('delete-world').click();
    assert.match(prompts[0], /recoverable archive is required first/);
    assert.match(prompts[0], /if it fails, deletion is canceled/);
    assert.match(prompts[0], /Existing backup files are preserved/);
    const request = requests.find(request => request.path === '/api/action');
    if (confirmation === 'Other') {
      assert.deepEqual(JSON.parse(request.init.body), { action: 'delete', world: 'Other', confirmation: 'Other' });
    } else {
      assert.equal(request, undefined);
    }
  }
});

test('new worlds send a case-sensitive seed only on the create action; blank means random', async () => {
  for (const seed of ['', 'Potato', '0123456789']) {
    const { ids, requests, prompts } = await page({ selectedWorld: 'Home' });
    ids.get('new-world').value = 'NewWorld';
    ids.get('new-seed').value = seed;
    await ids.get('create-world').click();
    const request = requests.find(request => request.path === '/api/action');
    assert.deepEqual(JSON.parse(request.init.body), { action: 'create', world: 'NewWorld', ...(seed ? { seed } : {}) });
    assert.ok(prompts.some(message => message.includes(seed || 'random') && message.includes('stay stopped')));
  }
});

test('invalid seeds and existing world names cannot trigger creation', async () => {
  for (const seed of ['12345678901', ' a', 'a-b', '$(id)', 'é']) {
    const { ids, requests } = await page({ selectedWorld: 'Home' });
    ids.get('new-world').value = 'NewWorld';
    ids.get('new-seed').value = seed;
    await ids.get('create-world').click();
    assert.match(ids.get('world-message').textContent, /Seeds use/);
    assert.ok(!requests.some(request => request.path === '/api/action'));
  }
  const { ids, requests } = await page({ selectedWorld: 'Home' });
  ids.get('new-world').value = 'Other';
  ids.get('new-seed').value = 'Potato';
  await ids.get('create-world').click();
  assert.match(ids.get('world-message').textContent, /already exists/);
  assert.ok(!requests.some(request => request.path === '/api/action'));
});

test('canceling seeded creation does nothing and seed is disabled during operations', async () => {
  const canceled = await page({ confirm: false });
  canceled.ids.get('new-world').value = 'NewWorld';
  canceled.ids.get('new-seed').value = 'Potato';
  await canceled.ids.get('create-world').click();
  assert.ok(!canceled.requests.some(request => request.path === '/api/action'));
  let finish;
  const actionWait = new Promise(resolve => { finish = resolve; });
  const { ids } = await page({ actionWait });
  ids.get('new-world').value = 'NewWorld';
  const pending = ids.get('create-world').click();
  await flush();
  assert.equal(ids.get('new-seed').disabled, true);
  finish();
  await pending;
  assert.equal(ids.get('new-seed').disabled, false);
});

test('world form has one short accessible hint instead of explanatory paragraphs', () => {
  const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
  const form = html.split('<section aria-label="World management">')[1].split('</section>')[0];
  const paragraphs = form.match(/<p\b[^>]*>/g) || [];
  assert.equal(paragraphs.filter(tag => !/\bhidden\b/.test(tag)).length, 1);
  assert.match(form, /id="world-message"[^>]*aria-live="polite" hidden/);
  assert.match(form, /<label for="new-seed">Seed \(optional\)<\/label>/);
  assert.match(form, /placeholder="Random if blank"[^>]*aria-describedby="seed-help"/);
  assert.match(form, /id="seed-help" class="field-hint">Seed: 1–10 letters\/numbers, case-sensitive\./);
});

test('world confirmations retain backup policy and disconnection warnings', async () => {
  for (const backups of [true, false]) {
    const { ids, prompts } = await page({ running: true, backups, confirm: false, selectedWorld: 'Home' });
    ids.get('saved-world').value = 'Other';
    await ids.get('select-world').click();
    assert.match(prompts[0], /Connected players will be disconnected/);
    assert.match(prompts[0], backups ? /A backup will be taken/ : /Automatic backups are disabled/);
  }
});

test('creation failures are visible beside the form and survive failed refreshes and polling', async () => {
  const reason = 'That name still has world saves. Choose another name.';
  const { ids, poll } = await page({ actionError: reason, refreshFailsAfterAction: true, selectedWorld: 'Home' });
  ids.get('new-world').value = 'Skoltr';
  ids.get('new-seed').value = 'DikkeTietn';
  await ids.get('create-world').click();
  const message = ids.get('world-message');
  assert.equal(message.hidden, false);
  assert.equal(message.dataset.error, 'true');
  assert.equal(message.textContent, `World creation failed: ${reason}`);
  assert.equal(ids.get('new-world').value, 'Skoltr');
  assert.equal(ids.get('new-seed').value, 'DikkeTietn');
  await poll();
  await flush();
  assert.equal(message.textContent, `World creation failed: ${reason}`);
});

test('a successful retry replaces the form error with a success message', async () => {
  const options = { actionError: 'Please try another name.', selectedWorld: 'Home' };
  const { ids } = await page(options);
  ids.get('new-world').value = 'NewWorld';
  await ids.get('create-world').click();
  assert.equal(ids.get('world-message').dataset.error, 'true');
  options.actionError = '';
  await ids.get('create-world').click();
  assert.equal(ids.get('world-message').textContent, 'Operation completed');
  assert.equal(ids.get('world-message').dataset.error, 'false');
});
