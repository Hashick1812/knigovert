'use strict';

const TOKEN = document.querySelector('meta[name="kv-token"]').content;
const $ = (id) => document.getElementById(id);

const SHORT_LABEL = { pdf: 'PDF', docx: 'DOCX', fb2: 'FB2', 'fb2.zip': 'FB2 в архиве', epub: 'EPUB', txt: 'TXT', html: 'HTML', md: 'Markdown' };
const FORMAT_NOTE = {
  pdf: 'Страницы выглядят одинаково на любом устройстве и при печати.',
  docx: 'Откроется в Word, текст можно редактировать. Сноски и оглавление сохраняются.',
  fb2: 'Для читалок и телефонов. Текст подстраивается под размер экрана.',
  'fb2.zip': 'Тот же FB2, но сжатый: файл меньше, читалки открывают его так же.',
  epub: 'Для читалок, Apple Books и большинства приложений для чтения.',
  txt: 'Только текст, без оформления и картинок. Откроется где угодно.',
  html: 'Один файл для браузера, картинки хранятся внутри него.',
  md: 'Текст с простой разметкой. Картинки лягут в папку рядом с файлом.',
};
// Книжный коленкор: цвет, ширина и высота корешка.
const CLOTH = {
  pdf: ['#8a2c38', 47, 170],
  docx: ['#2b4a7d', 42, 150],
  fb2: ['#2d6a4e', 38, 178],
  'fb2.zip': ['#22503b', 50, 160],
  epub: ['#c28a2b', 41, 146],
  txt: ['#6a6e75', 36, 172],
  html: ['#5a3e8c', 44, 154],
  md: ['#3b6264', 42, 164],
};
const SOURCE_COLORS = ['#2b4a7d', '#8a2c38', '#2d6a4e', '#c28a2b', '#5a3e8c', '#3b6264', '#6a6e75', '#7d4b2a'];

const state = {
  info: null,
  target: 'pdf',
  options: { page_size: 'A5', font_size: 11, toc: true, title_page: true, page_numbers: true },
  files: [],
  rowCounter: 0,
  book: {
    chapters: [],
    meta: { title: '', authors: '', annotation: '', lang: '', cover_id: null },
    format: 'pdf',
    current: null,
    checked: new Set(),
    undo: [],
    redo: [],
    sources: new Map(),
    pending: [],
    previews: new Map(),
    saving: false,
    busy: false,
  },
};

// --- Служебное ------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key in node && key !== 'list') node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function svgIcon(name) {
  const holder = document.createElement('span');
  holder.innerHTML = `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  return holder.firstElementChild;
}

async function api(path, body) {
  const options = { headers: { 'X-Token': TOKEN } };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Приложение не смогло выполнить действие.');
  return data;
}

async function uploadFile(file) {
  const response = await fetch('/api/files', {
    method: 'POST',
    headers: { 'X-Token': TOKEN, 'X-Filename': encodeURIComponent(file.name) },
    body: file,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Не удалось прочитать файл.');
  return data;
}

async function waitJob(jobId, onUpdate) {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (onUpdate) onUpdate(job);
    if (job.status === 'done') return job;
    if (job.status === 'error') throw new Error(job.error || 'Не удалось обработать файл.');
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

function toast(message, kind = '') {
  const close = el('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Закрыть сообщение' }, svgIcon('close'));
  const node = el('div', { class: `toast ${kind}` }, el('p', { text: message }), close);
  const remove = () => node.remove();
  close.addEventListener('click', remove);
  $('toasts').append(node);
  // Ошибку нельзя пропустить: она исчезает только по нажатию.
  if (kind !== 'error') setTimeout(remove, 6000);
}

function plural(count, one, few, many) {
  const tens = count % 100;
  const units = count % 10;
  if (tens >= 11 && tens <= 14) return many;
  if (units === 1) return one;
  if (units >= 2 && units <= 4) return few;
  return many;
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1).replace('.', ',')} МБ`;
}

const formatNumber = (value) => value.toLocaleString('ru-RU');

function openResult(path, reveal) {
  api('/api/open', { path, reveal }).catch((error) => toast(error.message, 'error'));
}

function resultActions(result) {
  return [
    el('button', { class: 'link', type: 'button', text: 'Открыть', onclick: () => openResult(result.path, false) }),
    el('button', { class: 'link', type: 'button', text: 'Показать в папке', onclick: () => openResult(result.path, true) }),
  ];
}

// --- Настройки формата ----------------------------------------------------

function saveOptions() {
  const payload = { ...state.options, target: state.target, book_format: state.book.format };
  api('/api/options', { options: payload }).catch(() => {});
}

