/* ЭФИР — логика интерфейса */

(function () {
  'use strict';

  const $ = function (id) { return document.getElementById(id); };
  const shell = $('shell');

  const state = {
    me: window.ME,
    rooms: [],
    room: null,
    myRole: null,          // admin | owner | moderator | member
    canManage: false,
    isBoss: false,
    channels: [],
    members: [],
    channel: null,
    messages: [],
    hasMore: false,
    loadingHistory: false,
    presence: {},          // channel_id -> [участники]
    locks: {},             // channel_id -> {user_id, name, role} | null
    speaking: {},
    screens: new Map(),
    tiles: new Map(),
    pending: new Map(),
    typingTimers: {},
    lastTypingSent: 0,
    channelKind: 'text',
    cardUser: null,
    stageWide: false,
    levelTimer: null,
    layout: { items: [] },   // личные папки и порядок комнат
    online: {},              // user_id -> на странице ли человек
    bans: [],                // запреты на вход в голосовые каналы комнаты
    layoutTimer: null
  };

  // Сначала HTTP, затем upgrade: работает и за прокси без WebSocket.
  const socket = io({ transports: ['polling', 'websocket'] });
  RTC.attach(socket);

  // Иконки в SVG: эмодзи микрофона и монитора на части систем
  // подменяются пустым квадратом, а тут вид одинаковый везде.
  const SVG = function (body) {
    return '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      body + '</svg>';
  };

  const ICONS = {
    mic: SVG('<rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v4"/>'),
    micOff: SVG('<rect x="9" y="2" width="6" height="11" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 17v4"/><path d="M4 3l16 18"/>'),
    screen: SVG('<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8"/>'),
    stop: SVG('<rect x="6" y="6" width="12" height="12" rx="2"/>'),
    hand: SVG('<path d="M7 11V6a1.5 1.5 0 0 1 3 0v5"/><path d="M10 11V4.5a1.5 1.5 0 0 1 3 0V11"/>' +
              '<path d="M13 11V6a1.5 1.5 0 0 1 3 0v7"/><path d="M16 10.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1a6 6 0 0 1-6-6v-2a1.5 1.5 0 0 1 3 0"/>'),
    logout: SVG('<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l-5-5 5-5"/><path d="M5 12h12"/>'),
    gear: SVG('<circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.6 1.6 0 0 0 .32 1.77l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.6 1.6 0 0 0 15 19.4a1.6 1.6 0 0 0-1 1.47V21a2 2 0 1 1-4 0v-.09A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.77.32l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.6 1.6 0 0 0 4.6 15a1.6 1.6 0 0 0-1.47-1H3a2 2 0 1 1 0-4h.09A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.32-1.77l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.6 1.6 0 0 0 9 4.6a1.6 1.6 0 0 0 1-1.47V3a2 2 0 1 1 4 0v.09A1.6 1.6 0 0 0 15 4.6a1.6 1.6 0 0 0 1.77-.32l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.6 1.6 0 0 0 19.4 9v0a1.6 1.6 0 0 0 1.47 1H21a2 2 0 1 1 0 4h-.09a1.6 1.6 0 0 0-1.47 1z"/>'),
    people: SVG('<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>'),
    folder: SVG('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
    pencil: SVG('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
    grip: SVG('<circle cx="9" cy="6" r="1.4"/><circle cx="15" cy="6" r="1.4"/><circle cx="9" cy="12" r="1.4"/>' +
              '<circle cx="15" cy="12" r="1.4"/><circle cx="9" cy="18" r="1.4"/><circle cx="15" cy="18" r="1.4"/>')
  };

  const ROLE_LABEL = {
    admin: 'админ сайта', owner: 'владелец', moderator: 'модератор', member: 'участник'
  };

  // ------------------------------------------------------------- утилиты

  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function linkify(text) {
    return esc(text).replace(/(https?:\/\/[^\s<]+)/g, function (url) {
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
    });
  }

  function timeOf(iso) {
    return new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  function dayOf(iso) {
    const d = new Date(iso);
    if (d.toDateString() === new Date().toDateString()) return 'Сегодня';
    if (d.toDateString() === new Date(Date.now() - 86400000).toDateString()) return 'Вчера';
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  function avatar(user, size) {
    const cls = size ? 'avatar avatar--' + size : 'avatar';
    if (user.avatar_url) {
      return '<img class="' + cls + '" src="' + esc(user.avatar_url) + '" alt="">';
    }
    return '<div class="' + cls + '" style="background:' + esc(user.color || '#7B929C') + '">' +
      esc(user.initials || '??') + '</div>';
  }

  function avatarWithDot(user, size) {
    const on = state.online[user.id] ? ' dot--on' : '';
    const title = state.online[user.id] ? 'в приложении' : 'не в сети';
    return '<span class="avatar-wrap">' + avatar(user, size) +
      '<i class="dot' + on + '" title="' + title + '"></i></span>';
  }

  function roleBadge(role) {
    if (!role || role === 'member') return '';
    return '<span class="role-badge role-badge--' + role + '">' + (ROLE_LABEL[role] || role) + '</span>';
  }

  function toast(text, level) {
    const el = document.createElement('div');
    el.className = 'toast' + (level ? ' toast--' + level : '');
    el.textContent = text;
    $('toasts').appendChild(el);
    setTimeout(function () { el.remove(); }, 4600);
  }

  async function api(url, options) {
    const response = await fetch(url, Object.assign({
      headers: { 'Content-Type': 'application/json' }
    }, options || {}));
    let data = {};
    try { data = await response.json(); } catch (err) { /* пустой ответ */ }
    if (!response.ok) throw new Error(data.error || 'Не получилось. Попробуйте ещё раз.');
    return data;
  }

  function remember(key, value) {
    try { localStorage.setItem('efir:' + key, String(value)); } catch (err) { /* приватный режим */ }
  }

  function recallText(key, fallback) {
    try {
      const raw = localStorage.getItem('efir:' + key);
      return raw === null ? fallback : raw;
    } catch (err) { return fallback; }
  }

  function recall(key, fallback) {
    try {
      const raw = localStorage.getItem('efir:' + key);
      return raw === null ? fallback : Number(raw);
    } catch (err) { return fallback; }
  }

  const modalFocus = new WeakMap();
  function openModal(id) {
    const modal = $(id);
    modalFocus.set(modal, document.activeElement);
    modal.classList.add('is-open');
    const sheet = modal.querySelector('.sheet');
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    const heading = sheet.querySelector('h2');
    if (heading) { heading.id = heading.id || id + '-title'; sheet.setAttribute('aria-labelledby', heading.id); }
    const focus = sheet.querySelector('input:not([type="hidden"]), button, textarea');
    if (focus) focus.focus();
  }
  function closeModal(id) {
    const modal = $(id);
    modal.classList.remove('is-open');
    const trigger = modalFocus.get(modal);
    if (trigger && trigger.isConnected) trigger.focus();
  }

  document.querySelectorAll('[data-close]').forEach(function (btn) {
    btn.addEventListener('click', function () { closeModal(btn.closest('.veil').id); });
  });

  document.querySelectorAll('.veil').forEach(function (veil) {
    veil.addEventListener('mousedown', function (event) {
      if (event.target === veil) closeModal(veil.id);
    });
  });

  document.addEventListener('keydown', function (event) {
    const modal = document.querySelector('.veil.is-open');
    if (event.key === 'Tab' && modal) {
      const items = Array.from(modal.querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]'))
        .filter(function (el) { return !el.disabled && el.getClientRects().length; });
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    if (event.key !== 'Escape') return;
    document.querySelectorAll('.veil.is-open').forEach(function (v) { closeModal(v.id); });
    closeDrawers();
  });

  // ------------------------------------------------------------- шторки на телефоне

  function closeDrawers() {
    shell.classList.remove('nav-open', 'roster-open');
    $('backdrop').classList.remove('is-on');
  }

  function toggleDrawer(name) {
    const cls = name + '-open';
    const opening = !shell.classList.contains(cls);
    closeDrawers();
    if (opening) {
      shell.classList.add(cls);
      $('backdrop').classList.add('is-on');
    }
  }

  $('btn-menu').addEventListener('click', function () { toggleDrawer('nav'); });
  $('btn-people').addEventListener('click', function () { toggleDrawer('roster'); });
  $('btn-close-nav').addEventListener('click', closeDrawers);
  $('btn-close-roster').addEventListener('click', closeDrawers);
  $('backdrop').addEventListener('click', closeDrawers);

  $('btn-people').innerHTML = ICONS.people;
  $('btn-room-settings').innerHTML = ICONS.gear;
  $('btn-logout').innerHTML = ICONS.logout;
  if ($('btn-admin')) $('btn-admin').innerHTML = ICONS.gear;

  // ------------------------------------------------------------- ввод строки

  let promptResolve = null;

  function askText(options) {
    $('prompt-eyebrow').textContent = options.eyebrow || 'Название';
    $('prompt-title').textContent = options.title || 'Введите название';
    $('prompt-label').textContent = options.label || 'Название';
    $('prompt-input').value = options.value || '';
    $('prompt-input').maxLength = options.max || 64;
    openModal('modal-prompt');
    setTimeout(function () { $('prompt-input').focus(); $('prompt-input').select(); }, 40);
    return new Promise(function (resolve) { promptResolve = resolve; });
  }

  function finishPrompt(value) {
    closeModal('modal-prompt');
    const resolve = promptResolve;
    promptResolve = null;
    if (resolve) resolve(value);
  }

  $('prompt-save').addEventListener('click', function () {
    finishPrompt($('prompt-input').value.trim());
  });

  $('prompt-input').addEventListener('keydown', function (event) {
    if (event.key === 'Enter') { event.preventDefault(); $('prompt-save').click(); }
  });

  $('modal-prompt').addEventListener('mousedown', function (event) {
    if (event.target === this) finishPrompt(null);
  });

  $('modal-prompt').querySelector('[data-close]').addEventListener('click', function () {
    finishPrompt(null);
  });

  // ------------------------------------------------------------- перетаскивание

  /* Тянем указателем, а не HTML5 drag and drop: тот на телефонах не работает
     вовсе. Жест начинается только с ручки — у неё touch-action: none, поэтому
     список остаётся прокручиваемым пальцем. */
  function enableDrag(container, options) {
    container.querySelectorAll('[data-grip]').forEach(function (grip) {
      grip.addEventListener('pointerdown', function (event) {
        if (event.button) return;
        const item = grip.closest(options.item);
        if (!item) return;
        event.preventDefault();

        const source = grip.dataset.grip;
        const startX = event.clientX;
        const startY = event.clientY;
        let ghost = null;
        let mark = null;
        let target = null;

        function paint(node, mode) {
          clear();
          if (!node) return;
          mark = node;
          node.classList.add(mode === 'into' ? 'drop-into'
            : (mode === 'before' ? 'drop-before' : 'drop-after'));
        }

        function clear() {
          if (mark) mark.classList.remove('drop-before', 'drop-after', 'drop-into');
          mark = null;
        }

        function locate(ev) {
          const under = document.elementFromPoint(ev.clientX, ev.clientY);
          const zone = under && under.closest('[data-drop]');
          if (!zone || zone === item) return null;
          const key = zone.dataset.drop;
          const box = zone.getBoundingClientRect();
          const ratio = (ev.clientY - box.top) / Math.max(box.height, 1);
          const canHold = options.accepts && options.accepts(source, key);
          const mode = canHold && ratio > 0.25 && ratio < 0.75 ? 'into'
            : (ratio < 0.5 ? 'before' : 'after');
          return { key: key, mode: mode, node: zone };
        }

        function move(ev) {
          if (!ghost) {
            if (Math.abs(ev.clientX - startX) + Math.abs(ev.clientY - startY) < 6) return;
            item.classList.add('is-dragging');
            ghost = item.cloneNode(true);
            ghost.classList.add('drag-ghost');
            ghost.classList.remove('is-dragging');
            document.body.appendChild(ghost);
          }
          ghost.style.left = ev.clientX + 'px';
          ghost.style.top = ev.clientY + 'px';
          target = locate(ev);
          paint(target && target.node, target && target.mode);
        }

        function stop() {
          document.removeEventListener('pointermove', move);
          document.removeEventListener('pointerup', stop);
          document.removeEventListener('pointercancel', stop);
          if (ghost) ghost.remove();
          item.classList.remove('is-dragging');
          clear();
          if (ghost && target) options.onDrop(source, target.key, target.mode);
        }

        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', stop);
        document.addEventListener('pointercancel', stop);
      });
    });
  }

  // ------------------------------------------------------------- я

  const PALETTE = ['#FFB020', '#3FBF8F', '#5AA9E6', '#E5484D',
                   '#B48EAD', '#E8A87C', '#7FC8A9', '#C9A227'];

  function renderMe() {
    $('me-avatar').innerHTML = avatar(state.me);
    $('me-name').textContent = state.me.display_name;
    $('me-login').textContent = '@' + state.me.username;
  }

  /* Черновик: ничего не применяется, пока не нажали «Сохранить».
     avatar === undefined — не трогали, null — убрать, строка — новая картинка. */
  let draft = { color: null, avatar: undefined };

  function draftUser() {
    return {
      color: draft.color || state.me.color,
      initials: state.me.initials,
      avatar_url: draft.avatar === undefined ? state.me.avatar_url
        : (draft.avatar === null ? null : draft.avatar)
    };
  }

  function paintProfile() {
    $('profile-avatar').innerHTML = avatar(draftUser(), 'lg');
    $('btn-avatar-drop').disabled = !draftUser().avatar_url;
  }

  function openProfile() {
    draft = { color: null, avatar: undefined };

    // гостевая учётка временная: ни аватарки, ни пароля за ней не держим
    $('avatar-tools').hidden = !!state.me.is_guest;
    $('password-block').hidden = !!state.me.is_guest;
    $('password-block').open = false;
    $('avatar-note').textContent = state.me.is_guest
      ? 'Гостям аватарки недоступны. Заведите аккаунт — он не пропадёт через неделю.'
      : 'Картинка ужимается до 256×256 и лежит файлом на сервере. Прежняя стирается сразу же.';

    $('profile-name').value = state.me.display_name;
    ['profile-current', 'profile-new', 'profile-repeat'].forEach(function (id) { $(id).value = ''; });
    $('profile-colors').innerHTML = PALETTE.map(function (color) {
      return '<button data-color="' + color + '" class="' +
        (color.toLowerCase() === (state.me.color || '').toLowerCase() ? 'is-on' : '') +
        '" style="color:' + color + '">' + color + '</button>';
    }).join('');
    $('profile-colors').querySelectorAll('[data-color]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        draft.color = btn.dataset.color;
        $('profile-colors').querySelectorAll('[data-color]').forEach(function (b) {
          b.classList.toggle('is-on', b === btn);
        });
        paintProfile();
      });
    });
    paintProfile();
    openModal('modal-profile');
  }

  $('btn-profile').addEventListener('click', openProfile);

  $('profile-save').addEventListener('click', async function () {
    const payload = {
      display_name: $('profile-name').value.trim(),
      color: draft.color || state.me.color
    };
    const fresh = $('profile-new').value;
    if (fresh) {
      if (fresh !== $('profile-repeat').value) { toast('Новые пароли не совпадают', 'error'); return; }
      payload.current_password = $('profile-current').value;
      payload.new_password = fresh;
    }

    try {
      let data = await api('/api/profile', { method: 'PATCH', body: JSON.stringify(payload) });

      if (draft.avatar === null) {
        data = await api('/api/profile/avatar', { method: 'DELETE' });
      } else if (typeof draft.avatar === 'string') {
        data = await api('/api/profile/avatar', {
          method: 'POST', body: JSON.stringify({ image: draft.avatar })
        });
      }

      state.me = Object.assign(state.me, data.user);
      draft = { color: null, avatar: undefined };
      renderMe();
      closeModal('modal-profile');
      toast(fresh ? 'Профиль и пароль обновлены' : 'Профиль обновлён', 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('btn-avatar-pick').addEventListener('click', function () { $('avatar-input').click(); });

  /* Ужимаем картинку в браузере: на сервер уходит квадрат 256×256,
     а не исходные несколько мегабайт с телефона. */
  async function squareImage(file, size) {
    const source = await (window.createImageBitmap
      ? createImageBitmap(file)
      : new Promise(function (resolve, reject) {
          const img = new Image();
          img.onload = function () { resolve(img); };
          img.onerror = reject;
          img.src = URL.createObjectURL(file);
        }));

    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext('2d');
    const scale = Math.max(size / source.width, size / source.height);
    const w = source.width * scale;
    const h = source.height * scale;
    ctx.drawImage(source, (size - w) / 2, (size - h) / 2, w, h);
    return canvas.toDataURL('image/jpeg', 0.85);
  }

  $('avatar-input').addEventListener('change', async function () {
    const file = this.files && this.files[0];
    this.value = '';
    if (!file) return;
    if (!/^image\//.test(file.type)) { toast('Это не картинка', 'error'); return; }
    try {
      draft.avatar = await squareImage(file, 256);
      paintProfile();
    } catch (err) { toast('Картинку прочитать не удалось', 'error'); }
  });

  $('btn-avatar-drop').addEventListener('click', function () {
    draft.avatar = null;   // применится по «Сохранить», отмена вернёт прежнюю
    paintProfile();
  });

  // ------------------------------------------------------------- комнаты

  async function loadRooms(selectId) {
    const data = await api('/api/rooms');
    state.rooms = data.rooms;
    if (!state.layout.items.length) await loadLayout();
    renderRooms();
    if (selectId) openRoom(selectId);
    else if (!state.room) {
      const mine = state.rooms.find(function (r) { return r.joined; });
      if (mine) openRoom(mine.id);
    }
  }

  // ---- личная раскладка: папки и порядок. Хранится за пользователем,
  // другим не показывается.

  async function loadLayout() {
    try {
      const data = await api('/api/layout');
      const items = data.layout && Array.isArray(data.layout.items) ? data.layout.items : [];
      state.layout = { items: items };
    } catch (err) {
      state.layout = { items: [] };
    }
  }

  function saveLayout() {
    clearTimeout(state.layoutTimer);
    state.layoutTimer = setTimeout(function () {
      api('/api/layout', { method: 'PUT', body: JSON.stringify({ layout: state.layout }) })
        .catch(function () { toast('Раскладку сохранить не удалось', 'error'); });
    }, 400);
  }

  function normalizeLayout() {
    const known = new Set(state.rooms.map(function (r) { return r.id; }));
    const seen = new Set();
    const items = [];

    (state.layout.items || []).forEach(function (item) {
      if (item.type === 'folder') {
        const rooms = (item.rooms || []).filter(function (id) {
          if (!known.has(id) || seen.has(id)) return false;
          seen.add(id);
          return true;
        });
        items.push({ type: 'folder', id: item.id, name: item.name || 'Папка',
                     open: item.open !== false, rooms: rooms });
      } else if (item.type === 'room' && known.has(item.id) && !seen.has(item.id)) {
        seen.add(item.id);
        items.push({ type: 'room', id: item.id });
      }
    });

    // новые комнаты попадают в конец списка
    state.rooms.forEach(function (room) {
      if (!seen.has(room.id)) items.push({ type: 'room', id: room.id });
    });

    state.layout.items = items;
  }

  function locateRoom(roomId) {
    for (let i = 0; i < state.layout.items.length; i++) {
      const item = state.layout.items[i];
      if (item.type === 'room' && item.id === roomId) return { list: state.layout.items, index: i };
      if (item.type === 'folder') {
        const j = item.rooms.indexOf(roomId);
        if (j >= 0) return { list: item.rooms, index: j, folder: item };
      }
    }
    return null;
  }

  function folderById(id) {
    return state.layout.items.find(function (it) { return it.type === 'folder' && it.id === id; });
  }

  function takeRoom(roomId) {
    const at = locateRoom(roomId);
    if (at) at.list.splice(at.index, 1);
  }

  function dropRoom(roomId, targetKey, mode) {
    const [kind, rawId] = targetKey.split(':');
    if (kind === 'folder' && mode === 'into') {
      const folder = folderById(rawId);
      if (!folder) return;
      takeRoom(roomId);
      folder.rooms.push(roomId);
      folder.open = true;
      return;
    }
    if (kind === 'folderbody') {
      const folder = folderById(rawId);
      if (!folder) return;
      takeRoom(roomId);
      folder.rooms.push(roomId);
      return;
    }
    if (kind === 'room') {
      const anchorId = parseInt(rawId, 10);
      if (anchorId === roomId) return;
      takeRoom(roomId);
      const at = locateRoom(anchorId);
      if (!at) return;
      const index = at.index + (mode === 'after' ? 1 : 0);
      if (at.folder) at.list.splice(index, 0, roomId);
      else at.list.splice(index, 0, { type: 'room', id: roomId });
      return;
    }
    if (kind === 'folder') {   // мимо папки — кладём рядом с ней на верхний уровень
      takeRoom(roomId);
      const index = state.layout.items.findIndex(function (it) {
        return it.type === 'folder' && it.id === rawId;
      });
      state.layout.items.splice(index + (mode === 'after' ? 1 : 0), 0,
                                { type: 'room', id: roomId });
    }
  }

  function dropFolder(folderId, targetKey, mode) {
    const [kind, rawId] = targetKey.split(':');
    if (kind === 'folderbody') return;   // папку в папку не вкладываем
    const from = state.layout.items.findIndex(function (it) {
      return it.type === 'folder' && it.id === folderId;
    });
    if (from < 0) return;
    const [moved] = state.layout.items.splice(from, 1);

    let to = state.layout.items.findIndex(function (it) {
      return (it.type === 'folder' && it.id === rawId) ||
             (it.type === 'room' && String(it.id) === rawId);
    });
    if (to < 0) to = state.layout.items.length - 1;
    state.layout.items.splice(to + (mode === 'after' ? 1 : 0), 0, moved);
  }

  function roomCard(room) {
    const active = state.room && state.room.id === room.id ? ' is-active' : '';
    let first = room.joined ? 'вы участник' : (room.locked ? 'по паролю' : 'открытая');
    if (!room.joined && state.me.is_guest && room.guests_allowed === false) first = 'гостям нельзя';
    return '<div class="room-item' + active + '" role="button" tabindex="0" ' +
      'data-room="' + room.id + '" data-drop="room:' + room.id + '">' +
      '<span class="grip" data-grip="room:' + room.id + '" title="Перетащить">' + ICONS.grip + '</span>' +
      '<span class="room-item__body">' +
        '<span class="room-item__top">' +
          (room.locked ? '<span class="lock" title="Нужен пароль">⌁</span>' : '') +
          '<span class="room-item__name">' + esc(room.name) + '</span>' +
        '</span>' +
        '<span class="room-item__meta">' + first + ' · ' + room.member_count + ' чел.</span>' +
      '</span></div>';
  }

  function renderRooms() {
    const box = $('room-list');
    if (!state.rooms.length) {
      box.innerHTML = '<p class="mono-note" style="padding:10px">Комнат пока нет. Создайте первую.</p>';
      return;
    }
    normalizeLayout();
    const byId = {};
    state.rooms.forEach(function (r) { byId[r.id] = r; });

    box.innerHTML = state.layout.items.map(function (item) {
      if (item.type === 'room') return byId[item.id] ? roomCard(byId[item.id]) : '';
      const inside = item.rooms.map(function (id) {
        return byId[id] ? roomCard(byId[id]) : '';
      }).join('') || '<div class="folder__empty">перетащите сюда комнату</div>';
      return '<div class="folder' + (item.open ? ' is-open' : '') + '" data-folder="' + item.id + '">' +
        '<div class="folder__head" data-drop="folder:' + item.id + '" data-toggle="' + item.id + '">' +
          '<span class="grip" data-grip="folder:' + item.id + '">' + ICONS.grip + '</span>' +
          '<span class="folder__caret">▶</span>' +
          '<span class="folder__name">' + esc(item.name) + '</span>' +
          '<span class="folder__count">' + item.rooms.length + '</span>' +
          '<button class="mini-btn" data-folder-rename="' + item.id + '" title="Переименовать">' +
            ICONS.pencil + '</button>' +
          '<button class="mini-btn mini-btn--danger" data-folder-remove="' + item.id + '" ' +
            'title="Удалить папку">×</button>' +
        '</div>' +
        '<div class="folder__body" data-drop="folderbody:' + item.id + '">' + inside + '</div>' +
      '</div>';
    }).join('');

    box.querySelectorAll('[data-room]').forEach(function (row) {
      const open = function (event) {
        if (event.target.closest('.grip')) return;
        enterRoom(parseInt(row.dataset.room, 10));
      };
      row.addEventListener('click', open);
      row.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(event); }
      });
    });

    box.querySelectorAll('[data-toggle]').forEach(function (head) {
      head.addEventListener('click', function (event) {
        if (event.target.closest('.grip') || event.target.closest('.mini-btn')) return;
        const folder = folderById(head.dataset.toggle);
        if (!folder) return;
        folder.open = !folder.open;
        renderRooms();
        saveLayout();
      });
    });

    box.querySelectorAll('[data-folder-rename]').forEach(function (btn) {
      btn.addEventListener('click', async function (event) {
        event.stopPropagation();
        const folder = folderById(btn.dataset.folderRename);
        if (!folder) return;
        const name = await askText({ eyebrow: 'Папка', title: 'Переименовать папку',
                                     label: 'Название', value: folder.name, max: 32 });
        if (!name) return;
        folder.name = name;
        renderRooms();
        saveLayout();
      });
    });

    box.querySelectorAll('[data-folder-remove]').forEach(function (btn) {
      btn.addEventListener('click', function (event) {
        event.stopPropagation();
        const id = btn.dataset.folderRemove;
        const index = state.layout.items.findIndex(function (it) {
          return it.type === 'folder' && it.id === id;
        });
        if (index < 0) return;
        const folder = state.layout.items[index];
        // комнаты не теряем — возвращаем их на верхний уровень
        const freed = folder.rooms.map(function (rid) { return { type: 'room', id: rid }; });
        state.layout.items.splice(index, 1, ...freed);
        renderRooms();
        saveLayout();
      });
    });

    enableDrag(box, {
      item: '.room-item, .folder',
      accepts: function (source, targetKey) {
        return source.startsWith('room:') && targetKey.startsWith('folder:');
      },
      onDrop: function (source, targetKey, mode) {
        const [kind, id] = source.split(':');
        if (kind === 'room') dropRoom(parseInt(id, 10), targetKey, mode);
        else dropFolder(id, targetKey, mode);
        renderRooms();
        saveLayout();
      }
    });
  }

  $('btn-new-folder').innerHTML = ICONS.folder;

  $('btn-new-folder').addEventListener('click', async function () {
    const name = await askText({ eyebrow: 'Папка', title: 'Новая папка',
                                 label: 'Название', value: '', max: 32 });
    if (!name) return;
    state.layout.items.unshift({ type: 'folder', id: 'f' + Date.now().toString(36),
                                 name: name, open: true, rooms: [] });
    renderRooms();
    saveLayout();
    toast('Папка «' + name + '» создана. Она видна только вам.', 'ok');
  });

  function enterRoom(roomId) {
    const room = state.rooms.find(function (r) { return r.id === roomId; });
    if (!room) return;
    if (room.joined || state.me.is_admin) { openRoom(roomId); return; }
    if (room.locked) {
      $('password-room-name').textContent = room.name;
      $('join-password').value = '';
      $('modal-password').dataset.room = roomId;
      openModal('modal-password');
      return;
    }
    joinRoom(roomId, '');
  }

  async function joinRoom(roomId, password) {
    try {
      await api('/api/rooms/' + roomId + '/join', {
        method: 'POST', body: JSON.stringify({ password: password })
      });
      closeModal('modal-password');
      await loadRooms(roomId);
    } catch (err) { toast(err.message, 'error'); }
  }

  async function openRoom(roomId) {
    if (state.room && state.room.id === roomId) { closeDrawers(); return; }
    if (state.room) socket.emit('close_room', { room_id: state.room.id });

    let data;
    try { data = await api('/api/rooms/' + roomId); }
    catch (err) { toast(err.message, 'error'); return; }

    state.room = data.room;
    state.myRole = data.my_role;
    state.canManage = data.can_manage;
    state.isBoss = data.is_boss;
    state.channels = data.channels;
    state.members = data.members;
    state.bans = data.bans || [];
    state.channel = null;
    state.presence = {};
    state.locks = {};

    $('room-title').textContent = data.room.name;
    $('room-topic').textContent = data.room.topic ||
      (data.room.locked ? 'Закрытая комната' : 'Открытая комната');
    $('room-actions').classList.add('is-on');
    $('btn-room-quit').textContent = state.isBoss ? 'Удалить' : 'Выйти';
    $('btn-new-channel').hidden = !state.canManage;
    $('btn-room-settings').hidden = !state.isBoss;

    socket.emit('open_room', { room_id: roomId });

    renderRooms();
    renderMembers();
    renderChannels();
    updateLockUi();

    const firstText = state.channels.find(function (c) { return c.kind === 'text'; });
    if (firstText) openChannel(firstText.id);
    else $('stream').innerHTML = emptyBlock('Нет текстовых каналов', 'Создайте канал, чтобы начать переписку');

    closeDrawers();
  }

  function emptyBlock(title, note) {
    return '<div class="empty"><div class="empty__content"><div class="empty__symbol" aria-hidden="true">' +
      SVG('<path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2v-9.5A8.5 8.5 0 0 1 10.5 4H13"/><path d="M17 3v8M13 7h8"/>') +
      '</div><span class="eyebrow">Ваше место в эфире</span><h3>' + esc(title) + '</h3>' +
      '<p class="mono-note">' + esc(note) + '</p><div class="empty__tip">Голосовые каналы слева — чтобы поговорить и показать экран</div></div></div>';
  }

  // ------------------------------------------------------------- каналы

  function renderChannels() {
    const box = $('channel-list');
    if (!state.room) { box.innerHTML = ''; return; }

    const text = state.channels.filter(function (c) { return c.kind === 'text'; });
    const voice = state.channels.filter(function (c) { return c.kind === 'voice'; });

    let html = '<div class="group-label"><span class="eyebrow">Текстовые</span></div>';
    html += text.map(function (c) { return channelRow(c, 0); }).join('') ||
      '<p class="mono-note" style="padding:4px 9px">пусто</p>';

    html += '<div class="group-label"><span class="eyebrow">Голосовые</span></div>';
    html += voice.map(function (channel) {
      const people = state.presence[channel.id] || [];
      let rows = '<div class="voice-occupants">';
      rows += people.map(function (p) {
        const classes = ['occupant'];
        if (state.speaking[p.sid]) classes.push('is-speaking');
        if (p.hand) classes.push('is-hand');
        const tags = [];
        if (p.hand) tags.push('<span class="occupant__tag occupant__tag--hand" title="Поднял руку">' + ICONS.hand + '</span>');
        if (p.muted) tags.push('<span class="occupant__tag occupant__tag--muted" title="Микрофон выключен">' + ICONS.micOff + '</span>');
        if (p.sharing) tags.push('<span class="occupant__tag occupant__tag--live">эфир</span>');
        return '<button class="' + classes.join(' ') + '" data-user="' + p.user_id + '">' +
          avatar(p, 'sm') + '<span class="occupant__name">' + esc(p.display_name) + '</span>' +
          tags.join('') + '</button>';
      }).join('');
      rows += '</div>';
      return channelRow(channel, people.length) + (people.length ? rows : '');
    }).join('') || '<p class="mono-note" style="padding:4px 9px">пусто</p>';

    box.innerHTML = html;

    box.querySelectorAll('[data-channel]').forEach(function (row) {
      const activate = function (event) {
        if (event.target.closest('[data-delete]')) return;
        const id = parseInt(row.dataset.channel, 10);
        const channel = state.channels.find(function (c) { return c.id === id; });
        if (!channel) return;
        if (channel.kind === 'voice') joinVoice(channel);
        else openChannel(id);
      };
      row.addEventListener('click', activate);
      row.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(event); }
      });
    });

    box.querySelectorAll('.occupant[data-user]').forEach(function (btn) {
      btn.addEventListener('click', function () { openUserCard(parseInt(btn.dataset.user, 10)); });
    });

    box.querySelectorAll('[data-rename]').forEach(function (btn) {
      btn.addEventListener('click', async function (event) {
        event.stopPropagation();
        const id = parseInt(btn.dataset.rename, 10);
        const channel = state.channels.find(function (c) { return c.id === id; });
        if (!channel) return;
        const name = await askText({ eyebrow: 'Канал', title: 'Переименовать канал',
                                     label: 'Название', value: channel.name, max: 48 });
        if (!name || name === channel.name) return;
        try {
          await api('/api/channels/' + id, { method: 'PATCH', body: JSON.stringify({ name: name }) });
        } catch (err) { toast(err.message, 'error'); }
      });
    });

    if (state.canManage) {
      enableDrag(box, {
        item: '.channel-item',
        accepts: function () { return false; },   // канал в канал не вкладывается
        onDrop: function (source, targetKey, mode) {
          moveChannel(parseInt(source.split(':')[1], 10),
                      parseInt(targetKey.split(':')[1], 10), mode);
        }
      });
    }

    box.querySelectorAll('[data-delete]').forEach(function (btn) {
      btn.addEventListener('click', async function (event) {
        event.stopPropagation();
        const id = parseInt(btn.dataset.delete, 10);
        if (!confirm('Удалить канал вместе со всей перепиской?')) return;
        try {
          await api('/api/channels/' + id, { method: 'DELETE' });
          state.channels = state.channels.filter(function (c) { return c.id !== id; });
          if (state.channel && state.channel.id === id) {
            state.channel = null;
            $('stream').innerHTML = emptyBlock('Канал удалён', 'Выберите другой канал слева');
            $('composer').style.display = 'none';
          }
          renderChannels();
        } catch (err) { toast(err.message, 'error'); }
      });
    });
  }

  /* Порядок каналов общий для всех, поэтому отправляем его на сервер.
     Текстовые и голосовые не смешиваем — иначе канал сменил бы раздел. */
  async function moveChannel(sourceId, targetId, mode) {
    const source = state.channels.find(function (c) { return c.id === sourceId; });
    const target = state.channels.find(function (c) { return c.id === targetId; });
    if (!source || !target || source.id === target.id) return;
    if (source.kind !== target.kind) {
      toast('Текстовые и голосовые каналы не меняются местами', 'error');
      return;
    }

    const ordered = state.channels.slice().sort(function (a, b) { return a.position - b.position; });
    const from = ordered.findIndex(function (c) { return c.id === sourceId; });
    ordered.splice(from, 1);
    let to = ordered.findIndex(function (c) { return c.id === targetId; });
    if (to < 0) return;
    ordered.splice(to + (mode === 'after' ? 1 : 0), 0, source);

    try {
      await api('/api/rooms/' + state.room.id + '/channels/order', {
        method: 'POST',
        body: JSON.stringify({ order: ordered.map(function (c) { return c.id; }) })
      });
    } catch (err) { toast(err.message, 'error'); }
  }

  function channelRow(channel, count) {
    const active = (state.channel && state.channel.id === channel.id) ||
      (RTC.channelId === channel.id) ? ' is-active' : '';
    const sigil = channel.kind === 'text' ? '#' : '((';
    const locked = state.locks[channel.id]
      ? '<span class="channel-item__lock" title="В канале включена тишина">' + ICONS.micOff + '</span>' : '';
    const badge = count ? '<span class="channel-item__count">' + count + '</span>' : '';
    const tools = state.canManage
      ? '<span class="grip" data-grip="channel:' + channel.id + '" title="Перетащить">' + ICONS.grip + '</span>' +
        '<button class="mini-btn" data-rename="' + channel.id + '" title="Переименовать">' + ICONS.pencil + '</button>' +
        '<button class="mini-btn mini-btn--danger" data-delete="' + channel.id + '" title="Удалить канал">×</button>'
      : '';
    // не <button>: вложенная кнопка удаления по правилам HTML выбрасывается
    // парсером наружу и оказывается отдельной строкой под каналом
    return '<div class="channel-item' + active + '" role="button" tabindex="0" data-channel="' +
      channel.id + '" data-drop="channel:' + channel.id + '" data-kind="' + channel.kind + '">' +
      (state.canManage ? tools.split('</span>')[0] + '</span>' : '') +
      '<span class="channel-item__sigil">' + sigil + '</span>' +
      '<span class="channel-item__name">' + esc(channel.name) + '</span>' +
      locked + badge +
      (state.canManage ? tools.slice(tools.indexOf('</span>') + 7) : '') + '</div>';
  }

  // ------------------------------------------------------------- текстовый канал

  async function openChannel(channelId) {
    const channel = state.channels.find(function (c) { return c.id === channelId; });
    if (!channel || channel.kind !== 'text') return;

    const previous = state.lastTextChannel || null;
    state.channel = channel;
    state.lastTextChannel = channelId;
    socket.emit('open_channel', { channel_id: channelId, previous: previous });

    $('channel-title').textContent = '# ' + channel.name;
    $('channel-hint').textContent = 'история сохраняется';
    $('composer').style.display = 'block';
    $('input').placeholder = 'Написать в #' + channel.name + '…';
    renderChannels();
    closeDrawers();

    $('stream').innerHTML = '<p class="mono-note" style="padding:20px">Загружаю историю…</p>';
    try {
      const data = await api('/api/channels/' + channelId + '/messages');
      if (!state.channel || state.channel.id !== channelId) return;
      state.messages = data.messages;
      state.hasMore = data.has_more;
      renderMessages(true);
    } catch (err) {
      $('stream').innerHTML = emptyBlock('Не удалось загрузить', err.message);
    }
  }

  function messageNode(message, previous) {
    const tail = previous && previous.author.id === message.author.id &&
      (new Date(message.created_at) - new Date(previous.created_at)) < 5 * 60 * 1000;
    const canDelete = message.author.id === state.me.id || state.canManage;

    const el = document.createElement('div');
    el.className = 'msg' + (tail ? ' msg--tail' : '');
    el.dataset.id = message.id;
    el.innerHTML = avatar(message.author) +
      '<div class="msg__body">' +
        (tail ? '' :
          '<div class="msg__head">' +
            '<span class="msg__author" style="color:' + esc(message.author.color) + '">' +
              esc(message.author.display_name) + '</span>' +
            '<span class="msg__time">' + timeOf(message.created_at) + '</span>' +
          '</div>') +
        '<div class="msg__text">' + linkify(message.content) + '</div>' +
      '</div>' +
      (canDelete ? '<button class="msg__del" title="Удалить">×</button>' : '');

    const del = el.querySelector('.msg__del');
    if (del) {
      del.addEventListener('click', async function () {
        try { await api('/api/messages/' + message.id, { method: 'DELETE' }); }
        catch (err) { toast(err.message, 'error'); }
      });
    }
    return el;
  }

  function renderMessages(scrollToEnd) {
    const stream = $('stream');
    if (!state.messages.length) {
      stream.innerHTML = emptyBlock('Здесь пока тихо',
        'Напишите первое сообщение — оно останется в истории канала');
      return;
    }

    stream.innerHTML = '';
    let previous = null;
    let day = null;
    state.messages.forEach(function (message) {
      const currentDay = dayOf(message.created_at);
      if (currentDay !== day) {
        day = currentDay;
        previous = null;
        const rule = document.createElement('div');
        rule.className = 'day-rule';
        rule.textContent = currentDay;
        stream.appendChild(rule);
      }
      stream.appendChild(messageNode(message, previous));
      previous = message;
    });

    if (scrollToEnd) stream.scrollTop = stream.scrollHeight;
  }

  function appendMessage(message) {
    const stream = $('stream');
    const nearBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 140;
    if (!state.messages.length) stream.innerHTML = '';

    const previous = state.messages[state.messages.length - 1];
    if (!previous || dayOf(previous.created_at) !== dayOf(message.created_at)) {
      const rule = document.createElement('div');
      rule.className = 'day-rule';
      rule.textContent = dayOf(message.created_at);
      stream.appendChild(rule);
      stream.appendChild(messageNode(message, null));
    } else {
      stream.appendChild(messageNode(message, previous));
    }
    state.messages.push(message);
    if (nearBottom) stream.scrollTop = stream.scrollHeight;
  }

  $('stream').addEventListener('scroll', async function () {
    const stream = $('stream');
    if (stream.scrollTop > 40 || !state.hasMore || state.loadingHistory || !state.channel) return;
    state.loadingHistory = true;
    const anchor = stream.scrollHeight;
    try {
      const data = await api('/api/channels/' + state.channel.id +
        '/messages?before=' + state.messages[0].id);
      state.messages = data.messages.concat(state.messages);
      state.hasMore = data.has_more;
      renderMessages(false);
      stream.scrollTop = stream.scrollHeight - anchor;
    } catch (err) { /* тихо */ }
    state.loadingHistory = false;
  });

  // ------------------------------------------------------------- отправка

  const input = $('input');

  input.addEventListener('input', function () {
    $('btn-send').disabled = !input.value.trim() || !socket.connected;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    $('count').textContent = input.value.length > 1600 ? (2000 - input.value.length) : '';

    const now = Date.now();
    if (state.channel && input.value && now - state.lastTypingSent > 2200) {
      state.lastTypingSent = now;
      socket.emit('typing', { channel_id: state.channel.id });
    }
  });

  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
  });
  $('btn-send').addEventListener('click', send);

  function send() {
    const content = input.value.trim();
    if (!content || !state.channel) return;
    if (!socket.connected) { toast('Нет связи с сервером. Текст сохранён в поле ввода.', 'error'); return; }
    const nonce = 'n' + Date.now() + Math.random().toString(16).slice(2);

    const draft = { id: 'tmp-' + nonce, content: content,
      created_at: new Date().toISOString(), author: state.me };
    const node = messageNode(draft, state.messages[state.messages.length - 1]);
    node.classList.add('is-pending');
    $('stream').appendChild(node);
    $('stream').scrollTop = $('stream').scrollHeight;
    state.pending.set(nonce, node);

    socket.emit('send_message', { channel_id: state.channel.id, content: content, nonce: nonce });
    input.value = '';
    input.style.height = 'auto';
    $('count').textContent = '';
    $('btn-send').disabled = true;
    input.focus();
  }

  // ------------------------------------------------------------- участники

  function voicePeerOf(userId) {
    const list = state.presence[RTC.channelId] || [];
    return list.find(function (p) { return p.user_id === userId; }) || null;
  }

  function renderMembers() {
    $('member-count').textContent = state.members.filter(function (m) { return state.online[m.id]; }).length + ' в сети';
    $('member-list').innerHTML = state.members.map(function (member) {
      const peer = voicePeerOf(member.id);
      const flags = [];
      if (peer && peer.hand) flags.push('<span class="occupant__tag occupant__tag--hand">' + ICONS.hand + '</span>');
      if (peer && peer.muted) flags.push('<span class="occupant__tag occupant__tag--muted">' + ICONS.micOff + '</span>');
      return '<button class="roster__item' + (peer && peer.hand ? ' is-hand' : '') +
        '" data-user="' + member.id + '">' + avatarWithDot(member) +
        '<div style="flex:1;min-width:0"><b>' + esc(member.display_name) + '</b>' +
        '<div>' + (roleBadge(member.role) || '<span class="role-badge">участник</span>') + '</div></div>' +
        '<div class="roster__flags">' + flags.join('') + '</div></button>';
    }).join('');

    $('member-list').querySelectorAll('[data-user]').forEach(function (btn) {
      btn.addEventListener('click', function () { openUserCard(parseInt(btn.dataset.user, 10)); });
    });
  }

  // ------------------------------------------------------------- карточка участника

  function openUserCard(userId) {
    const member = state.members.find(function (m) { return m.id === userId; }) ||
      voicePeerOf(userId);
    if (!member) return;

    state.cardUser = userId;
    const isSelf = userId === state.me.id;
    const name = member.display_name || member.username;

    $('user-avatar').outerHTML = avatar(member, 'lg').replace('class="avatar',
      'id="user-avatar" class="avatar');
    $('user-name').textContent = name;
    $('user-login').textContent = member.username ? '@' + member.username : '';

    const role = member.role || 'member';
    const badge = $('user-role');
    badge.className = 'role-badge role-badge--' + role;
    badge.textContent = ROLE_LABEL[role] || role;

    // назначать модератора может владелец комнаты и админ сайта
    const canAssign = state.isBoss && !isSelf && role !== 'owner' && role !== 'admin';
    $('user-mod-block').hidden = !canAssign;
    $('user-moderator').checked = role === 'moderator';

    const slider = $('user-volume');
    $('mic-sens-block').hidden = !isSelf;
    stopLevelMeter();

    if (isSelf) {
      $('user-volume-label').textContent = 'Громкость моего микрофона';
      slider.value = Math.round(RTC.micGain * 100);
      $('user-volume-note').textContent = RTC.canTuneMic()
        ? 'Как громко вас слышат остальные.'
        : 'Регулировка недоступна: браузер не дал обработать звук. Громкость настраивается в системе.';
      slider.disabled = !RTC.canTuneMic();

      const sens = Math.round(RTC.getMicSensitivity() * 100);
      $('mic-sens').value = sens;
      $('mic-sens').disabled = !RTC.canTuneMic();
      $('mic-sens-value').textContent = sens + '%';
      $('mic-mark').style.left = (100 - sens) + '%';
      if (!RTC.canTuneMic()) {
        $('mic-sens-note').textContent =
          'Недоступно: браузер не дал обработать звук микрофона.';
      } else if (!RTC.channelId) {
        $('mic-sens-note').textContent =
          'Порог применится, когда вы зайдёте в голосовой канал.';
      } else {
        startLevelMeter();
      }

      paintDenoise();
    } else {
      $('user-volume-label').textContent = 'Громкость этого участника';
      slider.value = Math.round(RTC.getPeerVolume(userId) * 100);
      $('user-volume-note').textContent = 'Настройка только ваша — других она не касается.';
      slider.disabled = false;
    }
    $('user-volume-value').textContent = slider.value + '%';

    renderUserBans(userId);

    // исключить можно только того, кто сейчас в моём голосовом канале
    const peer = voicePeerOf(userId);
    const myRole = myLockRole();
    const canKick = !isSelf && !!peer &&
      ['admin', 'owner', 'moderator'].indexOf(myRole) >= 0 &&
      ['owner', 'admin'].indexOf(peer.role) < 0 &&
      !(myRole === 'moderator' && peer.role === 'moderator');
    $('user-kick-block').hidden = !canKick;

    // убрать из комнаты можно и того, кого нет в голосовом канале
    const targetRole = member.is_admin ? 'admin' : (member.role || 'member');
    const canRemove = !isSelf &&
      state.members.some(function (m) { return m.id === userId; }) &&
      ['admin', 'owner', 'moderator'].indexOf(myRole) >= 0 &&
      ['owner', 'admin'].indexOf(targetRole) < 0 &&
      !(myRole === 'moderator' && targetRole === 'moderator');
    $('user-remove-block').hidden = !canRemove;

    openModal('modal-user');
  }

  function startLevelMeter() {
    stopLevelMeter();
    state.levelTimer = setInterval(function () {
      if (!$('modal-user').classList.contains('is-open')) { stopLevelMeter(); return; }
      const percent = Math.min(RTC.localLevel / 0.15, 1) * 100;
      const fill = $('mic-level');
      fill.style.width = percent.toFixed(1) + '%';
      fill.classList.toggle('is-open', RTC.gateIsOpen());
    }, 60);
  }

  function stopLevelMeter() {
    if (state.levelTimer) { clearInterval(state.levelTimer); state.levelTimer = null; }
  }

  function paintDenoise() {
    const value = Math.round(RTC.getDenoise() * 100);
    $('denoise-block').hidden = !RTC.canTuneMic();
    $('denoise').value = value;
    $('denoise').disabled = !RTC.canDenoise();
    $('denoise-value').textContent = value + '%';
    if (!RTC.canDenoise()) {
      $('denoise-note').textContent = RTC.channelId
        ? 'Шумодав не запустился: браузер не поддерживает обработку в аудиопотоке.'
        : 'Заработает, когда вы зайдёте в голосовой канал.';
    } else {
      $('denoise-note').textContent =
        'Убирает ровный фон: вентилятор, гул, шипение. Голос почти не задевает. ' +
        'На максимуме звук может стать «подводным» — тогда сбавьте.';
    }
  }

  RTC.on('denoise_ready', function () {
    if ($('modal-user').classList.contains('is-open') && state.cardUser === state.me.id) {
      paintDenoise();
    }
  });

  $('denoise').addEventListener('input', function () {
    const value = Number(this.value);
    $('denoise-value').textContent = value + '%';
    RTC.setDenoise(value / 100);
    remember('denoise', value / 100);
  });

  $('mic-sens').addEventListener('input', function () {
    const value = Number(this.value);
    $('mic-sens-value').textContent = value + '%';
    $('mic-mark').style.left = (100 - value) + '%';
    RTC.setMicSensitivity(value / 100);
    remember('micsens', value / 100);
  });

  $('user-volume').addEventListener('input', function () {
    const value = Number(this.value) / 100;
    $('user-volume-value').textContent = this.value + '%';
    if (state.cardUser === state.me.id) {
      RTC.setMicGain(value);
      remember('micgain', value);
    } else {
      RTC.setPeerVolume(state.cardUser, value);
      remember('vol:' + state.cardUser, value);
    }
  });

  function renderUserBans(userId) {
    const mine = state.bans.filter(function (ban) { return ban.user_id === userId; });
    const role = myLockRole();
    const canManageBans = ['admin', 'owner', 'moderator'].indexOf(role) >= 0;
    $('user-bans-block').hidden = !mine.length;
    if (!mine.length) return;

    $('user-bans').innerHTML = mine.map(function (ban) {
      const allowed = canManageBans &&
        (['admin', 'owner'].indexOf(role) >= 0 || ban.by_user_id === state.me.id);
      return '<div class="ban-row">' +
        '<b>' + esc(ban.channel_name) + '</b>' +
        '<span>' + esc(ban.by_name) + '</span>' +
        (allowed
          ? '<button class="btn btn--ghost btn--slim" data-unban="' + ban.channel_id + '">Снять</button>'
          : '') +
        '</div>';
    }).join('');

    $('user-bans').querySelectorAll('[data-unban]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        socket.emit('voice_unban', {
          channel_id: parseInt(btn.dataset.unban, 10),
          user_id: userId
        });
        btn.disabled = true;
        btn.textContent = 'Снимаю…';
      });
    });
  }

  $('user-boot').addEventListener('click', function () {
    socket.emit('voice_kick', { user_id: state.cardUser, ban: false });
    closeModal('modal-user');
  });

  $('user-kick').addEventListener('click', function () {
    socket.emit('voice_kick', { user_id: state.cardUser, ban: true });
    closeModal('modal-user');
  });

  $('user-remove').addEventListener('click', async function () {
    const member = state.members.find(function (m) { return m.id === state.cardUser; });
    if (!member) return;
    if (!confirm('Убрать ' + member.display_name + ' из комнаты «' + state.room.name + '»?')) return;
    try {
      await api('/api/rooms/' + state.room.id + '/members/' + state.cardUser, { method: 'DELETE' });
      closeModal('modal-user');
      toast(member.display_name + ' больше не участник комнаты', 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('user-moderator').addEventListener('change', async function () {
    const role = this.checked ? 'moderator' : 'member';
    try {
      await api('/api/rooms/' + state.room.id + '/members/' + state.cardUser + '/role', {
        method: 'POST', body: JSON.stringify({ role: role })
      });
      toast(this.checked ? 'Назначен модератором' : 'Права модератора сняты', 'ok');
    } catch (err) {
      this.checked = !this.checked;
      toast(err.message, 'error');
    }
  });

  // ------------------------------------------------------------- голос

  async function joinVoice(channel) {
    if (RTC.channelId === channel.id) return;
    if (RTC.joining) return;
    $('voice-deck').classList.add('is-on');
    $('voice-channel-name').textContent = channel.name;
    $('voice-status-text').textContent = 'Подключение…';
    try {
      const result = await RTC.join(channel.id);
      SFX.join();
      $('voice-deck').classList.add('is-on');
      $('voice-channel-name').textContent = channel.name;
      updateVoiceStatus();
      restoreVolumes();
      updateVoiceButtons();
      updateLockUi();
      renderChannels();
      closeDrawers();

      if (result.listenOnly) {
        toast(RTC.describeMicError(result.error) +
          ' Вы в канале — других слышно. Нажмите кнопку микрофона, когда подключите его.', 'error');
      }
    } catch (err) {
      if (!RTC.channelId) $('voice-deck').classList.remove('is-on');
      toast(RTC.describeMicError(err), 'error');
    }
  }

  function leaveVoice() {
    if (RTC.channelId) SFX.leave();
    RTC.leave();
    $('voice-deck').classList.remove('is-on');
    state.screens.clear();
    renderBroadcast();
    updateLockUi();
    renderChannels();
  }

  function restoreVolumes() {
    RTC.setMicGain(recall('micgain', 1));
    RTC.setMicSensitivity(recall('micsens', 0.7));
    RTC.setDenoise(recall('denoise', 0.5));
    state.members.forEach(function (m) {
      const value = recall('vol:' + m.id, 1);
      if (value !== 1) RTC.setPeerVolume(m.id, value);
    });
  }

  function myLockRole() {
    const peer = voicePeerOf(state.me.id);
    return (peer && peer.role) || state.myRole || 'member';
  }

  function lockHitsMe() {
    const info = state.locks[RTC.channelId];
    if (!info) return false;
    if (info.user_id === state.me.id) return false;
    return ['admin', 'owner'].indexOf(myLockRole()) < 0;
  }

  function updateVoiceButtons() {
    const mic = $('btn-mic');
    const present = RTC.hasMic();
    const blocked = lockHitsMe();

    mic.classList.toggle('is-on', present && RTC.micEnabled && !blocked);
    mic.classList.toggle('is-absent', !present);
    mic.setAttribute('aria-pressed', String(present && RTC.micEnabled && !blocked));
    mic.disabled = blocked || RTC.joining;

    if (!present) {
      mic.innerHTML = ICONS.micOff + '<span>Нет микрофона</span>';
      mic.title = 'Микрофон не найден. Подключите его и нажмите, чтобы повторить';
    } else if (blocked) {
      mic.innerHTML = ICONS.micOff + '<span>Заглушен</span>';
      mic.title = 'В канале включена тишина';
    } else {
      mic.innerHTML = RTC.micEnabled ? ICONS.mic + '<span>Микрофон</span>'
        : ICONS.micOff + '<span>Включить</span>';
      mic.title = RTC.micEnabled ? 'Выключить микрофон' : 'Включить микрофон';
    }

    const hand = $('btn-hand');
    hand.classList.toggle('is-on', RTC.handUp);
    hand.setAttribute('aria-pressed', String(RTC.handUp));
    hand.innerHTML = ICONS.hand + '<span>' + (RTC.handUp ? 'Опустить' : 'Рука') + '</span>';

    const share = $('btn-share');
    share.classList.toggle('is-live', RTC.isSharing());
    share.setAttribute('aria-pressed', String(RTC.isSharing()));
    share.innerHTML = RTC.isSharing() ? ICONS.stop + '<span>Остановить</span>'
      : ICONS.screen + '<span>Экран</span>';
  }

  function updateLockUi() {
    const info = state.locks[RTC.channelId] || null;
    const canPress = ['admin', 'owner', 'moderator'].indexOf(myLockRole()) >= 0;

    $('lock-switch').hidden = !RTC.channelId || !canPress;
    $('lock-switch').classList.toggle('is-on', !!info);
    $('lock-toggle').checked = !!info;

    // отжать может только нажавший или владелец
    const canRelease = !info || info.user_id === state.me.id ||
      ['admin', 'owner'].indexOf(myLockRole()) >= 0;
    $('lock-toggle').disabled = !!info && !canRelease;

    const bar = $('lockbar');
    if (info && RTC.channelId) {
      bar.hidden = false;
      bar.textContent = lockHitsMe()
        ? 'Тишина в канале: ' + info.name + ' выключил микрофоны. Включить свой нельзя, пока он её не снимет.'
        : 'Тишина в канале включена (' + info.name + '). Микрофоны остальных выключены.';
    } else {
      bar.hidden = true;
    }

    // на приём глушим тех, кого касается тишина
    const silenced = [];
    if (info) {
      (state.presence[RTC.channelId] || []).forEach(function (p) {
        if (p.user_id === info.user_id) return;
        if (['admin', 'owner'].indexOf(p.role) >= 0) return;
        silenced.push(p.user_id);
      });
    }
    RTC.setSilenced(silenced);
  }

  $('btn-mic').addEventListener('click', async function () {
    if (lockHitsMe()) {
      const info = state.locks[RTC.channelId];
      toast('В канале включена тишина. Снять её может ' + info.name + ' или владелец комнаты.', 'error');
      return;
    }
    if (!RTC.hasMic()) {
      try {
        await RTC.retryMic();
        RTC.setMicGain(recall('micgain', 1));
        RTC.setMicSensitivity(recall('micsens', 0.7));
        RTC.setDenoise(recall('denoise', 0.5));
        updateVoiceStatus();
        toast('Микрофон подключён', 'ok');
      } catch (err) { toast(RTC.describeMicError(err), 'error'); }
      updateVoiceButtons();
      return;
    }
    RTC.toggleMic();
    updateVoiceButtons();
  });

  $('btn-hand').addEventListener('click', function () {
    if (!RTC.channelId) { toast('Сначала зайдите в голосовой канал', 'error'); return; }
    RTC.raiseHand(!RTC.handUp);
    updateVoiceButtons();
  });

  $('btn-hangup').addEventListener('click', leaveVoice);

  $('lock-toggle').addEventListener('change', function () {
    socket.emit('mic_lock', { active: this.checked });
  });

  $('btn-share').addEventListener('click', async function () {
    if (!RTC.channelId) { toast('Сначала зайдите в голосовой канал', 'error'); return; }
    if (RTC.isSharing()) { RTC.stopShare(); updateVoiceButtons(); renderBroadcast(); return; }
    try {
      await RTC.startShare();
      updateVoiceButtons();
      renderBroadcast();

      let note = 'Трансляция началась · ' + RTC.QUALITY[RTC.quality].label;
      if (RTC.hasScreenAudio()) {
        note += RTC.screenAudioMode === 'duck' && RTC.screenSurface !== 'browser'
          ? ' · звук экрана приглушается при речи'
          : ' · со звуком';
      }
      toast(note, 'ok');
    } catch (err) {
      if (err && err.name === 'NotAllowedError') return;   // пользователь передумал
      toast(err.message || 'Не удалось начать трансляцию', 'error');
    }
  });

  // ------------------------------------------------------------- трансляции

  function sharingPeers() {
    return (state.presence[RTC.channelId] || []).filter(function (p) { return p.sharing; });
  }

  function renderBroadcast() {
    const grid = $('broadcast-grid');
    const peers = sharingPeers();
    const wanted = new Set(peers.map(function (p) { return p.sid; }));

    state.tiles.forEach(function (tile, sid) {
      if (!wanted.has(sid)) { tile.remove(); state.tiles.delete(sid); }
    });

    peers.forEach(function (peer) {
      const isSelf = peer.sid === RTC.selfSid;
      const stream = isSelf ? RTC.screenStream : state.screens.get(peer.sid);
      let tile = state.tiles.get(peer.sid);

      if (!tile) {
        tile = document.createElement('div');
        tile.className = 'screen';
        // Звук экрана воспроизводится отдельной аудиодорожкой. Видео всегда
        // muted, чтобы автозапуск картинки не зависел от разрешения звука.
        tile.innerHTML = '<video autoplay playsinline muted></video>' +
          '<div class="screen__label"><i></i><span></span></div>' +
          '<button class="screen__expand" title="На весь экран">⤢</button>';
        tile.querySelector('.screen__expand').addEventListener('click', function () {
          const video = tile.querySelector('video');
          if (video.requestFullscreen) video.requestFullscreen();
          else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
        });
        grid.appendChild(tile);
        state.tiles.set(peer.sid, tile);
      }

      const video = tile.querySelector('video');
      if (stream && video.srcObject !== stream) {
        video.srcObject = stream;
        video.play().catch(function () { /* автозапуск подождёт */ });
      }
      const preset = RTC.QUALITY[peer.quality] || null;
      const relayed = !isSelf && (RTC.peers.get(peer.sid) || {}).relay;
      tile.querySelector('.screen__label span').textContent =
        (isSelf ? 'Ваш экран' : peer.display_name) +
        (relayed ? ' · через сервер' : preset ? ' · ' + preset.label.toLowerCase() : '');
    });

    const on = peers.length > 0;
    $('workspace').classList.toggle('has-stage', on);
    if (!on) { state.stageWide = false; $('workspace').classList.remove('is-wide'); }
    $('broadcast-label').textContent = peers.length > 1
      ? 'Трансляции · ' + peers.length : 'Трансляция';
  }

  $('btn-stage-wide').addEventListener('click', function () {
    state.stageWide = !state.stageWide;
    $('workspace').classList.toggle('is-wide', state.stageWide);
    this.textContent = state.stageWide ? 'Показать чат' : 'Развернуть';
  });

  RTC.on('screen', function (data) { state.screens.set(data.sid, data.stream); renderBroadcast(); });

  RTC.on('screen_off', function (data) {
    state.screens.delete(data.sid);
    const tile = state.tiles.get(data.sid);
    if (tile) { tile.remove(); state.tiles.delete(data.sid); }
    renderBroadcast();
  });

  RTC.on('speaking', function (data) {
    const sid = data.sid === 'self' ? RTC.selfSid : data.sid;
    if (!sid || state.speaking[sid] === data.speaking) return;
    state.speaking[sid] = data.speaking;
    renderChannels();
  });

  RTC.on('peer_joined', function () { SFX.peerIn(); });
  RTC.on('peer_left', function () { SFX.peerOut(); });
  RTC.on('notice', function (data) { toast(data.text, data.level); });
  RTC.on('share_stopped', function () { updateVoiceButtons(); renderBroadcast(); });

  // ------------------------------------------------------------- качество

  function renderQuality() {
    $('relay-quality-note').hidden = !RTC.connectionSummary().relayed;
    $('quality-list').innerHTML = Object.keys(RTC.QUALITY).map(function (key) {
      const preset = RTC.QUALITY[key];
      let bars = '';
      for (let i = 1; i <= 4; i++) bars += '<i class="' + (i <= preset.bars ? 'on' : '') + '"></i>';
      return '<button class="quality' + (key === RTC.quality ? ' is-on' : '') +
        '" data-quality="' + key + '">' +
        '<span class="quality__bars">' + bars + '</span>' +
        '<span class="quality__meta"><b>' + preset.label + '</b><span>' + preset.detail + '</span></span>' +
        '</button>';
    }).join('');

    $('quality-list').querySelectorAll('[data-quality]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        await RTC.setQuality(btn.dataset.quality);
        renderQuality();
        renderBroadcast();
        toast('Качество: ' + RTC.QUALITY[RTC.quality].label, 'ok');
      });
    });
  }

  const SCREEN_AUDIO_NOTE = {
    off: 'Участники увидят картинку без звука. Голоса точно не вернутся обратно.',
    duck: 'Пока кто-то говорит, звук экрана приглушается — так голоса участников ' +
          'не уходят обратно в эфир. Меняется до начала показа.',
    raw: 'Весь системный звук уходит как есть. При показе всего экрана участники ' +
         'услышат сами себя — берите этот режим только с наушниками.'
  };

  function renderScreenAudio() {
    const mode = RTC.screenAudioMode;
    document.querySelectorAll('#screen-audio button').forEach(function (btn) {
      btn.classList.toggle('is-on', btn.dataset.audio === mode);
    });
    $('screen-audio-note').textContent = SCREEN_AUDIO_NOTE[mode];
  }

  document.querySelectorAll('#screen-audio button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      RTC.setScreenAudioMode(btn.dataset.audio);
      remember('screenaudio', btn.dataset.audio);
      renderScreenAudio();
      if (RTC.isSharing()) {
        toast('Режим применится при следующем запуске трансляции', 'ok');
      }
    });
  });

  $('btn-quality').addEventListener('click', function () {
    renderQuality();
    renderScreenAudio();
    openModal('modal-quality');
  });

  // ------------------------------------------------------------- окна

  $('btn-new-room').addEventListener('click', function () {
    ['room-name', 'room-topic-input', 'room-password'].forEach(function (id) { $(id).value = ''; });
    $('room-locked').checked = false;
    $('room-guests').checked = true;
    $('room-password-field').style.display = 'none';
    openModal('modal-room');
  });

  $('room-locked').addEventListener('change', function () {
    $('room-password-field').style.display = this.checked ? 'block' : 'none';
  });

  $('room-create').addEventListener('click', async function () {
    const payload = {
      name: $('room-name').value.trim(),
      topic: $('room-topic-input').value.trim(),
      password: $('room-locked').checked ? $('room-password').value.trim() : '',
      guests_allowed: $('room-guests').checked
    };
    if (!payload.name) { toast('Введите название комнаты', 'error'); return; }
    try {
      const data = await api('/api/rooms', { method: 'POST', body: JSON.stringify(payload) });
      closeModal('modal-room');
      await loadRooms(data.room.id);
      toast('Комната «' + data.room.name + '» создана, вы её владелец', 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  $('btn-room-settings').addEventListener('click', function () {
    if (!state.room) return;
    $('settings-name').value = state.room.name;
    $('settings-topic').value = state.room.topic || '';
    $('settings-locked').checked = state.room.locked;
    $('settings-guests').checked = state.room.guests_allowed !== false;
    $('settings-password').value = '';
    $('settings-password-field').hidden = !state.room.locked;
    $('settings-password-note').textContent = state.room.locked
      ? 'Оставьте пустым, чтобы не менять текущий пароль.'
      : 'От 4 символов.';
    openModal('modal-settings');
  });

  $('settings-locked').addEventListener('change', function () {
    $('settings-password-field').hidden = !this.checked;
    $('settings-password-note').textContent = this.checked && state.room && state.room.locked
      ? 'Оставьте пустым, чтобы не менять текущий пароль.'
      : 'От 4 символов.';
  });

  $('settings-save').addEventListener('click', async function () {
    if (!state.room) return;
    const payload = {
      name: $('settings-name').value.trim(),
      topic: $('settings-topic').value.trim(),
      locked: $('settings-locked').checked,
      password: $('settings-password').value.trim(),
      guests_allowed: $('settings-guests').checked
    };
    try {
      const data = await api('/api/rooms/' + state.room.id, {
        method: 'PATCH', body: JSON.stringify(payload)
      });
      applyRoomUpdate(data.room);
      closeModal('modal-settings');
      await loadRooms();
      toast('Настройки комнаты сохранены', 'ok');
    } catch (err) { toast(err.message, 'error'); }
  });

  function applyRoomUpdate(room) {
    if (!state.room || state.room.id !== room.id) return;
    state.room = Object.assign(state.room, room);
    $('room-title').textContent = room.name;
    $('room-topic').textContent = room.topic ||
      (room.locked ? 'Закрытая комната' : 'Открытая комната');
  }

  $('password-submit').addEventListener('click', function () {
    joinRoom(parseInt($('modal-password').dataset.room, 10), $('join-password').value);
  });

  $('join-password').addEventListener('keydown', function (event) {
    if (event.key === 'Enter') $('password-submit').click();
  });

  $('btn-new-channel').addEventListener('click', function () {
    state.channelKind = 'text';
    $('channel-name').value = '';
    document.querySelectorAll('#channel-kind button').forEach(function (b) {
      b.classList.toggle('is-on', b.dataset.kind === 'text');
    });
    openModal('modal-channel');
  });

  document.querySelectorAll('#channel-kind button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      state.channelKind = btn.dataset.kind;
      document.querySelectorAll('#channel-kind button').forEach(function (b) {
        b.classList.toggle('is-on', b === btn);
      });
    });
  });

  $('channel-create').addEventListener('click', async function () {
    const name = $('channel-name').value.trim();
    if (!name) { toast('Введите название канала', 'error'); return; }
    try {
      const data = await api('/api/rooms/' + state.room.id + '/channels', {
        method: 'POST', body: JSON.stringify({ name: name, kind: state.channelKind })
      });
      state.channels.push(data.channel);
      closeModal('modal-channel');
      renderChannels();
    } catch (err) { toast(err.message, 'error'); }
  });

  $('btn-room-quit').addEventListener('click', async function () {
    if (!state.room) return;
    const remove = state.isBoss;
    const question = remove
      ? 'Удалить комнату вместе со всеми каналами и перепиской?'
      : 'Выйти из комнаты «' + state.room.name + '»?';
    if (!confirm(question)) return;
    try {
      if (remove) await api('/api/rooms/' + state.room.id, { method: 'DELETE' });
      else await api('/api/rooms/' + state.room.id + '/leave', { method: 'POST' });
      if (RTC.channelId) leaveVoice();
      state.room = null;
      $('room-actions').classList.remove('is-on');
      $('room-title').textContent = 'Комната не выбрана';
      $('room-topic').textContent = 'Выберите комнату слева';
      $('channel-list').innerHTML = '';
      $('member-list').innerHTML = '';
      $('composer').style.display = 'none';
      $('stream').innerHTML = emptyBlock('Комната закрыта', 'Выберите другую слева или создайте новую');
      await loadRooms();
    } catch (err) { toast(err.message, 'error'); }
  });

  // ------------------------------------------------------------- сокет

  function serverStatus(online) {
    $('btn-connection').classList.toggle('is-online', online);
    $('connection-label').textContent = online ? 'На связи' : 'Нет связи';
    $('connection-banner').hidden = online;
    $('connection-banner').textContent = 'Соединение с сервером потеряно. Переподключаемся… Ваш черновик остаётся здесь.';
    $('btn-send').disabled = !online || !input.value.trim();
  }
  socket.on('connect', function () { serverStatus(true); });
  socket.on('connect_error', function () { serverStatus(false); });

  socket.on('ready', function (data) {
    state.online = {};
    (data.online || []).forEach(function (id) { state.online[id] = true; });
    renderMembers();
    const roomId = state.room && state.room.id;
    state.room = null;
    loadRooms(roomId).catch(function (err) { toast(err.message, 'error'); });
  });

  socket.on('online_changed', function (data) {
    if (data.online) state.online[data.user_id] = true;
    else delete state.online[data.user_id];
    renderMembers();
  });

  socket.on('profile_updated', function (data) {
    const fresh = data.user;
    if (fresh.id === state.me.id) {
      state.me = Object.assign(state.me, fresh);
      renderMe();
    }
    const member = state.members.find(function (m) { return m.id === fresh.id; });
    if (member) {
      Object.assign(member, fresh);
      renderMembers();
    }
    Object.keys(state.presence).forEach(function (channelId) {
      (state.presence[channelId] || []).forEach(function (peer) {
        if (peer.user_id === fresh.id) {
          peer.display_name = fresh.display_name;
          peer.color = fresh.color;
          peer.initials = fresh.initials;
          peer.avatar_url = fresh.avatar_url;
        }
      });
    });
    renderChannels();
  });

  socket.on('channels_updated', function (data) {
    if (!state.room || state.room.id !== data.room_id) return;
    state.channels = data.channels;
    const open = state.channel && state.channels.find(function (c) { return c.id === state.channel.id; });
    if (open) {
      state.channel = open;
      $('channel-title').textContent = '# ' + open.name;
      $('input').placeholder = 'Написать в #' + open.name + '…';
    }
    renderChannels();
  });

  socket.on('bans_updated', function (data) {
    if (!state.room || state.room.id !== data.room_id) return;
    state.bans = data.bans || [];
    if ($('modal-user').classList.contains('is-open') && state.cardUser) {
      renderUserBans(state.cardUser);
    }
  });

  socket.on('voice_kicked', function (data) {
    leaveVoice();
    toast(data.banned
      ? data.by + ' исключил вас из канала — вход закрыт'
      : data.by + ' выгнал вас из канала', 'error');
  });

  socket.on('member_left', function (data) {
    if (!state.room || state.room.id !== data.room_id) return;
    state.members = state.members.filter(function (m) { return m.id !== data.user_id; });
    renderMembers();
  });

  socket.on('removed_from_room', function (data) {
    if (state.room && state.room.id === data.room_id) {
      if (RTC.channelId) leaveVoice();
      state.room = null;
      state.channel = null;
      $('room-actions').classList.remove('is-on');
      $('room-title').textContent = 'Комната не выбрана';
      $('room-topic').textContent = 'Выберите комнату слева';
      $('channel-list').innerHTML = '';
      $('member-list').innerHTML = '';
      $('composer').style.display = 'none';
      $('stream').innerHTML = emptyBlock('Вас убрали из комнаты',
        data.by + ' убрал вас из «' + data.room_name + '». Зайти снова можно из списка слева.');
    }
    toast(data.by + ' убрал вас из комнаты «' + data.room_name + '»', 'error');
    loadRooms().catch(function () { /* список обновится позже */ });
  });

  socket.on('system_notice', function (data) {
    if (data.channel_id === RTC.channelId) toast(data.message, 'ok');
  });

  socket.on('new_message', function (message) {
    if (message.nonce && state.pending.has(message.nonce)) {
      state.pending.get(message.nonce).remove();
      state.pending.delete(message.nonce);
    }
    if (!state.channel || message.channel_id !== state.channel.id) return;
    appendMessage(message);
  });

  socket.on('message_deleted', function (data) {
    state.messages = state.messages.filter(function (m) { return m.id !== data.id; });
    const node = $('stream').querySelector('[data-id="' + data.id + '"]');
    if (node) node.remove();
  });

  socket.on('typing', function (data) {
    if (!state.channel || data.channel_id !== state.channel.id) return;
    $('typing').textContent = data.display_name + ' печатает…';
    clearTimeout(state.typingTimers[data.user_id]);
    state.typingTimers[data.user_id] = setTimeout(function () { $('typing').textContent = ''; }, 3200);
  });

  socket.on('voice_snapshot', function (data) {
    Object.keys(data.channels).forEach(function (id) {
      state.presence[parseInt(id, 10)] = data.channels[id];
    });
    Object.keys(data.locks || {}).forEach(function (id) {
      state.locks[parseInt(id, 10)] = data.locks[id];
    });
    renderChannels();
    renderMembers();
    renderBroadcast();
    updateLockUi();
  });

  socket.on('voice_presence', function (data) {
    state.presence[data.channel_id] = data.peers;
    state.locks[data.channel_id] = data.lock || null;
    renderChannels();
    renderMembers();
    renderBroadcast();
    updateLockUi();
    updateVoiceButtons();
  });

  socket.on('peer_update', function (data) {
    const list = state.presence[data.channel_id];
    if (!list) return;
    const index = list.findIndex(function (p) { return p.sid === data.peer.sid; });
    if (index >= 0) list[index] = data.peer;
    renderChannels();
    renderMembers();
    renderBroadcast();
  });

  socket.on('lock_state', function (data) {
    const was = state.locks[data.channel_id];
    state.locks[data.channel_id] = data.lock || null;
    updateLockUi();
    updateVoiceButtons();
    renderChannels();

    if (data.channel_id !== RTC.channelId) return;
    if (data.lock && !was) {
      // владелец, админ и тот, кто нажал, продолжают говорить
      if (lockHitsMe()) {
        RTC.setMicEnabled(false);
        toast(data.lock.name + ' включил тишину — ваш микрофон выключен', 'error');
      } else {
        toast('Тишина включена: микрофоны остальных выключены', 'ok');
      }
    } else if (!data.lock && was) {
      toast('Тишина снята — микрофон можно включить', 'ok');
    }
    updateVoiceButtons();
  });

  socket.on('mic_blocked', function (data) {
    toast('Микрофон заблокирован: тишину включил ' + data.by, 'error');
    updateVoiceButtons();
  });

  socket.on('hand_raised', function (data) {
    if (data.display_name === state.me.display_name) return;
    toast(data.display_name + ' поднял руку', 'hand');
    SFX.peerIn();
  });

  socket.on('role_changed', function (data) {
    const member = state.members.find(function (m) { return m.id === data.user_id; });
    if (member) member.role = data.role;
    if (data.user_id === state.me.id) {
      state.myRole = data.role;
      toast(data.role === 'moderator'
        ? 'Вас назначили модератором комнаты' : 'С вас сняли права модератора',
        data.role === 'moderator' ? 'ok' : 'error');
      api('/api/rooms/' + data.room_id).then(function (fresh) {
        state.canManage = fresh.can_manage;
        state.isBoss = fresh.is_boss;
        $('btn-new-channel').hidden = !state.canManage;
    $('btn-room-settings').hidden = !state.isBoss;
        renderChannels();
      }).catch(function () { /* не критично */ });
    }
    renderMembers();
    updateLockUi();
  });

  socket.on('room_updated', function (data) {
    applyRoomUpdate(data.room);
    const listed = state.rooms.find(function (r) { return r.id === data.room.id; });
    if (listed) { listed.name = data.room.name; listed.locked = data.room.locked; renderRooms(); }
  });

  socket.on('error_notice', function (data) { toast(data.message, 'error'); });

  socket.on('disconnect', function () { serverStatus(false); leaveVoice(); });

  function updateVoiceStatus() {
    const summary = RTC.connectionSummary();
    const status = summary.failed ? 'Связь не установлена'
      : !summary.total ? 'Ждём собеседника'
      : summary.connected < summary.total ? 'Соединяем участников…'
      : 'На связи · ' + (summary.connected + 1) + ' участника' + (summary.relayed ? ' · через сервер' : '');
    $('voice-status-text').textContent = status + (RTC.hasMic() ? '' : ' · только слушаю');
    $('voice-deck').classList.toggle('has-error', summary.failed);
  }
  RTC.on('connection', updateVoiceStatus);
  RTC.on('playback', function (data) { $('audio-banner').hidden = !data.blocked; });
  $('btn-enable-audio').addEventListener('click', function () {
    RTC.resumeAudio().catch(function (err) { toast(err.message, 'error'); });
  });

  async function showDiagnostics() {
    const box = $('connection-details');
    try {
      const d = await RTC.diagnostics();
      const row = function (name, value, ok) {
        return '<div class="diagnostic-row"><span>' + esc(name) + '</span><b class="' +
          (ok ? 'text-ok' : 'text-warn') + '">' + esc(value) + '</b></div>';
      };
      box.innerHTML = row('Сервер', d.signaling ? 'Подключён' : 'Нет связи', d.signaling) +
        row('Микрофон', d.microphone ? (d.muted ? 'Выключен' : 'Включён') : 'Не используется', d.microphone && !d.muted) +
        row('Воспроизведение', d.blockedAudio ? 'Нажмите «Включить звук»' : 'Разрешено', !d.blockedAudio) +
        (RTC.channelId ? row('Связь через сервер', d.serverRelay ? 'Доступна автоматически' : 'Недоступна', d.serverRelay) : '') +
        (RTC.channelId && d.hasTurn ? row('TURN', 'Настроен', true) : '') +
        d.peers.map(function (peer) {
          const member = state.members.find(function (m) { return m.id === peer.userId; });
          const label = peer.state === 'connected' ? 'Связь установлена' : peer.failed || peer.state === 'failed' ? 'Нет связи' : 'Подключение…';
          return '<div class="diagnostic-peer">' + row(member ? member.display_name : 'Участник', label, peer.state === 'connected') +
            '<p class="mono-note">Получено аудиопакетов: ' + peer.packets +
            (peer.rtt !== null ? ' · задержка ' + peer.rtt + ' мс' : '') +
            (peer.route === 'server' ? ' · через сервер' : peer.route === 'relay' ? ' · через TURN' : peer.route !== '—' ? ' · напрямую' : '') +
            (peer.route === 'server' && peer.frames ? ' · кадров экрана: ' + peer.frames : '') + '</p></div>';
        }).join('') +
        '<p class="hint">' + esc(d.lastError || (!RTC.channelId ? 'Зайдите в голосовой канал, чтобы проверить связь с участниками.' :
          d.serverRelay ? 'Если прямой маршрут недоступен, голос и экран автоматически передаются через сервер сайта. В резервном режиме экран — до 720p и 5 кадров/с.' :
          !d.hasTurn ? 'В этой версии сервера нет резервной передачи. Обновите приложение или подключите TURN.' :
          'Если звука нет, проверьте громкость участника и разрешение микрофона в браузере.')) + '</p>';
      $('btn-reconnect').disabled = !RTC.channelId || !d.peers.length;
    } catch (err) { box.textContent = 'Не удалось прочитать состояние соединения. Повторите проверку.'; }
  }
  $('btn-connection').addEventListener('click', function () { openModal('modal-connection'); showDiagnostics(); });
  $('btn-refresh-connection').addEventListener('click', showDiagnostics);
  $('btn-reconnect').addEventListener('click', function () { RTC.reconnect(); showDiagnostics(); });

  // ------------------------------------------------------------- старт

  renderMe();
  document.querySelectorAll('[title]').forEach(function (el) {
    if (el.matches('button, a') && !el.getAttribute('aria-label')) el.setAttribute('aria-label', el.title);
  });
  updateVoiceButtons();
  RTC.setScreenAudioMode(recallText('screenaudio', 'duck'));
})();
