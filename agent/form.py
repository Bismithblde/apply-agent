"""DOM form engine: find fillable controls, stamp them with data-aa-key, fill them, and verify."""
from __future__ import annotations

import logging
import random
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .models import Answer, ApplyResult, ApplyStatus, Field, Job

log = logging.getLogger(__name__)

SUCCESS_PHRASES = [
    "thank you for applying", "thanks for applying", "application has been received", "application was received",
    "received your application", "application has been submitted", "application was submitted",
    "application submitted", "successfully submitted", "your application is complete", "thank you for your application",
]
CODE_PHRASES = ["security code", "verification code", "enter the code", "sent a code", "code sent to", "verify your email"]

_EXTRACT_JS = r"""
(rootSel) => {
  const root = rootSel ? document.querySelector(rootSel) : document.body;
  if (!root) return [];
  window.__aaSeq = window.__aaSeq || 0;
  window.__aaKeysById = window.__aaKeysById || {};
  const JUNK = /^(attach|upload|upload file|choose file|browse|select\.{0,3}|type here\.{0,3}|or drag and drop here|drop files here|enter manually|dropbox|google drive)$/i;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const stripReq = s => clean(clean(s).split('✱')[0]).replace(/\s*\*+\s*$/, '').replace(/\s*\(required\)\s*$/i, '').trim();

  // minW is 1 for comboboxes: an empty React-Select input is only a few px wide.
  // React-Select also sets opacity 0 on the input once a value is chosen.
  const visible = (el, minW = 4) => {
    if (!el || !el.isConnected) return false;
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || (s.opacity === '0' && minW > 1)) return false;
    const r = el.getBoundingClientRect();
    return r.width >= minW && r.height >= 4;
  };
  // Text of a live node, skipping controls and display:none parts (status messages, menus).
  const textOf = node => {
    if (!node) return '';
    const w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const parts = [];
    let n;
    while ((n = w.nextNode())) {
      const p = n.parentElement;
      if (!p || p.closest('select, option, textarea, script, style, svg, [role=listbox], [role=option]')) continue;
      if (p !== node && !node.contains(p)) continue;
      if (!p.getClientRects().length && !p.closest('.visually-hidden, .sr-only')) continue;
      parts.push(n.textContent);
    }
    return clean(parts.join(' '));
  };
  const byIds = ids => clean((ids || '').split(/\s+/).map(id => textOf(document.getElementById(id))).join(' '));
  const isControl = n => n.matches && n.matches('input:not([type=hidden]), select, textarea, [role=combobox], button[aria-pressed]');

  const precedingText = el => {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    w.currentNode = el;
    let n, steps = 0, star = false;
    while ((n = w.previousNode()) && steps++ < 300) {
      const p = n.parentElement;
      if (!p || el.contains(n)) continue;
      if (p.closest('script, style, noscript, option, select, [role=listbox], [role=option]')) continue;
      const t = clean(n.textContent);
      if (!t) continue;
      if (/^[*✱]$/.test(t)) { star = true; continue; }
      if (!visible(p) && !p.closest('label, legend')) continue;
      if (JUNK.test(t)) continue;
      return star ? t + ' *' : t;
    }
    return '';
  };

  // A label-ish element inside a nearby ancestor that precedes `el` and holds no other controls.
  const containerLabel = el => {
    let a = el.parentElement;
    for (let depth = 0; a && a !== document.body && depth < 6; depth++, a = a.parentElement) {
      const foreign = [...a.querySelectorAll('input:not([type=hidden]), select, textarea, [role=combobox]')]
        .filter(c => !el.contains(c) && visible(c, 1) && c.getAttribute('aria-hidden') !== 'true');
      if (foreign.length) return ['', null];
      const cands = a.querySelectorAll('label, legend, [class*="label" i], [class*="question-title" i], [class*="questionTitle" i], h3, h4');
      for (const c of cands) {
        if (c.contains(el) || el.contains(c)) continue;
        if (!(c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
        if (c.querySelector('input, select, textarea')) continue;
        const t = textOf(c);
        if (t && !JUNK.test(t)) return [t, c];
      }
    }
    return ['', null];
  };

  const labelEls = el => {
    const out = [];
    if (el.id) { try { out.push(...document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`)); } catch (e) {} }
    return out;
  };

  // Returns [text, labelElement|null].
  const rawLabel = (el, isGroup) => {
    // If it's a group, check if el itself contains a formLabel, legend, or question heading
    if (isGroup && el) {
      const innerLabel = el.querySelector('[data-automation-id="formLabel"], legend, label.question, [class*="formLabel" i], [class*="label" i], h3, h4');
      if (innerLabel && !innerLabel.querySelector('input, select, textarea')) {
        const t = textOf(innerLabel);
        if (t && !JUNK.test(t)) return [t, innerLabel];
      }
    }
    // A lone Yes/No checkbox still belongs to its question's fieldset.
    const ownFieldset = el.closest('fieldset');
    if (!isGroup && el.matches('input[type=checkbox], input[type=radio]') && ownFieldset) {
      const controls = [...ownFieldset.querySelectorAll('input:not([type=hidden]), select, textarea')]
        .filter(c => visible(c, 1) || c === el);
      const legend = ownFieldset.querySelector(':scope > legend');
      if (controls.length === 1 && textOf(legend)) return [textOf(legend), legend];
    }
    const lb = el.getAttribute('aria-labelledby');
    let t = byIds(lb);
    if (t && !JUNK.test(t)) return [t, document.getElementById((lb || '').split(/\s+/)[0])];
    if (!isGroup) {
      for (const l of labelEls(el)) { t = textOf(l); if (t && !JUNK.test(t)) return [t, l]; }
      const wrap = el.closest('label');
      if (wrap) { t = textOf(wrap); if (t && !JUNK.test(t)) return [t, wrap]; }
    }
    t = clean(el.getAttribute('aria-label'));
    if (t && !JUNK.test(t)) return [t, null];
    // A legend labels this control only if its fieldset wraps nothing else (page-wide fieldsets hold section titles).
    const fs = el.closest('fieldset');
    const fsOwn = fs && ![...fs.querySelectorAll('input:not([type=hidden]), select, textarea, [role=combobox]')]
      .some(c => !el.contains(c) && c !== el && visible(c, 1));
    if (fsOwn) { const lg = fs.querySelector('legend'); t = textOf(lg); if (t) return [t, lg]; }
    const grp = el.parentElement && el.parentElement.closest('[role=group][aria-labelledby], [role=radiogroup][aria-labelledby]');
    if (grp) { t = byIds(grp.getAttribute('aria-labelledby')); if (t) return [t, grp]; }
    const cl = containerLabel(el);
    if (cl[0]) return cl;
    return [precedingText(el), null];
  };

  const sectionPrefix = el => {
    const sec = el.closest('[class*="education" i], [id*="education" i], [data-field-path*="education" i], [class*="employment" i], [id*="employment" i], [data-field-path*="employment" i], [class*="experience-form" i], [id*="experience-form" i]');
    if (!sec) return '';
    return /education/i.test([sec.className, sec.id, sec.getAttribute('data-field-path')].join(' '))
      ? 'Education: ' : 'Employment: ';
  };

  const isRequired = (el, raw, lblEl) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (/[*✱]/.test(clean(raw)) || /\(required\)/i.test(clean(raw))) return true;
    for (const l of [...labelEls(el), lblEl, el.closest('label'), el.closest('[data-automation-id*="formField" i]'), el.closest('fieldset'), el.parentElement].filter(Boolean)) {
      if (/required/i.test(String(l.className || '')) ||
          l.querySelector('.required, [class*="required" i], [data-automation-id="required"], [title="required" i], [aria-required="true"]')) return true;
    }
    const grp = el.closest('[role=group][aria-required="true"], [role=radiogroup][aria-required="true"]');
    return !!grp;
  };

  const stamp = el => {
    const identity = el.id ? `${el.tagName}:${el.id}` : '';
    let k = el.getAttribute('data-aa-key') || (identity && window.__aaKeysById[identity]);
    if (!k) { k = 'aa-' + (++window.__aaSeq); el.setAttribute('data-aa-key', k); }
    el.setAttribute('data-aa-key', k);
    if (identity) window.__aaKeysById[identity] = k;
    return k;
  };

  const out = [];
  const seenGroups = new Set();
  const push = (el, kind, opts, isGroup, extra) => {
    if (el.closest('[data-aa-skip]')) return;
    const [raw, lblEl] = rawLabel(el, isGroup);
    const label = stripReq(sectionPrefix(el) + raw) || clean(el.getAttribute('placeholder')) || el.name || el.id || '';
    out.push(Object.assign({
      key: stamp(el), label, kind, required: isRequired(el, raw, lblEl),
      options: opts || [], name: el.getAttribute('name') || el.id || '',
      placeholder: clean(el.getAttribute('placeholder')), current_value: '',
    }, extra || {}));
  };

  const optionLabel = inp => {
    for (const l of labelEls(inp)) { const t = textOf(l); if (t) return [t, l]; }
    const wrap = inp.closest('label');
    if (wrap) return [textOf(wrap), wrap];
    const t = clean(inp.getAttribute('aria-label')) || clean(inp.value);
    return [t, inp];
  };
  const commonAncestor = els => {
    let a = els[0].parentElement;
    while (a && !els.every(e => a.contains(e))) a = a.parentElement;
    return a || document.body;
  };
  const groupContainer = els => {
    let a = commonAncestor(els);
    // Single-option groups: climb to the option's list wrapper rather than the label.
    while (a && a.tagName === 'LABEL') a = a.parentElement;
    return a;
  };

  const all = [...root.querySelectorAll('input, select, textarea, [role=combobox], [role=radiogroup], button[aria-pressed]')];
  for (const el of all) {
    if (el.disabled || el.closest('[data-aa-skip]')) continue;
    const tag = el.tagName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    const name = el.getAttribute('name') || '';
    if (/^(g-recaptcha-response|h-captcha-response|cf-turnstile-response)/.test(name)) continue;
    if (el.closest('.g-recaptcha, .h-captcha, .cf-turnstile, .iti__dropdown-content')) continue;

    if (tag === 'INPUT' && type === 'file') {
      if (el.closest('[class*="autofill" i]')) continue;
      push(el, 'file', [], false, {current_value: el.files && el.files.length ? el.files[0].name : ''});
      continue;
    }
    if (tag === 'INPUT' && (type === 'radio' || type === 'checkbox')) {
      if (el.closest('[aria-pressed]') || el.parentElement.querySelector(':scope > button[aria-pressed]')) continue;
      const [optText, optEl] = optionLabel(el);
      if (!visible(el) && !visible(optEl)) continue;
      let members;
      if (name) members = [...root.querySelectorAll(`input[type="${type}"][name="${CSS.escape(name)}"]`)];
      if (!members || members.length < 2) {
        const box = el.closest('[data-automation-id^="formField-"], fieldset, [role=group], [role=radiogroup], ul');
        const inBox = box ? [...box.querySelectorAll(`input[type="${type}"]`)] : [];
        members = inBox.length >= 2 ? inBox : [el];
      }
      if (type === 'radio' || members.length >= 2) {
        const container = members.length >= 2 ? groupContainer(members) : groupContainer([el]);
        if (seenGroups.has(container)) continue;
        seenGroups.add(container);
        const opts = [], checked = [];
        members.forEach((m, i) => {
          const [t, oe] = optionLabel(m);
          oe.setAttribute('data-aa-opt', String(i));
          m.setAttribute('data-aa-optin', String(i));
          opts.push(t);
          if (m.checked) checked.push(t);
        });
        const req = members.some(m => m.required || m.getAttribute('aria-required') === 'true');
        push(container, type, opts, true, {current_value: checked.join(' | '), name});
        if (req) out[out.length - 1].required = true;
      } else {
        optEl.setAttribute('data-aa-opt', '0');
        el.setAttribute('data-aa-optin', '0');
        push(el, 'checkbox', [], false, {current_value: el.checked ? 'true' : ''});
      }
      continue;
    }
    if (el.getAttribute('role') === 'radiogroup') {
      const radios = [...el.querySelectorAll('[role=radio]')];
      if (!radios.length || !visible(el) || seenGroups.has(el)) continue;
      seenGroups.add(el);
      const opts = radios.map((r, i) => { r.setAttribute('data-aa-opt', String(i)); return textOf(r) || clean(r.getAttribute('aria-label')); });
      const cur = radios.filter(r => r.getAttribute('aria-checked') === 'true').map(r => textOf(r)).join(' | ');
      push(el, 'radio', opts, true, {current_value: cur});
      continue;
    }
    if (tag === 'BUTTON') {
      const box = el.parentElement;
      if (!box || seenGroups.has(box) || !visible(el)) continue;
      const btns = [...box.querySelectorAll(':scope > button[aria-pressed]')];
      if (btns.length < 2) continue;
      seenGroups.add(box);
      const opts = btns.map((b, i) => { b.setAttribute('data-aa-opt', String(i)); return textOf(b); });
      const cur = btns.filter(b => b.getAttribute('aria-pressed') === 'true').map(b => textOf(b)).join(' | ');
      const hidden = box.querySelector('input');
      push(box, 'radio', opts, true, {current_value: cur, name: hidden ? hidden.name : ''});
      continue;
    }
    const isCombo = el.getAttribute('role') === 'combobox';
    if (!visible(el, isCombo ? 1 : 4) || el.getAttribute('aria-hidden') === 'true') continue;
    if (isCombo) {
      if (tag === 'SELECT') { /* fall through to select */ }
      else {
        let cur = clean(el.value);
        const shell = el.closest('[class*="container" i], [class*="control" i]');
        const sv = shell && (shell.querySelector('[class*="single-value" i], [class*="singleValue" i]'));
        if (!cur && sv) cur = textOf(sv);
        const multi = shell ? [...shell.querySelectorAll('[class*="multi-value__label" i], [class*="multiValue" i] [class*="label" i]')].map(textOf) : [];
        if (!cur && multi.length) cur = multi.join(' | ');
        push(el, 'combobox', [], false, {current_value: cur});
        continue;
      }
    }
    if (tag === 'SELECT') {
      const opts = [...el.options].filter(o => !(o.value === '' && (/^(select|choose|please|--|click here|month|year)/i.test(clean(o.text)) || !clean(o.text)))).map(o => clean(o.text)).filter(Boolean);
      const sel = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      push(el, 'select', opts, false, {current_value: sel && sel.value !== '' ? clean(sel.text) : ''});
      continue;
    }
    if (tag === 'TEXTAREA') { push(el, 'textarea', [], false, {current_value: el.value}); continue; }
    if (tag === 'INPUT') {
      if (['hidden', 'submit', 'button', 'reset', 'image', 'password', 'range', 'color'].includes(type)) continue;
      const kind = {email: 'email', tel: 'tel', url: 'url', number: 'number', date: 'date'}[type] || 'text';
      push(el, kind, [], false, {current_value: el.value});
    }
  }
  return out;
}
"""