function optionCheck(key, label, onChange) {
  const input = el('input', { type: 'checkbox', checked: state.options[key] !== false });
  input.addEventListener('change', () => {
    state.options[key] = input.checked;
    saveOptions();
    onChange();
  });
  return el('label', { class: 'check' }, input, el('span', { text: label }));
}

function optionSelect(key, label, values, onChange) {
  const select = el('select');
  for (const [value, text] of values) {
    select.append(el('option', { value: String(value), text, selected: String(state.options[key]) === String(value) }));
  }
  select.addEventListener('change', () => {
    state.options[key] = key === 'font_size' ? Number(select.value) : select.value;
    saveOptions();
    onChange();
  });
  return el('label', { class: 'field' }, el('span', { text: label }), select);
}

function renderOptions(container, target) {
  const again = () => renderAllOptions();
  container.replaceChildren();
  if (target === 'pdf') {
    const sizes = [['A4', 'A4'], ['A5', 'A5 (как книга)'], ['B6', 'B6 (карманная)'], ['Letter', 'Letter']];
    const fonts = [9, 10, 11, 12, 13, 14, 16].map((size) => [size, `${size} пт`]);
    container.append(
      el('div', { class: 'pair' }, optionSelect('page_size', 'Размер страницы', sizes, again), optionSelect('font_size', 'Шрифт', fonts, again)),
      optionCheck('toc', 'Оглавление с номерами страниц', again),
      optionCheck('title_page', 'Титульная страница', again),
      optionCheck('page_numbers', 'Номера страниц', again),
    );
  } else if (target === 'docx') {
    container.append(optionCheck('toc', 'Оглавление', again), optionCheck('title_page', 'Титульная страница', again));
  } else if (target === 'html') {
    container.append(optionCheck('toc', 'Оглавление', again));
  }
}

function renderAllOptions() {
  renderOptions($('convert-options'), state.target);
  const keepPages = keepPagesActive();
  $('keep-pages-row').hidden = !keepPagesAvailable();
  renderOptions($('book-options'), keepPages ? '' : state.book.format);
}

// --- Конвертация ----------------------------------------------------------

function renderShelf() {
  const shelf = $('shelf');
  shelf.replaceChildren();
  for (const format of state.info.formats) {
    const [cloth, width, height] = CLOTH[format.key] || ['#6a6e75', 48, 160];
    const input = el('input', { type: 'radio', name: 'target', value: format.key, checked: format.key === state.target });
    input.addEventListener('change', () => {
      state.target = format.key;
      saveOptions();
      renderTarget();
      renderFiles();
    });
    const face = el('span', { class: 'spine-face' }, el('span', { text: SHORT_LABEL[format.key] || format.label }));
    face.style.setProperty('--cloth', cloth);
    face.style.setProperty('--w', `${width}px`);
    face.style.setProperty('--h', `${height}px`);
    if (format.key === 'epub') face.style.setProperty('--title', '#22180a');
    shelf.append(el('label', { class: 'spine', title: format.label }, input, face));
  }
}

function renderTarget() {
  $('target-note').textContent = FORMAT_NOTE[state.target] || '';
  renderOptions($('convert-options'), state.target);
  updateRunButton();
}

function needsConversion(row) {
  if (row.status !== 'ready' && row.status !== 'done') return false;
  if (row.error) return false;
  if (row.format === state.target) return false;
  return !row.results.some((item) => item.target === state.target);
}

function updateRunButton() {
  const button = $('convert-run');
  const label = SHORT_LABEL[state.target];
  const usable = state.files.filter((row) => !row.error && row.status !== 'uploading');
  const pending = state.files.filter(needsConversion);
  const busy = state.files.some((row) => row.status === 'working' || row.status === 'uploading');
  button.disabled = pending.length === 0;
  button.textContent = pending.length > 1
    ? `Сделать ${label}: ${pending.length} ${plural(pending.length, 'файл', 'файла', 'файлов')}`
    : `Сделать ${label}`;
  let hint = '';
  if (!state.files.length) hint = 'Сначала добавьте файлы.';
  else if (!pending.length && !busy && usable.length) {
    const same = usable.every((row) => row.format === state.target);
    hint = same ? `Эти файлы уже в формате ${label}. Выберите другую книгу на полке.` : `Все файлы уже в формате ${label}.`;
  }
  $('convert-hint').textContent = hint;
}

