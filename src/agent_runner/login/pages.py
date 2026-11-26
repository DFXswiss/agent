"""Recognition of the provider's sign-in pages (KOR-73, ADR-098 section 4; 05-runner.md section 11.3).

`CLASSIFY` is evaluated in the login's sandbox. It reads the page's structure and address - never the markers of the
auth-page double - and returns `{kind, url, view, code, boxes}`:

- `challenge`: a check for humans (a Cloudflare challenge or Turnstile widget, an hCaptcha or reCAPTCHA frame, the
  challenge titles "Just a moment", "Sicherheitsüberprüfung", "Verify you are human");
- `code`: the code page of the OAuth callback (`code` = the shown `code#state`, else built from the address);
- `authorize`: the OAuth authorize page with its allow button (marked `authorize`);
- `register`: inputs of a new account - full name (`name`), date of birth (`birth`, or `birth-day`, `birth-month`,
  `birth-year`), consent boxes (`box-<n>`, each with its class `terms`, `age`, `marketing` or `other` from its label
  and whether it is required) - and its button (`submit`);
- `check_email`: the "check your e-mail" state; with the input for a code from the mail (`mail-code`) and its button
  (`submit`) when the page offers it;
- `email`: an e-mail input (`email`) and its continue button (`continue`);
- `error` (an error page) or `unknown`.

The elements to act on carry `data-agent-login="<mark>"`, so the automation addresses them with `target`. `view` is
the viewport in CSS pixels (a click into the owner's picture is scaled to it), `marks` the marks set. The answer
comes from a page the provider controls: `parse` checks its form and maps anything else to `unknown`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

KINDS = frozenset({"challenge", "code", "authorize", "register", "check_email", "email", "error", "unknown"})
BOX_CLASSES = frozenset({"terms", "age", "marketing", "other"})
CODE_RE = re.compile(r"^[!-~]{1,512}$")
MARK_RE = re.compile(r"^[a-z0-9-]{1,32}$")
URL_LIMIT = 8192
MAX_BOXES = 16

CLASSIFY = r"""(() => {
  const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const all = (sel) => Array.from(document.querySelectorAll(sel)).filter(visible);
  const one = (sel) => all(sel)[0] || null;
  const text = (el) => (el ? (el.innerText || el.textContent || el.value || '') : '').replace(/\s+/g, ' ').trim();
  const marks = [];
  const mark = (el, name) => {
    if (el) {
      el.setAttribute('data-agent-login', name);
      marks.push(name);
    }
    return !!el;
  };
  const button = (re) => all('button, input[type=submit], [role=button]').find((el) => re.test(text(el))) || null;
  const body = text(document.body).slice(0, 20000);
  const title = document.title || '';
  const url = new URL(location.href);
  const out = { kind: 'unknown', url: location.href, view: [innerWidth, innerHeight], code: null, boxes: [], marks };
  document.querySelectorAll('[data-agent-login]').forEach((el) => el.removeAttribute('data-agent-login'));
  const challenge = /just a moment|sicherheitsüberprüfung|verify you are human|attention required/i;
  if (challenge.test(title) || one('iframe[src*="challenges.cloudflare.com"], iframe[src*="hcaptcha.com"], ' +
      'iframe[src*="recaptcha"], .cf-turnstile, #challenge-form, #challenge-stage, .g-recaptcha, .h-captcha') ||
      challenge.test(text(one('h1, h2')))) {
    out.kind = 'challenge';
    return out;
  }
  if (/\/oauth\/code\/callback|\/oauth\/code\b/.test(url.pathname)) {
    const shown = all('code, pre, input[readonly], [data-code], p, div')
      .map(text).find((value) => /^[A-Za-z0-9._~-]{8,}#[A-Za-z0-9._~-]+$/.test(value));
    const built = url.searchParams.get('code') && url.searchParams.get('state')
      ? url.searchParams.get('code') + '#' + url.searchParams.get('state') : null;
    out.kind = 'code';
    out.code = shown || built;
    return out;
  }
  const allow = /^(authori[sz]e|autorisieren|allow|zulassen|erlauben)\b/i;
  if (/\/oauth\/authorize/.test(url.pathname) && mark(button(allow), 'authorize')) {
    out.kind = 'authorize';
    return out;
  }
  const email = one('input[type=email], input[name=email], input[autocomplete=email], input[autocomplete=username]');
  const name = one('input[autocomplete=name], input[name*=name i]:not([type=email]):not([type=hidden]), ' +
    'input[id*=fullname i]');
  const birth = one('input[type=date], input[autocomplete=bday], input[name*=birth i]:not([name*=day i])');
  const parts = ['day', 'month', 'year'].map((part) => one('input[autocomplete=bday-' + part + '], ' +
    'select[autocomplete=bday-' + part + '], input[name*=birth i][name*=' + part + ' i], ' +
    'select[name*=birth i][name*=' + part + ' i]'));
  const boxes = all('input[type=checkbox]');
  const consent = (el) => {
    const named = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
    const label = text(el.closest('label')) || text(named) || text(el.parentElement);
    if (/terms|nutzungsbedingungen|bedingungen|privacy|datenschutz|acceptable use/i.test(label)) return 'terms';
    if (/marketing|promotion|newsletter|product updates|werbung/i.test(label)) return 'marketing';
    if (/\b18\b|\byears? old\b|\bjahre\b|\bage\b|\balter\b/i.test(label)) return 'age';
    return 'other';
  };
  const consentBoxes = boxes.filter((el) => consent(el) !== 'other');
  if (!email && (name || birth || parts.some(Boolean) || consentBoxes.length)) {
    out.kind = 'register';
    mark(name, 'name');
    if (birth) mark(birth, 'birth');
    ['day', 'month', 'year'].forEach((part, i) => mark(parts[i], 'birth-' + part));
    out.boxes = boxes.slice(0, 16).map((el, i) => {
      mark(el, 'box-' + i);
      return { mark: 'box-' + i, kind: consent(el), required: !!el.required, checked: !!el.checked };
    });
    mark(button(/continue|weiter|create|erstellen|sign up|registrieren|agree|zustimmen|submit|fertig|done/i) ||
      one('button[type=submit], input[type=submit]'), 'submit');
    return out;
  }
  const mailCode = one('input[autocomplete=one-time-code], input[name*=code i], input[id*=code i]');
  const sent = new RegExp('check your (e-?mail|inbox)|we sent|we\'ve sent|wir haben|prüfe dein|posteingang|' +
    'verification code|login code', 'i');
  if (!email && (mailCode || sent.test(body))) {
    out.kind = 'check_email';
    if (mark(mailCode, 'mail-code')) {
      mark(button(/verify|continue|weiter|bestätigen|sign in|anmelden|submit/i) ||
        one('button[type=submit], input[type=submit]'), 'submit');
    }
    return out;
  }
  if (mark(email, 'email')) {
    out.kind = 'email';
    mark(button(/continue with e-?mail|mit e-?mail fortfahren|^continue$|^weiter$|send|senden|sign in|anmelden/i) ||
      one('button[type=submit], input[type=submit]'), 'continue');
    return out;
  }
  if (/error|something went wrong|fehler/i.test(title + ' ' + text(one('h1')))) out.kind = 'error';
  return out;
})()"""


@dataclass(frozen=True)
class Box:
    mark: str
    kind: str
    required: bool
    checked: bool


@dataclass(frozen=True)
class Page:
    kind: str
    url: str
    width: float
    height: float
    code: str | None
    boxes: tuple[Box, ...]
    marks: frozenset[str] = frozenset()


UNKNOWN = Page("unknown", "", 0.0, 0.0, None, ())


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _box(item: object) -> Box | None:
    if not isinstance(item, dict):
        return None
    mark, kind = item.get("mark"), item.get("kind")
    required, checked = item.get("required"), item.get("checked")
    if (
        isinstance(mark, str)
        and MARK_RE.fullmatch(mark)
        and kind in BOX_CLASSES
        and isinstance(required, bool)
        and isinstance(checked, bool)
    ):
        return Box(mark, str(kind), required, checked)
    return None


def _form(data: object) -> Page | None:
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        return None
    url, view, code, boxes, marks = (data.get(name) for name in ("url", "view", "code", "boxes", "marks"))
    if not (
        isinstance(url, str)
        and len(url) <= URL_LIMIT
        and isinstance(view, list)
        and len(view) == 2
        and isinstance(boxes, list)
        and len(boxes) <= MAX_BOXES
        and isinstance(marks, list)
        and len(marks) <= MAX_BOXES * 2
        and all(isinstance(item, str) and MARK_RE.fullmatch(item) for item in marks)
    ):
        return None
    width, height = _number(view[0]), _number(view[1])
    checked = [_box(item) for item in boxes]
    found = tuple(item for item in checked if item is not None)
    if width is None or height is None or len(found) != len(checked):
        return None
    shown = code if isinstance(code, str) and CODE_RE.fullmatch(code) else None
    return Page(str(data["kind"]), url, width, height, shown, found, frozenset(str(item) for item in marks))


def parse(text: str) -> Page:
    """The page of a `CLASSIFY` answer; `UNKNOWN` for an answer out of form."""
    try:
        data = json.loads(text)
    except ValueError:
        return UNKNOWN
    return _form(data) or UNKNOWN
