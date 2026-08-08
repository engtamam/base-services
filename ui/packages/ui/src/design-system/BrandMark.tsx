// امتثالك brand mark — the SEAL: a notary-style ring holding a checkmark. It says "sealed · verified ·
// approved" — the essence of a compliance-proof product — and pairs with the seal moment on a signed
// document. Strokes take the theme accent (gold, and it follows any tenant white-label). Language-neutral
// on purpose; decorative → aria-hidden; pair with the wordmark. Tuned to still read at favicon sizes.
export function BrandMark({ size = 26, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
      aria-hidden="true"
      focusable="false"
      style={{ color: 'var(--accent)' }}
    >
      {/* outer ring */}
      <circle cx="16" cy="16" r="14" stroke="currentColor" strokeWidth="1.8" />
      {/* the notary dashed inner ring */}
      <circle cx="16" cy="16" r="10.8" stroke="currentColor" strokeWidth="1" strokeDasharray="1.3 2.4" opacity="0.85" />
      {/* the seal check */}
      <path
        d="M10.6 16.4 14.5 20.2 21.6 12.2"
        stroke="currentColor"
        strokeWidth="2.7"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  );
}