function fileRow(row) {
  const remove = el('button', { class: 'icon-btn', type: 'button', 'aria-label': `Убрать ${row.name} из списка`, title: 'Убрать из списка' }, svgIcon('close'));
  remove.addEventListener('click', () => {
    state.files = state.files.filter((item) => item !== row);
    renderFiles();
  });
  const node = el('div', { class: 'file-row' }, el('div', { class: 'file-name', text: row.name }), remove);
  const details = [row.format ? SHORT_LABEL[row.format] || row.format.toUpperCase() : '', formatSize(row.size)].filter(Boolean).join(', ');
  node.append(el('div', { class: 'file-meta', text: details }));
  if (row.status === 'uploading' || row.status === 'working') {
    const fill = el('div', { class: 'progress-fill' });
    const bar = el('div', { class: 'progress-bar unknown' }, fill);
    const stage = el('div', { class: 'progress-stage', text: row.status === 'uploading' ? 'Открываю файл' : 'В очереди' });
    row.bar = bar;
    row.fill = fill;
    row.stage = stage;
    node.append(el('div', { class: 'progress' }, bar, stage));
  }
  if (row.error) node.append(el('div', { class: 'file-error', text: row.error }));
  for (const item of row.results) {
    node.append(el('div', { class: 'result' }, el('span', { class: 'result-name', text: item.name }), ...resultActions(item)));
    for (const warning of item.warnings) node.append(el('div', { class: 'file-warning', text: warning }));
  }
  return node;
}

function renderFiles() {
  const list = $('file-list');
  list.replaceChildren(...state.files.map(fileRow));
  const drop = $('convert-drop');
  drop.classList.toggle('compact', state.files.length > 0);
  drop.classList.toggle('is-empty', state.files.length === 0);
  drop.querySelector('.drop-title').textContent = state.files.length ? 'Перетащите ещё файлы или' : 'Перетащите сюда файлы книг';
  updateRunButton();
}

async function addConvertFiles(files) {
  const rows = files.map((file) => ({ key: ++state.rowCounter, file, name: file.name, size: file.size, status: 'uploading', format: '', error: '', results: [] }));
  state.files.push(...rows);
  renderFiles();
  await Promise.all(rows.map(async (row) => {
    try {
      const info = await uploadFile(row.file);
      row.id = info.id;
      row.format = info.format;
      row.error = info.error;
      row.status = info.error ? 'failed' : 'ready';
    } catch (error) {
      row.error = error.message;
      row.status = 'failed';
    }
    row.file = null;
  }));
  renderFiles();
}

async function convertRow(row, target) {
  row.status = 'working';
  row.error = '';
  renderFiles();
  try {
    const { job_id: jobId } = await api('/api/convert', { file_id: row.id, target, options: state.options });
    const job = await waitJob(jobId, (update) => {
      if (row.stage) row.stage.textContent = update.stage;
      if (row.bar && update.progress !== null && update.progress !== undefined) {
        row.bar.classList.remove('unknown');
        row.fill.style.width = `${Math.round(update.progress * 100)}%`;
      } else if (row.bar) {
        row.bar.classList.add('unknown');
      }
    });
    row.results.unshift({ ...job.result, target, warnings: job.warnings });
    row.status = 'done';
  } catch (error) {
    row.error = error.message;
    row.status = 'ready';
    row.failedTarget = target;
  }
  renderFiles();
}

function runConversion() {
  const target = state.target;
  const rows = state.files.filter(needsConversion);
  // Очередь и ограничение числа задач держит приложение.
  for (const row of rows) convertRow(row, target);
}

// --- Сборка книги: состояние ---------------------------------------------

const book = state.book;
const snapshot = () => JSON.stringify(book.chapters);

function pushUndo() {
  book.undo.push(snapshot());
  if (book.undo.length > 200) book.undo.shift();
  book.redo.length = 0;
}

function bookChanged() {
  const known = new Set(book.chapters.map((chapter) => chapter.id));
  for (const id of [...book.checked]) if (!known.has(id)) book.checked.delete(id);
  if (book.current && !known.has(book.current)) book.current = book.chapters[0] ? book.chapters[0].id : null;
  api('/api/dirty', { dirty: book.chapters.length > 0 }).catch(() => {});
  renderBuilder();
}

function restore(from, to) {
  if (!from.length || book.busy) return;
  to.push(snapshot());
  book.chapters = JSON.parse(from.pop());
  bookChanged();
  renderPreview();
}

function sourceColor(source) {
  if (!book.sources.has(source)) book.sources.set(source, SOURCE_COLORS[book.sources.size % SOURCE_COLORS.length]);
  return book.sources.get(source);
}