_COMBO_NODES_JS = r"""
(el) => {
  const visible = e => {
    if (!e || e.closest('[hidden], [aria-hidden="true"]')) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const ids = e => ['aria-controls', 'aria-owns'].flatMap(a => (e.getAttribute(a) || '').split(/\s+/)).filter(Boolean);
  const combos = [...document.querySelectorAll('[role=combobox]')];
  const foreign = menu => menu.id && combos.some(c => c !== el && ids(c).includes(menu.id));
  const options = root => [...root.querySelectorAll('[role=option], [class*="select__option" i]')]
    .filter(o => visible(o) && o.getAttribute('aria-disabled') !== 'true' && !o.disabled
      && !foreign(o.closest('[role=listbox]') || root));
  const linked = ids(el);
  if (linked.length) {
    return [...new Set(linked.flatMap(id => {
      const menu = document.getElementById(id);
      return menu ? options(menu) : [];
    }))];
  }
  for (let a = el.parentElement, depth = 0; a && a !== document.body && a.tagName !== 'FORM' && depth < 5;
       a = a.parentElement, depth++) {
    if (combos.some(c => c !== el && a.contains(c))) break;
    const found = options(a);
    if (found.length) return found;
  }
  // An unlabelled portal is usable only when this is the sole active menu owner.
  if (el.getAttribute('aria-expanded') !== 'true' ||
      !(el === document.activeElement || el.contains(document.activeElement)) ||
      combos.some(c => c !== el && visible(c) && c.getAttribute('aria-expanded') === 'true')) return [];
  const menus = [...document.querySelectorAll('[role=listbox]')].filter(m => visible(m) && !foreign(m));
  return menus.length === 1 ? options(menus[0]) : [];
}
"""

