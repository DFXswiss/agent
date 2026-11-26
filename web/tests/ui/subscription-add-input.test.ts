// KOR-73 (ADR-100): the login input of "Add a subscription" for every combination of mailbox and account data.
import { describe, expect, it } from 'vitest';
import { addInput } from '../../src/ui/subscriptions/AddSubscription';
import { EMPTY_ACCOUNT, EMPTY_MAILBOX } from '../../src/ui/subscriptions/fields';

const MAILBOX = {
  ...EMPTY_MAILBOX,
  host: 'imap.example.com',
  user: 'a@example.com',
  password: 'pw',
  email: 'a@example.com',
};
const ACCOUNT = { ...EMPTY_ACCOUNT, on: true, name: 'Ada', consents: ['terms'] as const };
const FORM = {
  name: 'Team',
  note: '',
  automatic: false,
  email: '',
  mailbox: EMPTY_MAILBOX,
  account: EMPTY_ACCOUNT,
  orgs: [],
};

describe('addInput', () => {
  it('builds the single login without a parallel device', () => {
    expect(addInput('id', FORM, false)).toMatchObject({ mode: 'manual' });
    expect(addInput('id', { ...FORM, automatic: true }, false)).toMatchObject({ mode: 'automatic' });
    expect(addInput('id', { ...FORM, name: '' }, false)).toBeNull();
    expect(addInput('id', { ...FORM, note: 'for reviews' }, false)).toMatchObject({ note: 'for reviews' });
    expect(addInput('id', { ...FORM, note: 'n'.repeat(501) }, false)).toBeNull();
    expect(addInput('id', { ...FORM, automatic: true, email: 'no-at-sign' }, false)).toBeNull();
    expect(
      addInput('id', { ...FORM, automatic: true, email: `${'a'.repeat(64)}@${'b'.repeat(190)}` }, false),
    ).toBeNull();
    expect(addInput('id', { ...FORM, automatic: true, email: 'a@example.com' }, false)).toMatchObject({
      email: 'a@example.com',
    });
  });

  it('logs in by link and code without a mailbox and ignores account data then', () => {
    const input = addInput('id', { ...FORM, account: { ...ACCOUNT, name: '' } }, true);
    expect(input).toMatchObject({ mode: 'manual', create: { name: 'Team' } });
    expect(input).not.toHaveProperty('account');
  });

  it('logs in automatically with the own mailbox, with and without account data', () => {
    const plain = addInput('id', { ...FORM, mailbox: MAILBOX, email: 'a@example.com' }, true);
    expect(plain).toMatchObject({ mode: 'automatic', email: 'a@example.com' });
    expect(plain).not.toHaveProperty('account');
    expect(addInput('id', { ...FORM, mailbox: MAILBOX, account: ACCOUNT }, true)).toMatchObject({
      account: { name: 'Ada', consents: ['terms'] },
    });
  });

  it('is not valid with a half-filled mailbox or invalid account data', () => {
    expect(addInput('id', { ...FORM, mailbox: { ...MAILBOX, password: '' } }, true)).toBeNull();
    expect(addInput('id', { ...FORM, mailbox: MAILBOX, account: { ...ACCOUNT, name: '' } }, true)).toBeNull();
  });
});
