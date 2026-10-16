/* Personal Assistant — frontend (no framework, no build step).
 *
 * - Talks to the API on the same origin with relative URLs, so it works from any device.
 * - Sends the device's IANA timezone so "today" is computed for the user, not the server.
 * - All user-provided text goes through textContent (never innerHTML) to avoid XSS.
 * - Registers a service worker for installability and offline read access.
 */
(() => {
  'use strict';

  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const $ = (sel) => document.querySelector(sel);
  /** Unambiguous access to a form control by name (field(form, 'title') would collide with the title attribute). */
  const field = (form, name) => form.elements.namedItem(name);

  const state = {
    tasks: [],
    summary: null,
    filterStatus: '',
    search: '',
    editingTask: null,
    editingEvent: null,
  };

  // ---------- API ----------

  async function api(path, options = {}) {
    const headers = { 'X-Timezone': TZ, ...(options.headers || {}) };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, { ...options, headers, body: options.body !== undefined ? JSON.stringify(options.body) : undefined });
    if (res.status === 204) return null;
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON body */ }
    if (!res.ok) {
      const err = new Error(describeError(data, res.status));
      err.status = res.status;
      err.offline = res.status === 503 && data && data.offline;
      throw err;
    }
    return data;
  }

  function describeError(data, status) {
    if (data && data.detail) {
      if (typeof data.detail === 'string') return data.detail;
      if (Array.isArray(data.detail)) {
        return data.detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`.replace(/^: /, '')).join('; ');
      }
    }
    return status === 503 ? 'Server unavailable' : `Request failed (${status})`;
  }

  // ---------- Formatting ----------

  const pad = (n) => String(n).padStart(2, '0');

  /** ISO (UTC) → value for <input type="datetime-local"> in the device's local time. */
  function isoToLocalInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  /** datetime-local value (device local time) → ISO UTC for the API. */
  function localInputToIso(value) {
    return value ? new Date(value).toISOString() : null;
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' });
  const dateFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  const dateTimeFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });

  function formatDue(iso) {
    const d = new Date(iso);
    const now = new Date();
    const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
    const label = sameDay(d, now) ? `Today ${timeFmt.format(d)}`
      : sameDay(d, tomorrow) ? `Tomorrow ${timeFmt.format(d)}`
      : dateTimeFmt.format(d);
    const cls = d < now ? 'overdue' : sameDay(d, now) ? 'today' : '';
    return { label, cls };
  }

  function formatEventTime(startIso, endIso) {
    const s = new Date(startIso), e = new Date(endIso);
    return sameDay(s, e) ? `${timeFmt.format(s)} – ${timeFmt.format(e)}` : `${dateTimeFmt.format(s)} – ${dateTimeFmt.format(e)}`;
  }

  // ---------- DOM helpers ----------

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const CHECK_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>';

  let toastTimer;
  function toast(message, isError = false) {
    const t = $('#toast');
    t.textContent = message;
    t.className = `toast show${isError ? ' error' : ''}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = 'toast'; }, isError ? 5000 : 2500);
  }

  function setBusy(button, busy) {
    button.disabled = busy;
  }

  // ---------- Rendering ----------

  function renderSummary() {
    const s = state.summary;
    $('#stat-open').textContent = s ? s.total_open_tasks : '–';
    $('#stat-due').textContent = s ? s.tasks_due_today.length : '–';
    $('#stat-overdue').textContent = s ? s.overdue_tasks.length : '–';
    $('#stat-done').textContent = s ? s.completed_today : '–';

    const events = $('#event-list');
    events.replaceChildren();
    const list = s ? s.upcoming_events : [];
    $('#events-empty').hidden = list.length > 0;
    for (const ev of list) {
      events.append(el('div', { class: 'item' }, [
        el('button', { type: 'button', onclick: () => openEventDialog(ev), 'aria-label': `Edit event ${ev.title}` }, [
          el('span', { class: 'title', text: ev.title }),
          el('span', { class: 'sub', text: formatEventTime(ev.start_time, ev.end_time) + (ev.location ? ` · ${ev.location}` : '') }),
        ]),
      ]));
    }

    const overdue = $('#overdue-list');
    overdue.replaceChildren();
    const overdueTasks = s ? s.overdue_tasks : [];
    $('#overdue-card').hidden = overdueTasks.length === 0;
    for (const t of overdueTasks) {
      overdue.append(el('div', { class: 'item' }, [
        el('button', { type: 'button', onclick: () => openTaskDialog(t), 'aria-label': `Edit task ${t.title}` }, [
          el('span', { class: 'title', text: t.title }),
          el('span', { class: 'sub', text: `Due ${formatDue(t.due_date).label}` }),
        ]),
      ]));
    }
  }

  function renderTasks() {
    const list = $('#task-list');
    list.replaceChildren();
    $('#tasks-empty').hidden = state.tasks.length > 0;
    $('#tasks-empty').textContent = state.search || state.filterStatus ? 'No tasks match.' : 'No tasks yet. Tap + to add one.';

    for (const task of state.tasks) {
      const done = task.status === 'done';
      const check = el('button', {
        class: 'check', type: 'button', 'aria-pressed': String(done),
        'aria-label': done ? `Mark "${task.title}" as not done` : `Mark "${task.title}" as done`,
        onclick: () => toggleTask(task),
      }, [el('span', { class: 'box' })]);
      check.querySelector('.box').innerHTML = CHECK_SVG; // static markup, no user data

      const meta = el('span', { class: 'task-meta' }, [
        el('span', { class: `badge ${task.priority}`, text: task.priority }),
        task.status === 'in_progress' && el('span', { class: 'badge in_progress', text: 'in progress' }),
        ...(task.tags || []).map((tag) => el('span', { class: 'chip', text: tag })),
      ]);
      if (task.due_date && !done) {
        const due = formatDue(task.due_date);
        meta.append(el('span', { class: `due ${due.cls}`, text: `Due ${due.label}` }));
      }

      const main = el('button', { class: 'task-main', type: 'button', onclick: () => openTaskDialog(task), 'aria-label': `Edit task ${task.title}` }, [
        el('span', { class: 'task-title', text: task.title }),
        task.description && el('span', { class: 'task-desc', text: task.description }),
        meta,
      ]);

      list.append(el('li', { class: `task${done ? ' done' : ''}`, 'data-id': task.id }, [check, main]));
    }
  }

  // ---------- Data loading ----------

  let loadSeq = 0;
  async function loadAll() {
    const seq = ++loadSeq;
    const params = new URLSearchParams();
    if (state.filterStatus) params.set('status', state.filterStatus);
    if (state.search) params.set('q', state.search);
    const tasksUrl = `/tasks${params.toString() ? `?${params}` : ''}`;

    const [summaryRes, tasksRes] = await Promise.allSettled([
      api(`/today?tz=${encodeURIComponent(TZ)}`),
      api(tasksUrl),
    ]);
    if (seq !== loadSeq) return; // a newer load superseded this one

    if (summaryRes.status === 'fulfilled') state.summary = summaryRes.value;
    if (tasksRes.status === 'fulfilled') state.tasks = tasksRes.value;
    renderSummary();
    renderTasks();

    const failure = [summaryRes, tasksRes].find((r) => r.status === 'rejected');
    if (failure && !failure.reason.offline) toast(failure.reason.message, true);
    updateOfflineBanner(failure && failure.reason.offline);
  }

  function updateOfflineBanner(forceOffline) {
    $('#banner-offline').hidden = !(forceOffline || !navigator.onLine);
  }

  // ---------- Task actions ----------

  async function toggleTask(task) {
    const newStatus = task.status === 'done' ? 'todo' : 'done';
    // Optimistic update for a snappy feel on phones.
    const prev = task.status;
    task.status = newStatus;
    renderTasks();
    try {
      await api(`/tasks/${task.id}`, { method: 'PATCH', body: { status: newStatus } });
      await loadAll();
    } catch (e) {
      task.status = prev;
      renderTasks();
      toast(e.message, true);
    }
  }

  function openTaskDialog(task = null) {
    state.editingTask = task;
    const form = $('#task-form');
    form.reset();
    $('#task-dialog-title').textContent = task ? 'Edit task' : 'New task';
    $('#task-delete').hidden = !task;
    if (task) {
      field(form, 'title').value = task.title;
      field(form, 'description').value = task.description || '';
      field(form, 'priority').value = task.priority;
      field(form, 'status').value = task.status;
      field(form, 'due_date').value = isoToLocalInput(task.due_date);
      field(form, 'tags').value = (task.tags || []).join(', ');
    }
    $('#task-dialog').showModal();
    if (!isCoarsePointer()) field(form, 'title').focus();
  }

  async function saveTask(event) {
    event.preventDefault();
    const form = event.target;
    const body = {
      title: field(form, 'title').value.trim(),
      description: field(form, 'description').value.trim() || null,
      priority: field(form, 'priority').value,
      status: field(form, 'status').value,
      due_date: localInputToIso(field(form, 'due_date').value),
      tags: field(form, 'tags').value.split(',').map((t) => t.trim()).filter(Boolean),
    };
    const button = $('#task-save');
    setBusy(button, true);
    try {
      if (state.editingTask) {
        await api(`/tasks/${state.editingTask.id}`, { method: 'PATCH', body });
        toast('Task updated');
      } else {
        await api('/tasks', { method: 'POST', body });
        toast('Task added');
      }
      $('#task-dialog').close();
      await loadAll();
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(button, false);
    }
  }

  async function deleteTask() {
    if (!state.editingTask || !confirm(`Delete "${state.editingTask.title}"?`)) return;
    try {
      await api(`/tasks/${state.editingTask.id}`, { method: 'DELETE' });
      $('#task-dialog').close();
      toast('Task deleted');
      await loadAll();
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------- Event actions ----------

  function openEventDialog(ev = null) {
    state.editingEvent = ev;
    const form = $('#event-form');
    form.reset();
    $('#event-dialog-title').textContent = ev ? 'Edit event' : 'New event';
    $('#event-delete').hidden = !ev;
    if (ev) {
      field(form, 'title').value = ev.title;
      field(form, 'start_time').value = isoToLocalInput(ev.start_time);
      field(form, 'end_time').value = isoToLocalInput(ev.end_time);
      field(form, 'location').value = ev.location || '';
      field(form, 'description').value = ev.description || '';
    } else {
      const start = new Date(); start.setMinutes(0, 0, 0); start.setHours(start.getHours() + 1);
      const end = new Date(start); end.setHours(end.getHours() + 1);
      field(form, 'start_time').value = isoToLocalInput(start.toISOString());
      field(form, 'end_time').value = isoToLocalInput(end.toISOString());
    }
    $('#event-dialog').showModal();
    if (!isCoarsePointer()) field(form, 'title').focus();
  }

  async function saveEvent(event) {
    event.preventDefault();
    const form = event.target;
    const body = {
      title: field(form, 'title').value.trim(),
      start_time: localInputToIso(field(form, 'start_time').value),
      end_time: localInputToIso(field(form, 'end_time').value),
      location: field(form, 'location').value.trim() || null,
      description: field(form, 'description').value.trim() || null,
    };
    if (body.end_time <= body.start_time) { toast('End must be after start', true); return; }
    const button = $('#event-save');
    setBusy(button, true);
    try {
      if (state.editingEvent) {
        await api(`/events/${state.editingEvent.id}`, { method: 'PATCH', body });
        toast('Event updated');
      } else {
        await api('/events', { method: 'POST', body });
        toast('Event added');
      }
      $('#event-dialog').close();
      await loadAll();
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(button, false);
    }
  }

  async function deleteEvent() {
    if (!state.editingEvent || !confirm(`Delete "${state.editingEvent.title}"?`)) return;
    try {
      await api(`/events/${state.editingEvent.id}`, { method: 'DELETE' });
      $('#event-dialog').close();
      toast('Event deleted');
      await loadAll();
    } catch (e) {
      toast(e.message, true);
    }
  }

  // Auto-adjust end time when start changes (keeps duration).
  function keepDuration() {
    const form = $('#event-form');
    const start = new Date(field(form, 'start_time').value), end = new Date(field(form, 'end_time').value);
    if (!isNaN(start) && (isNaN(end) || end <= start)) {
      const newEnd = new Date(start); newEnd.setHours(newEnd.getHours() + 1);
      field(form, 'end_time').value = isoToLocalInput(newEnd.toISOString());
    }
  }

  // ---------- Service worker / PWA ----------

  function registerServiceWorker() {
    if (!('serviceWorker' in navigator) || location.protocol === 'file:') return;
    navigator.serviceWorker.register('/sw.js').then((reg) => {
      const showUpdate = (worker) => {
        $('#banner-update').hidden = false;
        $('#btn-update').onclick = () => worker.postMessage({ type: 'SKIP_WAITING' });
      };
      if (reg.waiting && navigator.serviceWorker.controller) showUpdate(reg.waiting);
      reg.addEventListener('updatefound', () => {
        const worker = reg.installing;
        worker && worker.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) showUpdate(worker);
        });
      });
    }).catch(() => { /* offline-first is a progressive enhancement */ });

    let refreshing = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (refreshing) return;
      refreshing = true;
      location.reload();
    });
  }

  // ---------- Misc ----------

  function isCoarsePointer() {
    return window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  function isTyping() {
    const a = document.activeElement;
    return a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT' || a.isContentEditable);
  }

  // ---------- Init ----------

  function init() {
    $('#today-date').textContent = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });

    $('#btn-new-task').addEventListener('click', () => openTaskDialog());
    $('#fab-new-task').addEventListener('click', () => openTaskDialog());
    $('#btn-new-event').addEventListener('click', () => openEventDialog());
    $('#btn-new-event-side').addEventListener('click', () => openEventDialog());
    $('#task-form').addEventListener('submit', saveTask);
    $('#task-delete').addEventListener('click', deleteTask);
    $('#event-form').addEventListener('submit', saveEvent);
    $('#event-delete').addEventListener('click', deleteEvent);
    $('#event-start').addEventListener('change', keepDuration);
    document.querySelectorAll('dialog [data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));
    document.querySelectorAll('dialog').forEach((d) => d.addEventListener('click', (e) => { if (e.target === d) d.close(); }));

    $('#filter-status').addEventListener('change', (e) => { state.filterStatus = e.target.value; loadAll(); });
    $('#search').addEventListener('input', debounce((e) => { state.search = e.target.value.trim(); loadAll(); }, 250));

    // Keyboard shortcuts on desktop: N = new task, E = new event, / = search.
    document.addEventListener('keydown', (e) => {
      if (isTyping() || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
      if (e.key === 'n') { e.preventDefault(); openTaskDialog(); }
      else if (e.key === 'e') { e.preventDefault(); openEventDialog(); }
      else if (e.key === '/') { e.preventDefault(); $('#search').focus(); }
    });

    window.addEventListener('online', () => { updateOfflineBanner(false); loadAll(); });
    window.addEventListener('offline', () => updateOfflineBanner(true));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) loadAll(); });

    // Launched via the PWA shortcut "New task".
    if (new URLSearchParams(location.search).get('new') === 'task') {
      history.replaceState(null, '', '/');
      setTimeout(() => openTaskDialog(), 50);
    }

    registerServiceWorker();
    loadAll();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
