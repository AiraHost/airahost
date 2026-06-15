/* eslint-disable @typescript-eslint/no-require-imports */
const fs = require('fs');
const html = fs.readFileSync('tests/2026-06-12/page-__2BR_REMODEL_w-19.html', 'utf8');
const regex = /\$([0-9,]+(\.[0-9]+)?)/g;
let match;
const set = new Set();
while ((match = regex.exec(html)) !== null) {
  set.add(match[0]);
}
console.log(Array.from(set).join(', '));
