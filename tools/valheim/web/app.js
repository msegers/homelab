'use strict';

const buttons = [...document.querySelectorAll('button[data-action]')];
let token = '';
let busy = false;
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

function updateControls() {
  for (const button of buttons) {
    button.disabled = busy || !known || !token ||
      (button.dataset.action === 'start' ? running : !running);
  }
  const unavailable = busy || !known || !token;
  const worldsUnavailable = unavailable || !worldsKnown;
  for (const id of ['saved-world', 'new-world', 'create-world']) element(id).disabled = worldsUnavailable;
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
    text('backup-policy', backups ? 'Automatic backup before world changes: enabled.' : 'Automatic backup before world changes: OFF. Deletion is irreversible without your own backup.');
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
    updateControls();
  }
}

async function worldAction(action, world, confirmation) {
  if (busy || !known || !worldsKnown || !token) return;
  if (!/^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$/.test(world)) {
    text('message', 'Enter a world name using letters, numbers, underscores or hyphens (up to 128 characters).');
    return;
  }
  if (action === 'world' && !confirm(`Use world "${world}"? ${running ? 'Connected players will be disconnected; the server will stop and resume. ' : ''}${backups ? 'A backup will be taken.' : 'Automatic backups are disabled.'}`)) return;
  busy = true;
  updateControls();
  text('message', 'World operation in progress… This may take several minutes.');
  try {
    text('message', await api('/api/action', action, { world, ...(confirmation === undefined ? {} : { confirmation }) }));
  } catch (error) {
    text('message', error.message);
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
    text('message', 'That world already exists. Choose it from the saved worlds list.');
    return;
  }
  return worldAction('world', name);
});
element('delete-world').addEventListener('click', () => {
  const name = element('saved-world').value;
  if (!name || name === selectedWorld || name === runningWorld) return;
  const confirmation = prompt(`Delete world "${name}"? ${backups ? 'A backup will be taken first.' : 'NO automatic backup: this is irreversible.'} ${running ? 'Connected players will be disconnected.' : ''} Type the exact world name to confirm.`);
  if (confirmation !== name) return;
  return worldAction('delete', name, confirmation);
});

for (const button of buttons) {
  button.addEventListener('click', async () => {
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
setInterval(() => { if (!busy) refresh(); }, 15000);
