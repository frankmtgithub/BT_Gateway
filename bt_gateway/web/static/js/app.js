/**
 * BT Gateway — real-time status updates via Socket.IO
 */

const socket = io();

// Known connection names we have seen in log events.  Used to populate the
// filter dropdown on the dashboard.
const knownConnections = new Set();

// ── Socket.IO event handlers ────────────────────────────────────────────────

socket.on('connect', function() {
    console.log('Socket.IO connected');
});

socket.on('status_update', function(data) {
    updateDashboard(data);
});

socket.on('plc_status', function(data) {
    updatePLCBadge(data.status);
    if (data.address !== undefined) {
        const el = document.getElementById('plc-address');
        if (el) el.textContent = data.address || 'Not paired';
    }
});

socket.on('device_connected', function(data) {
    addLogEntry({
        direction: 'system',
        device: data.name,
        address: data.address,
        message: 'connected',
    });
    fetchStatus();
});

socket.on('device_disconnected', function(data) {
    addLogEntry({
        direction: 'system',
        device: data.name,
        address: data.address,
        message: 'disconnected',
    });
    fetchStatus();
});

socket.on('message_log', function(data) {
    addLogEntry(data);
});

socket.on('debug_log', function(data) {
    addDebugEntry(data);
});

// ── Dashboard updater ───────────────────────────────────────────────────────

function fetchStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => updateDashboard(data))
        .catch(err => console.error('Status fetch failed:', err));
}

function updateDashboard(data) {
    // PLC
    if (data.plc) {
        updatePLCBadge(data.plc.status);

        const addrEl = document.getElementById('plc-address');
        const adapterEl = document.getElementById('plc-adapter');
        const portEl = document.getElementById('plc-port');
        if (addrEl) addrEl.textContent = data.plc.address || 'Not paired';
        if (adapterEl) adapterEl.textContent = data.plc.adapter || 'Not configured';
        if (portEl) portEl.textContent = data.plc.port != null
            ? '/dev/rfcomm' + data.plc.port : '--';
    }

    if (typeof data.debug_mode === 'boolean') {
        const sw = document.getElementById('debug-mode-switch');
        if (sw) sw.checked = data.debug_mode;
    }

    // Devices table (dashboard)
    const tbody = document.getElementById('devices-tbody');
    if (!tbody) return;

    const devices = data.devices || {};
    const entries = Object.entries(devices);

    // Keep filter dropdown fresh
    entries.forEach(([, info]) => knownConnections.add(info.name));
    refreshLogFilter();

    if (!entries.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center py-4">' +
            'No devices configured. Go to <a href="/pairing">Pairing</a> to add devices.' +
            '</td></tr>';
        updateDeviceCount(0);
        return;
    }

    let connectedCount = 0;
    tbody.innerHTML = entries.map(function([addr, info]) {
        const connected = info.connected;
        if (connected) connectedCount++;
        const statusClass = connected ? 'connected' : 'disconnected';
        const statusText = connected ? 'Connected' : 'Disconnected';
        const port = info.port != null ? '/dev/rfcomm' + info.port : '(none)';
        return '<tr>' +
            '<td><span class="status-dot ' + statusClass + '"></span>' + statusText + '</td>' +
            '<td>' + escapeHtml(info.name) + '</td>' +
            '<td class="font-monospace">' + escapeHtml(addr) + '</td>' +
            '<td class="font-monospace">' + escapeHtml(port) + '</td>' +
            '</tr>';
    }).join('');

    updateDeviceCount(connectedCount);
}

function updatePLCBadge(status) {
    // Nav badge
    const navBadge = document.getElementById('nav-plc-status');
    if (navBadge) {
        navBadge.textContent = formatStatus(status);
        navBadge.className = 'badge badge-' + status;
    }

    // Dashboard elements
    const statusBadge = document.getElementById('plc-status-badge');
    const statusText = document.getElementById('plc-status-text');
    const card = document.getElementById('plc-card');

    if (statusBadge) {
        statusBadge.textContent = formatStatus(status);
        statusBadge.className = 'badge fs-6 badge-' + status;
    }
    if (statusText) {
        statusText.innerHTML = '<span class="status-dot ' + status + '"></span>' + formatStatus(status);
    }
    if (card) {
        card.className = 'card';
        if (status === 'connected') card.classList.add('border-success');
        else if (status === 'connecting') card.classList.add('border-warning');
        else if (status === 'not_configured' || status === 'not_paired') card.classList.add('border-secondary');
        else card.classList.add('border-danger');
    }
}

