// The BASE shared catalog for @base-ui components (auth, footer). React-free, no app import. Every
// key lives once in EN + AR. A host extends/overrides via makeT(locale, channelDict). New product
// strings belong in the host's own dict, not here — this covers only what the shared components render.
export const baseEn = {
  'auth.signIn': 'Sign in',
  'auth.createAccount': 'Create account',
  'auth.email': 'Email',
  'auth.password': 'Password',
  'auth.name': 'Your name',
  'auth.orgName': 'Organization name',
  'auth.company': 'Company',
  'auth.individual': 'Individual',
  'auth.back': 'Back',
  'auth.pickOrg': 'Choose an organization',
  'auth.pickOrgHint': 'You belong to more than one — pick where to continue.',
  'auth.err.ERROR': 'Something went wrong. Please try again.',
} as const;

export const baseAr: Record<keyof typeof baseEn, string> = {
  'auth.signIn': 'تسجيل الدخول',
  'auth.createAccount': 'إنشاء حساب',
  'auth.email': 'البريد الإلكتروني',
  'auth.password': 'كلمة المرور',
  'auth.name': 'اسمك',
  'auth.orgName': 'اسم المنشأة',
  'auth.company': 'منشأة',
  'auth.individual': 'فرد',
  'auth.back': 'رجوع',
  'auth.pickOrg': 'اختر منشأة',
  'auth.pickOrgHint': 'أنت عضو في أكثر من منشأة — اختر أين تكمل.',
  'auth.err.ERROR': 'حدث خطأ. حاول مرة أخرى.',
};

export type BaseKey = keyof typeof baseEn;
export const baseKeys = Object.keys(baseEn) as BaseKey[];
