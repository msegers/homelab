import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('./homelab.js', import.meta.url), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page() {
  const element = (tagName) => ({ tagName, text: '', dataset: {}, style: {}, children: [], attributes: {},
    get textContent() { return this.text; },
    set textContent(value) { this.text = value; this.children = []; },
    get lastElementChild() { return this.children.at(-1); },
    setAttribute(name, value) { this.attributes[name] = value; },
    append(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, fn) { this[name] = fn; },
  });
  const ids = new Map(['refresh', 'message', 'cpu', 'memory', 'uptime', 'load', 'disks', 'services', 'service-summary', 'machine-model', 'cpu-model', 'cpu-topology', 'ram-total']
    .map(id => [id, element()]));
  const requests = [];
  const options = { fail: false, wait: null, partial: false, memoryUsed: 1024 ** 3,
    diskUsed: 1024 ** 3, diskAvailable: 2 * 1024 ** 3, diskTotal: 3 * 1024 ** 3, diskStatus: 'Mounted',
    serviceState: 'exited', serviceStatus: 'Exited',
    specs: { machine: 'Example PC', cpu_model: 'Example CPU', cores: 4, threads: 8 } };
  vm.runInNewContext(source, {
    document: { getElementById: id => ids.get(id), createElement: element },
    AbortSignal,
    fetch: async (path, init) => {
      requests.push({ path, init });
      if (options.wait) await options.wait;
      if (options.fail) throw new Error('Unavailable');
      return { ok: true, json: async () => ({ ok: true, result: {
        sampled_at: '2026-11-07T12:00:00Z',
        host: options.partial ? null : { specs: options.specs, cpu_percent: 25, memory_used_bytes: options.memoryUsed,
          memory_total_bytes: 2 * 1024 ** 3, uptime_seconds: 90000, load_average: [0.1, 0.2, 0.3] },
        services: [{ name: '<img src=x onerror=alert(1)>', state: options.serviceState, status: options.serviceStatus, cpu: null }],
        disks: [
          { name: 'System / SSD', mount: '/', device: '/dev/sda1', status: options.diskStatus,
            used_bytes: options.diskUsed, available_bytes: options.diskAvailable, total_bytes: options.diskTotal },
          { name: 'Storage / RAID', mount: '/srv/storage', device: null, status: 'Not mounted',
            used_bytes: null, available_bytes: null, total_bytes: null, used_percent: null },
        ],
        warnings: options.partial ? ['Host statistics unavailable.'] : [],
      } }) };
    },
    // Deliberately no timers: any attempt to add polling fails these tests.
  });
  await flush();
  return { ids, requests, options };
}

