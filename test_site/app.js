// =============================================================
// Task Manager App — with intentional dead code for testing
// =============================================================

let tasks = [];
let currentFilter = 'all';

// ── LIVE CODE: Core task management ─────────────────────────

function addTask() {
    const input = document.getElementById('taskInput');
    const text = input.value.trim();
    if (!text) return;
    tasks.push({ id: Date.now(), text, done: false });
    input.value = '';
    renderTasks();
}

function toggleTask(id) {
    const task = tasks.find(t => t.id === id);
    if (task) task.done = !task.done;
    renderTasks();
}

function deleteTask(id) {
    tasks = tasks.filter(t => t.id !== id);
    renderTasks();
}

function setFilter(filter, btn) {
    currentFilter = filter;
    document.querySelectorAll('.filters button').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    renderTasks();
}

function renderTasks() {
    const list = document.getElementById('taskList');
    const filtered = tasks.filter(t => {
        if (currentFilter === 'active') return !t.done;
        if (currentFilter === 'done') return t.done;
        return true;
    });
    list.innerHTML = filtered.map(t => `
        <li class="task-item ${t.done ? 'done' : ''}">
            <input type="checkbox" ${t.done ? 'checked' : ''} onchange="toggleTask(${t.id})"/>
            <span>${escapeHtml(t.text)}</span>
            <button onclick="deleteTask(${t.id})">&times;</button>
        </li>
    `).join('');
    updateStats();
}

function updateStats() {
    const total = tasks.length;
    const done = tasks.filter(t => t.done).length;
    document.getElementById('stats').textContent =
        `${total} tasks total, ${done} done, ${total - done} remaining`;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ── LIVE CODE: Modal ────────────────────────────────────────

function openModal() {
    document.getElementById('modalOverlay').style.display = 'block';
    document.getElementById('aboutModal').style.display = 'block';
}

function closeModal() {
    document.getElementById('modalOverlay').style.display = 'none';
    document.getElementById('aboutModal').style.display = 'none';
}

// ── DEAD CODE: Never called anywhere ────────────────────────

function exportTasksToCSV() {
    // This function is never called from the UI
    const header = 'ID,Text,Done\n';
    const rows = tasks.map(t => `${t.id},"${t.text}",${t.done}`).join('\n');
    const blob = new Blob([header + rows], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'tasks.csv';
    a.click();
    URL.revokeObjectURL(url);
}

function importTasksFromJSON(jsonString) {
    // This function is never called from the UI
    try {
        const imported = JSON.parse(jsonString);
        if (Array.isArray(imported)) {
            tasks = imported.map(t => ({
                id: t.id || Date.now() + Math.random(),
                text: String(t.text || ''),
                done: Boolean(t.done),
            }));
            renderTasks();
        }
    } catch (e) {
        console.error('Failed to import tasks:', e);
    }
}

function sortTasksByPriority(priorityMap) {
    // This function is never called — sorting feature was cut
    tasks.sort((a, b) => {
        const pa = priorityMap[a.id] || 0;
        const pb = priorityMap[b.id] || 0;
        return pb - pa;
    });
    renderTasks();
}

function bulkMarkDone(ids) {
    // This function is never called from the UI
    ids.forEach(id => {
        const task = tasks.find(t => t.id === id);
        if (task) task.done = true;
    });
    renderTasks();
}

function formatTaskAge(task) {
    // This utility is never called
    const ms = Date.now() - task.id;
    const hours = Math.floor(ms / 3600000);
    const minutes = Math.floor((ms % 3600000) / 60000);
    if (hours > 0) return `${hours}h ${minutes}m ago`;
    return `${minutes}m ago`;
}

function generateTaskReport() {
    // Never called — reporting feature was abandoned
    const report = {
        totalTasks: tasks.length,
        completed: tasks.filter(t => t.done).length,
        pending: tasks.filter(t => !t.done).length,
        averageTextLength: tasks.reduce((sum, t) => sum + t.text.length, 0) / (tasks.length || 1),
        createdDates: tasks.map(t => new Date(t.id).toISOString()),
    };
    return JSON.stringify(report, null, 2);
}

function clearAllCompletedWithUndo() {
    // Never called — undo feature was scrapped
    const removed = tasks.filter(t => t.done);
    tasks = tasks.filter(t => !t.done);
    renderTasks();

    // Store for undo
    const undoTimeout = setTimeout(() => {
        removed.length = 0;
    }, 10000);

    window._undoData = { removed, undoTimeout };
}

function undoClearCompleted() {
    // Never called — companion to clearAllCompletedWithUndo
    if (window._undoData && window._undoData.removed.length > 0) {
        tasks = tasks.concat(window._undoData.removed);
        clearTimeout(window._undoData.undoTimeout);
        window._undoData = null;
        renderTasks();
    }
}

function searchTasks(query) {
    // Never called — search feature was planned but not wired up
    const lowerQuery = query.toLowerCase();
    return tasks.filter(t => t.text.toLowerCase().includes(lowerQuery));
}

function renderSearchResults(query) {
    // Never called — companion to searchTasks
    const results = searchTasks(query);
    const list = document.getElementById('taskList');
    list.innerHTML = results.map(t => `
        <li class="task-item">
            <span>${escapeHtml(t.text)}</span>
        </li>
    `).join('');
}

// ── DEAD CODE: Unused utility class ─────────────────────────

class TaskAnalytics {
    constructor() {
        this.events = [];
    }

    trackEvent(eventName, data) {
        this.events.push({
            event: eventName,
            data: data,
            timestamp: Date.now(),
        });
    }

    getEventCount(eventName) {
        return this.events.filter(e => e.event === eventName).length;
    }

    getAverageTimeBetweenEvents(eventName) {
        const filtered = this.events
            .filter(e => e.event === eventName)
            .sort((a, b) => a.timestamp - b.timestamp);
        if (filtered.length < 2) return 0;
        let totalDiff = 0;
        for (let i = 1; i < filtered.length; i++) {
            totalDiff += filtered[i].timestamp - filtered[i - 1].timestamp;
        }
        return totalDiff / (filtered.length - 1);
    }

    exportEvents() {
        return JSON.stringify(this.events, null, 2);
    }

    clearEvents() {
        this.events = [];
    }
}

// ── DEAD CODE: Unused drag-and-drop feature ─────────────────

function enableDragAndDrop() {
    // Never called — drag/drop feature was abandoned
    const items = document.querySelectorAll('.task-item');
    items.forEach(item => {
        item.setAttribute('draggable', true);
        item.addEventListener('dragstart', handleDragStart);
        item.addEventListener('dragover', handleDragOver);
        item.addEventListener('drop', handleDrop);
        item.addEventListener('dragend', handleDragEnd);
    });
}

function handleDragStart(e) {
    e.dataTransfer.setData('text/plain', e.target.dataset.id);
    e.target.style.opacity = '0.4';
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
}

function handleDrop(e) {
    e.preventDefault();
    const draggedId = parseInt(e.dataTransfer.getData('text/plain'));
    const targetId = parseInt(e.target.closest('.task-item').dataset.id);
    const dragIdx = tasks.findIndex(t => t.id === draggedId);
    const targetIdx = tasks.findIndex(t => t.id === targetId);
    if (dragIdx !== -1 && targetIdx !== -1) {
        const [removed] = tasks.splice(dragIdx, 1);
        tasks.splice(targetIdx, 0, removed);
        renderTasks();
    }
}

function handleDragEnd(e) {
    e.target.style.opacity = '1';
}

// ── Init ────────────────────────────────────────────────────

renderTasks();
