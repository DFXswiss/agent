// The personal message of whoever asks for access (rule E-28, KOR-64), shown where the user approves: the agent's
// message of a pairing link (`t`) on `/connect` and in the agent block of `/invite`, the inviter's message of an org
// invitation package on `/invite`. A quote marked as the requester's words, rendered as plain text only (React
// escapes it: no link, formatting or control comes out of it). Nothing is shown without a message.
export interface RequesterMessageProps {
  readonly message: string | null;
  /** Says whose words these are. */
  readonly caption: string;
  readonly testId: string;
}

export function RequesterMessage({ message, caption, testId }: RequesterMessageProps) {
  if (message === null) return null;
  return (
    <figure className="flex flex-col gap-1" data-testid={testId}>
      <figcaption className="text-sm text-gray-700">{caption}</figcaption>
      <blockquote className="break-words border-l-4 border-gray-400 pl-3 italic" data-testid={`${testId}.text`}>
        {message}
      </blockquote>
    </figure>
  );
}