/** Главы, к которым применяется действие: отмеченные или текущая. */
function targetChapters() {
  if (book.checked.size) return book.chapters.filter((chapter) => book.checked.has(chapter.id));
  return book.chapters.filter((chapter) => chapter.id === book.current);
}

function moveSelected(delta) {
  const ids = new Set(targetChapters().map((chapter) => chapter.id));
  if (!ids.size) return;
  const list = book.chapters;
  const order = delta < 0 ? list.map((_, index) => index) : list.map((_, index) => list.length - 1 - index);
  let moved = false;
  const next = [...list];
  for (const index of order) {
    const swap = index + delta;
    if (ids.has(next[index].id) && swap >= 0 && swap < next.length && !ids.has(next[swap].id)) {
      if (!moved) pushUndo();
      [next[index], next[swap]] = [next[swap], next[index]];
      moved = true;
    }
  }
  if (!moved) return;
  book.chapters = next;
  bookChanged();
  const focusId = [...ids][0];
  const row = document.querySelector(`.chapter[data-id="${focusId}"]`);
  if (row) row.scrollIntoView({ block: 'nearest' });
}

function moveTo(ids, beforeId) {
  const moving = book.chapters.filter((chapter) => ids.has(chapter.id));
  const rest = book.chapters.filter((chapter) => !ids.has(chapter.id));
  const position = beforeId === null ? rest.length : rest.findIndex((chapter) => chapter.id === beforeId);
  pushUndo();
  rest.splice(position < 0 ? rest.length : position, 0, ...moving);
  book.chapters = rest;
  bookChanged();
}

function changeLevel(delta) {
  const chapters = targetChapters();
  if (!chapters.length) return;
  pushUndo();
  for (const chapter of chapters) chapter.level = Math.min(6, Math.max(1, chapter.level + delta));
  bookChanged();
}

function removeSelected() {
  const ids = new Set(targetChapters().map((chapter) => chapter.id));
  if (!ids.size) return;
  pushUndo();
  book.chapters = book.chapters.filter((chapter) => !ids.has(chapter.id));
  bookChanged();
  renderPreview();
  toast(`Удалено глав: ${ids.size}. Отменить можно кнопкой со стрелкой или Ctrl+Z.`);
}

/** Пока приложение объединяет или делит главы, список менять нельзя: результат встанет не туда. */
async function exclusive(action) {
  if (book.busy) return;
  book.busy = true;
  updateTools();
  try {
    await action();
  } catch (error) {
    toast(error.message, 'error');
  }
  book.busy = false;
  updateTools();
}

function mergeSelected() {
  return exclusive(async () => {
    const chapters = book.chapters.filter((chapter) => book.checked.has(chapter.id));
    if (chapters.length < 2) return;
    const merged = await api('/api/builder/merge', {
      ids: chapters.map((chapter) => chapter.id),
      titles: chapters.map((chapter) => chapter.title),
      title: chapters[0].title,
      keep_titles: $('merge-keep').checked,
    });
    pushUndo();
    merged.level = chapters[0].level;
    const position = book.chapters.findIndex((chapter) => chapter.id === chapters[0].id);
    const ids = new Set(chapters.map((chapter) => chapter.id));
    book.chapters = book.chapters.filter((chapter) => !ids.has(chapter.id));
    book.chapters.splice(position, 0, merged);
    book.checked.clear();
    book.current = merged.id;
    bookChanged();
    renderPreview();
  });
}

function splitChapter(chapterId, at) {
  return exclusive(async () => {
    const chapter = book.chapters.find((item) => item.id === chapterId);
    if (!chapter) return;
    const parts = await api('/api/builder/split', { id: chapterId, at, title: '' });
    pushUndo();
    parts.first.title = chapter.title;
    parts.first.level = chapter.level;
    parts.second.level = chapter.level;
    book.chapters.splice(book.chapters.findIndex((item) => item.id === chapterId), 1, parts.first, parts.second);
    book.current = parts.second.id;
    bookChanged();
    renderPreview();
    const input = document.querySelector(`.chapter[data-id="${parts.second.id}"] .chapter-title`);
    if (input) input.focus();
  });
}

