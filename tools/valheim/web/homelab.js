/* One request on page load; no timers or visibility-triggered background work. */
"use strict";

const byId = (id) => document.getElementById(id);
const bytes = (value) => `${(value / 1024 ** 3).toFixed(2)} GiB`;
let busy = false;
let hasSnapshot = false;

function appendMeter(target, percent, label) {
  if (!Number.isFinite(percent) || percent < 0 || percent > 100) return;
  const meter = document.createElement("meter");
  meter.min = 0;
  meter.max = 100;
  meter.value = percent;
  meter.setAttribute("aria-label", `${label}: ${percent.toFixed(1)}% used`);
  meter.textContent = `${percent.toFixed(1)}%`;
  target.append(meter);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function diskRow(disk) {
  const row = element("div", "disk-row");
  const heading = element("div", "heading");
  heading.append(element("h3", "", disk.name));
  row.append(heading);
  row.append(element("p", "disk-mount", disk.mount));
  const total = disk.total_bytes;
  const used = disk.used_bytes;
  const available = disk.available_bytes;
  if (disk.status !== "Mounted" || ![total, used, available].every(Number.isFinite)
      || total <= 0 || used < 0 || available < 0 || used + available > total) {
    row.append(element("p", "disk-error", disk.status === "Not mounted"
      ? "Not mounted — space unavailable." : "Cannot read disk space."));
    return row;
  }
  const reserved = total - used - available;
  const bar = element("div", "disk-bar");
  bar.setAttribute("role", "meter");
  bar.setAttribute("aria-label", `${disk.name} disk space`);
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", "100");
  bar.setAttribute("aria-valuenow", String(100 * used / total));
  bar.setAttribute("aria-valuetext", `${bytes(used)} used, ${bytes(available)} available, ${bytes(reserved)} reserved, ${bytes(total)} total`);
  const legend = element("div", "disk-legend");
  for (const [kind, value] of [["used", used], ["available", available], ["reserved", reserved]]) {
    const segment = element("span", `disk-${kind}`);
    segment.style.width = `${100 * value / total}%`;
    segment.setAttribute("aria-hidden", "true");
    bar.append(segment);
    if (kind !== "reserved" || value > 0) {
      legend.append(element("span", `disk-${kind}`, `${bytes(value)} ${kind}`));
    }
  }
  legend.append(element("span", "disk-total", `${bytes(total)} total`));
  row.append(bar);
  row.append(legend);
  return row;
}

async function refresh() {
  if (busy) return;
  busy = true;
  byId("refresh").disabled = true;
  byId("message").textContent = "Sampling statistics…";
  byId("message").dataset.error = "false";
  try {
    const response = await fetch("/api/metrics", {
      cache: "no-store",
      signal: AbortSignal.timeout(22000),
    });
    const reply = await response.json();
    if (!response.ok || !reply.ok) throw new Error(reply.error || "Statistics unavailable.");
    const snapshot = reply.result;
    const host = snapshot.host;
    const specs = host?.specs;
    byId("machine-model").textContent = specs?.machine ?? "—";
    byId("cpu-model").textContent = specs?.cpu_model ?? "—";
    byId("cpu-topology").textContent = `${specs?.cores ?? "—"} / ${specs?.threads ?? "—"}`;
    byId("ram-total").textContent = host ? bytes(host.memory_total_bytes) : "—";
    byId("cpu").textContent = host ? `${host.cpu_percent.toFixed(1)}%` : "—";
    byId("memory").textContent = host ? `${bytes(host.memory_used_bytes)} / ${bytes(host.memory_total_bytes)}` : "—";
    if (host && host.memory_total_bytes > 0) {
      const percent = 100 * host.memory_used_bytes / host.memory_total_bytes;
      byId("memory").textContent += ` · ${percent.toFixed(1)}%`;
      appendMeter(byId("memory"), percent, "Host memory");
    }
    byId("uptime").textContent = host ? `${Math.floor(host.uptime_seconds / 86400)}d ${Math.floor(host.uptime_seconds / 3600) % 24}h ${Math.floor(host.uptime_seconds / 60) % 60}m` : "—";
    byId("load").textContent = host ? host.load_average.map((v) => v.toFixed(2)).join(" / ") : "—";
    byId("disks").replaceChildren(...(snapshot.disks ?? []).map(diskRow));
    const rows = snapshot.services.map((service) => {
      const row = document.createElement("tr");
      for (const value of [service.name, service.status || service.state, service.cpu,
        service.memory, service.network, service.block_io, service.pids]) {
        const cell = document.createElement("td");
        cell.textContent = value ?? "—";
        row.append(cell);
      }
      return row;
    });
    byId("services").replaceChildren(...rows);
    const running = snapshot.services.filter((s) => s.state === "running").length;
    byId("service-summary").textContent = snapshot.services.length
      ? `${running} running / ${snapshot.services.length} containers`
      : "No Compose containers returned. Check warnings if Docker is unavailable.";
    byId("message").textContent = `Sampled ${new Date(snapshot.sampled_at).toLocaleString()}. ${snapshot.warnings.join(" ")}`;
    byId("message").dataset.error = String(snapshot.warnings.length > 0);
    hasSnapshot = true;
  } catch (error) {
    byId("message").textContent = `${error.message} ${hasSnapshot ? "Previous snapshot retained; it may be stale." : "No snapshot loaded."}`;
    byId("message").dataset.error = "true";
  } finally {
    busy = false;
    byId("refresh").disabled = false;
  }
}

byId("refresh").addEventListener("click", refresh);
refresh();
