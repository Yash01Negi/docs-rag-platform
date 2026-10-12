/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional absolute API origin for production builds, e.g. https://api.example.com */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