function updateDeviceCount(count) {
    const el = document.getElementById('device-count');
    if (el) el.textContent = count;
}

// ── Message log ─────────────────────────────────────────────────────────────

function refreshLogFilter() {
    const sel = document.getElementById('log-filter');
    if (!sel) return;
    const current = sel.value;
    const names = ['', 'PLC', ...Array.from(knownConnections).sort()];
    sel.innerHTML = names.map(n =>
        `<option value="${escapeAttr(n)}">${n === '' ? 'All connections' : escapeHtml(n)}</option>`
    ).join('');
    sel.value = current || '';
}

function currentLogFilter() {
    const sel = document.getElementById('log-filter');
    return sel ? sel.value : '';
}

function addLogEntry(data) {
    const log = document.getElementById('message-log');
    if (!log) return;

    // Remove "waiting" placeholder
    const placeholder = log.querySelector('.text-muted');
    if (placeholder && (placeholder.textContent.startsWith('Waiting') ||
                        placeholder.textContent.startsWith('Log cleared'))) {
        placeholder.remove();
    }

    const direction = data.direction;
    const device = data.device || 'unknown';
    const message = data.message != null ? String(data.message) : '';
    const delivered = data.delivered !== false;
    const error = data.error || null;

    // Track known connections for the filter dropdown
    if (device && device !== 'PLC') knownConnections.add(device);
    refreshLogFilter();

    // Honour current filter
    const filter = currentLogFilter();
    if (filter) {
        const allowed = (direction === 'plc_to_device')
            ? (filter === 'PLC' || filter === device)
            : (filter === 'PLC' ? device === 'PLC' : filter === device);
        if (!allowed) return;
    }

    let arrow, dirClass, label;
    if (direction === 'device_to_plc') {
        arrow = '&gt;&gt;'; dirClass = 'log-dir-in';
        label = device + ' → PLC';
    } else if (direction === 'plc_to_device') {
        arrow = '&lt;&lt;'; dirClass = 'log-dir-out';
        label = 'PLC → ' + device;
    } else {
        arrow = '··'; dirClass = 'log-dir-sys';
        label = device;
    }

    const now = new Date();
    const time = now.toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    let badge = '';
    if (!delivered) {
        badge = ' <span class="badge bg-warning text-dark">undelivered' +
            (error ? ' (' + escapeHtml(error) + ')' : '') + '</span>';
    }
    entry.innerHTML = '<span class="log-time">[' + time + ']</span> ' +
        '<span class="' + dirClass + '">' + arrow + '</span> ' +
        '<span class="log-label">' + escapeHtml(label) + '</span>' + badge + ' ' +
        '<span class="log-msg">' + escapeHtml(message) + '</span>';

    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;

    // Keep last 500 entries
    while (log.children.length > 500) {
        log.removeChild(log.firstChild);
    }
}

function addDebugEntry(data) {
    const log = document.getElementById('message-log');
    if (!log) return;

    const placeholder = log.querySelector('.text-muted');
    if (placeholder && (placeholder.textContent.startsWith('Waiting') ||
                        placeholder.textContent.startsWith('Log cleared'))) {
        placeholder.remove();
    }

    const name = data.name || 'unknown';
    const address = data.address || '';
    const raw = data.raw != null ? String(data.raw) : '';

    if (name && name !== 'PLC') knownConnections.add(name);
    refreshLogFilter();

    const filter = currentLogFilter();
    if (filter && filter !== name) return;

    const now = new Date();
    const time = now.toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = 'log-entry debug';
    const label = 'DEBUG ' + (data.source === 'plc' ? 'PLC' : name) +
        (address ? ' (' + address + ')' : '');
    entry.innerHTML = '<span class="log-time">[' + time + ']</span> ' +
        '<span class="log-dir-debug">RX</span> ' +
        '<span class="log-label">' + escapeHtml(label) + '</span> ' +
        '<span class="log-msg">' + escapeHtml(raw) + '</span>';

    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;

    while (log.children.length > 500) {
        log.removeChild(log.firstChild);
    }
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function formatStatus(status) {
    const map = {
        'connected': 'Connected',
        'disconnected': 'Disconnected',
        'connecting': 'Connecting',
        'not_configured': 'Not Configured',
        'not_paired': 'Not Paired',
    };
    return map[status] || status;
}

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}

function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, '&quot;');
}