async function addBuilderFiles(files) {
  const split = $('builder-split').checked;
  const entries = files.map((file) => ({ name: file.name, stage: 'Открываю файл', file }));
  book.pending.push(...entries);
  renderBuilder();
  // Файлы добавляются строго по порядку, иначе главы перемешаются.
  for (const entry of entries) {
    try {
      const info = await uploadFile(entry.file);
      if (info.error) throw new Error(`${entry.name}: ${info.error}`);
      const { job_id: jobId } = await api('/api/builder/add', { file_id: info.id, split });
      const job = await waitJob(jobId, (update) => {
        if (entry.stage !== update.stage) {
          entry.stage = update.stage;
          renderPending();
        }
      });
      const wasEmpty = book.chapters.length === 0;
      pushUndo();
      book.chapters.push(...job.result.chapters);
      const meta = job.result.meta;
      if (wasEmpty && !book.meta.title) {
        book.meta = { ...book.meta, ...meta };
        syncMetaFields();
      }
      if (!book.current && book.chapters.length) book.current = book.chapters[0].id;
      for (const warning of job.warnings) toast(`${entry.name}: ${warning}`);
      book.pending = book.pending.filter((item) => item !== entry);
      bookChanged();
      if (wasEmpty) renderPreview();
    } catch (error) {
      book.pending = book.pending.filter((item) => item !== entry);
      toast(error.message.startsWith(entry.name) ? error.message : `${entry.name}: ${error.message}`, 'error');
      renderBuilder();
    }
  }
}

// --- Сборка книги: отрисовка ----------------------------------------------

function renderPending() {
  document.querySelectorAll('.pending-row').forEach((node) => node.remove());
  const list = $('chapter-list');
  for (const entry of book.pending) {
    list.append(el('li', { class: 'pending-row', text: `${entry.name}: ${entry.stage.toLowerCase()}` }));
  }
}

function chapterRow(chapter) {
  const checkbox = el('input', { type: 'checkbox', checked: book.checked.has(chapter.id), 'aria-label': `Выбрать главу ${chapter.title || 'без названия'}` });
  checkbox.addEventListener('click', (event) => {
    event.stopPropagation();
    if (checkbox.checked) book.checked.add(chapter.id);
    else book.checked.delete(chapter.id);
    updateTools();
  });

  const title = el('input', {
    class: 'chapter-title',
    type: 'text',
    value: chapter.title,
    placeholder: 'Без названия',
    'aria-label': 'Название главы',
  });
  let before = chapter.title;
  title.addEventListener('focus', () => {
    before = chapter.title;
    selectCurrent(chapter.id);
  });
  title.addEventListener('input', () => {
    chapter.title = title.value;
    if (book.current === chapter.id) $('preview-title').textContent = chapter.title || 'Без названия';
  });
  title.addEventListener('change', () => {
    if (chapter.title === before) return;
    const current = chapter.title;
    chapter.title = before;
    pushUndo();
    chapter.title = current;
    before = current;
    updateTools();
  });
  title.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') title.blur();
    if (event.key === 'Escape') {
      title.value = before;
      chapter.title = before;
      title.blur();
    }
  });

  const grip = el('span', { class: 'grip', draggable: 'true', title: 'Перетащите, чтобы изменить порядок' }, svgIcon('grip'));
  const cloth = el('span', { class: 'chapter-cloth', title: chapter.source });
  cloth.style.setProperty('--cloth', sourceColor(chapter.source));
  const body = el('div', { class: 'chapter-body' }, title);
  body.style.setProperty('--level', chapter.level);
  const words = el('span', { class: 'chapter-words', text: chapter.words ? `${formatNumber(chapter.words)} ${plural(chapter.words, 'слово', 'слова', 'слов')}` : `${chapter.blocks} ${plural(chapter.blocks, 'блок', 'блока', 'блоков')}` });

  const row = el('li', { class: `chapter${book.current === chapter.id ? ' current' : ''}`, 'data-id': chapter.id, tabindex: '-1' }, grip, checkbox, cloth, body, words);
  row.addEventListener('click', () => selectCurrent(chapter.id));

  grip.addEventListener('dragstart', (event) => {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('application/x-knigovert-chapter', chapter.id);
    event.dataTransfer.setDragImage(row, 10, 12);
    dragging = book.checked.has(chapter.id) ? new Set(book.checked) : new Set([chapter.id]);
    setTimeout(() => {
      for (const id of dragging || []) {
        const node = document.querySelector(`.chapter[data-id="${id}"]`);
        if (node) node.classList.add('dragging');
      }
    });
  });
  grip.addEventListener('dragend', () => {
    dragging = null;
    document.querySelectorAll('.chapter').forEach((node) => node.classList.remove('dragging', 'drop-before', 'drop-after'));
  });
  row.addEventListener('dragover', (event) => {
    if (!dragging) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const box = row.getBoundingClientRect();
    const after = event.clientY > box.top + box.height / 2;
    row.classList.toggle('drop-after', after);
    row.classList.toggle('drop-before', !after);
  });
  row.addEventListener('dragleave', () => row.classList.remove('drop-before', 'drop-after'));
  row.addEventListener('drop', (event) => {
    if (!dragging) return;
    event.preventDefault();
    const after = row.classList.contains('drop-after');
    row.classList.remove('drop-before', 'drop-after');
    if (dragging.has(chapter.id)) return;
    const position = book.chapters.indexOf(chapter);
    let beforeId = chapter.id;
    if (after) {
      const next = book.chapters.slice(position + 1).find((item) => !dragging.has(item.id));
      beforeId = next ? next.id : null;
    }
    moveTo(dragging, beforeId);
  });
  return row;
}

