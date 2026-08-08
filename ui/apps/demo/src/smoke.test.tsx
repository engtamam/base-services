import { render } from '@testing-library/react';
import { expect, test } from 'vitest';
import { Button } from '@base-ui/design-system/Button';
import { makeT } from '@base-ui/i18n';

test('a primitive renders and i18n falls back to the key', () => {
  const { getByRole } = render(<Button>Go</Button>);
  expect(getByRole('button').textContent).toBe('Go');
  expect(makeT('ar')('auth.signIn')).toBe('تسجيل الدخول');
  expect(makeT('en')('nonexistent.key')).toBe('nonexistent.key');
});
