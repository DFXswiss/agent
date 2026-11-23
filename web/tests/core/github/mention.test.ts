// The composer's GitHub hint (KOR-25 part three): GitHub addresses, `owner/name` as a word of its own, the GitHub
// words in German and English – and the texts that only look like one.
import { describe, expect, it } from 'vitest';
import { mentionsGithub } from '../../../src/core/github/mention';

describe('mentionsGithub', () => {
  it('finds the first task of the client and other repository names', () => {
    for (const text of [
      'schau dir das neuste issue in example-org/example-repo repo an',
      'Check dfx/agent',
      'see (dfx/agent), please',
      'what changed in "dfx/agent"?',
      'owner/repo.js.',
      'dfx/agent: build fails',
      'Bitte „dfx/agent“ prüfen',
    ]) {
      expect(mentionsGithub(text), text).toBe(true);
    }
  });

  it('finds GitHub addresses', () => {
    for (const text of [
      'https://github.com/dfx/agent/pull/7',
      'look at github.com',
      'GITHUB.COM/x',
      'gist.github.com/abc',
      'api.github.com/repos',
    ]) {
      expect(mentionsGithub(text), text).toBe(true);
    }
  });

  it('finds the GitHub words in German and English', () => {
    for (const text of [
      'Öffne ein Issue',
      'list the issues',
      'Erstelle einen Pull Request',
      'review the pull-requests',
      'pullrequest',
      'merge the PR',
      'Zwei PRs offen',
      'clone the repo',
      'alle Repos',
      'this repository',
      'die Repositorys',
      'all repositories',
      'GitHub Actions',
      'github',
    ]) {
      expect(mentionsGithub(text), text).toBe(true);
    }
  });

  it('ignores texts that only look like one', () => {
    for (const text of [
      '',
      'Write a poem about the sea',
      'and/or',
      'Ja/Nein Frage',
      'speed in km/h',
      'Client/Server architecture',
      'on 23/11 at 1/2 past',
      'see src/core/file.ts',
      'path a/b/c',
      'mygithub.company',
      'notgithub.com',
      'the pr of it',
      'reposition the box',
      'issued yesterday',
      'impression',
      'x/',
      '/y',
      'a/1',
      '1/b',
    ]) {
      expect(mentionsGithub(text), text).toBe(false);
    }
  });
});
