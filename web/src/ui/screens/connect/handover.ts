// The handover to the agent's loopback listener (11-agent-access.md section 5.1 step 4): a top-level form POST in the
// same tab (no window, frame or fetch), so the handover secret is a form field and never part of a URL or the browser
// history. The hub's CSP allows it (`form-action 'self' http://127.0.0.1:*`).
import type { LoopbackForm } from '../../../core/agents/pairing';

export function submitHandover(form: LoopbackForm, doc: Document = document): void {
  const element = doc.createElement('form');
  element.method = 'post';
  element.action = form.action;
  element.hidden = true;
  element.dataset.testid = 'connect.handover-form';
  for (const [name, value] of form.fields) {
    const input = doc.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    element.append(input);
  }
  doc.body.append(element);
  element.submit();
}