let dragging = null;

function selectCurrent(id) {
  if (book.current === id) return;
  book.current = id;
  document.querySelectorAll('.chapter').forEach((node) => node.classList.toggle('current', node.dataset.id === id));
  document.querySelectorAll('.strip button').forEach((node) => node.classList.toggle('current', node.dataset.id === id));
  updateTools();
  renderPreview();
}

function renderStrip() {
  const strip = $('book-strip');
  strip.hidden = book.chapters.length === 0;
  strip.replaceChildren(...book.chapters.map((chapter) => {
    const part = el('button', {
      type: 'button',
      class: book.current === chapter.id ? 'current' : '',
      'data-id': chapter.id,
      title: `${chapter.title || 'Без названия'} (${chapter.source})`,
      'aria-label': `Перейти к главе ${chapter.title || 'без названия'}`,
      tabIndex: -1,
    });
    part.style.setProperty('--cloth', sourceColor(chapter.source));
    part.style.setProperty('--words', Math.max(chapter.words, 20));
    part.addEventListener('click', () => {
      selectCurrent(chapter.id);
      const row = document.querySelector(`.chapter[data-id="${chapter.id}"]`);
      if (row) row.scrollIntoView({ block: 'nearest' });
    });
    return part;
  }));
}

function updateTools() {
  const hasTarget = targetChapters().length > 0 && !book.busy;
  const count = book.checked.size;
  for (const id of ['tool-up', 'tool-down', 'tool-indent', 'tool-outdent', 'tool-remove']) $(id).disabled = !hasTarget;
  $('tool-merge').disabled = count < 2 || book.busy;
  $('tool-merge').textContent = count >= 2 ? `Объединить ${count} ${plural(count, 'главу', 'главы', 'глав')}` : 'Объединить';
  $('tool-merge').title = count < 2 ? 'Отметьте галочками две или больше глав' : '';
  $('merge-keep-row').hidden = count < 2;
  $('tool-undo').disabled = !book.undo.length || book.busy;
  $('tool-redo').disabled = !book.redo.length || book.busy;
  const all = $('check-all');
  all.checked = count > 0 && count === book.chapters.length;
  all.indeterminate = count > 0 && count < book.chapters.length;
}

function keepPagesAvailable() {
  return book.format === 'pdf' && book.chapters.length > 0 && book.chapters.every((chapter) => chapter.pdf_pages);
}

function keepPagesActive() {
  return keepPagesAvailable() && $('keep-pages').checked;
}

function renderBuilder() {
  const hasContent = book.chapters.length > 0 || book.pending.length > 0;
  $('builder-empty').hidden = hasContent;
  $('chapter-tools').hidden = book.chapters.length === 0;
  const list = $('chapter-list');
  const scroll = list.scrollTop;
  list.replaceChildren(...book.chapters.map(chapterRow));
  renderPending();
  list.scrollTop = scroll;
  renderStrip();
  const words = book.chapters.reduce((sum, chapter) => sum + chapter.words, 0);
  $('book-summary').textContent = book.chapters.length
    ? `${book.chapters.length} ${plural(book.chapters.length, 'глава', 'главы', 'глав')}, ${formatNumber(words)} ${plural(words, 'слово', 'слова', 'слов')}`
    : '';
  $('book-save').disabled = book.chapters.length === 0 || book.saving;
  $('book-save').textContent = book.saving ? 'Сохраняю книгу' : `Сохранить как ${SHORT_LABEL[book.format]}`;
  updateTools();
  renderAllOptions();
}