test('one load request, explicit refresh, text-only values and missing metrics', async () => {
  const { ids, requests } = await page();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/api/metrics');
  assert.equal(requests[0].init.cache, 'no-store');
  assert.equal(ids.get('cpu').textContent, '25.0%');
  assert.equal(ids.get('cpu').children.length, 0);
  assert.match(ids.get('memory').textContent, /50.0%/);
  const memoryMeter = ids.get('memory').children[0];
  assert.equal(memoryMeter.tagName, 'meter');
  assert.equal(memoryMeter.value, 50);
  assert.equal(memoryMeter.max, 100);
  assert.equal(memoryMeter.attributes['aria-label'], 'Host memory: 50.0% used');
  const disk = ids.get('disks').children[0];
  assert.equal(disk.className, 'disk-row');
  const legend = disk.children[3];
  assert.equal(legend.children[0].textContent, '1.00 GiB used');
  assert.equal(legend.children[1].textContent, '2.00 GiB available');
  assert.equal(legend.children[2].textContent, '3.00 GiB total');
  const diskMeter = disk.children[2];
  assert.equal(diskMeter.attributes.role, 'meter');
  assert.ok(Math.abs(Number(diskMeter.attributes['aria-valuenow']) - 100 / 3) < 0.001);
  assert.equal(diskMeter.attributes['aria-label'], 'System / SSD disk space');
  assert.equal(ids.get('disks').children[1].children[2].className, 'disk-error');
  assert.equal(ids.get('disks').children[1].children[2].textContent, 'Not mounted — space unavailable.');
  assert.equal(ids.get('disks').children[1].children.length, 3);
  assert.equal(ids.get('services').children[0].children[0].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(ids.get('services').children[0].children[2].textContent, '—');
  await ids.get('refresh').click();
  assert.equal(requests.length, 2);
});

test('failed refresh retains visibly stale snapshot and permits retry', async () => {
  const { ids, options } = await page();
  options.fail = true;
  await ids.get('refresh').click();
  assert.match(ids.get('message').textContent, /Previous snapshot retained/);
  assert.equal(ids.get('cpu').textContent, '25.0%');
  assert.equal(ids.get('refresh').disabled, false);
  options.fail = false;
  options.partial = true;
  await ids.get('refresh').click();
  assert.equal(ids.get('cpu').textContent, '—');
  assert.equal(ids.get('memory').textContent, '—');
  assert.equal(ids.get('memory').children.length, 0);
  assert.match(ids.get('message').textContent, /Host statistics unavailable/);
});

test('capacity meters handle empty, full and unknown values without indeterminate bars', async () => {
  const { ids, options } = await page();
  for (const percent of [0, 100]) {
    options.memoryUsed = 2 * 1024 ** 3 * percent / 100;
    options.diskUsed = options.diskTotal * percent / 100;
    options.diskAvailable = options.diskTotal - options.diskUsed;
    await ids.get('refresh').click();
    assert.equal(ids.get('memory').children[0].value, percent);
    assert.equal(ids.get('disks').children[0].children[2].children[0].style.width, `${percent}%`);
  }
  options.diskUsed = null;
  await ids.get('refresh').click();
  assert.equal(ids.get('disks').children[0].children[2].textContent, 'Cannot read disk space.');
  assert.equal(ids.get('disks').children[0].children.length, 3);
});

test('stacked disk bar accounts for reserves and suppresses normal mount status', async () => {
  const { ids, options } = await page();
  options.diskTotal = 4 * 1024 ** 3;
  await ids.get('refresh').click();
  const disk = ids.get('disks').children[0];
  assert.deepEqual(Array.from(disk.children[2].children, s => s.style.width), ['25%', '50%', '25%']);
  assert.equal(disk.children[3].children[2].textContent, '1.00 GiB reserved');
  assert.match(disk.children[2].attributes['aria-valuetext'], /1.00 GiB reserved/);
  assert.ok(!JSON.stringify(disk).includes('Mounted'));
  options.diskStatus = 'Statistics unavailable';
  await ids.get('refresh').click();
  assert.equal(ids.get('disks').children[0].children[2].textContent, 'Cannot read disk space.');
});

test('overlapping refreshes cannot start duplicate requests', async () => {
  const { ids, requests, options } = await page();
  let release;
  options.wait = new Promise(resolve => { release = resolve; });
  const pending = ids.get('refresh').click();
  await ids.get('refresh').click();
  assert.equal(requests.length, 2);
  assert.equal(ids.get('refresh').disabled, true);
  release();
  await pending;
  assert.equal(ids.get('refresh').disabled, false);
});

test('service status avoids redundant state prefixes with a fallback when status is missing', async () => {
  const { ids, options } = await page();
  for (const [state, status] of [['running', 'Up 3 hours (healthy)'], ['exited', 'Exited (0) 2 hours ago'], ['restarting', 'Restarting (1) 5 seconds ago'], ['paused', 'Up 3 hours (Paused)']]) {
    options.serviceState = state;
    options.serviceStatus = status;
    await ids.get('refresh').click();
    assert.equal(ids.get('services').children[0].children[1].textContent, status);
  }
  options.serviceState = 'created';
  options.serviceStatus = '';
  await ids.get('refresh').click();
  assert.equal(ids.get('services').children[0].children[1].textContent, 'created');
});

test('host specifications render as text and missing facts do not hide live readings', async () => {
  const { ids, options } = await page();
  assert.equal(ids.get('machine-model').textContent, 'Example PC');
  assert.equal(ids.get('cpu-model').textContent, 'Example CPU');
  assert.equal(ids.get('cpu-topology').textContent, '4 / 8');
  assert.equal(ids.get('ram-total').textContent, '2.00 GiB');
  options.specs = { machine: '<script>text only</script>', threads: 8 };
  await ids.get('refresh').click();
  assert.equal(ids.get('machine-model').textContent, '<script>text only</script>');
  assert.equal(ids.get('cpu-model').textContent, '—');
  assert.equal(ids.get('cpu-topology').textContent, '— / 8');
  assert.equal(ids.get('cpu').textContent, '25.0%');
  options.partial = true;
  await ids.get('refresh').click();
  assert.equal(ids.get('machine-model').textContent, '—');
  assert.equal(ids.get('ram-total').textContent, '—');
});
