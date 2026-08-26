/* Arabic/RTL support for the demonstration path.
 *
 * Scope is deliberate, not lazy: the shell, the navigation, the Overview and the
 * Allocation map are what an audience is shown, so those are what read in
 * Arabic. Deeper screens stay English rather than carrying half-translations
 * that drift — a string translated in one place and not another reads as a bug,
 * while an entire screen that is consistently English reads as scope.
 *
 * `t()` falls back to the English string, and to the key itself if even that is
 * missing, so a forgotten key can never blank a control. That fallback only
 * works because `en` is a real table below - an empty one renders raw keys. Direction and
 * language live on <html>, which is what flips the stylesheet's logical
 * properties; views re-render on the gemp:lang event so open screens follow.
 */

'use strict';

const STRINGS = {
  /* English is a TABLE, not an absence.
   *
   * `t()` falls back to the key when a string is missing, so an empty English
   * table does not fall back to English - it renders `nav.overview` in the rail
   * and `shell.signout` on the button. The default language has to be spelled
   * out like any other.
   *
   * These are the exact strings the hardcoded NAV_LABEL and shell markup carried
   * before the catalogue existed. */
  en: {
    'nav.overview': 'Overview',
    'nav.map': 'Allocation map',
    'nav.alerts': 'Alert inbox',
    'nav.forecasts': 'Forecasting',
    'nav.runs': 'Allocations',
    'nav.evidence': 'Evidence',
    'nav.integrity': 'Integrity',
    'nav.admin': 'Admin',
    'nav.account': 'Account',

    'shell.signout': 'Sign out',
    'shell.reread': 'Re-read',
    'shell.skip': 'Skip to content',
    'shell.sections': 'GEMP sections',

    'overview.title': 'Portfolio overview',
    'overview.buildings': 'Buildings',
    'overview.readings': 'Readings stored',
    'overview.openAlerts': 'Open alerts',
    'overview.measured': 'Costed on measurement',
    'overview.demandPanel': 'Portfolio demand',
    'overview.alertsPanel': 'Alerts per day',
    'overview.latestRun': 'Most recent allocation',
    'overview.storedNotLive': 'stored, not live',
    'overview.budget': 'Budget',
    'overview.funded': 'Funded',
    'overview.spent': 'Spent',
    'overview.shareSpent': 'Share of budget spent',
    'overview.lifetimeBenefit': 'Lifetime benefit',
    'overview.districtCap': 'District cap',

    'map.title': 'Allocation map',
    'map.budget': 'Budget',
    'map.controls': 'Controls',
    'map.method': 'Allocation method',
    'map.rankBy': 'Rank by',
    'map.compare': 'Compare all four methods',
    'map.whyBuildingSpecific': 'Why building-specific?',
    'map.districtCap': 'Max funded per district',
  },

  ar: {
    'nav.overview': 'نظرة عامة',
    'nav.map': 'خريطة التخصيص',
    'nav.alerts': 'صندوق التنبيهات',
    'nav.forecasts': 'التنبؤ بالأحمال',
    'nav.runs': 'التخصيصات',
    'nav.evidence': 'الأدلة',
    'nav.integrity': 'سلامة البيانات',
    'nav.admin': 'الإدارة',
    'nav.account': 'الحساب',

    'shell.signout': 'تسجيل الخروج',
    'shell.reread': 'إعادة القراءة',
    'shell.skip': 'تجاوز إلى المحتوى',
    'shell.sections': 'أقسام المنصة',

    'overview.title': 'نظرة عامة على المحفظة',
    'overview.buildings': 'المباني',
    'overview.readings': 'قراءات مخزّنة',
    'overview.openAlerts': 'تنبيهات مفتوحة',
    'overview.measured': 'مُكلفة بالقياس',
    'overview.demandPanel': 'طلب المحفظة',
    'overview.alertsPanel': 'التنبيهات اليومية',
    'overview.latestRun': 'آخر تخصيص',
    'overview.storedNotLive': 'مخزّن، وليس مباشرًا',
    'overview.budget': 'الميزانية',
    'overview.funded': 'مموَّل',
    'overview.spent': 'المُنفَق',
    'overview.shareSpent': 'نسبة إنفاق الميزانية',
    'overview.lifetimeBenefit': 'الفائدة الدائمة',
    'overview.districtCap': 'سقف الحي',

    'map.title': 'خريطة التخصيص',
    'map.budget': 'الميزانية',
    'map.controls': 'عناصر التحكم',
    'map.method': 'طريقة التخصيص',
    'map.rankBy': 'الترتيب حسب',
    'map.compare': 'قارن الطرق الأربع',
    'map.whyBuildingSpecific': 'لماذا يختلف الاختيار بحسب المبنى؟',
    'map.districtCap': 'الحد الأقصى للتمويل لكل حي',
  },
};

const RTL = new Set(['ar']);

export const LANGS = [
  { code: 'en', label: 'English' },
  { code: 'ar', label: 'العربية' },
];

const STORE_KEY = 'gemp-lang';

export function currentLang() {
  return localStorage.getItem(STORE_KEY) === 'ar' ? 'ar' : 'en';
}

export function locale() {
  return currentLang() === 'ar' ? 'ar-EG' : 'en-US';
}

function applyDocument(lang) {
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL.has(lang) ? 'rtl' : 'ltr';
}

export function setLangCode(code) {
  const next = code === 'ar' ? 'ar' : 'en';
  localStorage.setItem(STORE_KEY, next);
  applyDocument(next);
  window.dispatchEvent(new CustomEvent('gemp:lang', { detail: { lang: next } }));
}

/* Applied once at boot, before first paint of any view, so direction is never
 * wrong-then-fixed: a flash of left-to-right before flipping is exactly the
 * kind of sloppiness that says "bolted on". */
applyDocument(currentLang());

const has = (table, key) =>
  Boolean(table) && Object.prototype.hasOwnProperty.call(table, key);

export function t(key) {
  const table = STRINGS[currentLang()];
  if (has(table, key)) return table[key];
  /* An untranslated string reads as English, not as `overview.budget`. A gap in
   * the Arabic table is a missing translation; showing the key turns it into a
   * visible defect. */
  if (has(STRINGS.en, key)) return STRINGS.en[key];
  return key;
}
