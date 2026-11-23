/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** b64u Ed25519 platform root public key, compiled in (06-web.md section 10.4). */
  readonly VITE_PLATFORM_ROOT_KEY: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