_COMBO_OPTIONS_JS = r"""
(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  return (__COMBO_NODES__)(el).map(o => clean(o.innerText || o.textContent))
    .filter(t => t && !/^(no options|no results|loading)/i.test(t));
}
""".replace('__COMBO_NODES__', _COMBO_NODES_JS)

_COMBO_PICK_JS = r"""
(el, wanted) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  return (__COMBO_NODES__)(el).find(o => clean(o.innerText || o.textContent) === wanted) || null;
}
""".replace('__COMBO_NODES__', _COMBO_NODES_JS)

_READ_JS = r"""
([key, kind]) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const el = document.querySelector(`[data-aa-key="${key}"]`);
  if (!el) return {missing: true};
  if (kind === 'radio' || (kind === 'checkbox' && el.querySelectorAll('[data-aa-opt]').length > 0)) {
    const opts = [...el.querySelectorAll('[data-aa-opt]')];
    return {states: opts.map(o => {
      const inp = el.querySelector(`[data-aa-optin="${o.getAttribute('data-aa-opt')}"]`) || o.querySelector('input') || o;
      const on = inp.checked === true || o.getAttribute('aria-pressed') === 'true' || o.getAttribute('aria-checked') === 'true'
        || inp.getAttribute('aria-checked') === 'true';
      return [clean(o.textContent) || clean(inp.value), !!on];
    })};
  }
  if (kind === 'checkbox') {
    const inp = el.tagName === 'INPUT' ? el : el.querySelector('input[type="checkbox"], input') || el;
    const isChecked = Boolean(inp.checked || inp.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-checked') === 'true' || el.querySelector('input:checked, [aria-checked="true"]'));
    return {checked: isChecked};
  }
  if (kind === 'file') {
    const box = el.closest('[class*="file" i], [role=group], .field-wrapper, li') || el.parentElement;
    return {files: el.files ? el.files.length : 0, text: clean(box ? box.innerText : '')};
  }
  if (el.tagName === 'SELECT') { const o = el.options[el.selectedIndex]; return {value: o && o.value !== '' ? clean(o.text) : ''}; }
  if (kind === 'combobox') {
    let single = '', metadata = '', hasDisplay = false;
    let a = el.parentElement;
    for (let i = 0; a && i < 5; i++, a = a.parentElement) {
      if (a.querySelectorAll('[data-aa-key]').length > 1) break;
      if ([...a.querySelectorAll('[role=combobox]')].some(c => c !== el)) break;
      hasDisplay ||= !!a.querySelector('[class*="placeholder" i], [class*="single-value" i], [class*="singleValue" i], [class*="multi-value" i], [class*="multiValue" i]')
        || /value-container/i.test(a.className || '');
      const sv = a.querySelector('[class*="single-value" i], [class*="singleValue" i]');
      const mv = [...a.querySelectorAll('[class*="multi-value" i], [class*="multiValue" i]')];
      if (sv || mv.length) {
        single = sv ? clean(sv.innerText) : mv.map(m => clean(m.innerText)).join(', ');
        const node = sv || mv[0];
        metadata = clean([node, ...node.querySelectorAll('[aria-label], [title], img[alt]')]
          .map(n => n.getAttribute('aria-label') || n.getAttribute('title') || n.getAttribute('alt')).join(' '));
        const countryNode = [node, ...node.querySelectorAll('[data-country-code], [class*="iti__flag"]')]
          .find(n => n.hasAttribute('data-country-code') || /\biti__[a-z]{2}\b/.test(n.className || ''));
        const country = countryNode && (countryNode.getAttribute('data-country-code')
          || (countryNode.className.match(/\biti__([a-z]{2})\b/) || [])[1]);
        if (country && /^[a-z]{2}$/i.test(country))
          metadata = clean(metadata + ' ' + new Intl.DisplayNames(['en'], {type: 'region'}).of(country.toUpperCase()));
        const flag = single.match(/\p{Regional_Indicator}{2}/u);
        if (flag) {
          const country = [...flag[0]].map(c => String.fromCharCode(c.codePointAt(0) - 0x1f1e6 + 65)).join('');
          metadata = clean(metadata + ' ' + new Intl.DisplayNames(['en'], {type: 'region'}).of(country));
        }
        break;
      }
    }
    const expanded = el.getAttribute('aria-expanded') === 'true';
    const hasOptions = (__COMBO_NODES__)(el).length > 0;
    return {value: clean(el.value), single, metadata, expanded, hasOptions, hasDisplay,
            invalid: el.getAttribute('aria-invalid') === 'true',
            ownText: el.matches('input, textarea') ? '' : clean(el.innerText)};
  }
  return {value: el.value};
}
""".replace('__COMBO_NODES__', _COMBO_NODES_JS)

