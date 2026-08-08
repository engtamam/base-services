// THE THEME, IN ONE PLACE.
//
// applyTheme.ts and theme.config.ts existed as BYTE-IDENTICAL copies in client, auditor and admin —
// three files to edit for one palette change, and three chances to edit two of them. They were
// identical, so lifting them changed no behaviour; leaving them would have guaranteed a drift nobody
// noticed until two apps rendered different greys.
export * from './applyTheme';
export * from './theme.config';
