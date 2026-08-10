// Generic, prop-driven auth surface. No backend import — the host wires onLogin/onChooseOrg/onSignup
// to identity-service (or any API), maps error CODES to copy, and supplies strings via the i18n seam.
export { AuthLayout, type AuthLayoutProps } from './AuthLayout';
export { LoginForm, type LoginFormProps, type LoginOrg } from './LoginForm';
export { SignupForm, type SignupFormProps, type SignupPayload, type SignupKind } from './SignupForm';
export { OtpVerify, type OtpVerifyProps } from './OtpVerify';
export { ChangePassword, type ChangePasswordProps } from './ChangePassword';
export { ForgotPassword, type ForgotPasswordProps } from './ForgotPassword';
// The legal viewer is the shared rich block engine — render outstanding terms with DocBlocksView
// (exported from '../components'); AcceptTerms wraps it for the post-auth access gate.
export { AcceptTerms, type AcceptTermsProps, type OutstandingDoc } from './AcceptTerms';
export { AuthI18nProvider, AuthI18nContext, useAuthI18n, type AuthI18n } from './i18n';