_PAGE_TEXT_JS = r"""
() => {
  const pick = ['.job__description', '#content .section-wrapper', '.posting-page', '[class*="jobPosting" i]',
                '[class*="job-description" i]', '[class*="description" i]', 'main', 'article', '#content'];
  for (const s of pick) {
    const el = document.querySelector(s);
    if (el && el.innerText && el.innerText.trim().length > 400) return el.innerText;
  }
  return document.body ? document.body.innerText : '';
}
"""


# ---------- matching ----------

def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", str(s).lower()).split())


def option_score(value: str, option: str) -> float:
    """0..1 similarity between a wanted value and an option label."""
    v, o = _norm(value), _norm(option)
    if not v or not o:
        return 0.0
    if v == o:
        return 1.0
    # Similar spelling does not make different dates, quantities, or percentages equivalent.
    numbers = set(re.findall(r"\b\d+\b", v))
    option_numbers = set(re.findall(r"\b\d+\b", o))
    if numbers and option_numbers and not numbers <= option_numbers:
        return 0.0
    months = {"january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december"}
    wanted_months, offered_months = set(v.split()) & months, set(o.split()) & months
    if wanted_months and offered_months and not wanted_months & offered_months:
        return 0.0
    if v in ("yes", "no"):
        return 0.95 if o.split()[0] == v else 0.0
    vt, ot = v.split(), o.split()
    if o.startswith(v + " ") or v.startswith(o + " "):
        return 0.9
    if set(vt) <= set(ot):
        return 0.85 - 0.01 * min(10, len(ot) - len(vt))
    if set(ot) <= set(vt):
        return 0.8
    return SequenceMatcher(None, v, o).ratio() * 0.9


def best_option(value: Any, options: list[str], threshold: float = 0.75) -> str | None:
    if value is None or not options:
        return None
    v = str(value)
    # Ties go to the option closest in length ("United States of America" over "... Minor Outlying Islands").
    scored = sorted(((option_score(v, o), o) for o in options), key=lambda t: (-t[0], abs(len(t[1]) - len(v))))
    return scored[0][1] if scored[0][0] >= threshold else None


def option_matches(value: Any, option: str) -> bool:
    """Equivalent option identities, allowing display qualifiers but never spelling similarity."""
    months = ("january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december")
    def calendar(text: str) -> tuple[set[str], set[str], set[str]]:
        words = set(_norm(text).split())
        return (set(re.findall(r"\b(?:19|20)\d{2}\b", text)),
                {m for m in months if m in words or m[:3] in words},
                words & {"spring", "summer", "fall", "autumn", "winter"})
    wanted, offered = _norm(str(value)), _norm(option)
    if not wanted or not offered:
        return False
    if wanted == offered:
        return True
    wc, oc = calendar(str(value)), calendar(option)
    if any(wc) or any(oc):
        if wc[0] != oc[0]:
            return False
        term_range = (re.fullmatch(r"(?:spring|summer|fall|winter) \d{4}", wanted)
                      and offered.startswith(wanted + " ") and wc[2] == oc[2])
        if not term_range and wc[1:] != oc[1:]:
            return False
        month_names = {name for month in months for name in (month, month[:3])}
        if wanted in month_names and offered in month_names:
            return True
    if option_score(value, option) == 0:
        return False
    if wanted in {"yes", "no"}:
        return offered.split()[0] == wanted
    def identity(text: str) -> str:
        text = re.sub(r"\([^)]*\)", "", text)
        text = re.sub(r"\s*[-–—/|].*$", "", text)
        text = re.sub(r"\s*\+\d+\s*$", "", text)
        result = _norm(text)
        countries = {"us", "usa", "united states", "united states of america"}
        if result in countries:
            return "united states"
        return re.sub(r"\bdegree\b", "", result).strip()
    if identity(str(value)) and identity(str(value)) == identity(option):
        # Country calling codes must agree when both labels include one.
        codes = re.findall(r"\+\d+", str(value))
        other_codes = re.findall(r"\+\d+", option)
        return not (codes and other_codes and codes != other_codes)
    if re.fullmatch(r"\d{4}", wanted) and (offered == wanted or offered.startswith(wanted + " ")):
        return True
    if re.fullmatch(r"\d{4}", offered) and (wanted == offered or wanted.startswith(offered + " ")):
        return True
    # A selected internship term can also display its explicit month range.
    return bool(re.fullmatch(r"(?:spring|summer|fall|winter) \d{4}", wanted)
                and offered.startswith(wanted + " "))


