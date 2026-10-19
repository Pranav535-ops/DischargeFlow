// Languages offered for patient instructions (ADR-002 item 18): IETF BCP 47 codes with English names, the same list
// the backend names in ai/instructions.py:LANGUAGE_NAMES. The API accepts any valid code; these are the common ones.
export const LANGUAGES = [
  ['en', 'English'], ['es', 'Spanish'], ['zh', 'Chinese'], ['vi', 'Vietnamese'], ['tl', 'Tagalog'],
  ['ar', 'Arabic'], ['fr', 'French'], ['ko', 'Korean'], ['ru', 'Russian'], ['pt', 'Portuguese'],
  ['ht', 'Haitian Creole'], ['hi', 'Hindi'], ['pl', 'Polish'], ['de', 'German'], ['it', 'Italian'],
  ['ja', 'Japanese'], ['fa', 'Persian'], ['ur', 'Urdu'], ['bn', 'Bengali'], ['so', 'Somali'],
];

const NAMES = Object.fromEntries(LANGUAGES);

export const languageName = (code) => (code ? NAMES[code.toLowerCase().split('-')[0]] || code : 'Not recorded');

// Scripts written right to left, so the printed sheet sets dir="rtl".
const RTL = new Set(['ar', 'fa', 'ur', 'he', 'yi', 'ps']);
export const isRtl = (code) => RTL.has((code || '').toLowerCase().split('-')[0]);
