// Where a block's own-store image `ref` is served from. In the client/admin apps this is the api-gateway
// prefix for the standards service (GATEWAY_PREFIX['standards']); the shared renderer must not import an
// app-generated file, so the host sets it ONCE at startup. Absolute (http/data:) refs ignore this.
let ASSET_BASE = '';

/** Set the base a bare image `ref` is resolved against (call once at app startup). */
export function setBlockAssetBase(base: string): void {
  ASSET_BASE = base || '';
}

/** The configured asset base (empty until the host sets it — then a bare ref is served same-origin). */
export function blockAssetBase(): string {
  return ASSET_BASE;
}