def matching_option(value: Any, options: list[str]) -> str | None:
    matches = [option for option in options if option_matches(value, option)]
    exact = [option for option in matches if _norm(str(value)) == _norm(option)]
    if not exact and len({_norm(option) for option in matches}) > 1:
        return None
    return best_option(value, exact or matches, threshold=0)


def searchable_options(field: Field) -> bool:
    """Entity searches can return an incomplete menu; enumerated answers must fit observed choices."""
    return field.kind == "combobox" and bool(re.fullmatch(
        r"(?:education(?:\s*\d+)?\s*[:–—-]?\s*)?(?:school(?: or university)?|university|college|institution)(?: name)?"
        r"|(?:current )?(?:location(?: \(city\))?|city(?: / town)?|employer|company)(?: name)?",
        field.label.strip(), re.I))


# ---------- extraction ----------

def extract_fields(scope, root: str | None = None, *, probe_options: bool = True) -> list[Field]:
    """All visible fillable controls under `root` (CSS selector) in a Page or Frame."""
    try:
        raw = scope.evaluate(_EXTRACT_JS, root)
    except Exception as e:
        log.warning("extract_fields failed: %s", e)
        return []
    fields = [Field(key=r["key"], label=r["label"][:2000], kind=r["kind"], required=bool(r["required"]),
                    options=[o for o in r["options"] if o][:3000], name=r["name"] or "",
                    placeholder=r["placeholder"] or "", current_value=str(r["current_value"] or ""))
              for r in raw]
    if probe_options:
        for f in [f for f in fields if f.kind == "combobox" and not f.options][:30]:
            f.options = _probe_combo(scope, f.key)
    return fields


def _open_combo(loc, timeout: int = 2500) -> None:
    """Open a combobox's menu. React-Select's placeholder covers its tiny input and intercepts clicks,
    so fall back to focus + ArrowDown, which every ARIA combobox treats as "open"."""
    try:
        loc.click(timeout=timeout)
    except Exception:
        loc.focus(timeout=timeout)
        loc.press("ArrowDown", timeout=timeout)


def _probe_combo(scope, key: str) -> list[str]:
    loc = scope.locator(f'[data-aa-key="{key}"]')
    try:
        _open_combo(loc)
        time.sleep(0.35)
        opts = loc.evaluate(_COMBO_OPTIONS_JS)
        loc.press("Escape", timeout=1500)
        return list(dict.fromkeys(opts))[:300]
    except Exception:
        return []


# ---------- filling ----------

def _missing(a: Answer | None) -> bool:
    return a is None or a.source == "unknown" or a.value is None or a.value == "" or a.value == []


def _pause(lo: float = 0.05, hi: float = 0.2) -> None:
    time.sleep(random.uniform(lo, hi))


def fill_fields(scope, fields: list[Field], answers: list[Answer]) -> list[str]:
    """Fill each answered field, verify it, and return the keys that could not be filled."""
    by_key = {a.key: a for a in answers}
    failed: list[str] = []
    for f in fields:
        a = by_key.get(f.key)
        if _missing(a):
            continue
        if f.kind == "checkbox" and not f.options and a.value is False and f.current_value != "true":
            continue
        try:
            ok = _fill_one(scope, f, a.value)
        except Exception as e:
            log.info("fill %s (%s) failed: %s", f.key, f.label[:60], str(e).splitlines()[0][:200])
            ok = False
        if not ok:
            failed.append(f.key)
        _pause()
    return sorted(set(failed) | set(_dropdown_failures(scope, fields, answers)))


def _fill_one(scope, f: Field, value: Any) -> bool:
    _recover_field(scope, f)
    loc = scope.locator(f'[data-aa-key="{f.key}"]').first
    if f.kind == "file":
        p = Path(str(value))
        if not p.exists():
            return False
        loc.set_input_files(str(p), timeout=10000)
        time.sleep(1.0)
        return _verify(scope, f, str(value))
    if f.kind == "select":
        opts = loc.evaluate("e => [...e.options].filter(o => !o.disabled && !o.parentElement.disabled).map(o => o.text.trim())")
        target = matching_option(value, opts)
        if not target:
            return False
        loc.select_option(label=target, timeout=5000)
        return _verify(scope, f, str(value))
    if f.kind == "combobox":
        return _fill_combo(scope, loc, f, str(value))
    if f.kind == "radio" or (f.kind == "checkbox" and f.options):
        wanted = value if isinstance(value, list) else [v.strip() for v in str(value).split("|")] if f.kind == "checkbox" else [str(value)]
        return _fill_choices(scope, loc, f, [str(w) for w in wanted if str(w).strip()])
    if f.kind == "checkbox":
        want = value is True or str(value).strip().lower() in ("true", "yes", "1", "checked")
        _recover_field(scope, f)
        read_res = scope.evaluate(_READ_JS, [f.key, "checkbox"])
        state = read_res.get("checked")
        if bool(state) != want:
            clicked = False
            elem_id = loc.get_attribute("id")
            for sel in (f'[data-aa-opt="0"]', f'label[for="{elem_id}"]' if elem_id else None):
                if sel and scope.locator(sel).count() and scope.locator(sel).first.is_visible():
                    try:
                        scope.locator(sel).first.click(timeout=3000)
                        clicked = True
                        break
                    except Exception:
                        pass
            if not clicked:
                try:
                    loc.evaluate("""e => {
                        const lbl = e.closest('label') || document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
                        if (lbl) { lbl.click(); return; }
                        const p = e.closest('[data-automation-id*="checkbox" i]') || e.parentElement;
                        (p || e).click();
                    }""")
                    clicked = True
                except Exception:
                    pass
            time.sleep(0.2)
            read_res = scope.evaluate(_READ_JS, [f.key, "checkbox"])
            if bool(read_res.get("checked")) != want:
                try:
                    loc.set_checked(want, timeout=2000, force=True)
                except Exception:
                    try:
                        loc.click(timeout=2000, force=True)
                    except Exception:
                        pass
                try:
                    loc.evaluate("""(e, want) => {
                        if (e.checked !== want) {
                            e.checked = want;
                            e.dispatchEvent(new Event('input', {bubbles: true}));
                            e.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }""", want)
                except Exception:
                    pass
            time.sleep(0.2)
            _recover_field(scope, f)
            read_res = scope.evaluate(_READ_JS, [f.key, "checkbox"])
        if read_res.get("missing"):
            return True
        return bool(read_res.get("checked")) == want
    text = str(value)
    if f.kind == "date" and not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return False
    try:
        info = scope.evaluate("""([key]) => {
          const el = document.querySelector('[data-aa-key="' + CSS.escape(key) + '"]');
          if (!el) return {missing: true};
          return {
            disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true' || el.closest('[disabled]')),
            readOnly: Boolean(el.readOnly || el.hasAttribute('readonly')),
            value: el.value != null ? el.value : ''
          };
        }""", [f.key])
        if not info.get("missing"):
            if info.get("disabled") or info.get("readOnly"):
                log.info("field %s (%s) is disabled or read-only; treating as filled", f.key, f.label)
                return True
            cur_val = info.get("value", "")
            if cur_val == text or (cur_val and cur_val.strip() == text.strip()):
                return True
            if f.kind == "tel":
                d1, d2 = re.sub(r"\D", "", cur_val), re.sub(r"\D", "", text)
                if bool(d2) and (d1.endswith(d2) or d2.endswith(d1)) and len(d1) >= 7:
                    return True
        else:
            return False
    except Exception as e:
        log.debug("pre-fill check for %s failed: %s", f.key, e)
    _type_into(loc, text, f.kind)
    return _verify(scope, f, text)