async function renderPreview() {
  const preview = $('preview');
  const chapter = book.chapters.find((item) => item.id === book.current);
  $('preview-title').textContent = chapter ? chapter.title || 'Без названия' : 'Текст главы';
  $('preview-hint').hidden = Boolean(chapter);
  if (!chapter) {
    preview.replaceChildren();
    return;
  }
  const id = chapter.id;
  let data = book.previews.get(id);
  if (!data) {
    try {
      data = await api(`/api/builder/chapter/${id}`);
      book.previews.set(id, data);
    } catch (error) {
      toast(error.message, 'error');
      return;
    }
  }
  if (book.current !== id) return;
  // Строки приходят от приложения, весь текст книги в них уже экранирован.
  const parts = [];
  data.html.forEach((line) => {
    const match = line.match(/data-block="(\d+)"/);
    const index = match ? Number(match[1]) : 0;
    if (index > 0) {
      parts.push(`<button class="split-gap" type="button" data-at="${index}" tabindex="-1"><span>Разделить главу здесь</span></button>`);
    }
    parts.push(line);
  });
  if (data.notes.length) {
    parts.push('<div class="notes">');
    for (const note of data.notes) {
      parts.push(`<div class="note" id="preview-note-${note.label}"><sup>${note.label}</sup> ${note.html}</div>`);
    }
    parts.push('</div>');
  }
  preview.innerHTML = parts.join('');
  preview.dataset.chapter = id;
  preview.scrollTop = 0;
}

function syncMetaFields() {
  $('book-title').value = book.meta.title || '';
  $('book-authors').value = book.meta.authors || '';
  const thumb = $('cover-thumb');
  const cover = book.meta.cover_id;
  thumb.hidden = !cover;
  $('cover-clear').hidden = !cover;
  $('cover-pick').textContent = cover ? 'Заменить' : 'Выбрать картинку';
  if (cover) thumb.src = `/api/image/${cover}?t=${encodeURIComponent(TOKEN)}`;
}

async function saveBook() {
  book.saving = true;
  renderBuilder();
  const box = $('book-result');
  box.hidden = true;
  try {
    const { job_id: jobId } = await api('/api/builder/export', {
      format: book.format,
      meta: book.meta,
      chapters: book.chapters.map(({ id, title, level }) => ({ id, title, level })),
      options: { ...state.options, keep_pdf_pages: keepPagesActive() },
    });
    const job = await waitJob(jobId, (update) => {
      $('book-save').textContent = update.stage;
    });
    box.replaceChildren(
      el('span', { class: 'result-name', text: job.result.name }),
      el('div', { class: 'actions' }, ...resultActions(job.result)),
      ...job.warnings.map((warning) => el('span', { class: 'file-warning', text: warning })),
    );
    box.hidden = false;
  } catch (error) {
    toast(error.message, 'error');
  }
  book.saving = false;
  renderBuilder();
}

// --- Вкладки, папка, перетаскивание --------------------------------------

function showTab(name) {
  for (const key of ['convert', 'builder']) {
    const selected = key === name;
    const tab = $(`tab-${key}`);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(`view-${key}`).hidden = !selected;
  }
  state.tab = name;
}

function setFolder(path) {
  state.info.output_dir = path;
  $('folder-open').textContent = path;
}

async function changeFolder() {
  try {
    let result = await api('/api/output-dir', {});
    if (result.unsupported) {
      const typed = window.prompt('Полный путь к папке для готовых файлов:', result.output_dir);
      if (!typed) return;
      result = await api('/api/output-dir', { path: typed });
    }
    setFolder(result.output_dir);
  } catch (error) {
    toast(error.message, 'error');
  }
}

function acceptFiles(files) {
  if (!files.length) return;
  if (state.tab === 'builder') addBuilderFiles(files);
  else addConvertFiles(files);
}

function setupDrop() {
  let depth = 0;
  const veil = $('drop-veil');
  const hasFiles = (event) => event.dataTransfer && [...event.dataTransfer.types].includes('Files');
  window.addEventListener('dragenter', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    veil.hidden = false;
  });
  window.addEventListener('dragover', (event) => {
    if (hasFiles(event)) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    }
  });
  window.addEventListener('dragleave', (event) => {
    if (!hasFiles(event)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) veil.hidden = true;
  });
  window.addEventListener('drop', (event) => {
    if (!hasFiles(event)) return;
    // Без этого окно перешло бы к файлу и приложение закрылось.
    event.preventDefault();
    depth = 0;
    veil.hidden = true;
    acceptFiles([...event.dataTransfer.files]);
  });
}

