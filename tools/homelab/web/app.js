'use strict';

const buttons = [...document.querySelectorAll('button[data-action]')];
let token = '';
let busy = false;
let refreshing = false;
let known = false;
let running = false;
let selectedWorld = '';
let runningWorld = '';
let worldNames = [];
let worldsKnown = false;
let backups = true;
const element = id => document.getElementById(id);

function text(id, value) {
  document.getElementById(id).textContent = value;
}

function worldMessage(message, error = false) {
  const target = element('world-message');
  target.textContent = message;
  target.hidden = !message;
  target.dataset.error = String(error);
}

function updateControls() {
  element('refresh-status').disabled = busy || refreshing;
  for (const button of buttons) {
    button.disabled = busy || refreshing || !known || !token ||
      (button.dataset.action === 'start' ? running : !running);
  }
  const unavailable = busy || refreshing || !known || !token;
  const worldsUnavailable = unavailable || !worldsKnown;
  for (const id of ['saved-world', 'new-world', 'new-seed', 'create-world']) element(id).disabled = worldsUnavailable;
  const chosen = element('saved-world').value;
  element('select-world').disabled = worldsUnavailable || !chosen || chosen === selectedWorld;
  element('delete-world').disabled = worldsUnavailable || !chosen || chosen === selectedWorld || chosen === runningWorld;
}

async function api(path, action, fields = {}) {
  const options = action ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Valheim-Token': token },
    body: JSON.stringify({ action, ...fields }),
  } : { cache: 'no-store' };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok || !result.ok) {
    if (response.status === 403) token = '';
    throw new Error(result.error || 'Request failed. Reload the page and check server logs.');
  }
  return result.result;
}

async function refresh() {
  if (refreshing) return false;
  refreshing = true;
  updateControls();
  try {
    const status = await api('/api/status');
    running = status.running;
    known = true;
    text('running', running ? 'Running' : 'Stopped');
    document.getElementById('running').dataset.running = String(running);
    text('game', status.game_state || '—');
    text('players', status.players ?? 'Unknown');
    text('world', status.selected_world || '—');
    selectedWorld = status.selected_world;
    runningWorld = running ? status.running_world : '';
    backups = status.pre_change_backups !== false;
    text('running-world', runningWorld || '—');
    try {
      const names = await api('/api/worlds');
      if (!Array.isArray(names)) throw new Error('Invalid world list');
      worldNames = names;
      worldsKnown = true;
      const previous = element('saved-world').value;
      element('saved-world').replaceChildren(...names.map(name => {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name === selectedWorld ? `${name} (selected)` : name;
        return option;
      }));
      element('saved-world').value = names.includes(previous) ? previous : selectedWorld;
    } catch (error) {
      worldNames = [];
      worldsKnown = false;
      element('saved-world').replaceChildren();
      text('message', error.message);
    }
    text('idle', status.idle_enabled ? `${status.idle_timeout_seconds / 60} minutes empty` : 'Disabled');
    return worldsKnown;
  } catch (error) {
    known = false;
    text('running', 'Unavailable');
    document.getElementById('running').dataset.running = 'false';
    text('game', '—');
    text('players', '—');
    text('message', error.message);
    return false;
  } finally {
    refreshing = false;
    updateControls();
  }
}

async function worldAction(action, world, confirmation, seed) {
  if (busy || refreshing || !known || !worldsKnown || !token) return;
  if (!/^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$/.test(world)) {
    worldMessage('Enter a world name using letters, numbers, underscores or hyphens (up to 128 characters).', true);
    return;
  }
  if (seed !== undefined && !/^[A-Za-z0-9]{1,10}$/.test(seed)) {
    worldMessage('Seeds use 1–10 letters or numbers and are case-sensitive. Leave blank for a random seed.', true);
    return;
  }
  if (action === 'world' || action === 'create') {
    const details = [`Use world "${world}"?`];
    if (action === 'create') details.push(`Seed: ${seed === undefined ? 'random' : `"${seed}"`}.`);
    details.push(running
      ? 'Connected players will be disconnected; the server will stop and resume.'
      : 'The server will stay stopped until you press Start.');
    details.push(backups ? 'A backup will be taken.' : 'Automatic backups are disabled.');
    if (!confirm(details.join(' '))) return;
  }
  busy = true;
  updateControls();
  worldMessage('World operation in progress… This may take several minutes.');
  try {
    worldMessage(await api('/api/action', action, { world, ...(confirmation === undefined ? {} : { confirmation }), ...(seed === undefined ? {} : { seed }) }));
  } catch (error) {
    const label = action === 'create' ? 'World creation' : action === 'delete' ? 'World deletion' : 'World switch';
    worldMessage(`${label} failed: ${error.message}`, true);
  } finally {
    await refresh();
    busy = false;
    updateControls();
  }
}

element('saved-world').addEventListener('change', updateControls);
element('select-world').addEventListener('click', () => worldAction('world', element('saved-world').value));
element('create-world').addEventListener('click', () => {
  const name = element('new-world').value;
  if (worldNames.includes(name)) {
    worldMessage('That world already exists. Choose it from the saved worlds list.', true);
    return;
  }
  const seed = element('new-seed').value;
  return worldAction('create', name, undefined, seed === '' ? undefined : seed);
});
element('delete-world').addEventListener('click', () => {
  const name = element('saved-world').value;
  if (!name || name === selectedWorld || name === runningWorld) return;
  const confirmation = prompt(`Delete world "${name}"? A recoverable archive is required first; if it fails, deletion is canceled. Existing backup files are preserved. ${running ? 'Connected players will be disconnected.' : ''} Type the exact world name to confirm.`);
  if (confirmation !== name) return;
  return worldAction('delete', name, confirmation);
});

for (const button of buttons) {
  button.addEventListener('click', async () => {
    if (busy || refreshing) return;
    const action = button.dataset.action;
    if (action !== 'start' && !confirm(`${action === 'stop' ? 'Stop' : 'Restart'} the server? Connected players will be disconnected.`)) return;
    busy = true;
    updateControls();
    text('message', `${action} in progress… This may take several minutes.`);
    try {
      text('message', await api('/api/action', action));
    } catch (error) {
      text('message', error.message);
    } finally {
      await refresh();
      busy = false;
      updateControls();
    }
  });
}

element('refresh-status').addEventListener('click', () => {
  if (!busy) return refresh();
});

async function initialize() {
  try {
    const response = await fetch('/api/session', { cache: 'no-store' });
    if (!response.ok) throw new Error('Cannot initialize controls. Reload the page.');
    token = (await response.json()).token;
    if (await refresh()) text('message', 'Ready.');
  } catch (error) {
    text('message', error.message);
  }
}

initialize();
setInterval(() => {
  if (!busy && !refreshing && element('auto-refresh').checked) return refresh();
}, 15000);