def _type_into(loc, text: str, kind: str) -> None:
    loc.scroll_into_view_if_needed(timeout=3000)
    if kind == "date":
        loc.fill(text, timeout=5000)
        return
    loc.click(timeout=5000)
    loc.fill("", timeout=5000)
    if len(text) > 200:
        loc.fill(text[:-1], timeout=5000)
        loc.press_sequentially(text[-1], delay=30)
    else:
        loc.press_sequentially(text, delay=random.randint(12, 35))


def _fill_combo(scope, loc, f: Field, value: str) -> bool:
    loc.scroll_into_view_if_needed(timeout=3000)
    if _verify(scope, f, value):
        return True
    _open_combo(loc, 4000)
    time.sleep(0.3)
    live = loc.evaluate(_COMBO_OPTIONS_JS)
    target = matching_option(value, live)
    if not target:
        # Typing filters local options and triggers async searches (location, school).
        typed = (value.split(",")[0].strip() if "," in value else value)[:40]
        inp = loc if loc.evaluate("e => e.matches('input,textarea')") else loc.locator("input:not([type=hidden]), textarea").first
        if inp.count():
            try:
                inp.fill("", timeout=3000)
            except Exception:
                pass
            inp.press_sequentially(typed, delay=random.randint(25, 50))
        else:
            loc.press_sequentially(typed, delay=random.randint(25, 50))
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.5)
            live = loc.evaluate(_COMBO_OPTIONS_JS)
            target = matching_option(value, live)
            if target:
                break
    if not target:
        try:
            loc.press("Escape", timeout=1000)
        except Exception:
            pass
        return False
    if not _click_option(scope, loc, target):
        return False
    _recover_field(scope, f)
    if loc.count():
        loc.evaluate("e => { if (e === document.activeElement || e.contains(document.activeElement)) document.activeElement.blur(); }", timeout=1500)
    deadline = time.monotonic() + 2
    matched = False
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if _verify(scope, f, value):
            matched = True
            break
    if not matched:
        log.info("dropdown %s (%s) did not commit requested value %r", f.key, f.label[:60], value)
    return matched


def _recover_field(scope, f: Field) -> None:
    if not f.name or scope.locator(f'[data-aa-key="{f.key}"]').count():
        return
    scope.evaluate("""([key, name]) => {
      const nodes = [...document.querySelectorAll('input,select,textarea,[role=combobox]')]
        .filter(e => e.id === name || e.getAttribute('name') === name);
      if (nodes.length === 1 && !nodes[0].hasAttribute('data-aa-key'))
        nodes[0].setAttribute('data-aa-key', key);
    }""", [f.key, f.name])


def _click_option(scope, loc, target: str) -> bool:
    handle = None
    try:
        handle = loc.evaluate_handle(_COMBO_PICK_JS, target)
        option = handle.as_element()
        if option is None:
            return False
        option.click(timeout=4000)
        return True
    except Exception:
        return False
    finally:
        if handle is not None:
            handle.dispose()


def _fill_choices(scope, loc, f: Field, wanted: list[str]) -> bool:
    states = scope.evaluate(_READ_JS, [f.key, f.kind]).get("states") or []
    labels = [s[0] for s in states] or f.options
    targets: set[str] = set()
    for w in wanted:
        target = matching_option(w, labels)
        if target is None:
            return False
        targets.add(target)
    for target in labels:
        idx = labels.index(target)
        want = target in targets
        if not want and f.kind != "checkbox":
            continue
        if states and bool(states[idx][1]) == want:
            continue
        opt = loc.locator(f'[data-aa-opt="{idx}"]').first
        try:
            opt.scroll_into_view_if_needed(timeout=2000)
            opt.click(timeout=3000)
        except Exception:
            inp = loc.locator(f'[data-aa-optin="{idx}"]').first
            if inp.count():
                inp.set_checked(want, timeout=3000, force=True)
            else:
                opt.click(timeout=3000, force=True)
        _pause(0.1, 0.25)
    states = scope.evaluate(_READ_JS, [f.key, f.kind]).get("states") or []
    on = {s[0] for s in states if s[1]}
    return on == targets


