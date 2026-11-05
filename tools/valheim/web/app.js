'use strict';

const buttons = [...document.querySelectorAll('button[data-action]')];
let token = '';
let busy = false;
let known = false;
let running = false;

function text(id, value) {
  document.getElementById(id).textContent = value;
}

function updateControls() {
  for (const button of buttons) {
    button.disabled = busy || !known || !token ||
      (button.dataset.action === 'start' ? running : !running);
  }
}

async function api(path, action) {
  const options = action ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Valheim-Token': token },
    body: JSON.stringify({ action }),
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
    text('idle', status.idle_enabled ? `${status.idle_timeout_seconds / 60} minutes empty` : 'Disabled');
    return true;
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