function setupEvents() {
  $('tab-convert').addEventListener('click', () => showTab('convert'));
  $('tab-builder').addEventListener('click', () => showTab('builder'));
  document.querySelector('.tabs').addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const name = state.tab === 'convert' ? 'builder' : 'convert';
    showTab(name);
    $(`tab-${name}`).focus();
  });

  const input = $('file-input');
  const pick = () => input.click();
  $('convert-pick').addEventListener('click', pick);
  $('builder-pick').addEventListener('click', pick);
  input.addEventListener('change', () => {
    acceptFiles([...input.files]);
    input.value = '';
  });
  $('convert-run').addEventListener('click', runConversion);
  $('folder-open').addEventListener('click', () => openResult(state.info.output_dir, false));
  $('folder-change').addEventListener('click', changeFolder);

  $('book-title').addEventListener('input', (event) => { book.meta.title = event.target.value; });
  $('book-authors').addEventListener('input', (event) => { book.meta.authors = event.target.value; });
  $('book-format').addEventListener('change', (event) => {
    book.format = event.target.value;
    saveOptions();
    renderBuilder();
  });
  $('keep-pages').addEventListener('change', renderAllOptions);
  $('book-save').addEventListener('click', saveBook);

  $('cover-pick').addEventListener('click', () => $('cover-input').click());
  $('cover-clear').addEventListener('click', () => {
    book.meta.cover_id = null;
    syncMetaFields();
  });
  $('cover-input').addEventListener('change', async (event) => {
    const [file] = event.target.files;
    event.target.value = '';
    if (!file) return;
    try {
      const info = await uploadFile(file);
      const { cover_id: coverId } = await api('/api/builder/cover', { file_id: info.id });
      book.meta.cover_id = coverId;
      syncMetaFields();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  $('tool-up').addEventListener('click', () => moveSelected(-1));
  $('tool-down').addEventListener('click', () => moveSelected(1));
  $('tool-indent').addEventListener('click', () => changeLevel(1));
  $('tool-outdent').addEventListener('click', () => changeLevel(-1));
  $('tool-merge').addEventListener('click', mergeSelected);
  $('tool-remove').addEventListener('click', removeSelected);
  $('tool-undo').addEventListener('click', () => restore(book.undo, book.redo));
  $('tool-redo').addEventListener('click', () => restore(book.redo, book.undo));
  $('check-all').addEventListener('change', (event) => {
    book.checked = new Set(event.target.checked ? book.chapters.map((chapter) => chapter.id) : []);
    renderBuilder();
  });

  $('preview').addEventListener('click', (event) => {
    const gap = event.target.closest('.split-gap');
    if (gap) {
      splitChapter(book.current, Number(gap.dataset.at));
      return;
    }
    const link = event.target.closest('a');
    if (!link) return;
    // Ссылка из книги не должна увести окно приложения со страницы.
    event.preventDefault();
    const href = link.getAttribute('href') || '';
    if (href.startsWith('#')) {
      const target = document.getElementById(href.slice(1));
      if (target) target.scrollIntoView({ block: 'center' });
    }
  });

  document.addEventListener('keydown', (event) => {
    if (state.tab !== 'builder') return;
    const typing = event.target.matches('input[type="text"], textarea, select');
    if (event.altKey && event.key === 'ArrowUp') { event.preventDefault(); moveSelected(-1); }
    else if (event.altKey && event.key === 'ArrowDown') { event.preventDefault(); moveSelected(1); }
    else if (typing) return;
    else if (event.ctrlKey && event.key.toLowerCase() === 'z') { event.preventDefault(); restore(book.undo, book.redo); }
    else if (event.ctrlKey && event.key.toLowerCase() === 'y') { event.preventDefault(); restore(book.redo, book.undo); }
    else if (event.key === 'Delete' && targetChapters().length) { event.preventDefault(); removeSelected(); }
  });
}

async function start() {
  setupDrop();
  setupEvents();
  try {
    state.info = await api('/api/info');
  } catch (error) {
    toast(`Приложение не запустилось: ${error.message}`, 'error');
    return;
  }
  const saved = state.info.options || {};
  const known = new Set(state.info.formats.map((format) => format.key));
  for (const key of ['page_size', 'font_size', 'toc', 'title_page', 'page_numbers']) {
    if (saved[key] !== undefined) state.options[key] = saved[key];
  }
  if (known.has(saved.target)) state.target = saved.target;
  if (known.has(saved.book_format)) book.format = saved.book_format;
  setFolder(state.info.output_dir);
  // В браузере приложение узнаёт о закрытии вкладки по тому, что сигналы прекратились.
  setInterval(() => { api('/api/ping').catch(() => {}); }, 20000);
  const select = $('book-format');
  for (const format of state.info.formats) {
    select.append(el('option', { value: format.key, text: format.label, selected: format.key === book.format }));
  }
  renderShelf();
  renderTarget();
  renderFiles();
  renderBuilder();
  syncMetaFields();
}

start();
