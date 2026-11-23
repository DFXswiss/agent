// Serialisation of WebAuthn results for the hub (03-api.md section 4.1): the registration credential of
// `POST /v1/passkeys/register/finish` (`api/objects.json#/$defs/attestation_credential`) and the session certificate
// of `POST /v1/passkeys/login/finish` (04-crypto.md section 4.9, `agent_core.passkey.certificate.Certificate`).
// The inputs are typed structurally, so the same code serves `PublicKeyCredential`s and test doubles.
import { b64uEncode } from '../encoding/b64u';
import { bytesOf } from '../encoding/bytes';
import { fail } from '../crypto/errors';

const TRANSPORTS = ['usb', 'nfc', 'ble', 'smart-card', 'hybrid', 'internal'];

type Bytes = ArrayBuffer | ArrayBufferView;

export interface AttestationResult {
  readonly rawId: Bytes;
  readonly response: {
    readonly clientDataJSON: Bytes;
    readonly attestationObject: Bytes;
    getTransports?(): string[];
  };
}

export interface AssertionResult {
  readonly rawId: Bytes;
  readonly response: {
    readonly clientDataJSON: Bytes;
    readonly authenticatorData: Bytes;
    readonly signature: Bytes;
  };
}

export interface AttestationCredentialJson {
  id: string;
  clientDataJSON: string;
  attestationObject: string;
  transports: string[];
}

export interface CertificateJson {
  statement: string;
  credential_id: string;
  authenticator_data: string;
  client_data_json: string;
  signature: string;
}

const b64u = (value: Bytes) => b64uEncode(bytesOf(value));

function credentialIdText(rawId: Bytes): string {
  const raw = bytesOf(rawId);
  if (raw.length === 0 || raw.length > 1023) fail('header_invalid');
  return b64uEncode(raw);
}

/** The `credential` of `register/finish`; unknown transports are dropped (the contract lists six). */
export function attestationJson(credential: AttestationResult): AttestationCredentialJson {
  const transports = credential.response.getTransports?.() ?? [];
  return {
    id: credentialIdText(credential.rawId),
    clientDataJSON: b64u(credential.response.clientDataJSON),
    attestationObject: b64u(credential.response.attestationObject),
    transports: [...new Set(transports.filter((name) => TRANSPORTS.includes(name)))],
  };
}

/** The `certificate` of `login/finish`: the statement bytes and the assertion values. */
export function certificateJson(statement: Uint8Array, assertion: AssertionResult): CertificateJson {
  return {
    statement: b64uEncode(statement),
    credential_id: credentialIdText(assertion.rawId),
    authenticator_data: b64u(assertion.response.authenticatorData),
    client_data_json: b64u(assertion.response.clientDataJSON),
    signature: b64u(assertion.response.signature),
  };
}