def _verify(scope, f: Field, expected: str) -> bool:
    _recover_field(scope, f)
    got = scope.evaluate(_READ_JS, [f.key, f.kind])
    if got.get("missing"):
        return f.kind == "file" and Path(expected).name.lower() in _safe_text(scope).lower()
    if f.kind == "file":
        return got.get("files", 0) > 0 or Path(expected).stem.lower() in (got.get("text") or "").lower()
    if f.kind == "combobox":
        if got.get("expanded") or got.get("hasOptions") or got.get("invalid"):
            return False
        shown = (got.get("single") if got.get("hasDisplay") else
                 got.get("value") or got.get("ownText")) or ""
        if option_matches(expected, shown):
            return True
        if any(option_matches(expected, part.strip()) for part in shown.split(",")):
            return True
        metadata = got.get("metadata") or ""
        if metadata and option_matches(expected, f"{metadata} {shown}"):
            return True
        # Calling code dropdowns (often labeled "Country" or "Country Code") may display only the dialed code (e.g. "+1")
        code_match = re.search(r"\+\d+", shown.strip())
        if code_match:
            code = code_match.group(0)
            if code in re.findall(r"\+\d+", expected):
                return True
            if code == "+1" and any(u in expected.lower() for u in ("united states", "usa", "us", "canada")):
                return True
        return False
    if f.kind == "select":
        return option_matches(expected, got.get("value") or "")
    val = str(got.get("value") or "")
    if f.kind == "tel":
        d1, d2 = re.sub(r"\D", "", val), re.sub(r"\D", "", expected)
        return bool(d2) and (d1.endswith(d2) or d2.endswith(d1)) and len(d1) >= 7
    return _norm(val) == _norm(expected)


def _dropdown_failures(scope, fields: list[Field], answers: list[Answer]) -> list[str]:
    """Recheck dropdowns and date components, including years rendered as text inputs."""
    by_key = {answer.key: answer for answer in answers}
    failed = []
    for field in fields:
        answer = by_key.get(field.key)
        date_control = field.kind == "date" or (field.kind in ("text", "number") and re.search(
            r"\bgraduat\w*\b|\b(?:start|end) date\b|\beducation\b.*\b(?:from|to)\b", field.label, re.I))
        if (field.kind not in ("select", "combobox") and not date_control) or _missing(answer):
            continue
        try:
            matches = _verify(scope, field, str(answer.value))
        except Exception:
            matches = False
        if not matches:
            failed.append(field.key)
    return failed


def _safe_text(scope) -> str:
    try:
        return scope.evaluate("() => document.body.innerText")
    except Exception:
        return ""


# ---------- page helpers ----------

def page_text(page, max_chars: int = 8000) -> str:
    """Visible job-description text (main document, plus embedded ATS frames when the page is thin)."""
    chunks: list[str] = []
    try:
        chunks.append(page.evaluate(_PAGE_TEXT_JS) or "")
    except Exception:
        pass
    frames = getattr(page, "frames", [])
    if sum(len(c) for c in chunks) < 1500:
        for fr in frames[1:]:
            if any(h in (fr.url or "") for h in ("greenhouse.io", "lever.co", "ashbyhq.com")):
                try:
                    chunks.append(fr.evaluate(_PAGE_TEXT_JS) or "")
                except Exception:
                    pass
    text = "\n".join(c for c in chunks if c)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:max_chars]


def click_text(scope, texts: list[str], roles=("button", "link")) -> bool:
    """Click the first visible element whose accessible name matches one of `texts` (case-insensitive)."""
    for t in texts:
        pat = re.compile(rf"^\s*{re.escape(t)}\s*$", re.I)
        for role in roles:
            try:
                loc = scope.get_by_role(role, name=pat)
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(timeout=5000)
                        return True
            except Exception:
                continue
    for t in texts:
        try:
            loc = scope.get_by_text(re.compile(rf"^\s*{re.escape(t)}\s*$", re.I))
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=5000)
                    return True
        except Exception:
            continue
    return False


# ---------- adapter helpers ----------

def fill_form(scope, job: Job, profile: dict, *, job_text: str = "", extra_context: str = "",
              passes: int = 2) -> tuple[list[Field], list[Answer], list[str]]:
    """Extract, answer, and fill; re-extract to catch conditional fields (at most `passes` rounds).
    Returns (current fields, answers for them, keys that failed to fill)."""
    from .answers import answer_fields
    answers: dict[str, Answer] = {}
    failed: set[str] = set()
    fields: list[Field] = []
    known: dict[str, Field] = {}
    for i in range(passes):
        fields = extract_fields(scope, probe_options=False)
        for f in fields:
            old = known.get(f.key)
            if old and (f.label != old.label or f.kind != old.kind or f.name != old.name):
                log.info("field %s remapped from %s to %s; refreshing answer", f.key,
                         old.label[:70], f.label[:70])
                answers.pop(f.key, None)
                failed.discard(f.key)
                old = None
            if old and old.options and not f.options:
                f.options = known[f.key].options
            elif f.kind == "combobox" and not f.options and not old:
                f.options = _probe_combo(scope, f.key)
        new = [f for f in fields if f.key not in answers]
        retry = [f for f in fields if f.key in failed]
        if not new and not retry:
            break
        if new:
            for a in answer_fields(new, job, profile, job_text=job_text, extra_context=extra_context,
                                   context_fields=fields):
                answers[a.key] = a
        todo = new + retry
        failed -= {f.key for f in retry}
        failed |= set(fill_fields(scope, todo, [answers[f.key] for f in todo if f.key in answers]))
        known.update({f.key: f for f in fields})
        log.info("fill pass %d: %d fields, %d new, %d failed", i + 1, len(fields), len(new), len(failed))
    keys = {f.key for f in fields}
    # Some ATSs replace the file input with a filename chip after upload; keep those fields.
    fields += [f for k, f in known.items() if k not in keys and f.kind == "file" and k not in failed
               and not _missing(answers.get(k))]
    keys = {f.key for f in fields}
    current_answers = [answers[f.key] for f in fields if f.key in answers]
    failed.update(_dropdown_failures(scope, fields, current_answers))
    return fields, current_answers, sorted(k for k in failed if k in keys)


