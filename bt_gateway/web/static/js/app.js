/**
 * BT Gateway — real-time status updates via Socket.IO
 */

const socket = io();

// ── Socket.IO event handlers ────────────────────────────────────────────────

socket.on('connect', function() {
    console.log('Socket.IO connected');
});

socket.on('status_update', function(data) {
    updateDashboard(data);
});

socket.on('plc_status', function(data) {
    updatePLCBadge(data.status);
});

socket.on('device_connected', function(data) {
    addLogEntry('in', data.name + ' connected');
    fetchStatus();
});

socket.on('device_disconnected', function(data) {
    addLogEntry('out', data.name + ' disconnected');
    fetchStatus();
});

socket.on('message_log', function(data) {
    const dir = data.direction === 'device_to_plc' ? 'in' : 'out';
    const label = data.direction === 'device_to_plc'
        ? data.device + ' -> PLC'
        : 'PLC -> ' + data.device;
    addLogEntry(dir, label + ': ' + data.preview);
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
        if (addrEl) addrEl.textContent = data.plc.address || 'Not configured';
        if (adapterEl) adapterEl.textContent = data.plc.adapter || 'Not configured';
    }

    // Devices table
    const tbody = document.getElementById('devices-tbody');
    if (!tbody) return;

    const devices = data.devices || {};
    const entries = Object.entries(devices);

    if (!entries.length) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-muted text-center py-4">' +
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
        return '<tr>' +
            '<td><span class="status-dot ' + statusClass + '"></span>' + statusText + '</td>' +
            '<td>' + escapeHtml(info.name) + '</td>' +
            '<td class="font-monospace">' + escapeHtml(addr) + '</td>' +
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
        else if (status === 'not_configured') card.classList.add('border-secondary');
        else card.classList.add('border-danger');
    }
}

function updateDeviceCount(count) {
    const el = document.getElementById('device-count');
    if (el) el.textContent = count;
}

// ── Message log ─────────────────────────────────────────────────────────────

function addLogEntry(direction, text) {
    const log = document.getElementById('message-log');
    if (!log) return;

    // Remove "waiting" placeholder
    const placeholder = log.querySelector('.text-muted');
    if (placeholder && placeholder.textContent.startsWith('Waiting')) {
        placeholder.remove();
    }

    const now = new Date();
    const time = now.toLocaleTimeString();
    const dirClass = direction === 'in' ? 'log-dir-in' : 'log-dir-out';
    const arrow = direction === 'in' ? '>>' : '<<';

    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = '<span class="log-time">[' + time + ']</span> ' +
        '<span class="' + dirClass + '">' + arrow + '</span> ' +
        escapeHtml(text);

    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;

    // Keep last 200 entries
    while (log.children.length > 200) {
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
    };
    return map[status] || status;
}

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}
