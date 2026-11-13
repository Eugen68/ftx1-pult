'use strict';
// ======================================================================= Редактор каналов памяти (как ADMS / RT Systems)
const memEd = (() => {
  const CH_MIN = 1, CH_MAX = 99;
  const FM = ['FM', 'FM-N', 'DATA-FM', 'DATA-FM-N'];
  const FIELDS = ['empty', 'freq', 'mode', 'name', 'shift', 'tone_mode', 'ctcss', 'dcs', 'clar', 'rx_clar', 'tx_clar'];
  const GCOL = ['#4cc9f0', '#f8961e', '#90be6d', '#f94144', '#b5179e', '#ffd166', '#43aa8b', '#9d8df1', '#e76f51', '#6c9bd2'];
  const st = {
    chans: {}, rig: {}, groups: [], file: '', info: null, sel: new Set(), filterGrp: '*', busy: false,
    status: {},              // ch -> {ok, msg}
  };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const blank = ch => ({ ch, empty: true, freq: 0, mode: 'FM', name: '', clar: 0, rx_clar: false, tx_clar: false, tone_mode: 0,
    ctcss: 88.5, dcs: 23, shift: 0, offset_khz: 0, group: '', comment: '' });
  const defOffset = f => { const m = f / 1e6; return m >= 28 && m < 30 ? 100 : m >= 50 && m < 54 ? 600 : m >= 144 && m < 148 ? 600 : m >= 430 && m < 440 ? 7600 : 0; };
  const fmtMHz = f => f ? (f / 1e6).toFixed(6).replace(/0{1,3}$/, '') : '';
  const allCh = () => { const a = []; for (let i = CH_MIN; i <= CH_MAX; i++) a.push(i); for (const k of Object.keys(st.info?.pms || {})) if (st.chans[k]) a.push(+k); return a; };
  const label = ch => st.info?.pms?.[ch] || String(ch).padStart(2, '0');
  const txFreq = c => c.freq + (c.shift === 1 ? 1 : c.shift === 2 ? -1 : 0) * (c.offset_khz || 0) * 1000;
  const same = (a, b) => FIELDS.every(k => (a.empty && b.empty) || (k === 'ctcss' ? Math.abs(a[k] - b[k]) < 0.05 : String(a[k] ?? '') === String(b[k] ?? '')))
    || (a.empty && b.empty);
  const isDirty = ch => { const c = st.chans[ch], r = st.rig[ch]; if (!r) return !c.empty; return !same(c, r); };
  const grpColor = g => GCOL[Math.max(0, st.groups.indexOf(g)) % GCOL.length];

  function ensure() { for (let i = CH_MIN; i <= CH_MAX; i++) if (!st.chans[i]) st.chans[i] = blank(i); }
  function saveDraft() { store.set('memDraft', { chans: st.chans, rig: st.rig, groups: st.groups, file: st.file }); }
  function loadDraft() {
    const d = store.get('memDraft', null);
    if (d && d.chans) { st.chans = d.chans; st.rig = d.rig || {}; st.groups = d.groups || []; st.file = d.file || ''; }
    ensure();
  }

  // ------------------------------------------------------------------ отрисовка
  function opts(list, cur, fmt = v => v) { return list.map(v => `<option value="${esc(v)}"${String(v) === String(cur) ? ' selected' : ''}>${esc(fmt(v))}</option>`).join(''); }
  function rowHtml(c) {
    const ch = c.ch, fm = FM.includes(c.mode), e = c.empty;
    const modes = st.info?.modes || ['LSB', 'USB', 'CW-U', 'CW-L', 'FM', 'FM-N', 'AM', 'AM-N', 'DATA-U', 'DATA-L', 'DATA-FM', 'RTTY-U', 'RTTY-L', 'PSK'];
    const ctcss = st.info?.ctcss || [88.5], dcs = st.info?.dcs || [23];
    const s = st.status[ch];
    const stIcon = s ? (s.ok ? '<span class="ok" title="Записан и проверен">✔</span>' : `<span class="bad" title="${esc(s.msg)}">⚠</span>`) : '';
    const grpSel = `<select data-f="group" style="border-left:4px solid ${c.group ? grpColor(c.group) : 'transparent'}"><option value=""></option>${opts(st.groups, c.group)}</select>`;
    return `<tr data-ch="${ch}" class="${e ? 'empty' : ''}${isDirty(ch) ? ' dirty' : ''}${st.sel.has(ch) ? ' sel' : ''}">
      <td><input type="checkbox" data-f="_sel"${st.sel.has(ch) ? ' checked' : ''}></td>
      <td class="num">${label(ch)}</td>
      <td>${grpSel}</td>
      <td><input data-f="name" class="nm" maxlength="12" value="${esc(c.name)}" placeholder="${e ? '' : '—'}"></td>
      <td><input data-f="freq" class="fq" inputmode="decimal" value="${e ? '' : fmtMHz(c.freq)}" placeholder="пусто"></td>
      <td><select data-f="mode">${opts(modes, c.mode)}</select></td>
      <td><select data-f="shift"${fm ? '' : ' disabled'}>${opts([0, 1, 2], c.shift, v => ['симпл.', '+', '−'][v])}</select></td>
      <td><input data-f="offset_khz" class="of" type="number" min="0" step="5" value="${c.shift ? c.offset_khz : ''}"${fm && c.shift ? '' : ' disabled'}></td>
      <td class="txf">${!e && c.shift ? fmtMHz(txFreq(c)) : ''}</td>
      <td><select data-f="tone_mode"${fm ? '' : ' disabled'}>${opts([0, 1, 2, 3], c.tone_mode, v => ['выкл', 'ENC', 'TSQ', 'DCS'][v])}</select></td>
      <td><select data-f="ctcss"${fm && (c.tone_mode === 1 || c.tone_mode === 2) ? '' : ' disabled'}>${opts(ctcss, c.ctcss, v => (+v).toFixed(1))}</select></td>
      <td><select data-f="dcs"${fm && c.tone_mode === 3 ? '' : ' disabled'}>${opts(dcs, c.dcs, v => String(v).padStart(3, '0'))}</select></td>
      <td><input data-f="clar" class="cl" type="number" min="-9999" max="9999" step="10" value="${c.clar || ''}"></td>
      <td><input type="checkbox" data-f="rx_clar"${c.rx_clar ? ' checked' : ''}></td>
      <td><input type="checkbox" data-f="tx_clar"${c.tx_clar ? ' checked' : ''}></td>
      <td><input data-f="comment" class="cm" maxlength="200" value="${esc(c.comment)}"></td>
      <td class="act">${stIcon}<button class="lnk" data-a="go" title="Включить этот канал на трансивере"${e ? ' disabled' : ''}>▶</button></td>
    </tr>`;
  }
  function visible(c) {
    if (!$('#meShowEmpty').checked && c.empty) return false;
    if ($('#meOnlyDirty').checked && !isDirty(c.ch)) return false;
    if (st.filterGrp === '' && c.group) return false;
    if (st.filterGrp !== '*' && st.filterGrp !== '' && c.group !== st.filterGrp) return false;
    const q = $('#meSearch').value.trim().toLowerCase();
    if (q && !(`${c.name} ${fmtMHz(c.freq)} ${c.comment} ${c.ch}`.toLowerCase().includes(q))) return false;
    return true;
  }
  function render() {
    ensure();
    $('#meRows').innerHTML = allCh().map(ch => st.chans[ch]).filter(visible).map(rowHtml).join('');
    renderSide(); renderInfo();
  }
  function renderRow(ch) {
    const tr = $(`#meRows tr[data-ch="${ch}"]`);
    if (!tr) return;
    const t = document.createElement('tbody'); t.innerHTML = rowHtml(st.chans[ch]);
    tr.replaceWith(t.firstElementChild);
  }
  function renderSide() {
    const cnt = g => Object.values(st.chans).filter(c => !c.empty && (g === '*' ? true : g === '' ? !c.group : c.group === g)).length;
    const item = (g, name, col) => `<div class="me-g${st.filterGrp === g ? ' on' : ''}" data-g="${esc(g)}">
      <i style="background:${col}"></i><span>${esc(name)}</span><b>${cnt(g)}</b>${g && g !== '*' ? '<button class="lnk" data-a="ren" title="Переименовать">✎</button><button class="lnk" data-a="del" title="Удалить группу">✕</button>' : ''}</div>`;
    $('#meGroups').innerHTML = item('*', 'Все каналы', 'transparent') + st.groups.map(g => item(g, g, grpColor(g))).join('') + item('', 'Без группы', '#3a4a5c');
    $('#meSetGrp').innerHTML = '<option value="">Группа для отмеченных…</option><option value="__none">— без группы —</option>' + opts(st.groups, null);
  }
  function renderInfo() {
    const dirty = allCh().filter(isDirty);
    $('#meDirtyN').textContent = dirty.length;
    $('#meSelInfo').textContent = `Отмечено: ${st.sel.size}`;
    $('#meFile').textContent = st.file ? `файл «${st.file}»` : 'не сохранено в файл';
    $('#meStop').classList.toggle('hidden', !st.busy);
    ['#meRead', '#meWrite', '#meWriteSel'].forEach(id => $(id).disabled = st.busy);
    saveDraft();
  }
  function setStatus(t) { $('#meStatus').textContent = t; }

  // ------------------------------------------------------------------ правка ячеек
  function onEdit(e) {
    const el = e.target, tr = el.closest('tr[data-ch]'); if (!tr || !el.dataset.f) return;
    const ch = +tr.dataset.ch, c = st.chans[ch], f = el.dataset.f;
    if (f === '_sel') { el.checked ? st.sel.add(ch) : st.sel.delete(ch); tr.classList.toggle('sel', el.checked); renderInfo(); return; }
    let v = el.type === 'checkbox' ? el.checked : el.value;
    if (f === 'freq') {
      const t = String(v).replace(',', '.').trim();
      if (!t) { Object.assign(c, blank(ch), { group: c.group, comment: c.comment }); }
      else {
        let mhz = parseFloat(t); if (!(mhz > 0)) { toast('Частота: например 145.500'); renderRow(ch); return; }
        if (mhz > 1000) mhz /= 1000;                       // ввели в кГц
        const hz = Math.round(mhz * 1e6);
        if (hz < 30e3 || hz > 470e6) { toast('Частота вне диапазона FTX-1 (0,03–470 МГц)'); renderRow(ch); return; }
        const wasEmpty = c.empty; c.freq = hz; c.empty = false;
        if (wasEmpty) c.mode = hz >= 144e6 || (hz >= 29.5e6 && hz < 29.7e6) ? 'FM' : hz < 10e6 ? 'LSB' : 'USB';
        if (!c.offset_khz) c.offset_khz = defOffset(hz);
      }
    } else if (['shift', 'tone_mode', 'dcs', 'clar', 'offset_khz'].includes(f)) c[f] = parseInt(v, 10) || 0;
    else if (f === 'ctcss') c[f] = parseFloat(v);
    else if (f === 'name') c[f] = String(v).replace(/[^\x20-\x7e]|;/g, '').slice(0, 12);
    else c[f] = v;
    if (f === 'shift' && c.shift && !c.offset_khz) c.offset_khz = defOffset(c.freq);
    if (f === 'mode' && !FM.includes(c.mode)) { c.shift = 0; c.tone_mode = 0; }
    if (c.empty && f !== 'group' && f !== 'comment' && f !== 'freq') { toast('Сначала укажите частоту'); }
    delete st.status[ch];
    renderRow(ch); renderInfo();
    if (f === 'group') renderSide();
  }

  // ------------------------------------------------------------------ действия
  const selList = () => [...st.sel].sort((a, b) => a - b);
  function doRead() {
    const from = clamp(+$('#meFrom').value || 1, 1, 99), to = clamp(+$('#meTo').value || 99, from, 99);
    if (allCh().some(isDirty) && !confirm('В таблице есть незаписанные изменения. Прочитать из трансивера и заменить ими таблицу (для читаемых номеров)?')) return;
    st.busy = true; renderInfo(); prog(0, 1, 'Чтение…');
    send({ t: 'mem_read', from, to, tones: $('#meTones').checked, pms: $('#mePms').checked });
  }
  function doWrite(list) {
    const chans = list.map(ch => st.chans[ch]).filter(Boolean);
    const emptyOnRig = chans.filter(c => c.empty && st.rig[c.ch] && !st.rig[c.ch].empty);
    const toWrite = chans.filter(c => !c.empty);
    if (!toWrite.length) { toast(emptyOnRig.length ? 'Очистить канал по CAT нельзя — только с трансивера' : 'Нечего записывать'); return; }
    const bad = toWrite.filter(c => !c.freq);
    if (bad.length) { toast('У каналов ' + bad.map(c => c.ch).join(', ') + ' нет частоты'); return; }
    if (!confirm(`Записать в трансивер каналов: ${toWrite.length}?\nVFO-B на время записи будет занят, потом вернётся.` +
      (emptyOnRig.length ? `\n\nКаналы ${emptyOnRig.map(c => c.ch).join(', ')} очищены только в таблице — в трансивере их очистите вручную.` : ''))) return;
    st.busy = true; renderInfo(); prog(0, toWrite.length, 'Запись…');
    send({ t: 'mem_write', chans: toWrite });
  }
  function prog(i, n, text) {
    const p = $('#meProg'); p.classList.toggle('hidden', n == null);
    if (n == null) return;
    $('i', p).style.width = (n ? i / n * 100 : 0).toFixed(1) + '%';
    $('span', p).textContent = text;
  }
  function moveSel() {
    const list = selList().filter(ch => ch <= CH_MAX); if (!list.length) return toast('Отметьте каналы');
    const to = parseInt(prompt(`Перенести ${list.length} отмеченных каналов начиная с номера:`, list[0]), 10);
    if (!(to >= 1 && to + list.length - 1 <= CH_MAX)) return;
    const data = list.map(ch => ({ ...st.chans[ch] }));
    const targets = data.map((_, i) => to + i);
    const busy = targets.filter(t => !list.includes(t) && !st.chans[t].empty);
    if (busy.length && !confirm(`Каналы ${busy.join(', ')} заняты — заменить?`)) return;
    list.forEach(ch => { st.chans[ch] = { ...blank(ch), group: '' }; });
    data.forEach((c, i) => { st.chans[targets[i]] = { ...c, ch: targets[i] }; });
    st.sel = new Set(targets); render();
  }
  function sortSel() {
    const list = (st.sel.size ? selList() : allCh().filter(ch => ch <= CH_MAX)).filter(ch => ch <= CH_MAX);
    const used = list.map(ch => st.chans[ch]).filter(c => !c.empty).sort((a, b) => a.freq - b.freq).map(c => ({ ...c }));
    list.forEach((ch, i) => { st.chans[ch] = i < used.length ? { ...used[i], ch } : { ...blank(ch), group: '' }; });
    render(); toast('Отсортировано по частоте');
  }

  // ------------------------------------------------------------------ сообщения сервера
  function on(m) {
    switch (m.t) {
      case 'mem_info': {
        st.info = { ...(st.info || {}), ...m, files: m.files };
        const cur = $('#meFiles').value;
        $('#meFiles').innerHTML = '<option value="">Файлы…</option>' + (m.files || []).map(f => `<option value="${esc(f.name)}">${esc(f.name)}</option>`).join('');
        if (m.saved) { st.file = m.saved; $('#meFiles').value = m.saved; toast(`Сохранено: «${m.saved}»`); }
        else if (cur) $('#meFiles').value = cur;
        if (m.busy !== undefined && !m.busy) st.busy = false;
        render(); break;
      }
      case 'mem_progress':
        prog(m.i, m.n, m.what === 'read' ? `Чтение канала ${m.i} из ${m.n}` : m.what === 'tones' ? 'Чтение тонов (трансивер переключается по каналам)…' : `Запись: ${m.i} из ${m.n} (канал ${m.ch})`);
        break;
      case 'mem_data':
        st.busy = false; prog(null);
        for (const c of m.chans) {
          const old = st.chans[c.ch];
          if (m.imported || m.file) st.chans[c.ch] = { ...c };
          else { st.chans[c.ch] = { ...c, group: old?.group || '', comment: old?.comment || '', offset_khz: old?.offset_khz && c.shift ? old.offset_khz : c.offset_khz }; st.rig[c.ch] = { ...c }; delete st.status[c.ch]; }
        }
        if (m.file) { st.file = m.file; if (m.groups) st.groups = m.groups; }
        if (m.imported) for (const c of m.chans) if (c.group && !st.groups.includes(c.group)) st.groups.push(c.group);
        setStatus(m.msg || ''); toast(m.msg || 'Готово', 3000); render(); break;
      case 'mem_written':
        st.busy = false; prog(null);
        for (const r of m.results) {
          st.status[r.ch] = r;
          if (r.ok) st.rig[r.ch] = { ...st.chans[r.ch] };
        }
        setStatus(m.msg); toast(m.msg + (m.results.some(r => !r.ok) ? ' — см. значки ⚠' : ''), 5000); render(); break;
      case 'mem_err': st.busy = false; prog(null); toast(m.msg, 6000); setStatus(m.msg); renderInfo(); break;
      case 'mem_csv': {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob(['﻿' + m.text], { type: 'text/csv' }));
        a.download = (st.file || 'FTX-1 память') + '.csv'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
        break;
      }
      case 'mem_vfo': {
        let ch = m.ch || selList()[0] || allCh().find(x => x <= CH_MAX && st.chans[x].empty);
        if (!ch) return toast('Нет свободного канала');
        const c = st.chans[ch];
        Object.assign(c, { empty: false, freq: m.freq, mode: m.mode || c.mode });
        if (!c.offset_khz) c.offset_khz = defOffset(c.freq);
        render(); toast(`Канал ${ch}: ${fmtMHz(m.freq)} ${m.mode}`); break;
      }
    }
  }

  // ------------------------------------------------------------------ события
  function open() {
    loadDraft();
    $('#memEd').classList.remove('hidden'); document.body.classList.add('me-open');
    send({ t: 'mem_hello' }); render();
  }
  function close() { $('#memEd').classList.add('hidden'); document.body.classList.remove('me-open'); saveDraft(); }
  $('#btnMemEd').onclick = open;
  $('#meClose').onclick = close;
  $('#memEd').addEventListener('keydown', e => { if (e.key === 'Escape' && !e.target.matches('input,select')) close(); });
  $('#meRows').addEventListener('change', onEdit);
  $('#meRows').addEventListener('click', e => {
    const b = e.target.closest('button[data-a]'); if (!b) return;
    const ch = +b.closest('tr').dataset.ch;
    if (b.dataset.a === 'go') { send({ t: 'mem_recall', ch }); toast(`Трансивер: канал ${label(ch)}`); }
  });
  $('#meAll').onchange = e => { $$('#meRows tr[data-ch]').forEach(tr => { const ch = +tr.dataset.ch; e.target.checked ? st.sel.add(ch) : st.sel.delete(ch); }); render(); };
  $('#meGroups').addEventListener('click', e => {
    const row = e.target.closest('.me-g'); if (!row) return;
    const g = row.dataset.g, a = e.target.closest('button')?.dataset.a;
    if (a === 'del') {
      if (!confirm(`Удалить группу «${g}»? Каналы останутся, без группы.`)) return;
      st.groups = st.groups.filter(x => x !== g); Object.values(st.chans).forEach(c => { if (c.group === g) c.group = ''; });
      if (st.filterGrp === g) st.filterGrp = '*';
    } else if (a === 'ren') {
      const n = (prompt('Новое название группы:', g) || '').trim(); if (!n || n === g) return;
      if (st.groups.includes(n)) return toast('Такая группа уже есть');
      st.groups = st.groups.map(x => x === g ? n : x); Object.values(st.chans).forEach(c => { if (c.group === g) c.group = n; });
      if (st.filterGrp === g) st.filterGrp = n;
    } else st.filterGrp = g;
    render();
  });
  const addGrp = () => {
    const n = $('#meNewGrp').value.trim(); if (!n) return;
    if (!st.groups.includes(n)) st.groups.push(n);
    $('#meNewGrp').value = '';
    if (st.sel.size && confirm(`Добавить в группу «${n}» отмеченные каналы (${st.sel.size})?`)) st.sel.forEach(ch => { st.chans[ch].group = n; });
    render();
  };
  $('#meAddGrp').onclick = addGrp;
  $('#meNewGrp').addEventListener('keydown', e => { if (e.key === 'Enter') addGrp(); });
  ['#meShowEmpty', '#meOnlyDirty'].forEach(id => $(id).onchange = render);
  $('#meSearch').oninput = render;
  $('#meSetGrp').onchange = e => {
    const v = e.target.value; e.target.value = ''; if (!v) return;
    if (!st.sel.size) return toast('Отметьте каналы');
    st.sel.forEach(ch => { st.chans[ch].group = v === '__none' ? '' : v; }); render();
  };
  $('#meRead').onclick = doRead;
  $('#meWrite').onclick = () => doWrite(allCh().filter(isDirty));
  $('#meWriteSel').onclick = () => { if (!st.sel.size) return toast('Отметьте каналы'); doWrite(selList()); };
  $('#meStop').onclick = () => send({ t: 'mem_stop' });
  $('#meFromVfo').onclick = () => send({ t: 'mem_from_vfo', ch: selList()[0] || null });
  $('#meMove').onclick = moveSel;
  $('#meSort').onclick = sortSel;
  $('#meClear').onclick = () => {
    if (!st.sel.size) return toast('Отметьте каналы');
    if (!confirm(`Очистить в таблице каналы: ${selList().join(', ')}?`)) return;
    st.sel.forEach(ch => { st.chans[ch] = blank(ch); }); render();
  };
  $('#meUndo').onclick = () => {
    if (!st.sel.size) return toast('Отметьте каналы');
    st.sel.forEach(ch => { const r = st.rig[ch]; if (r) st.chans[ch] = { ...r, group: st.chans[ch].group, comment: st.chans[ch].comment }; });
    render();
  };
  const data = () => ({ chans: allCh().map(ch => st.chans[ch]), groups: st.groups });
  $('#meSave').onclick = () => {
    const name = st.file || (prompt('Название набора каналов:', 'Мои каналы') || '').trim();
    if (name) send({ t: 'mem_save', name, ...data() });
  };
  $('#meSaveAs').onclick = () => { const name = (prompt('Сохранить как:', st.file || 'Мои каналы') || '').trim(); if (name) send({ t: 'mem_save', name, ...data() }); };
  $('#meFiles').onchange = e => {
    const n = e.target.value; if (!n) return;
    if (allCh().some(isDirty) && !confirm(`Открыть «${n}»? Таблица заменится содержимым файла (в трансивер ничего не пишется).`)) { e.target.value = ''; return; }
    send({ t: 'mem_load', name: n });
  };
  $('#meDelFile').onclick = () => { const n = $('#meFiles').value; if (!n) return toast('Выберите файл в списке'); if (confirm(`Удалить файл «${n}»?`)) send({ t: 'mem_delete', name: n }); };
  $('#meCsvOut').onclick = () => send({ t: 'mem_csv_out', chans: allCh().map(ch => st.chans[ch]).filter(c => !c.empty) });
  $('#meCsvIn').onchange = async e => {
    const f = e.target.files[0]; e.target.value = ''; if (!f) return;
    if (!confirm('Каналы из CSV заменят каналы с теми же номерами в таблице. Продолжить?')) return;
    send({ t: 'mem_csv_in', text: await f.text() });
  };
  return { on, open };
})();