def _visible_text(scope) -> str:
    try:
        return (scope.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
    except Exception:
        return ""


def _visible_errors(scope) -> list[str]:
    js = r"""() => [...document.querySelectorAll('[class*="error" i], [role=alert], .invalid-feedback')]
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
      .map(e => (e.innerText || '').trim()).filter(t => t && t.length < 200)"""
    try:
        return list(dict.fromkeys(scope.evaluate(js)))[:5]
    except Exception:
        return []


def _challenge_visible(page) -> bool:
    for fr in page.frames:
        url = fr.url or ""
        if ("hcaptcha.com" in url and "frame=challenge" in url) or "recaptcha/api2/bframe" in url or "enterprise/bframe" in url:
            try:
                el = fr.frame_element()
                box = el.bounding_box()
                if box and box["width"] > 100 and box["height"] > 100 and el.is_visible():
                    return True
            except Exception:
                continue
    return False


def wait_for_outcome(page, scope, before_text: str, success_url: str | None = None,
                     timeout: float = 30) -> str:
    """'submitted' | 'code' | 'captcha' | 'unknown' after the final submit click."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        urls = [page.url, getattr(scope, "url", "")]
        if success_url and any(re.search(success_url, u or "") for u in urls):
            return "submitted"
        text = _visible_text(scope) + "\n" + (_visible_text(page) if scope is not page else "")
        if any(p in text and p not in before_text for p in SUCCESS_PHRASES):
            return "submitted"
        if any(p in text and p not in before_text for p in CODE_PHRASES):
            return "code"
        if _challenge_visible(page):
            return "captcha"
    return "unknown"


def _result(status: ApplyStatus, reason: str, fields: list[Field], answers: list[Answer],
            shot: str | None, url: str | None) -> ApplyResult:
    from .ats.base import _missing as missing
    by_key = {a.key: a for a in answers}
    return ApplyResult(
        status=status, reason=reason, screenshot=shot, final_url=url, fields_total=len(fields),
        unanswered_required=[f.label or f.key for f in fields if f.required and missing(by_key.get(f.key))],
        generated_answers=[f.label or f.key for f in fields if (a := by_key.get(f.key)) and a.source == "generated"],
        answers=answers,
    )


def finish_application(job: Job, ctx, scope, fields: list[Field], answers: list[Answer], failed: list[str], *,
                       submit_texts: list[str], success_url: str | None = None) -> ApplyResult:
    """Decide, solve captcha, click submit, and confirm. Never clicks submit in dry-run/fill_only."""
    from . import captcha, mail
    from .ats.base import blocked_result, submit_decision
    page = ctx.page
    dropdown_failed = _dropdown_failures(scope, fields, answers)
    failed = sorted(set(failed) | set(dropdown_failed))
    labels = {f.key: f.label or f.key for f in fields}
    failed_req = [labels[k] for k in failed if k in dropdown_failed or any(f.key == k and f.required for f in fields)]
    ok, reason = submit_decision(fields, answers, ctx.submit_mode, ctx.dry_run)
    if failed_req:
        shot = ctx.screenshot(job, "fill_failed")
        why = "could not fill: " + "; ".join(failed_req[:5]) + ("" if ok else f" | {reason}")
        return _result(ApplyStatus.NEEDS_REVIEW, why, fields, answers, shot, page.url)
    if not ok or ctx.dry_run or ctx.submit_mode == "fill_only":
        shot = ctx.screenshot(job, "dry_run" if ctx.dry_run else "review")
        if failed:
            reason += " | optional fields not filled: " + "; ".join(labels[k] for k in failed[:5])
        return blocked_result(fields, answers, reason, ctx.dry_run, shot, page.url)

    cap = captcha.solve_if_present(page, ctx.config, job.id)
    if cap.status != "none":
        ctx.notes.append(f"captcha {cap.status} {cap.kind} {cap.detail}".strip())
    if captcha.blocks_submit(cap, ctx.config):
        shot = ctx.screenshot(job, "captcha")
        return _result(ApplyStatus.NEEDS_REVIEW, f"captcha {cap.status}: {cap.kind} {cap.detail}".strip(),
                       fields, answers, shot, page.url)
    before = _visible_text(scope) + "\n" + (_visible_text(page) if scope is not page else "")
    ctx.mark_submit_attempt(job.id)
    submit_ts = time.time()
    if not click_text(scope, submit_texts, roles=("button",)):
        shot = ctx.screenshot(job, "no_submit")
        return _result(ApplyStatus.NEEDS_REVIEW, "submit button not found", fields, answers, shot, page.url)
    state = wait_for_outcome(page, scope, before, success_url)
    shot = ctx.screenshot(job, state)
    if state == "submitted":
        return _result(ApplyStatus.SUBMITTED, "submitted", fields, answers, shot, page.url)
    if state == "code":
        ver = mail.wait_for_verification((submit_ts or time.time()) - 15, timeout=90, want="code")
        if ver and ver.code:
            code = ver.code.strip()
            # 1. Look for multi-box OTP inputs or inputs in security code section
            boxes = []
            target = page if hasattr(page, "locator") else scope
            try:
                sec_heading = target.locator('text=/security code|verification code|one-time code/i')
                if sec_heading.count():
                    parent = sec_heading.first.locator("xpath=ancestor::*[.//input][1]")
                    if parent.count():
                        vis = [inp for inp in parent.locator("input").all() if inp.is_visible()]
                        if vis:
                            boxes = vis
            except Exception:
                pass
            if not boxes:
                try:
                    short = [inp for inp in target.locator('input[maxlength="1"]:visible, input[aria-label*="digit" i]:visible, input[aria-label*="character" i]:visible').all() if inp.is_visible()]
                    if len(short) >= 4:
                        boxes = short
                except Exception:
                    pass
            if not boxes:
                try:
                    named = [inp for inp in target.locator('input[name*="code" i]:visible, input[id*="code" i]:visible, input[autocomplete*="one-time-code" i]:visible').all() if inp.is_visible()]
                    if named:
                        boxes = [named[0]]
                except Exception:
                    pass

            try:
                if len(boxes) > 1 and len(boxes) == len(code):
                    for i, char in enumerate(code):
                        boxes[i].fill(char)
                elif boxes:
                    try:
                        boxes[0].fill(code)
                    except Exception:
                        for i, char in enumerate(code[:len(boxes)]):
                            boxes[i].fill(char)

                btn_texts = ["submit application", "submit", "verify", "confirm", "continue", "submit your application", "complete application"]
                if not click_text(page, btn_texts, roles=("button",)):
                    if scope is not page:
                        click_text(scope, btn_texts, roles=("button",))
                state = wait_for_outcome(page, scope, before, success_url)
                shot = ctx.screenshot(job, state)
                if state == "submitted":
                    return _result(ApplyStatus.SUBMITTED, "submitted", fields, answers, shot, page.url)
            except Exception as e:
                log.warning("entering verification code failed: %s", e)
        return _result(ApplyStatus.NEEDS_REVIEW, "email security code requested after submit", fields, answers,
                       shot, page.url)
    if state == "captcha":
        return _result(ApplyStatus.NEEDS_REVIEW, "captcha challenge shown after submit", fields, answers,
                       shot, page.url)
    errs = _visible_errors(scope)
    return _result(ApplyStatus.NEEDS_REVIEW, "no confirmation after submit" + (": " + "; ".join(errs) if errs else ""),
                   fields, answers, shot, page.url)
